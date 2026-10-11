"""Cooperative cancellation, including an atomic final-file commit boundary."""

import threading


class ProcessingCancelled(Exception):
    def __init__(self):
        super().__init__("任务被用户取消。")


class CancellationToken:
    def __init__(self):
        self.event = threading.Event()
        self._commit_lock = threading.RLock()

    def cancel(self):
        with self._commit_lock:
            self.event.set()

    def check(self):
        if self.event.is_set():
            raise ProcessingCancelled()

    def wait(self, seconds):
        self.check()
        if self.event.wait(max(0, seconds)):
            raise ProcessingCancelled()

    def commit(self, action):
        with self._commit_lock:
            self.check()
            return action()
