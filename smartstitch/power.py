"""Keep macOS awake while work or cluster availability requires it."""
from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable

LOGGER = logging.getLogger(__name__)


class SleepInhibitor:
    def __init__(self, reasons: Callable[[], list[str]], interval: float = 1.0):
        self.reasons = reasons
        self.interval = interval
        self.lock = threading.RLock()
        self.stopped = threading.Event()
        self.thread: threading.Thread | None = None
        self.process: subprocess.Popen | None = None
        self.current_reasons: list[str] = []
        self.error: str | None = None

    def status(self) -> dict:
        with self.lock:
            return {"supported": sys.platform == "darwin",
                    "active": self.process is not None and self.process.poll() is None,
                    "reasons": list(self.current_reasons), "error": self.error}

    def refresh(self) -> None:
        reasons = self.reasons()
        with self.lock:
            if self.stopped.is_set():
                return
            self.current_reasons = reasons
            if self.process is not None and self.process.poll() is not None:
                self.process = None
            if not reasons:
                self._release()
                self.error = None
            elif self.process is None and sys.platform == "darwin":
                try:
                    # No -d/-u: the display can turn off and the user can lock.
                    # -w also releases the assertion if the parent crashes.
                    self.process = subprocess.Popen(
                        ["/usr/bin/caffeinate", "-ims", "-w", str(os.getpid())],
                        stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    self.error = None
                except OSError as exc:
                    message = str(exc)
                    if self.error != message:
                        LOGGER.warning("Unable to prevent sleep: %s", message)
                    self.error = message

    def start(self) -> None:
        with self.lock:
            if self.stopped.is_set() or self.thread is not None:
                return
            self.refresh()
            self.thread = threading.Thread(target=self._monitor, name="sleep-inhibitor", daemon=True)
            self.thread.start()

    def _monitor(self) -> None:
        while not self.stopped.wait(self.interval):
            try:
                self.refresh()
            except Exception:
                LOGGER.exception("Unable to check sleep protection state")

    def _release(self) -> None:
        process, self.process = self.process, None
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=1)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)

    def shutdown(self, timeout: float = 2.0) -> None:
        self.stopped.set()
        with self.lock:
            self._release()
            self.current_reasons = []
        if self.thread and self.thread is not threading.current_thread():
            self.thread.join(max(0.0, timeout))
