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

    def start(self):
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()

    def _poll(self):
        payload = read(self.path)
        if payload["mounted"] is False:
            LOG.debug("%s is not mounted", self.path)
        self.publish("hdd", payload)

    def snapshot(self):
        return {"hdd": read(self.path)}
