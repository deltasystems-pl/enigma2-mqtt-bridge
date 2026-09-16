"""The recording disk.

There is no event for a disk going away. A network mount that does not come back
after a reboot, a USB disk somebody unplugged, a drive that dropped off the bus —
each leaves a receiver that looks perfectly healthy and records nothing. So this
is a poll, once a minute, and it publishes only when the answer changes.

`ismount` rather than `exists`: `/media/hdd` is a directory whether or not
anything is mounted on it, and a recording written into the directory *under* a
missing mount is the failure this topic exists to catch.
"""

import os
import threading

from .enigma2 import Ticker
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("hdd")

RECORDING_PATH = "/media/hdd"
POLL_MILLISECONDS = 60000

BYTES_PER_MEGABYTE = 1024 * 1024


def free_megabytes(path=RECORDING_PATH):
    """Free space as a whole number of megabytes, or None when it cannot be read."""
    try:
        status = os.statvfs(path)
    except (OSError, AttributeError):
        return None
    try:
        return int(status.f_bavail * status.f_frsize / BYTES_PER_MEGABYTE)
    except (TypeError, ValueError, ZeroDivisionError):
        return None


def read(path=RECORDING_PATH):
    """The `hdd` payload."""
    try:
        mounted = bool(os.path.ismount(path))
    except OSError:
        mounted = False
    return {
        "mounted": mounted,
        "path": path,
        "free_mb": free_megabytes(path) if mounted else None,
    }


class HddPublisher(Publisher):
    """`hdd` — mounted, where, and how much room is left."""

    name = "hdd"

    def __init__(self, bridge=None, path=RECORDING_PATH):
        Publisher.__init__(self, bridge)
        self.path = path
        self._ticker = Ticker(self._poll, "hdd")
        self._cached = None
        self._worker_running = False
        self._worker_lock = threading.Lock()
        self._generation = 0
        self._latest_started_generation = None
        self._probe_serial = 0
        self._latest_probe_serial = 0
        self._stopped = True

    def start(self):
        self._generation += 1
        self._stopped = False
        self._start_probe()
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._generation += 1
        self._stopped = True
        self._ticker.stop()

    def _poll(self):
        self._start_probe()

    def _start_probe(self):
        if self._stopped:
            return False
        with self._worker_lock:
            if self._worker_running:
                return False
            self._worker_running = True
            self._probe_serial += 1
            serial = self._probe_serial
            self._latest_probe_serial = serial
        generation = self._generation
        self._latest_started_generation = generation
        try:
            threading.Thread(
                target=self._probe,
                args=(generation, serial),
                name="mqttbridge-hdd",
                daemon=True,
            ).start()
        except Exception:
            with self._worker_lock:
                self._worker_running = False
            LOG.exception("could not start the recording disk probe")
            return False
        return True

    def _probe(self, generation, serial):
        try:
            payload = read(self.path)
        except Exception:
            LOG.exception("the recording disk probe raised")
            payload = {"mounted": False, "path": self.path, "free_mb": None}
        with self._worker_lock:
            self._worker_running = False
        client = getattr(self.bridge, "client", None) if self.bridge is not None else None
        dispatch = getattr(client, "_dispatch", None)
        if dispatch is None:
            LOG.debug("the recording disk probe has no main-loop dispatcher; dropping its result")
            return
        try:
            dispatch(self._finish_probe, generation, serial, payload)
        except Exception:
            LOG.exception("could not return the recording disk probe to the main loop")

    def _finish_probe(self, generation, serial, payload):
        if serial != self._latest_probe_serial:
            return
        if self._stopped or generation != self._generation:
            if (
                not self._stopped
                and self._latest_started_generation != self._generation
            ):
                self._start_probe()
            return
        self._cached = payload
        if payload["mounted"] is False:
            LOG.debug("%s is not mounted", self.path)
        self.publish("hdd", payload)

    def snapshot(self):
        return {"hdd": self._cached} if self._cached is not None else {}
