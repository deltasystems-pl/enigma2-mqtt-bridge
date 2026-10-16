"""The menu creator's receiver half: the catalogue Home Assistant publishes, and the edits.

The receiver menu is Home Assistant's (`menu.py`, ADR-0020): the companion integration keeps it in
its own options and publishes it. The menu creator - a page in the receiver's OpenWebif
(`webif.py`, ADR-0022) - lets somebody build that menu from a browser without Home Assistant's
own options flow, and **without a Home Assistant token on the receiver**:

- the integration publishes a **catalogue**, retained, on `enigma2mqtt/menu_catalogue/<node>`:
  every entity a menu row could be bound to, with its name, area and allowed controls, and the
  menu as it is stored, with a `revision` - a hash of it;
- the page edits a copy and saves it; the receiver publishes the whole edited menu, once, on
  `<base>/<node>/menu_edit` with the `revision` it started from;
- the integration validates it with its options flow's own rules, stores it or refuses it, and
  answers on `enigma2mqtt/menu_edit_result/<node>` with the edit's id. The page's save waits for
  that answer, at most `ANSWER_SECONDS`.

**The receiver judges nothing about a menu.** It checks the catalogue's envelope - an object of
the version it knows, a revision, two lists, at most `MAX_CATALOGUE_BYTES` - and keeps the exact
bytes for the page, which reads every string in them as untrusted text. It checks the edit's
envelope - a revision, a list, at most `MAX_EDIT_BYTES` encoded - and that the creator is offered.
Whether an entity may be bound, which control it takes and what a label may say is Home
Assistant's to decide, and it decides on every save.

**Never queued.** An edit is published on the open session only. One the broker had not
acknowledged when the connection went is taken back out of the client's queue, as a
`menu_action` is, and the save waiting for it is answered at once: `not_connected` when the edit
never reached the broker, `no_answer` when it did and Home Assistant may still have it.

Everything here runs on enigma2's main loop - the MQTT client hands its messages there, and
OpenWebif's requests run there - and nothing raises into it.
"""

import json
import re
import secrets
import time
import unicodedata

from .enigma2 import Ticker
from .log import get_logger

LOG = get_logger("menueditor")

EDITOR_CAPABILITY = "menu_editor"

# The integration's two topics, outside the node's tree like the menu's: it owns them.
CATALOGUE_TOPIC_PREFIX = "enigma2mqtt/menu_catalogue/"
RESULT_TOPIC_PREFIX = "enigma2mqtt/menu_edit_result/"
# The edit, in the node's tree. An event: QoS 1, never retained.
EDIT_SUFFIX = "menu_edit"
EDIT_QOS = 1

CATALOGUE_VERSION = 1
# The catalogue's own bound - not the menu's 64 KiB and not a command's 4 KiB. The integration
# keeps it under this by leaving entities out, and says how many in `left_out`.
MAX_CATALOGUE_BYTES = 1048576
# The encoded edit, envelope included.
MAX_EDIT_BYTES = 65536
# An answer is a few hundred bytes; anything much larger is not one.
MAX_RESULT_BYTES = 4096
ANSWER_SECONDS = 10
# Saves waiting for their answers at once, from every open page together. The page sends one at
# a time; this bounds what several tabs, or a script, can hold open.
MAX_PENDING = 4
PATH_LIMIT = 200
DETAIL_LIMIT = 200

# What Home Assistant answers.
SAVED = "saved"
STALE = "stale"
INVALID = "invalid"
BUSY = "busy"
NOT_CLAIMED = "not_claimed"
# What the receiver answers itself, without Home Assistant.
NO_ANSWER = "no_answer"
NOT_CONNECTED = "not_connected"
NOT_ALLOWED = "not_allowed"
UNREADABLE = "unreadable"
TOO_BIG = "too_big"

# Why there is no catalogue to show.
NO_CATALOGUE = "none"
NEWER_CATALOGUE = "newer"

_REVISION = re.compile(r"[0-9a-f]{16}")
_EDIT_ID = re.compile(r"[0-9a-f]{12}")
_RESULT = re.compile(r"[a-z_]{1,32}")
_EDIT_ID_BYTES = 6

# Whether OpenWebif took the page (`webif.mounted`): the creator is offered only then.
_mounted = False


def mark_mounted():
    global _mounted
    _mounted = True


def mounted():
    return _mounted


def forget_mounted():
    """For the tests: the page as never mounted."""
    global _mounted
    _mounted = False


def is_revision(value):
    return isinstance(value, str) and bool(_REVISION.fullmatch(value))


class InvalidCatalogue(ValueError):
    """Not a catalogue of this contract; the sentence says why."""


class NewerCatalogue(InvalidCatalogue):
    """A catalogue of a `v` this plugin does not know: a newer integration, not a broken one."""

    def __init__(self, version):
        InvalidCatalogue.__init__(self, f"a catalogue of version {version!r}, which this "
                                  "plugin does not know")
        self.version = version


def _loads(text, what):
    try:
        return json.loads(text)
    except RecursionError:
        # Hundreds of thousands of brackets fit in a megabyte.
        raise InvalidCatalogue(what + " is nested too deeply") from None
    except (ValueError, OverflowError, TypeError):
        raise InvalidCatalogue(what + " is not JSON") from None


def _text(payload, limit, what):
    """`payload` as text: None for an empty one, InvalidCatalogue when it is not UTF-8."""
    if payload is None:
        return None
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > limit:
            raise InvalidCatalogue(f"{what} is {len(payload)} bytes, over {limit}")
        if not bytes(payload).strip():
            return None
        try:
            return bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            raise InvalidCatalogue(what + " is not UTF-8") from None
    text = str(payload)
    if len(text.encode("utf-8", "surrogatepass")) > limit:
        raise InvalidCatalogue(f"{what} is over {limit} bytes")
    return text if text.strip() else None


def parse_catalogue(payload):
    """The catalogue a payload holds: None for "no catalogue" (an empty payload), else a summary.

    The summary is what the page and the log need - the exact bytes, the revision, how many
    entities and how many were left out - and nothing is read deeper than the envelope: the
    entities and the menu are for the page, which treats every string in them as untrusted.
    Raises InvalidCatalogue for anything else, and NewerCatalogue for a `v` this plugin does
    not know. Never anything else: a parse that fails is a refusal, whatever the payload holds.
    """
    text = _text(payload, MAX_CATALOGUE_BYTES, "the catalogue")
    if text is None:
        return None
    raw = _loads(text, "the catalogue")
    if not isinstance(raw, dict):
        raise InvalidCatalogue("the catalogue is not a JSON object")
    version = raw.get("v")
    if not isinstance(version, int) or isinstance(version, bool):
        raise InvalidCatalogue("v is not a number")
    if version != CATALOGUE_VERSION:
        raise NewerCatalogue(version)
    revision = raw.get("revision")
    if not is_revision(revision):
        raise InvalidCatalogue("revision is not 16 lowercase hexadecimal digits")
    menu = raw.get("menu")
    entities = raw.get("entities")
    if not isinstance(menu, list):
        raise InvalidCatalogue("menu is not a list")
    if not isinstance(entities, list):
        raise InvalidCatalogue("entities is not a list")
    left_out = raw.get("left_out")
    if isinstance(left_out, bool) or not isinstance(left_out, int) or left_out < 0:
        left_out = None
    encoded = payload if isinstance(payload, (bytes, bytearray)) else text.encode("utf-8")
    return {
        "raw": bytes(encoded),
        "revision": revision,
        "entities": len(entities),
        "left_out": left_out,
    }


def _clean(value, limit):
    """Text from Home Assistant as the page may show it: one line, no format characters, cut."""
    pieces = []
    for character in str(value):
        category = unicodedata.category(character)
        if category in ("Cf", "Cs"):
            continue
        pieces.append(" " if category in ("Cc", "Zl", "Zp") else character)
    return "".join(pieces)[:limit]


def parse_answer(payload):
    """`(id, answer)` from a result payload, or None when it is not one.

    The answer is what the page gets: `result`, `revision`, `path` and `detail`, each checked
    for its type and the texts cut. A result word this plugin does not know is passed on - the
    page has a sentence for one - since a newer integration may answer in a word added later.
    """
    if isinstance(payload, (bytes, bytearray)):
        if len(payload) > MAX_RESULT_BYTES:
            return None
        try:
            payload = bytes(payload).decode("utf-8")
        except UnicodeDecodeError:
            return None
    try:
        raw = json.loads(str(payload or ""))
    except (ValueError, RecursionError, OverflowError, TypeError):
        return None
    if not isinstance(raw, dict):
        return None
    ident = raw.get("id")
    result = raw.get("result")
    if not isinstance(ident, str) or not _EDIT_ID.fullmatch(ident):
        return None
    if not isinstance(result, str) or not _RESULT.fullmatch(result):
        return None
    revision = raw.get("revision")
    path = raw.get("path")
    detail = raw.get("detail")
    return ident, {
        "result": result,
        "revision": revision if is_revision(revision) else None,
        "path": _clean(path, PATH_LIMIT) if isinstance(path, str) else None,
        "detail": _clean(detail, DETAIL_LIMIT) if isinstance(detail, str) else None,
    }


def encode_edit(ident, base, menu, ts):
    """`(payload, bytes)` of one `menu_edit` as the bridge publishes it; ValueError says why not."""
    if not is_revision(base):
        raise ValueError(UNREADABLE)
    if not isinstance(menu, list):
        raise ValueError(UNREADABLE)
    payload = {"id": ident, "base": base, "ts": int(ts), "menu": menu}
    try:
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True,
                             separators=(",", ":")).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        # A lone surrogate from a browser's JSON (a UnicodeEncodeError, which is a ValueError),
        # or a structure too deep to write back.
        raise ValueError(UNREADABLE) from None
    if len(encoded) > MAX_EDIT_BYTES:
        raise ValueError(TOO_BIG)
    return payload, encoded


def _acknowledged(info):
    try:
        return bool(info.is_published())
    except Exception:
        return True


class MenuEditor:
    """The catalogue last received, and the saves waiting for Home Assistant's answer.

    Owned by the menu's publisher (`menu.MenuPublisher`), so it lives exactly as long as the
    menu does: only while `menu_allowed` is on, for one session of the bridge.
    """

    def __init__(self, publisher):
        self.publisher = publisher
        self.catalogue = None
        self.received = None
        self.reason = NO_CATALOGUE
        self.clock = time.time
        # edit id -> {"callback", "ticker", "info"}
        self.pending = {}
        self._versions_logged = set()

    @property
    def bridge(self):
        return self.publisher.bridge

    # ---------------------------------------------------------------- topics --

    def _node(self):
        bridge = self.bridge
        return getattr(bridge, "node_id", "") if bridge is not None else ""

    def catalogue_topic(self):
        node = self._node()
        return CATALOGUE_TOPIC_PREFIX + node if node else None

    def result_topic(self):
        node = self._node()
        return RESULT_TOPIC_PREFIX + node if node else None

    def topics(self):
        return [topic for topic in (self.catalogue_topic(), self.result_topic()) if topic]

    def on_message(self, topic, payload, retain=False):
        if topic == self.catalogue_topic():
            self.on_catalogue(payload)
        elif topic == self.result_topic():
            self.on_result(payload, retain)

    # ------------------------------------------------------------- catalogue --

    def on_catalogue(self, payload):
        try:
            catalogue = parse_catalogue(payload)
        except NewerCatalogue as error:
            if error.version not in self._versions_logged:
                self._versions_logged.add(error.version)
                LOG.warning("ignoring the menu catalogue: %s", error)
            self.catalogue = None
            self.received = None
            self.reason = NEWER_CATALOGUE
            return
        except InvalidCatalogue as error:
            LOG.warning("ignoring an invalid menu catalogue, keeping the one before it: %s", error)
            return
        if catalogue is None:
            LOG.info("Home Assistant has no menu catalogue for this receiver")
            self.catalogue = None
            self.received = None
            self.reason = NO_CATALOGUE
            return
        self.catalogue = catalogue
        self.received = self.clock()
        self.reason = None
        LOG.debug("menu catalogue %s: %d entities, %s left out, %d bytes", catalogue["revision"],
                  catalogue["entities"], catalogue["left_out"], len(catalogue["raw"]))

    def status(self):
        """What the Menu page says about the catalogue."""
        catalogue = self.catalogue
        if catalogue is None:
            return {"revision": None, "received": None, "left_out": None, "entities": None,
                    "reason": self.reason}
        return {"revision": catalogue["revision"], "received": self.received,
                "left_out": catalogue["left_out"], "entities": catalogue["entities"],
                "reason": None}

    # ------------------------------------------------------------------ edits --

    def offered(self):
        """Whether a save can be taken now: the page is mounted and the menu is offered."""
        return mounted() and self.publisher.claimed()

    def submit(self, base, menu, callback):
        """Publish one edit; `callback(answer)` is called once with Home Assistant's answer.

        Returns None when the edit was published and the callback will follow, or the result
        word that answers the save at once - and then the callback is never called.
        """
        bridge = self.bridge
        if not self.offered():
            return NOT_ALLOWED
        if bridge is None or not bridge.connected:
            return NOT_CONNECTED
        if len(self.pending) >= MAX_PENDING:
            return BUSY
        ident = secrets.token_hex(_EDIT_ID_BYTES)
        while ident in self.pending:
            ident = secrets.token_hex(_EDIT_ID_BYTES)
        try:
            payload, _encoded = encode_edit(ident, base, menu, self.clock())
        except ValueError as refusal:
            return str(refusal)
        info = bridge.publish_event(EDIT_SUFFIX, payload, qos=EDIT_QOS)
        if info is None or getattr(info, "rc", 0):
            if info is not None:
                bridge.withdraw_event(info)
            LOG.warning("a menu edit was not sent: no open connection to the broker")
            return NOT_CONNECTED
        ticker = Ticker(lambda: self._expire(ident), "menu edit")
        self.pending[ident] = {"callback": callback, "ticker": ticker, "info": info}
        if not ticker.start(ANSWER_SECONDS * 1000, True):
            LOG.warning("no timer for the menu edit %s; it waits for its answer", ident)
        LOG.info("menu edit %s sent (from revision %s, %d entries at the top)", ident, base,
                 len(menu))
        return None

    def on_result(self, payload, retain=False):
        if retain:
            # An answer is an event. A retained one is old, whoever left it there.
            LOG.debug("ignoring a retained menu edit answer")
            return
        parsed = parse_answer(payload)
        if parsed is None:
            LOG.debug("ignoring an unreadable menu edit answer")
            return
        ident, answer = parsed
        if ident not in self.pending:
            LOG.debug("ignoring an answer for a menu edit this receiver is not waiting for")
            return
        LOG.info("menu edit %s: Home Assistant answered %s", ident, answer["result"])
        self._finish(ident, answer)

    def _expire(self, ident):
        if ident in self.pending:
            LOG.warning("menu edit %s: Home Assistant did not answer within %d s", ident,
                        ANSWER_SECONDS)
            self._finish(ident, {"result": NO_ANSWER})

    def _finish(self, ident, answer):
        entry = self.pending.pop(ident, None)
        if entry is None:
            return
        try:
            entry["ticker"].stop()
        except Exception:
            LOG.debug("the menu edit's timer could not be stopped")
        try:
            entry["callback"](answer)
        except Exception:
            LOG.exception("the answer to a menu edit could not be handed to the page")

    def connection_changed(self, connected):
        """The broker connection went: no save waits for an answer that cannot come now.

        An edit the broker had not acknowledged is taken out of the client's queue, so it is
        never sent minutes later, and its save is answered `not_connected` - nothing reached
        Home Assistant. One the broker had is answered `no_answer`: Home Assistant may have it,
        and a reload of the page shows whether it was stored.
        """
        if connected:
            return
        for ident, entry in list(self.pending.items()):
            info = entry.get("info")
            reached = info is not None and _acknowledged(info)
            if not reached and info is not None and self.bridge is not None:
                if self.bridge.withdraw_event(info):
                    LOG.info("dropped a menu edit the broker had not acknowledged")
            self._finish(ident, {"result": NO_ANSWER if reached else NOT_CONNECTED})

    def stop(self):
        """The session ends: every waiting save is answered now."""
        for ident in list(self.pending):
            self._finish(ident, {"result": NO_ANSWER})
