"""Opt-in, privacy-bounded conditional-access telemetry."""

import math
import os
import re
import stat
import time

from .enigma2 import Ticker
from .service import NavPublisher, _service_info, info_constant

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


def read_cam(session, path=ECM_PATH, service_since=0.0, now=None):
    """Return only telemetry refreshed after the latest known service start."""
    encrypted = _encrypted(session)
    payload = dict(EMPTY)
    payload["encrypted"] = encrypted
    if encrypted is False:
        payload["active"] = False
        return payload
    if encrypted is not True:
        return payload

    text, modified = _read_bounded(path)
    current = time.time() if now is None else now
    if (
        text is None
        or modified is None
        or modified < service_since
        or modified > current + 1
        or current - modified > FRESH_SECONDS
    ):
        return payload
    parsed = _parse_ecm(text)
    if parsed is None:
        return payload
    payload["system"], payload["ecm_ms"] = parsed
    payload["active"] = True
    return payload


class CamPublisher(NavPublisher):
    """`cam` — bounded fresh ECM activity, never CAM credentials or controls."""

    name = "cam"
    events = ("evStart", "evTunedIn", "evUpdatedInfo")

    def __init__(self, bridge=None, path=ECM_PATH):
        NavPublisher.__init__(self, bridge)
        self.path = path
        self._service_since = 0.0
        self._poll = Ticker(self._publish, "cam telemetry")

    def start(self):
        if not self.value("cam_telemetry") or info_constant("sIsCrypted") is None:
            return False
        if not NavPublisher.start(self):
            return False
        self._poll.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._poll.stop()
        NavPublisher.stop(self)

    def on_service_event(self, event):
        from .service import event_id

        if event == event_id("evStart"):
            self._service_since = time.time()
        self._publish()

    def _payload(self):
        return read_cam(self.session, self.path, self._service_since)

    def _publish(self):
        # An already queued eTimer callback can run after stop/disable. Never
        # recreate a retained privacy-sensitive topic after its tombstone.
        if self._nav is None or not self.value("cam_telemetry"):
            return
        self.publish("cam", self._payload())

    def snapshot(self):
        return {"cam": self._payload()}
