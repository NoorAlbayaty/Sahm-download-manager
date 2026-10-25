"""External helper tools for video downloads: yt-dlp, FFmpeg and Deno.
On Windows they live in %APPDATA%\\Sahm\\tools and are downloaded on first use."""
import os
import sys
import time
import shutil
import zipfile
import subprocess
import threading

from settings import data_dir

IS_WIN = sys.platform == "win32"
EXE = ".exe" if IS_WIN else ""
NO_WINDOW = 0x08000000 if IS_WIN else 0     # CREATE_NO_WINDOW: no console popping up

SOURCES = {
    "yt-dlp": "https://github.com/yt-dlp/yt-dlp/releases/latest/download/yt-dlp.exe",
    "ffmpeg": "https://github.com/yt-dlp/FFmpeg-Builds/releases/download/latest/"
              "ffmpeg-master-latest-win64-gpl.zip",
    "deno": "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-pc-windows-msvc.zip",
}


def bundled_dir():
    """Tools shipped inside the program (installed next to Sahm.exe, or vendor_tools/ when run from source)."""
    if getattr(sys, "frozen", False):
        return os.path.join(os.path.dirname(sys.executable), "tools")
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "vendor_tools")


def tools_dir():
    d = os.path.join(data_dir(), "tools")
    os.makedirs(d, exist_ok=True)
    return d


def find(name):
    """Path of a tool: user folder (self-updated yt-dlp), then the bundled copy, then PATH."""
    for d in (tools_dir(), bundled_dir()):
        p = os.path.join(d, name + EXE)
        if os.path.exists(p):
            return p
    return shutil.which(name)


def missing():
    return [n for n in ("yt-dlp", "ffmpeg", "deno") if not find(n)]


def env():
    """Environment for yt-dlp: our tools first on PATH (so it finds ffmpeg + deno), UTF-8 output."""
    e = dict(os.environ)
    e["PATH"] = os.pathsep.join([tools_dir(), bundled_dir(), e.get("PATH", "")])
    e["PYTHONUTF8"] = "1"
    e["PYTHONIOENCODING"] = "utf-8"
    return e


def run(args, timeout=None):
    return subprocess.run(args, capture_output=True, env=env(), timeout=timeout,
                          creationflags=NO_WINDOW, stdin=subprocess.DEVNULL)


def install_from_download(name, downloaded_path):
    """Put a downloaded tool in place (extract the .exe from zips)."""
    dest_dir = tools_dir()
    if name == "yt-dlp":
        os.replace(downloaded_path, os.path.join(dest_dir, "yt-dlp" + EXE))
        return
    wanted = {"ffmpeg": ("ffmpeg.exe", "ffprobe.exe"), "deno": ("deno.exe",)}[name]
    with zipfile.ZipFile(downloaded_path) as z:
        for member in z.namelist():
            base = os.path.basename(member).lower()
            if base in wanted:
                with z.open(member) as src, open(os.path.join(dest_dir, base), "wb") as dst:
                    shutil.copyfileobj(src, dst)
    os.remove(downloaded_path)


_update_lock = threading.Lock()


def prepare():
    """At startup: copy the bundled yt-dlp to the user folder (Program Files is read-only,
    and yt-dlp must be able to update itself), then update it in the background."""
    src = os.path.join(bundled_dir(), "yt-dlp" + EXE)
    dst = os.path.join(tools_dir(), "yt-dlp" + EXE)
    try:
        if os.path.exists(src) and (not os.path.exists(dst) or os.path.getmtime(src) > os.path.getmtime(dst)):
            shutil.copy2(src, dst)
    except OSError:
        pass
    update_ytdlp_in_background()


def auto_install(limiter=None, progress=None, stop=None):
    """Silent fallback when the bundle is missing: fetch the tools with Sahm's own engine.
    progress(fraction) is called while downloading. Returns True when everything is ready."""
    need = missing()
    if not need:
        return True
    if not IS_WIN:
        return False
    import urllib.parse
    from engine import DownloadTask, Status
    tmp = os.path.join(tools_dir(), "downloading")
    tasks = {}
    for n in need:
        fname = os.path.basename(urllib.parse.urlparse(SOURCES[n]).path)
        t = DownloadTask(SOURCES[n], tmp, fname, connections=8)
        t.start(limiter)
        tasks[n] = t
    while True:
        if stop is not None and stop.is_set():
            for t in tasks.values():
                t.pause()
            return False
        done = sum(t.downloaded for t in tasks.values())
        total = sum(t.size or 0 for t in tasks.values())
        if progress and total:
            progress(done / total)
        if any(t.status == Status.ERROR for t in tasks.values()):
            return False
        if all(t.status == Status.COMPLETED for t in tasks.values()):
            break
        time.sleep(0.3)
    for n, t in tasks.items():
        install_from_download(n, t.path)
    return not missing()


def update_ytdlp_in_background(force=False):
    """Run `yt-dlp -U` at most once a day (YouTube changes often)."""
    def work():
        with _update_lock:
            exe = find("yt-dlp")
            if not exe or not os.path.abspath(exe).startswith(os.path.abspath(tools_dir())):
                return
            stamp = os.path.join(tools_dir(), "last_update")
            try:
                if not force and time.time() - os.path.getmtime(stamp) < 86400:
                    return
            except OSError:
                pass
            try:
                run([exe, "-U"], timeout=300)
                with open(stamp, "w") as f:
                    f.write(str(time.time()))
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()


def kill_tree(proc):
    if proc is None or proc.poll() is not None:
        return
    if IS_WIN:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                       capture_output=True, creationflags=NO_WINDOW)
    else:
        proc.kill()


def write_cookie_file(cookies, path):
    """cookies: list of chrome.cookies objects -> Netscape cookies.txt for yt-dlp."""
    lines = ["# Netscape HTTP Cookie File"]
    for c in cookies or []:
        try:
            domain = c["domain"]
            host_only = c.get("hostOnly", False)
            if not host_only and not domain.startswith("."):
                domain = "." + domain
            if any(ch in str(c.get(k, "")) for k in ("domain", "path", "name", "value") for ch in "\t\r\n"):
                continue          # would break the cookies.txt format
            lines.append("\t".join([
                domain, "FALSE" if host_only else "TRUE", c.get("path", "/"),
                "TRUE" if c.get("secure") else "FALSE",
                str(int(c.get("expirationDate", 0) or 0)), c["name"], c.get("value", "")]))
        except (KeyError, TypeError):
            continue
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # owner-only
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    return path
