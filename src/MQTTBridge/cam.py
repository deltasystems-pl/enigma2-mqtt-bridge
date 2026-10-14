"""Opt-in, privacy-bounded conditional-access telemetry."""

import math
import os
import re
import stat
import time

from .enigma2 import Ticker
from .log import get_logger
from .service import NavPublisher, _service_info, info_constant

LOG = get_logger("cam")

ECM_PATH = "/tmp/ecm.info"
MAX_BYTES = 8 * 1024
FRESH_SECONDS = 15
POLL_MILLISECONDS = 2000

EMPTY = {"system": None, "active": None, "encrypted": None, "ecm_ms": None}
_SECONDS = re.compile(r"^(\d+(?:\.\d+)?)\s*(?:s|sec|seconds?)?$", re.I)
_MILLISECONDS = re.compile(r"^(\d+)\s*(?:ms|msec|milliseconds?)$", re.I)
_SYSTEMS = {
    "betacrypt": "BetaCrypt",
    "biss": "BISS",
    "bulcrypt": "BulCrypt",
    "conax": "Conax",
    "cryptoworks": "CryptoWorks",
    "dre-crypt": "DRE-Crypt",
    "irdeto": "Irdeto",
    "mediaguard": "Mediaguard",
    "nagra": "Nagra",
    "nagravision": "Nagravision",
    "powervu": "PowerVu",
    "seca": "SECA",
    "viaccess": "Viaccess",
    "videoguard": "VideoGuard",
}
MAX_ECM_MILLISECONDS = 600000


def _encrypted(session):
    info = _service_info(session)
    key = info_constant("sIsCrypted")
    if info is None or key is None:
        return None
    try:
        value = int(info.getInfo(key))
    except Exception:
        return None
    if value in (0, 1):
        return bool(value)
    return None


def _read_bounded(path):
    """Read one regular, non-symlink file without following a replacement."""
    try:
        before = os.lstat(path)
        if not stat.S_ISREG(before.st_mode) or before.st_size > MAX_BYTES:
            return None, None
        flags = os.O_RDONLY | os.O_NONBLOCK | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(path, flags)
        try:
            opened = os.fstat(descriptor)
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_size > MAX_BYTES
                or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
            ):
                return None, None
            data = os.read(descriptor, MAX_BYTES + 1)
        finally:
            os.close(descriptor)
    except OSError:
        return None, None
    if len(data) > MAX_BYTES:
        return None, None
    return data.decode("utf-8", "replace"), opened.st_mtime


def _ecm_milliseconds(value):
    text = str(value or "").strip()
    if len(text) > 32:
        return None
    match = _MILLISECONDS.fullmatch(text)
    try:
        if match:
            milliseconds = int(match.group(1))
        else:
            match = _SECONDS.fullmatch(text)
            if not match:
                return None
            seconds = float(match.group(1))
            if not math.isfinite(seconds):
                return None
            milliseconds = int(round(seconds * 1000))
    except (OverflowError, ValueError):
        return None
    return milliseconds if 0 <= milliseconds <= MAX_ECM_MILLISECONDS else None


def _parse_ecm(text):
    values = {}
    for line in text.splitlines():
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        values[key.strip().lower()] = value.strip()
    system = values.get("system")
    if system is not None:
        system = _SYSTEMS.get(system.strip().lower())
    milliseconds = _ecm_milliseconds(values.get("ecm time"))
    if milliseconds is None:
        return None
    return system, milliseconds


def _fresh(modified, service_since, current):
    """Whether an ECM written at `modified` belongs to this service and is recent."""
    return (
        modified >= service_since
        and modified <= current + 1
        and current - modified <= FRESH_SECONDS
    )


def read_cam(session, path=ECM_PATH, service_since=0.0, now=None):
    """Return only telemetry refreshed after the latest known service start."""
    return cam_state(session, path, service_since, now)[0]


def cam_state(session, path=ECM_PATH, service_since=0.0, now=None, held=None):
    """`(payload, reading)`: the payload, and the reading to hand back at the next poll.

    A reading is `(system, ecm_ms, modified)` - the two parsed values and the
    file's modification time, never any of its text. `held` is the one the last
    poll returned. It is reported again only while the file is **absent** and
    the reading is still fresh by the rule a file is held to: the cam removes the
    file for a few seconds while it is still decoding - measured when a
    descrambled stream to a stream client ended - and writes it again at the
    next ECM, so a poll that lands in that gap would otherwise say `active: null`
    for a channel the television is decoding. Once `FRESH_SECONDS` have passed
    since that ECM, the answer is the one an absent file always gave. A file that
    is there but stale, malformed or not a regular file ends the reading at once,
    and so does a poll that finds the service free-to-air or cannot tell.
    """
    encrypted = _encrypted(session)
    payload = dict(EMPTY)
    payload["encrypted"] = encrypted
    # A poll that did not see the reading ends it: it is held only from one
    # poll that saw it to the next.
    if encrypted is False:
        payload["active"] = False
        return payload, None
    if encrypted is not True:
        return payload, None

    text, modified = _read_bounded(path)
    current = time.time() if now is None else now
    if text is None or modified is None:
        if held is None or os.path.lexists(path) or not _fresh(held[2], service_since, current):
            return payload, None
        payload["system"], payload["ecm_ms"] = held[0], held[1]
        payload["active"] = True
        return payload, held
    if not _fresh(modified, service_since, current):
        return payload, None
    parsed = _parse_ecm(text)
    if parsed is None:
        return payload, None
    payload["system"], payload["ecm_ms"] = parsed
    payload["active"] = True
    return payload, (parsed[0], parsed[1], modified)


class CamPublisher(NavPublisher):
    """`cam` - bounded fresh ECM activity, never CAM credentials or controls."""

    name = "cam"
    events = ("evStart", "evTunedIn", "evUpdatedInfo")

    def __init__(self, bridge=None, path=ECM_PATH):
        NavPublisher.__init__(self, bridge)
        self.path = path
        self._service_since = 0.0
        # The last fresh reading, parsed values only: see `cam_state`.
        self._held = None
        self._poll = Ticker(self._publish, "cam telemetry")

    def start(self):
        if not self.value("cam_telemetry"):
            self.switched_off = True
            LOG.info("cam_telemetry is off; conditional-access status is not published")
            return False
        if info_constant("sIsCrypted") is None:
            return False
        if not NavPublisher.start(self):
            return False
        self._poll.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._poll.stop()
        self._held = None
        NavPublisher.stop(self)

    def on_service_event(self, event):
        from .service import event_id

        if event == event_id("evStart"):
            self._service_since = time.time()
            # Nothing read for the previous service carries across a zap.
            self._held = None
        self._publish()

    def _payload(self):
        payload, self._held = cam_state(
            self.session, self.path, self._service_since, held=self._held
        )
        return payload

    def _publish(self):
        # An already queued eTimer callback can run after stop/disable. Never
        # recreate a retained privacy-sensitive topic after its tombstone.
        if self._nav is None or not self.value("cam_telemetry"):
            return
        self.publish("cam", self._payload())

    def snapshot(self):
        return {"cam": self._payload()}
