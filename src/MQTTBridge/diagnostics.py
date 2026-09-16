"""Bounded diagnostics for an Enigma2 main loop that stops making progress."""

import os
import sys
import threading
import time

from .enigma2 import Ticker
from .log import get_logger

LOG = get_logger("diagnostics")

HEARTBEAT_MILLISECONDS = 1000
STALL_SECONDS = 2.0
REPORT_SECONDS = 10.0
MAX_STACK_FRAMES = 20


class LoopMonitor:
    """Watch the UI heartbeat from a daemon without joining the UI lifecycle."""

    def __init__(self, clock=None, frames=None, thread_factory=None):
        self._clock = clock or time.monotonic
        self._frames = frames or sys._current_frames
        self._thread_factory = thread_factory or threading.Thread
        self._ticker = Ticker(self._beat, "event-loop heartbeat")
        self._lock = threading.Lock()
        self._heartbeat = None
        self._ui_thread = None
        self._last_report = None
        self._stalled = False
        self._run = 0
        self._stop_event = None

    def start(self):
        now = self._clock()
        with self._lock:
            if self._stop_event is not None:
                return True
            self._run += 1
            run = self._run
            self._heartbeat = now
            self._ui_thread = threading.get_ident()
            self._last_report = None
            self._stalled = False
            stop_event = threading.Event()
            self._stop_event = stop_event
        if not self._ticker.start(HEARTBEAT_MILLISECONDS):
            with self._lock:
                if self._stop_event is stop_event:
                    self._stop_event = None
                    self._run += 1
            return False
        try:
            worker = self._thread_factory(
                target=self._watch, args=(run, stop_event), name="mqttbridge-diagnostics"
            )
            worker.daemon = True
            worker.start()
        except Exception:
            self._ticker.stop()
            stop_event.set()
            with self._lock:
                if self._stop_event is stop_event:
                    self._stop_event = None
                    self._run += 1
            LOG.exception("could not start the event-loop monitor")
            return False
        return True

    def stop(self):
        self._ticker.stop()
        with self._lock:
            self._run += 1
            stop_event = self._stop_event
            self._stop_event = None
        if stop_event is not None:
            stop_event.set()

    def _beat(self):
        with self._lock:
            self._heartbeat = self._clock()
            self._ui_thread = threading.get_ident()

    def _watch(self, run, stop_event):
        while not stop_event.wait(HEARTBEAT_MILLISECONDS / 1000.0):
            self._inspect(run)

    def _inspect(self, run, now=None):
        now = self._clock() if now is None else now
        with self._lock:
            if run != self._run or self._heartbeat is None:
                return
            age = max(0.0, now - self._heartbeat)
            ui_thread = self._ui_thread
            was_stalled = self._stalled
            if age <= STALL_SECONDS:
                self._stalled = False
                self._last_report = None
            elif self._last_report is None or now - self._last_report >= REPORT_SECONDS:
                self._stalled = True
                self._last_report = now
            else:
                return
        if age <= STALL_SECONDS:
            if was_stalled:
                LOG.warning("event loop recovered; heartbeat age %.1fs", age)
            return
        stack = self._safe_stack(ui_thread)
        LOG.warning("event loop stalled; heartbeat age %.1fs; stack %s", age, stack)

    def _safe_stack(self, thread_id):
        try:
            frame = self._frames().get(thread_id)
        except Exception:
            return "unavailable"
        entries = []
        while frame is not None and len(entries) < MAX_STACK_FRAMES:
            code = frame.f_code
            entries.append(
                f"{os.path.basename(code.co_filename)}:{code.co_name}:{frame.f_lineno}"
            )
            frame = frame.f_back
        return " <- ".join(entries) if entries else "unavailable"
