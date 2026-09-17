"""Opt-in, privacy-bounded health telemetry from OSCam's local WebIf."""

import hashlib
import hmac
import json
import os
import re
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import unquote, urlsplit
from urllib.request import (
    HTTPDigestAuthHandler,
    HTTPPasswordMgrWithDefaultRealm,
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from .enigma2 import Ticker
from .log import get_logger, register_secret
from .publisher import Publisher

LOG = get_logger("oscam")

POLL_MILLISECONDS = 30000
TIMEOUT_SECONDS = 3
MAX_RESPONSE_BYTES = 128 * 1024
MAX_READERS = 64
MAX_COUNT = 65535
STALE_SECONDS = 90
# urllib's Digest handler refuses after five retries. Divide the remaining
# absolute budget across the initial request plus those retries so its internal
# challenge loop cannot restart the full timeout each time.
MAX_DIGEST_REQUESTS = 6
_PROBE_SLOT = threading.Lock()

_VERSION = re.compile(
    r"^[0-9]{1,3}\.[0-9]{1,3}(?:[._-][A-Za-z0-9]+)*(?: build r[0-9]{1,8})?$"
)
_PROTOCOLS = frozenset(
    {
        "camd33",
        "camd35",
        "camd35_tcp",
        "cccam",
        "constcw",
        "gbox",
        "ghttp",
        "internal",
        "mouse",
        "mp35",
        "newcamd",
        "pcsc",
        "phoenix",
        "radegast",
        "scam",
        "sc8in1",
        "serial",
        "smartreader",
        "stapi",
        "stapi5",
    }
)
_LOCAL_PROTOCOLS = frozenset(
    {"internal", "mouse", "mp35", "pcsc", "phoenix", "sc8in1", "smartreader", "stapi", "stapi5"}
)
_SERVER_PROTOCOLS = _PROTOCOLS - _LOCAL_PROTOCOLS
_LOCAL_STATUS = {
    "CARDOK": "ready",
    "OFF": "no_card",
    "NEEDINIT": "initializing",
    "ERROR": "error",
    "READER DEVICE ERROR": "error",
    "SLEEP": "sleeping",
    "DUPLICATE": "duplicate",
    "UNKNOWN": "unknown",
    "UNDEF": "unknown",
}
_SERVER_STATUS = {
    "CONNECTED": "connected",
    "OK": "connected",
    "OFF": "disconnected",
    "ERROR": "error",
    "READER DEVICE ERROR": "error",
    "NEEDINIT": "connecting",
    "SLEEP": "sleeping",
    "DUPLICATE": "duplicate",
    "UNKNOWN": "unknown",
    "UNDEF": "unknown",
}


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, new_url):
        return None


def _bounded_int(value, maximum=MAX_COUNT):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        found = value
    elif isinstance(value, str) and value.isdigit():
        try:
            found = int(value)
        except (ValueError, OverflowError):
            return None
    else:
        return None
    return found if 0 <= found <= maximum else None


def _safe_version(value):
    text = str(value or "").strip()
    return text if _VERSION.fullmatch(text) else None


def _safe_protocol(value):
    text = str(value or "").strip().lower()
    return text if text in _PROTOCOLS else None


def _reader_label(row):
    value = row.get("rname_enc") or row.get("name_enc") or row.get("label")
    if not isinstance(value, str):
        return None
    label = unquote(value).strip()
    return label if 0 < len(label) <= 128 else None


def _shared_cards(connection):
    if not isinstance(connection, dict):
        return None
    values = connection.get("entitlements")
    if isinstance(values, dict):
        values = [values]
    if not isinstance(values, list):
        return None
    for value in values:
        if isinstance(value, dict) and "cccount" in value:
            return _bounded_int(value.get("cccount"))
    return None


def _opaque_id(kind, label, salt):
    digest = hmac.new(salt, label.encode("utf-8"), hashlib.sha256).hexdigest()[:12]
    prefixes = {"reader": "reader_", "server": "server_", "unknown": "source_"}
    return prefixes[kind] + digest


def normalize(status_document, readers_document, salt, process_running=True):
    """Reduce OSCam JSON to the public schema, discarding every raw identity."""
    root = status_document.get("oscam") if isinstance(status_document, dict) else None
    configured = readers_document.get("oscam") if isinstance(readers_document, dict) else None
    if not isinstance(root, dict) or not isinstance(configured, dict):
        raise ValueError("OSCam API response has no root object")
    status = root.get("status")
    rows = status.get("client") if isinstance(status, dict) else None
    readers = configured.get("readers")
    if not isinstance(rows, list) or not isinstance(readers, list):
        raise ValueError("OSCam API response has no reader lists")

    configured_by_label = {}
    for row in readers:
        if not isinstance(row, dict) or (label := _reader_label(row)) is None:
            continue
        configured_by_label[label] = row
        if len(configured_by_label) > MAX_READERS:
            raise ValueError("OSCam API returned too many readers")
    live_by_label = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("type") not in ("r", "p"):
            continue
        if (label := _reader_label(row)) is not None:
            live_by_label[label] = row
            if len(set(configured_by_label) | set(live_by_label)) > MAX_READERS:
                raise ValueError("OSCam API returned too many readers")

    labels = sorted(set(configured_by_label) | set(live_by_label))
    if len(labels) > MAX_READERS:
        raise ValueError("OSCam API returned too many readers")
    public = []
    cards_ready = 0
    servers_connected = 0
    shared_total = 0
    shared_known = False
    enabled_count = 0
    healthy_count = 0
    for label in labels:
        configured_row = configured_by_label.get(label, {})
        live = live_by_label.get(label, {})
        raw_type = live.get("type") or configured_row.get("type")
        protocol = _safe_protocol(configured_row.get("protocol") or live.get("protocol"))
        if raw_type == "r" or (not raw_type and protocol in _LOCAL_PROTOCOLS):
            kind = "reader"
        elif raw_type == "p" or (not raw_type and protocol in _SERVER_PROTOCOLS):
            kind = "server"
        else:
            kind = "unknown"
        enabled = str(configured_row.get("enabled", "1")) == "1"
        if enabled:
            enabled_count += 1
        connection = live.get("connection")
        raw_status = connection.get("status") if isinstance(connection, dict) else None
        mapping = _LOCAL_STATUS if kind == "reader" else _SERVER_STATUS if kind == "server" else {}
        normalized_status = mapping.get(str(raw_status or "").strip().upper(), "unknown")
        healthy = enabled and normalized_status in ("ready", "connected")
        if healthy:
            healthy_count += 1
        if enabled and kind == "reader" and normalized_status == "ready":
            cards_ready += 1
        shared = None
        if enabled and kind == "server" and protocol == "cccam":
            shared = _shared_cards(connection)
            if shared is not None:
                shared_known = True
                shared_total = min(MAX_COUNT, shared_total + shared)
        public.append(
            {
                "id": _opaque_id(kind, label, salt),
                "kind": kind,
                "enabled": enabled,
                "status": "disabled" if not enabled else normalized_status,
                "protocol": protocol,
                "shared_cards": shared,
            }
        )

    uptime = _bounded_int(root.get("apiruntime"), 10 * 365 * 24 * 60 * 60)
    readonly = str(root.get("readonly", ""))
    return {
        "software": "OSCam",
        "version": _safe_version(root.get("version")),
        "software_running": bool(process_running),
        "api_reachable": True,
        "api_access": "granted",
        "readonly": readonly == "1" if readonly in ("0", "1") else None,
        "uptime_s": uptime,
        "readers_configured": len(configured_by_label),
        "readers_enabled": enabled_count,
        "readers_healthy": healthy_count,
        "cards_ready": cards_ready,
        "servers_connected": servers_connected
        + sum(1 for row in public if row["kind"] == "server" and row["status"] == "connected"),
        "shared_cards": shared_total if shared_known else None,
        "readers": public,
    }


def process_running(proc_root="/proc"):
    """Whether an OSCam process exists, without reading argv or publishing its name."""
    try:
        names = os.listdir(proc_root)[:4096]
    except OSError:
        return None
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(os.path.join(proc_root, name, "comm"), encoding="ascii") as handle:
                command = handle.read(65).strip().lower()
        except (OSError, UnicodeError):
            continue
        if command == "oscam" or command.startswith("oscam-"):
            return True
    return False


def _opener(url, username, password):
    passwords = HTTPPasswordMgrWithDefaultRealm()
    split = urlsplit(url)
    origin = split.scheme + "://" + split.netloc + "/"
    passwords.add_password(None, origin, username or "", password or "")
    return build_opener(ProxyHandler({}), _NoRedirect(), HTTPDigestAuthHandler(passwords))


def _request_json(opener, url, deadline):
    request = Request(url, headers={"Accept": "application/json"})
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError("OSCam API deadline expired")
    with opener.open(request, timeout=remaining / MAX_DIGEST_REQUESTS) as response:
        if time.monotonic() >= deadline:
            raise TimeoutError("OSCam API deadline expired")
        final = urlsplit(response.geturl())
        if final.scheme != "http" or final.hostname != "127.0.0.1":
            raise ValueError("OSCam API left loopback")
        chunks = []
        size = 0
        reader = getattr(response, "read1", response.read)
        while size <= MAX_RESPONSE_BYTES:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise TimeoutError("OSCam API deadline expired")
            # HTTPResponse exposes the underlying socket. Updating its timeout
            # makes the total deadline authoritative even for a slow-drip body.
            sock = getattr(getattr(getattr(response, "fp", None), "raw", None), "_sock", None)
            if sock is not None:
                sock.settimeout(remaining)
            chunk = reader(min(8192, MAX_RESPONSE_BYTES + 1 - size))
            if not chunk:
                break
            chunks.append(chunk)
            size += len(chunk)
        data = b"".join(chunks)
    if len(data) > MAX_RESPONSE_BYTES:
        raise ValueError("OSCam API response is too large")
    decoded = json.loads(data.decode("utf-8"))
    if not isinstance(decoded, dict):
        raise ValueError("OSCam API response is not an object")
    return decoded


def unavailable(process, reachable=False, access=None):
    return {
        "software": "OSCam" if process else None,
        "version": None,
        "software_running": process,
        "api_reachable": reachable,
        "api_access": access,
        "readonly": None,
        "uptime_s": None,
        "readers_configured": None,
        "readers_enabled": None,
        "readers_healthy": None,
        "cards_ready": None,
        "servers_connected": None,
        "shared_cards": None,
        "readers": [],
    }


def probe(
    port,
    username="",
    password="",
    salt=b"",
    timeout=TIMEOUT_SECONDS,
    opener=None,
    proc_root="/proc",
):
    """Fetch only OSCam's two read-only JSON views from loopback."""
    running = process_running(proc_root)
    base = "http://127.0.0.1:" + str(int(port)) + "/oscamapi.json?part="
    client = opener or _opener(base, username, password)
    deadline = time.monotonic() + timeout
    try:
        status = _request_json(client, base + "status", deadline)
        readers = _request_json(client, base + "readerlist", deadline)
        return normalize(status, readers, salt, True)
    except HTTPError as error:
        if error.code in (401, 403):
            return unavailable(running, reachable=True, access="denied")
        return unavailable(running, reachable=True)
    except (OSError, URLError, ValueError, UnicodeError, json.JSONDecodeError, TimeoutError):
        return unavailable(running)


class OscamPublisher(Publisher):
    """`oscam` — local software, API and neutral reader health."""

    name = "oscam"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._ticker = Ticker(self._poll, "oscam telemetry")
        self._lock = threading.Lock()
        self._running = False
        self._stopped = True
        self._generation = 0
        self._serial = 0
        self._latest_serial = 0
        self._cached = None
        self._salt = None
        self._last_completion = 0.0
        self._stale_published = False

    def start(self):
        if not self.value("oscam_telemetry"):
            return False
        self._salt = self._identity_salt()
        if self._salt is None:
            LOG.error("could not persist the OSCam reader identity salt")
            return False
        # Keep every historical value registered: an old in-flight worker can
        # still fail after Setup saved a replacement credential.
        register_secret(self.value("oscam_password"))
        self._generation += 1
        self._stopped = False
        self._cached = unavailable(None)
        self._last_completion = time.monotonic()
        self._stale_published = False
        self.publish("oscam", self._cached)
        self._start_probe()
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._generation += 1
        self._stopped = True
        self._ticker.stop()

    def _poll(self):
        if self._stopped or not self.value("oscam_telemetry"):
            return
        if (
            self._running
            and self._last_completion
            and time.monotonic() - self._last_completion >= STALE_SECONDS
            and not self._stale_published
        ):
            self._stale_published = True
            self._cached = unavailable(None)
            self.publish("oscam", self._cached)
        self._start_probe()

    def _start_probe(self):
        if self._stopped:
            return False
        with self._lock:
            if self._running:
                return False
            if not _PROBE_SLOT.acquire(False):
                return False
            self._running = True
            self._serial += 1
            serial = self._serial
            self._latest_serial = serial
        generation = self._generation
        port = self.value("oscam_port") or 8888
        username = self.value("oscam_username") or ""
        password = self.value("oscam_password") or ""
        try:
            threading.Thread(
                target=self._probe,
                args=(generation, serial, port, username, password, self._salt),
                name="mqttbridge-oscam",
                daemon=True,
            ).start()
        except Exception:
            with self._lock:
                self._running = False
            _PROBE_SLOT.release()
            LOG.exception("could not start the OSCam health probe")
            return False
        return True

    def _probe(self, generation, serial, port, username, password, salt):
        try:
            try:
                if salt is None:
                    payload = unavailable(None)
                else:
                    payload = probe(port, username, password, salt=salt)
            except Exception:
                # Never format an HTTP/auth exception: third-party implementations
                # may include a username, URL or response fragment in its text.
                LOG.warning("OSCam health probe failed")
                payload = unavailable(None)
        finally:
            with self._lock:
                self._running = False
            _PROBE_SLOT.release()
        client = getattr(self.bridge, "client", None) if self.bridge is not None else None
        dispatch = getattr(client, "_dispatch", None)
        if dispatch is None:
            return
        try:
            dispatch(self._finish_probe, generation, serial, payload)
        except Exception:
            LOG.exception("could not return the OSCam probe to the main loop")

    def _finish_probe(self, generation, serial, payload):
        if serial != self._latest_serial or self._stopped or generation != self._generation:
            return
        if not self.value("oscam_telemetry"):
            return
        self._cached = payload
        self._last_completion = time.monotonic()
        self._stale_published = False
        self.publish("oscam", payload)

    def snapshot(self):
        return {"oscam": self._cached} if self._cached is not None else {}

    def _identity_salt(self):
        element = getattr(getattr(self.bridge, "settings", None), "oscam_identity_salt", None)
        text = str(getattr(element, "value", "") or "")
        try:
            if len(text) == 64:
                return bytes.fromhex(text)
        except ValueError:
            pass
        if element is None:
            return None
        text = os.urandom(32).hex()
        try:
            from Components.config import configfile

            element.value = text
            element.save()
            configfile.save()
        except Exception:
            element.value = ""
            return None
        return bytes.fromhex(text)
