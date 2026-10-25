"""Sahm download engine: multi-connection segmented downloads with
dynamic segment splitting, pause/resume and a shared speed limiter.
Pure Python (no Qt) so it can be tested headless."""
import os
import re
import time
import uuid
import threading
import urllib.parse

import requests

CHUNK = 64 * 1024
MIN_SPLIT = 1024 * 1024          # never create a segment smaller than 1 MB
MAX_RETRIES = 6
DEFAULT_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/129.0 Safari/537.36")


class Status:
    QUEUED = "queued"
    CONNECTING = "connecting"
    DOWNLOADING = "downloading"
    PAUSED = "paused"
    COMPLETED = "completed"
    ERROR = "error"
    ACTIVE = (CONNECTING, DOWNLOADING)


class RateLimiter:
    """Token bucket shared by every connection of every download."""

    def __init__(self, bytes_per_sec=0):
        self.rate = bytes_per_sec
        self._lock = threading.Lock()
        self._tokens = 0.0
        self._last = time.monotonic()

    def set_rate(self, bytes_per_sec):
        self.rate = max(0, int(bytes_per_sec))

    def consume(self, n):
        rate = self.rate
        if rate <= 0:
            return
        with self._lock:
            now = time.monotonic()
            self._tokens = min(rate, self._tokens + (now - self._last) * rate)
            self._last = now
            self._tokens -= n
            wait = -self._tokens / rate if self._tokens < 0 else 0
        if wait > 0:
            time.sleep(min(wait, 2.0))


_bad_chars = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


_reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}


def safe_filename(name):
    """A bare file name that cannot escape the download folder (no paths, drives, '..',
    control characters or Windows device names like CON/NUL)."""
    name = str(name or "").replace("\\", "/").split("/")[-1]
    name = _bad_chars.sub("_", name).strip(" .")
    if name.split(".")[0].upper() in _reserved:
        name = "_" + name
    if len(name) > 200:
        root, ext = os.path.splitext(name)
        name = root[:200 - len(ext[:20])] + ext[:20]
    return name or "download"


def filename_from_response(resp, url):
    cd = resp.headers.get("Content-Disposition", "")
    m = re.search(r"filename\*\s*=\s*([^']*)''([^;]+)", cd, re.I)
    if m:
        try:
            return safe_filename(urllib.parse.unquote(m.group(2), encoding=m.group(1) or "utf-8"))
        except LookupError:      # unknown charset in the header
            return safe_filename(urllib.parse.unquote(m.group(2)))
    m = re.search(r'filename\s*=\s*"([^"]+)"', cd, re.I) or re.search(r"filename\s*=\s*([^;]+)", cd, re.I)
    if m:
        return safe_filename(m.group(1).strip())
    path = urllib.parse.urlparse(resp.url or url).path
    base = urllib.parse.unquote(os.path.basename(path))
    return safe_filename(base) if base else "download"


def unique_path(path):
    if not os.path.exists(path) and not os.path.exists(path + ".sahmpart"):
        return path
    root, ext = os.path.splitext(path)
    i = 1
    while True:
        cand = f"{root} ({i}){ext}"
        if not os.path.exists(cand) and not os.path.exists(cand + ".sahmpart"):
            return cand
        i += 1


class DownloadTask:
    def __init__(self, url, folder, filename=None, connections=8, headers=None, task_id=None):
        self.id = task_id or uuid.uuid4().hex[:12]
        self.url = url
        self.final_url = url
        self.folder = folder
        self.filename = safe_filename(filename) if filename else ""
        self.connections = max(1, min(32, int(connections)))
        self.headers = dict(headers or {})
        self.size = None              # total bytes, None = unknown
        self.resumable = False
        self.probed = False
        self.segments = []            # [start, end_inclusive, done]
        self.single_done = 0
        self.status = Status.QUEUED
        self.error = ""
        self.added = time.time()
        self.finished = None
        self.speed = 0.0
        # runtime only
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._active = set()
        self._fatal = None
        self._last_sample = (time.monotonic(), 0)
        self.limiter = None
        self.on_finish = None

    # ---------- persistence ----------
    def to_dict(self):
        with self._lock:
            return {
                "id": self.id, "url": self.url, "final_url": self.final_url,
                "folder": self.folder, "filename": self.filename,
                "connections": self.connections,
                # don't keep login cookies on disk once the file is done
                "headers": {k: v for k, v in self.headers.items()
                            if not (k.lower() == "cookie" and self.status == Status.COMPLETED)},
                "size": self.size, "resumable": self.resumable, "probed": self.probed,
                "segments": [list(s) for s in self.segments],
                "single_done": self.single_done,
                "status": self.status if self.status not in Status.ACTIVE else Status.PAUSED,
                "error": self.error, "added": self.added, "finished": self.finished,
            }

    @classmethod
    def from_dict(cls, d):
        t = cls(d["url"], d["folder"], d.get("filename"), d.get("connections", 8),
                d.get("headers"), d.get("id"))
        t.final_url = d.get("final_url") or t.url
        if not str(t.url).startswith(("http://", "https://")):
            raise ValueError("bad url in saved list")
        t.size = d.get("size")
        t.resumable = d.get("resumable", False)
        t.probed = d.get("probed", False)
        t.segments = [list(s) for s in d.get("segments", [])]
        t.single_done = d.get("single_done", 0)
        st = d.get("status", Status.PAUSED)
        t.status = Status.PAUSED if st in Status.ACTIVE else st
        t.error = d.get("error", "")
        t.added = d.get("added", time.time())
        t.finished = d.get("finished")
        return t

    # ---------- info ----------
    @property
    def path(self):
        return os.path.join(self.folder, self.filename or "download")

    @property
    def part_path(self):
        return self.path + ".sahmpart"

    @property
    def downloaded(self):
        with self._lock:
            if self.segments:
                return sum(s[2] for s in self.segments)
            return self.single_done

    @property
    def progress(self):
        if self.status == Status.COMPLETED:
            return 1.0
        if not self.size:
            return 0.0
        return min(1.0, self.downloaded / self.size)

    @property
    def eta(self):
        if not self.size or self.speed < 1:
            return None
        return max(0, (self.size - self.downloaded) / self.speed)

    @property
    def active_connections(self):
        return len(self._active)

    def is_running(self):
        return self._thread is not None and self._thread.is_alive()

    def sample_speed(self):
        now = time.monotonic()
        done = self.downloaded
        t0, d0 = self._last_sample
        dt = now - t0
        if dt >= 0.4:
            inst = max(0, done - d0) / dt
            self.speed = inst if self.speed == 0 else self.speed * 0.6 + inst * 0.4
            if self.status not in Status.ACTIVE:
                self.speed = 0.0
            self._last_sample = (now, done)

    # ---------- control ----------
    def start(self, limiter=None):
        if self.is_running() or self.status == Status.COMPLETED:
            return
        self.limiter = limiter or self.limiter or RateLimiter()
        self._stop.clear()
        self._fatal = None
        self.error = ""
        self.status = Status.CONNECTING
        self._thread = threading.Thread(target=self._run, daemon=True, name=f"sahm-{self.id}")
        self._thread.start()

    def pause(self):
        self._stop.set()
        if self.status == Status.QUEUED:
            self.status = Status.PAUSED

    def wait(self, timeout=None):
        if self._thread:
            self._thread.join(timeout)

    def reset(self):
        """Forget progress (used for redownload)."""
        self.pause()
        self.wait(5)
        with self._lock:
            self.segments = []
            self.single_done = 0
            self.probed = False
            self.size = None
        try:
            os.remove(self.part_path)
        except OSError:
            pass
        self.status = Status.QUEUED

    # ---------- internals ----------
    def _req_headers(self, extra=None):
        h = {"User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"}
        h.update(self.headers)
        if extra:
            h.update(extra)
        return h

    def _probe(self):
        r = requests.get(self.url, headers=self._req_headers({"Range": "bytes=0-"}),
                         stream=True, allow_redirects=True, timeout=(15, 30))
        try:
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            self.final_url = r.url
            cr = r.headers.get("Content-Range", "")
            m = re.match(r"bytes\s+\d+-\d+/(\d+)", cr)
            if r.status_code == 206 and m:
                self.size = int(m.group(1))
                self.resumable = True
            else:
                cl = r.headers.get("Content-Length")
                self.size = int(cl) if cl and cl.isdigit() else None
                self.resumable = False
            if not self.filename:
                self.filename = filename_from_response(r, self.url)
                os.makedirs(self.folder, exist_ok=True)
                self.filename = os.path.basename(unique_path(self.path))
            self.probed = True
        finally:
            r.close()

    def _run(self):
        try:
            os.makedirs(self.folder, exist_ok=True)
            if not self.probed:
                self._probe()
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            self.status = Status.DOWNLOADING
            if self.resumable and self.size:
                self._run_segmented()
            else:
                self._run_single()
            if self._fatal:
                raise self._fatal
            if self._stop.is_set():
                self.status = Status.PAUSED
                return
            final = self.path
            if os.path.exists(final):
                final = unique_path(final)
                self.filename = os.path.basename(final)
            os.replace(self.part_path, final)
            self.status = Status.COMPLETED
            self.finished = time.time()
            self.speed = 0.0
            if self.on_finish:
                try:
                    self.on_finish(self)
                except Exception:
                    pass
        except Exception as e:  # noqa
            if self._stop.is_set():
                self.status = Status.PAUSED
            else:
                self.status = Status.ERROR
                self.error = str(e)[:300]
        finally:
            self._active.clear()

    # --- segmented (IDM style) ---
    def _prepare_file(self):
        exists = os.path.exists(self.part_path)
        if not exists:
            with self._lock:
                for s in self.segments:
                    s[2] = 0
            with open(self.part_path, "wb") as f:
                f.truncate(self.size)
        elif os.path.getsize(self.part_path) != self.size:
            with open(self.part_path, "r+b") as f:
                f.truncate(self.size)

    def _make_segments(self):
        n = max(1, min(self.connections, self.size // MIN_SPLIT or 1))
        ps = self.size // n
        self.segments = [[i * ps, (i + 1) * ps - 1 if i < n - 1 else self.size - 1, 0] for i in range(n)]

    @staticmethod
    def _remaining(seg):
        return seg[1] - (seg[0] + seg[2]) + 1

    def _run_segmented(self):
        with self._lock:
            if not self.segments:
                self._make_segments()
        self._prepare_file()
        while True:
            if self._stop.is_set() or self._fatal:
                break
            with self._lock:
                # 1) launch idle unfinished segments
                for i, seg in enumerate(self.segments):
                    if len(self._active) >= self.connections:
                        break
                    if i not in self._active and self._remaining(seg) > 0:
                        self._launch(i)
                # 2) dynamic splitting: help the slowest/biggest segment
                while len(self._active) < self.connections:
                    best, best_rem = None, 0
                    for i in self._active:
                        rem = self._remaining(self.segments[i])
                        if rem > best_rem:
                            best, best_rem = i, rem
                    if best is None or best_rem < 2 * MIN_SPLIT:
                        break
                    seg = self.segments[best]
                    mid = seg[0] + seg[2] + best_rem // 2
                    new = [mid, seg[1], 0]
                    seg[1] = mid - 1
                    self.segments.append(new)
                    self._launch(len(self.segments) - 1)
                if not self._active and all(self._remaining(s) <= 0 for s in self.segments):
                    break
            time.sleep(0.15)
        # wait for workers to exit
        deadline = time.monotonic() + 10
        while self._active and time.monotonic() < deadline:
            time.sleep(0.05)

    def _launch(self, idx):
        self._active.add(idx)
        threading.Thread(target=self._worker, args=(idx,), daemon=True).start()

    def _worker(self, idx):
        session = requests.Session()
        retries = 0
        try:
            while not self._stop.is_set() and not self._fatal:
                with self._lock:
                    seg = self.segments[idx]
                    pos, end = seg[0] + seg[2], seg[1]
                if pos > end:
                    break
                try:
                    r = session.get(self.final_url, stream=True, timeout=(15, 30),
                                    headers=self._req_headers({"Range": f"bytes={pos}-{end}"}))
                    if r.status_code != 206:
                        raise IOError(f"HTTP {r.status_code} (range not honoured)")
                    with open(self.part_path, "r+b") as f:
                        f.seek(pos)
                        for chunk in r.iter_content(CHUNK):
                            if self._stop.is_set():
                                break
                            with self._lock:
                                rem = self._remaining(seg)
                            if rem <= 0:
                                break
                            if len(chunk) > rem:
                                chunk = chunk[:rem]
                            f.write(chunk)
                            with self._lock:
                                seg[2] += len(chunk)
                            retries = 0
                            if self.limiter:
                                self.limiter.consume(len(chunk))
                    r.close()
                except Exception as e:  # network hiccup -> retry with backoff
                    retries += 1
                    if retries > MAX_RETRIES:
                        self._fatal = e
                        break
                    self._stop.wait(min(2 ** retries, 20))
        finally:
            session.close()
            with self._lock:
                self._active.discard(idx)

    # --- single stream (server without Range support) ---
    def _run_single(self):
        self.single_done = 0
        self._active.add(0)
        r = requests.get(self.final_url, headers=self._req_headers(), stream=True, timeout=(15, 30))
        try:
            if r.status_code >= 400:
                raise IOError(f"HTTP {r.status_code}")
            with open(self.part_path, "wb") as f:
                for chunk in r.iter_content(CHUNK):
                    if self._stop.is_set():
                        return
                    f.write(chunk)
                    with self._lock:
                        self.single_done += len(chunk)
                    if self.limiter:
                        self.limiter.consume(len(chunk))
            if not self.size:
                self.size = self.single_done
        finally:
            r.close()
