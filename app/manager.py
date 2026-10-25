"""Download list, queue (max concurrent downloads) and scheduler."""
import os
import json
import time
import datetime

from engine import DownloadTask, RateLimiter, Status
from video import VideoTask
from settings import data_dir


class Manager:
    def __init__(self, settings, store_path=None):
        self.settings = settings
        self.store_path = store_path or os.path.join(data_dir(), "downloads.json")
        self.tasks = []
        self.limiter = RateLimiter()
        self.queue_running = False
        self._last_save = 0
        self._sched_fired = set()
        self.on_complete = None   # callback(task) – called from worker thread
        self.apply_settings()
        self.load()

    # ---------- persistence ----------
    def load(self):
        try:
            with open(self.store_path, encoding="utf-8") as f:
                for d in json.load(f):
                    try:
                        t = (VideoTask if d.get("kind") == "video" else DownloadTask).from_dict(d)
                    except (KeyError, ValueError, TypeError):
                        continue          # skip a damaged entry instead of losing the whole list
                    t.on_finish = self._finished
                    self.tasks.append(t)
        except (OSError, ValueError, KeyError):
            pass

    def save(self):
        data = [t.to_dict() for t in self.tasks]
        tmp = self.store_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, self.store_path)
        self._last_save = time.monotonic()

    def apply_settings(self):
        self.limiter.set_rate(int(self.settings.get("speed_limit_kb", 0)) * 1024)

    # ---------- operations ----------
    def add(self, url, folder=None, filename=None, connections=None, headers=None, start_now=True):
        t = DownloadTask(url, folder or self.settings["download_dir"], filename,
                         connections or self.settings["connections"], headers)
        t.on_finish = self._finished
        self.tasks.append(t)
        if start_now:
            t.start(self.limiter)
        else:
            t.status = Status.QUEUED
        self.save()
        return t

    def add_video(self, url, folder=None, fmt="bv*+ba/b", title="", mp3=False, cookie_file=None,
                  size_hint=None, start_now=True):
        t = VideoTask(url, folder or self.settings["download_dir"], fmt, title,
                      self.settings["connections"], mp3, cookie_file, size_hint)
        t.on_finish = self._finished
        self.tasks.append(t)
        if start_now:
            t.start(self.limiter)
        self.save()
        return t

    def start(self, task):
        if task.status == Status.COMPLETED:
            return
        task.start(self.limiter)

    def pause(self, task):
        task.pause()

    def remove(self, task, delete_file=False):
        task.pause()
        task.wait(3)
        if task in self.tasks:
            self.tasks.remove(task)
        if hasattr(task, "forget_cookies"):
            task.forget_cookies()
        for p in ([task.part_path] + ([task.path] if delete_file else [])):
            try:
                os.remove(p)
            except OSError:
                pass
        self.save()

    def redownload(self, task):
        task.reset()
        task.start(self.limiter)

    def start_queue(self):
        """Run the downloads the user added to the queue - never ones they paused, or failed ones."""
        self.queue_running = True

    def stop_queue(self):
        """Stop only what the queue started; those downloads stay in the queue for next time."""
        self.queue_running = False
        for t in self.tasks:
            if getattr(t, "from_queue", False) and t.status in Status.ACTIVE:
                t.pause()
                t.requeue = True

    def pause_all(self):
        for t in self.tasks:
            t.pause()

    def shutdown(self):
        self.pause_all()
        for t in self.tasks:
            t.wait(3)
        self.save()

    def _finished(self, task):
        if self.on_complete:
            self.on_complete(task)

    # ---------- periodic ----------
    def active_count(self):
        return sum(1 for t in self.tasks if t.status in Status.ACTIVE)

    def total_speed(self):
        return sum(t.speed for t in self.tasks)

    def tick(self):
        """Call ~twice per second from the UI thread."""
        for t in self.tasks:
            t.sample_speed()
            if getattr(t, "requeue", False) and not t.is_running():
                t.status, t.requeue, t.from_queue = Status.QUEUED, False, False
        if self.queue_running:
            slots = int(self.settings["max_concurrent"]) - self.active_count()
            waiting = [t for t in self.tasks if t.status == Status.QUEUED]
            for t in waiting[:max(0, slots)]:
                t.from_queue = True
                t.start(self.limiter)
            if not waiting and self.active_count() == 0:
                self.queue_running = False
        self._scheduler()
        if time.monotonic() - self._last_save > 5:
            self.save()

    def _scheduler(self):
        s = self.settings
        if not s.get("sched_enabled"):
            return
        now = datetime.datetime.now()
        hm = now.strftime("%H:%M")
        key_day = now.strftime("%Y-%m-%d")
        if hm == s.get("sched_start") and ("start", key_day) not in self._sched_fired:
            self._sched_fired.add(("start", key_day))
            self.start_queue()
        if s.get("sched_stop") and hm == s.get("sched_stop") and ("stop", key_day) not in self._sched_fired:
            self._sched_fired.add(("stop", key_day))
            self.stop_queue()
