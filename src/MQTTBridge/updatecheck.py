"""The update check: which releases exist, read from the signed release index and nowhere else.

This is the receiver's half of reading the index
([ADR-0015](../../docs/adr/0015-signed-self-update.md), the rule in `trust.py`, the publication
in [RELEASE-INDEX.md](../../docs/RELEASE-INDEX.md)). It answers one question for a consumer -
which releases could this receiver install, and which of them is the newest it could - on the
retained `update` topic. Installing one is not done here.

**Two ways in, one rule.** The index reaches the receiver either from the origin, fetched by the
receiver itself, or relayed by the companion integration on the retained topic
`enigma2mqtt/release_index`, for a receiver that has no internet of its own. Both go through
`trust.accept` with the same memory, so an index relayed by anybody with publish rights on the
broker is worth exactly what a fetched one is: nothing until its signature verifies with a key
this build embeds, and nothing if its key or serial is behind what this receiver has already
accepted. The relay is a courier, not an authority.

**Who may cause a connection.** The receiver never assumes internet, and never looks for it on its
own. With `update_check` off - the default, and a box-only setting that `cmd/config` cannot
reach - it makes no connection other than the broker's: no daily check, and `cmd/update_check`
over MQTT is refused. The OpenWebif page may check with it off, because whoever the page admits
could switch it on anyway (`origin.py`) and asking is the consent. A relayed index needs no
setting at all: it causes no connection.

**How a check fetches.** The origin is a constant, fetched over TLS with a context built for the
call - `ssl.create_default_context()`, asserted to verify the certificate and the host name - so a
third-party plugin that switched verification off for the whole process (by replacing
`ssl._create_default_https_context`) cannot switch it off here. `http.client` is used directly,
so a redirect is an answer that is refused, never followed: the origin serves its files itself.
The signature file is fetched first, with five seconds and a 1 KiB cap: that is the probe that
says whether the origin is `reachable`. Then the index, ten seconds, 64 KiB. Each body is read
with a ceiling of one byte past its cap, so an answer with no end is never read whole, and one
byte too many is `too_large` - before the signature is looked at. A pair whose signature does not
verify is read once more: the two files are two requests, and a publication can land between
them. A pair that fails twice is judged.

**Limits.** Manual checks - `cmd/update_check`, from the broker or the page - share one
ten-minute limit (the origin's own cache lifetime); inside it the answer is the stored result and
no request is made. The daily check runs only with `update_check` on, when the last check is a
day old, and only once the receiver's clock has been set: a receiver boots with its clock in
1970, and a check then would fail its TLS and record a stamp that means nothing. Every check is
stamped before it asks, so one that fails has still spent its turn.

**Where it is kept.** `/etc/enigma2/mqttbridge-index.json` (0600), written whole and renamed into
place: the trust state in the shape `trust.store` writes, plus one member of this module's,
`held`, keeping per lineage the last accepted index and its signature file (base64), where it came
from, and the last check's stamp, error and probe result. Everything else in the file is kept as
it was. A memory that cannot be read is never rewritten and judges nothing (`trust.BadMemory`); a
held index is verified again whenever it is loaded, so a build whose keys no longer sign it does
not show it - and its memory stays, because that is about keys, not about the file.

**Threads.** Verifying a signature costs about 40 ms on an armv7 receiver, and every fetch can take
seconds, so none of it runs on enigma2's main loop: each job - loading the file, a check, a relayed
index - runs on a worker thread, one at a time, and hands its result back through the bridge's
main-loop dispatcher (`reactor.callFromThread` where the image has Twisted). Only the hand-back
touches what is published. A job asked for while another runs waits for it; of relayed indexes
only the newest waits.
"""

import base64
import binascii
import http.client
import json
import os
import threading
import time
from urllib.parse import urlsplit

from . import buildid, trust, uninstall
from .enigma2 import Ticker
from .log import get_logger
from .origin import MQTT, granted
from .publisher import Refusal
from .version import CONTRACT

LOG = get_logger("updatecheck")

STATE_PATH = "/etc/enigma2/mqttbridge-index.json"
RELEASE_INDEX_TOPIC = "enigma2mqtt/release_index"
SUFFIX = "update"
# `checked` moves with every check; a check that found nothing new is not a change (ADR-0006).
VOLATILE = ("checked",)

MANUAL_INTERVAL = 10 * 60
DAILY_INTERVAL = 24 * 60 * 60
# The daily tick fires a moment after the stamp it compares with was taken; without the slack
# every other day would be skipped.
DAILY_SLACK = 5 * 60
TICK_MILLISECONDS = 60 * 60 * 1000
PROBE_TIMEOUT = 5
FETCH_TIMEOUT = 10
# 2026-09-01T00:00:00Z. A clock before this - or before the build's own commit - has not been set.
CLOCK_FLOOR = 1788220800
MAX_AVAILABLE = 20
# A 64 KiB index and a 1 KiB signature file in base64, in their JSON object, with room to spare -
# and a hard stop before anything is parsed.
MAX_RELAY_PAYLOAD_BYTES = 88 * 1024
MAX_STATE_BYTES = 512 * 1024

REACHABLE = "reachable"
UNREACHABLE = "unreachable"
UNKNOWN = "unknown"
SOURCE_ORIGIN = "origin"
SOURCE_RELAY = "relay"

# `check_error`: these, or one of `trust.REASONS` - the codes the companion integration uses.
ERROR_UNREACHABLE = "unreachable"
ERROR_REDIRECT = "redirect"
ERROR_HTTP = "http_error"
ERROR_BAD_MEMORY = "bad_memory"
ERROR_BAD_KEYS = "bad_keys"
ERROR_INTERNAL = "internal_error"

# What a relayed payload can come to besides the rule's own verdicts.
MALFORMED_RELAY = "malformed_relay"
TOO_LARGE = "too_large"
ACCEPT = "accept"
TAKEN_BACK = "taken_back"
# The exact bytes already held, delivered again: nothing new, and not a failure.
UNCHANGED = "unchanged"
WRITE_FAILED = "write_failed"

NOT_PERMITTED = "checking for updates is switched off in the plugin's settings"


class Unreachable(Exception):
    """The origin could not be asked: no route, no answer in time, or TLS that did not verify."""


# ------------------------------------------------------------------------ fetching --


def verified_context():
    """A TLS context for one request that verifies the certificate and the host name."""
    import ssl

    context = ssl.create_default_context()
    if context.verify_mode != ssl.CERT_REQUIRED or not context.check_hostname:
        raise Unreachable("this Python's default TLS context does not verify certificates")
    return context


def https_get(url, cap, timeout, connection_factory=None):
    """`(status, body)` of one GET of `url`, the body at most `cap + 1` bytes; or Unreachable.

    Never follows a redirect: a 3xx is returned as the status it is. `timeout` bounds the
    connection, every read, and the whole body.
    """
    parts = urlsplit(url)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("only an https address is fetched, not " + url)
    factory = connection_factory or http.client.HTTPSConnection
    deadline = time.monotonic() + timeout
    try:
        connection = factory(parts.hostname, parts.port or 443, timeout=timeout,
                             context=verified_context())
        try:
            connection.request("GET", parts.path or "/", headers={
                "User-Agent": "enigma2-mqtt-bridge",
                "Accept-Encoding": "identity",
            })
            response = connection.getresponse()
            body = b""
            while len(body) <= cap:
                if time.monotonic() > deadline:
                    raise Unreachable(f"no whole answer within {timeout} s")
                chunk = response.read(min(8192, cap + 1 - len(body)))
                if not chunk:
                    break
                body += chunk
            return response.status, body
        finally:
            connection.close()
    except (OSError, http.client.HTTPException) as error:
        raise Unreachable(str(error)) from error


def in_thread(job):
    threading.Thread(target=job, name="mqttbridge-update-check", daemon=True).start()


def _status_problem(status):
    if 300 <= status < 400:
        return ERROR_REDIRECT
    if status != 200:
        return ERROR_HTTP
    return None


# ------------------------------------------------------------------ what is offered --


def installed_packages(root="/"):
    """The names opkg says are installed - packages and what they provide - or None if unknown."""
    configured = uninstall._opkg_options(root).get("status_file")
    if configured is not None:
        path = uninstall._under(root, configured)
    else:
        path = next((os.path.join(root, base, "status") for base in uninstall.OPKG_DATABASES
                     if os.path.isfile(os.path.join(root, base, "status"))), None)
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return None
    names = set()
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            name, value = line.split(":", 1)
            fields[name.strip()] = value.strip()
        package = fields.get("Package")
        status = fields.get("Status", "").split()
        if not package or status[-1:] != ["installed"]:
            continue
        names.add(package)
        for provided in fields.get("Provides", "").split(","):
            provided = provided.split("(", 1)[0].strip()
            if provided:
                names.add(provided)
    return frozenset(names)


def _unmet(release, installed, integration_mode, contract):
    """Why this receiver could not install `release`, as a reason code, or None."""
    if release["contract"] != contract:
        return "incompatible"
    # Without the integration's own word (`enigma2mqtt/integration/<node>`, which a later
    # release reads), a release that needs a newer integration is taken to be unmet where one
    # is in use, and met where none is.
    if release["min_integration"] is not None and integration_mode:
        return "incompatible"
    if installed is not None:
        for name in release["depends"]:
            if name not in installed:
                return "depends"
    return None


def offer(index, installed=None, integration_mode=False, contract=CONTRACT):
    """`(latest_compatible, available)` for an accepted index.

    `available` is the newest twenty releases at or above the index's floor that are not
    withdrawn, newest first, each with the reason it is not compatible. `installed` is what
    `installed_packages` read, None when it could not: a dependency nobody can check is not
    judged here, and the install checks it again.
    """
    floor = trust.version_key(index["floor"])
    candidates = sorted(
        (entry for entry in index["releases"]
         if entry["withdrawn"] is None and trust.version_key(entry["version"]) >= floor),
        key=lambda entry: trust.version_key(entry["version"]),
        reverse=True,
    )
    available = []
    for entry in candidates[:MAX_AVAILABLE]:
        reason = _unmet(entry, installed, integration_mode, contract)
        available.append({"version": entry["version"], "compatible": reason is None,
                          "reason": reason})
    latest = next((entry["version"] for entry in available if entry["compatible"]), None)
    return latest, available


# -------------------------------------------------------------------- stored state --


def read_state(path):
    """The stored state, or None when there is none; `trust.BadMemory` when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise trust.BadMemory(f"the file cannot be read: {error}") from error
    if len(raw) > MAX_STATE_BYTES:
        raise trust.BadMemory(f"the file is over {MAX_STATE_BYTES} bytes")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise trust.BadMemory(f"the file is not JSON: {error}") from error
    return trust.check_state(state)


def write_state(path, state):
    """Write `state` whole, 0600, renamed into place. False, logged, when it could not be."""
    temporary = path + ".tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        LOG.exception("could not write %s", path)
        return False
    return True


def _held_part(state, lineage):
    held = state.get("held") if isinstance(state, dict) else None
    part = held.get(lineage) if isinstance(held, dict) else None
    return part if isinstance(part, dict) else {}


def _with_held(state, lineage, part):
    updated = dict(state if state is not None else trust.empty_state())
    held = updated.get("held")
    held = dict(held) if isinstance(held, dict) else {}
    held[lineage] = part
    updated["held"] = held
    return updated


def _b64(raw):
    return base64.b64encode(raw).decode("ascii")


def _unb64(value):
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def _whole_or_none(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else None


# -------------------------------------------------------------------------- checker --


class UpdateChecker:
    """The `update` topic, `cmd/update_check`, the daily check and the relayed index."""

    # Class attributes, so that the tests can point every checker at a temporary file, a fake
    # opkg, a fake origin and an inline worker before a bridge builds one.
    path = STATE_PATH
    opkg_root = "/"
    fetch = staticmethod(https_get)
    run_in_background = staticmethod(in_thread)

    def __init__(self, bridge, path=None, keys=None, acceptance=None, origin=None, clock=None,
                 build=None, overrides=None):
        self.bridge = bridge
        if path is not None:
            self.path = path
        self.clock = clock or time.time
        build = buildid.LOADED if build is None else build
        try:
            configured_origin, configured_keys, configured_acceptance = trust.configured(
                build, buildid.LOADED_OVERRIDES if overrides is None else overrides
            )
            if not trust.is_origin(configured_origin):
                raise trust.Refused("malformed_index", f"not an origin: {configured_origin!r}")
        except (trust.Refused, trust.OverlappingKeys) as error:
            # Only an acceptance build can get here. It judges nothing rather than fall back to
            # the release keys, which would make it indistinguishable from what it tests.
            LOG.error("this build's release index keys cannot be used (%s): no index is judged",
                      error)
            configured_origin, configured_keys, configured_acceptance = trust.ORIGIN, None, True
        self.origin = origin if origin is not None else configured_origin
        self.keys = keys if keys is not None else configured_keys
        self.acceptance = configured_acceptance if acceptance is None else acceptance
        self.clock_floor = max(CLOCK_FLOOR, (build or {}).get("time") or 0)
        self._ticker = Ticker(self._tick, "update check")
        self._lock = threading.Lock()
        self._busy = False
        self._pending = []
        # What the `update` topic says. Main loop only.
        self._checked = None
        self._check_error = None
        self._origin = UNKNOWN
        self._held = None
        self._installed = None
        self.last_relay_verdict = None

    @property
    def lineage(self):
        return trust.LINEAGES[1] if self.acceptance else trust.LINEAGES[0]

    def _value(self, name):
        return None if self.bridge is None else self.bridge.value(name)

    # ------------------------------------------------------------------ lifecycle --

    def start(self):
        """Load what was kept, and start the hourly look at whether a daily check is due."""
        self._ticker.start(TICK_MILLISECONDS)
        self._enqueue(("load",))

    def stop(self):
        self._ticker.stop()

    # ------------------------------------------------------------------- the topic --

    def payload(self):
        index = self._held[0] if self._held else None
        latest, available = None, []
        if index is not None:
            latest, available = offer(index, self._installed,
                                      self._value("ha_mode") == "integration", CONTRACT)
        return {
            "origin": self._origin,
            "checked": self._checked,
            "check_error": self._check_error,
            "index": None if index is None else {
                "serial": index["serial"], "issued": index["issued"], "source": self._held[1],
            },
            "latest_compatible": latest,
            "available": available,
            # The install is a later release's; until then there is never one to report.
            "transaction": None,
        }

    def publish(self):
        if self.bridge is None or not self.bridge.connected:
            return None
        return self.bridge.publish_state(SUFFIX, self.payload(), volatile=VOLATILE)

    # ------------------------------------------------------------------ the asking --

    def request_check(self, origin=MQTT):
        """`cmd/update_check`: None, having started a check or answered from the last one."""
        if not granted(self._value, "update_check", origin):
            return Refusal(NOT_PERMITTED, "not_permitted")
        now = int(self.clock())
        if self._checked is not None and self._checked <= now < self._checked + MANUAL_INTERVAL:
            LOG.info("the last check was %d s ago; answering with its result", now - self._checked)
            self.publish()
            return None
        self._begin_check(now)
        return None

    def _tick(self):
        if not self._value("update_check"):
            return
        now = int(self.clock())
        if now < self.clock_floor:
            LOG.info("not checking for updates yet: the receiver's clock has not been set")
            return
        if self._checked is not None and self._checked <= now < (
            self._checked + DAILY_INTERVAL - DAILY_SLACK
        ):
            return
        LOG.info("the daily check for plugin updates")
        self._begin_check(now)

    def _begin_check(self, now):
        # Stamped before the request: a check that fails has still spent its turn.
        self._checked = now
        self._enqueue(("check", now))

    def on_release_index(self, payload, retain=True):
        """A message on `enigma2mqtt/release_index`: judged on the worker, never obeyed."""
        size = len(payload) if payload is not None else 0
        if size == 0:
            LOG.debug("the relayed release index was retracted; keeping what was accepted")
            return
        if size > MAX_RELAY_PAYLOAD_BYTES:
            LOG.warning("dropping a relayed release index of %d bytes, over the %d byte limit",
                        size, MAX_RELAY_PAYLOAD_BYTES)
            self.last_relay_verdict = TOO_LARGE
            return
        if isinstance(payload, str):
            payload = payload.encode("utf-8")
        self._enqueue(("relay", bytes(payload)))

    # ------------------------------------------------------------------- the worker --

    def _enqueue(self, job):
        with self._lock:
            if self._busy:
                if job[0] == "relay":
                    self._pending = [queued for queued in self._pending if queued[0] != "relay"]
                    self._pending.append(job)
                elif all(queued[0] != job[0] for queued in self._pending):
                    self._pending.append(job)
                return
            self._busy = True
        self._launch(job)

    def _launch(self, job):
        kind = job[0]

        def work():
            try:
                if kind == "load":
                    result = self._load()
                elif kind == "check":
                    result = self._check(job[1])
                else:
                    result = self._relay(job[1])
            except Exception:
                LOG.exception("the update check's %s job raised", kind)
                result = {"kind": kind, "verdict": ERROR_INTERNAL}
                if kind == "check":
                    result.update(checked=job[1], check_error=ERROR_INTERNAL)
            self._hand_back(result)

        try:
            self.run_in_background(work)
        except Exception:
            LOG.exception("could not start the update check's worker")
            with self._lock:
                self._busy = False

    def _hand_back(self, result):
        if self.bridge is None:
            self._deliver(result)
            return
        dispatch = getattr(getattr(self.bridge, "client", None), "_dispatch", None)
        if dispatch is None:
            LOG.warning("the update check finished with no main loop to report to")
            with self._lock:
                self._busy = False
            return
        try:
            dispatch(self._deliver, result)
        except Exception:
            LOG.exception("could not hand the update check's result to the main loop")
            with self._lock:
                self._busy = False

    def _deliver(self, result):
        with self._lock:
            self._busy = False
            following = self._pending.pop(0) if self._pending else None
        try:
            self._apply(result)
        except Exception:
            LOG.exception("could not apply the update check's result")
        self.publish()
        if following is not None:
            self._enqueue(following)

    def _apply(self, result):
        kind = result["kind"]
        if "installed" in result:
            self._installed = result["installed"]
        if "held" in result:
            self._held = result["held"]
        if kind == "check":
            self._checked = result["checked"]
            self._check_error = result.get("check_error")
            if "origin" in result:
                self._origin = result["origin"]
        elif kind == "load":
            # A check started since has newer news than the file.
            if self._checked is None:
                self._checked = result.get("checked")
                self._check_error = result.get("check_error")
                self._origin = result.get("origin", UNKNOWN)
        else:
            self.last_relay_verdict = result.get("verdict")

    # ---------------------------------------------------------- jobs, on the worker --

    def _authentic(self, part):
        """`(index, source)` of a held index that this build's keys still sign, else None."""
        index_raw, signature_raw = _unb64(part.get("index")), _unb64(part.get("sig"))
        if index_raw is None or signature_raw is None or self.keys is None:
            return None
        try:
            index, _key = trust.authenticate(index_raw, signature_raw, self.keys)
        except trust.Refused as refused:
            LOG.info("the kept release index no longer verifies (%s); not showing it",
                     refused.reason)
            return None
        source = part.get("source")
        return index, source if source in (SOURCE_ORIGIN, SOURCE_RELAY) else SOURCE_ORIGIN

    def _load(self):
        result = {"kind": "load", "installed": installed_packages(self.opkg_root), "held": None}
        try:
            state = read_state(self.path)
        except trust.BadMemory as error:
            LOG.warning("%s cannot be read, so no release index is judged until it is removed: "
                        "%s", self.path, error)
            result["check_error"] = ERROR_BAD_MEMORY
            return result
        part = _held_part(state, self.lineage)
        origin = part.get("origin")
        error = part.get("check_error")
        result.update(
            checked=_whole_or_none(part.get("checked")),
            check_error=error if isinstance(error, str) else None,
            origin=origin if origin in (REACHABLE, UNREACHABLE) else UNKNOWN,
            held=self._authentic(part),
        )
        if self.keys is None:
            result["check_error"] = ERROR_BAD_KEYS
        return result

    def _judge(self, index_raw, signature_raw, source):
        """`(verdict, held or None for no change, state to write or None)` for one pair."""
        if self.keys is None:
            return ERROR_BAD_KEYS, None, None
        try:
            state = read_state(self.path)
            memory = trust.memory_for(state, self.keys, acceptance=self.acceptance)
        except trust.BadMemory as error:
            LOG.warning("%s cannot be read, so no release index is judged until it is removed: "
                        "%s", self.path, error)
            return ERROR_BAD_MEMORY, None, None
        part = _held_part(state, self.lineage)
        try:
            accepted = trust.accept(index_raw, signature_raw, self.keys, memory)
        except trust.Refused as refused:
            if refused.reason == "replay":
                if (_unb64(part.get("index")), _unb64(part.get("sig"))) == (index_raw,
                                                                            signature_raw):
                    return UNCHANGED, None, state
                taken = self._take_back(index_raw, signature_raw, part, memory, source)
                if taken is not None:
                    return TAKEN_BACK, taken[0], _with_held(state, self.lineage, taken[1])
            LOG.warning("a release index from the %s was refused (%s): %s", source,
                        refused.reason, refused.detail)
            return refused.reason, None, state
        stored = trust.store(state, self.keys, accepted.memory, acceptance=self.acceptance)
        part = dict(part, index=_b64(index_raw), sig=_b64(signature_raw), source=source)
        LOG.info("accepted release index serial %d from the %s, signed by key %s",
                 accepted.index["serial"], source, accepted.key.key_id)
        return ACCEPT, (accepted.index, source), _with_held(stored, self.lineage, part)

    def _take_back(self, index_raw, signature_raw, part, memory, source):
        """Hold again the very index the memory says was accepted, when none is held.

        The file's index can be lost while its memory - rightly - still names the serial; the
        same genuine index then comes back and the rule calls it a replay. Only with nothing
        authentic held, and at a serial equal to the one remembered for its key. (A silenced key,
        or one below the rank floor, never gets here: the rule refuses it as `rank` before it
        looks at the serial.)
        """
        if self._authentic(part) is not None:
            return None
        try:
            index, key = trust.authenticate(index_raw, signature_raw, self.keys)
        except trust.Refused:
            return None
        if memory["serials"].get(key.key_id) != index["serial"]:
            return None
        LOG.info("holding release index serial %d again: accepted before, no longer held",
                 index["serial"])
        return (index, source), dict(part, index=_b64(index_raw), sig=_b64(signature_raw),
                                     source=source)

    def _check(self, stamp):
        result = {"kind": "check", "checked": stamp, "check_error": None}
        if self.keys is None:
            result["check_error"] = ERROR_BAD_KEYS
            return result
        state = None
        for attempt in (1, 2):
            try:
                status, signature_raw = self.fetch(
                    self.origin + trust.SIGNATURE_FILE, trust.MAX_SIGNATURE_BYTES, PROBE_TIMEOUT
                )
            except Unreachable as error:
                LOG.info("the release origin cannot be reached: %s", error)
                result.update(origin=UNREACHABLE, check_error=ERROR_UNREACHABLE)
                break
            result["origin"] = REACHABLE
            problem = _status_problem(status)
            if problem is None:
                try:
                    status, index_raw = self.fetch(
                        self.origin + trust.INDEX_FILE, trust.MAX_INDEX_BYTES, FETCH_TIMEOUT
                    )
                    problem = _status_problem(status)
                except Unreachable as error:
                    LOG.info("the release index could not be fetched: %s", error)
                    problem = ERROR_UNREACHABLE
            if problem is not None:
                LOG.info("the release origin answered %s: %s", status, problem)
                result["check_error"] = problem
                break
            verdict, held, state = self._judge(index_raw, signature_raw, SOURCE_ORIGIN)
            if verdict == "bad_signature" and attempt == 1:
                LOG.info("the release index and its signature do not match; reading both again "
                         "in case a publication landed between the two requests")
                continue
            if verdict in (ACCEPT, TAKEN_BACK):
                result["held"] = held
            elif verdict != UNCHANGED:
                result["check_error"] = verdict
            break
        self._record(result, state)
        return result

    def _record(self, result, state):
        """Keep the check's stamp, error and probe result with the state the judgement left."""
        if result.get("check_error") in (ERROR_BAD_MEMORY, ERROR_BAD_KEYS):
            return
        if state is None:
            try:
                state = read_state(self.path)
            except trust.BadMemory:
                return
        part = dict(_held_part(state, self.lineage))
        part.update(checked=result["checked"], check_error=result["check_error"])
        if "origin" in result:
            part["origin"] = result["origin"]
        if not write_state(self.path, _with_held(state, self.lineage, part)):
            result.pop("held", None)

    def _relay(self, payload):
        result = {"kind": "relay"}
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, RecursionError):
            body = None
        index_raw = _unb64(body.get("index")) if isinstance(body, dict) else None
        signature_raw = _unb64(body.get("sig")) if isinstance(body, dict) else None
        if index_raw is None or signature_raw is None:
            LOG.warning("dropping a relayed release index that is not "
                        "{\"index\": base64, \"sig\": base64}")
            result["verdict"] = MALFORMED_RELAY
            return result
        verdict, held, state = self._judge(index_raw, signature_raw, SOURCE_RELAY)
        if verdict in (ACCEPT, TAKEN_BACK):
            if write_state(self.path, state):
                result["held"] = held
            else:
                verdict = WRITE_FAILED
        # The rule's own word for the bytes already held: `replay`, and not a failure.
        result["verdict"] = "replay" if verdict == UNCHANGED else verdict
        return result
