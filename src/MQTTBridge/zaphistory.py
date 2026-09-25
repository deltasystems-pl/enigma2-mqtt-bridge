"""The receiver's own zap history: what it holds, a zap back into it, and clearing it.

The receiver keeps a list of the channels zapped to - the list its "History Zap"
screen shows when KEY_NEXT or KEY_PREVIOUS is pressed. It lives on the channel
selection the info bar holds, `InfoBar.instance.servicelist`, as `history`: a
Python list of entries, oldest first, each entry itself a list of
`eServiceReference` - the path the channel list was on at the time (root,
bouquet) with the service last. `history_pos` is the entry the receiver
considers current. The image keeps at most `HISTORYSIZE` entries (20 on the
images measured) and never two for the same service.

This module publishes **that list and nothing of its own** (ADR-0014). There is
no second history kept here: a user-interface restart empties the receiver's,
and then this topic says it is empty, because that is what the receiver will
show.

**Read fresh every time, never held.** The image's panic branch of the 0 key
does not empty the list, it *replaces* it with a new list object. A reference to
the old list would go on showing the channels that were cleared, for ever. So
every look starts again at `InfoBar.instance.servicelist.history`.

**Polled, not hooked.** Three different paths change the list - the channel
list's zap (after `playService`, in its timeshift callback), the panic branch
(which replaces it) and the history screen's choice (which reorders it) - and
none of them announces anything. The same two-second look `bouquet_context`
already takes costs at most twenty `toString()` calls, and names are resolved
and a payload built only when that cheap key changed.

**The entries are the ones the receiver's screen shows.** `historyZap` skips an
entry whose service `eServiceCenter` has no information for - a channel removed
from every list since it was watched - and so does this.

**Clearing is the 0 key's own code, not a second one.** `cmd/history_clear` runs
`InfoBar.instance.keyNumberGlobal(0)`, the handler 0 reaches when nothing is
open on screen. With the image's `panicbutton` setting on that empties the
history and zaps to channel 1 - the first channel of the first bouquet - and
adds that one channel back. It is not injected as a key press: a key goes to
whatever screen has focus. Every case in which 0 would *not* clear is refused
first, with a sentence and a stable reason code, so a consumer can say it in
the household's language.
"""

from .enigma2 import (
    Ticker,
    current_service_reference,
    enigma_attribute,
    identity,
    missing,
    navigation,
    reference_string,
    same_service,
    service_name,
)
from .log import get_logger
from .publisher import Publisher, Refusal

LOG = get_logger("zaphistory")

TOPIC = "zap_history"
CLEAR_CAPABILITY = "history_clear"

POLL_MILLISECONDS = 2000
SLOW_POLL_MILLISECONDS = 60000
# A minute of fast looks before the polling slows down, as `bouquet_context`.
BIND_ATTEMPTS = 30

# The refusals of `cmd/history_clear`, in the order they are asked, each with the
# reason code `last_error.reason` carries. The sentence is the contract's human
# text; the code is what a consumer translates.
STANDBY = ("the receiver is in standby, where 0 does not clear the history", "standby")
PANIC_OFF = (
    "the receiver's panic-button setting is off, so 0 goes back one channel instead of "
    "clearing the history",
    "panic_off",
)
TOO_SHORT = ("the receiver's zap history holds at most one channel, and 0 does nothing then",
             "too_short")
TIMESHIFT = ("timeshift is active; 0 would ask on screen whether to leave it", "timeshift")
ZAP_BLOCKED = ("the receiver is holding zaps for a moment after timeshift; try again",
               "zap_blocked")
PIP = ("picture-in-picture is showing and 0 is set to act on it", "pip")
PLAYBACK = ("a recording is being played back", "playback")
SCREEN_OPEN = ("a screen is open on the receiver, and 0 does not reach the zap history there",
               "screen_open")
NOT_CLEARED = ("the receiver did not clear its zap history", "not_cleared")

NOT_AVAILABLE = "the zap history is not available on this image"
CLEAR_NOT_AVAILABLE = "clearing the zap history is not available on this image"
UNREADABLE = "the receiver's zap history cannot be read"
GONE = "that channel is no longer in the receiver's zap history"
# The same sentence `service.zap` gives, for the same missing piece.
NO_PLAYER = "this image's navigation has no playService"

# What the channel selection has to offer for the topic and `cmd/zap_history`,
# and what the info bar has to offer on top for `cmd/history_clear`.
HISTORY_METHODS = ("historyMenuClosed", "setHistoryPath")
CLEAR_METHODS = ("keyNumberGlobal", "recallPrevService", "checkTimeshiftRunning")


def refusal(pair):
    sentence, reason = pair
    return Refusal(sentence, reason)


def infobar_instance():
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return None
    return getattr(InfoBar, "instance", None)


def _usage(name):
    """`config.usage.<name>`, or None where the image has no such element."""
    try:
        from Components.config import config
    except Exception:
        return None
    usage = getattr(config, "usage", None)
    return getattr(usage, name, None) if usage is not None else None


def panic_button():
    """`config.usage.panicbutton` as a boolean, or None where the image has none."""
    element = _usage("panicbutton")
    if element is None:
        return None
    try:
        return bool(element.value)
    except Exception:
        return None


def history_size():
    """`Screens.ChannelSelection.HISTORYSIZE` as the image has it, or None."""
    try:
        from Screens.ChannelSelection import HISTORYSIZE
    except Exception:
        return None
    try:
        return int(HISTORYSIZE)
    except (TypeError, ValueError):
        return None


def playing_back(session):
    """Whether the dialog on screen is the image's movie player.

    The info bar is not the current dialog while a recording plays, so neither
    the 0 key nor a history zap means what it means over live television.
    """
    try:
        from Screens.InfoBar import MoviePlayer
    except Exception:
        return False
    dialog = getattr(session, "current_dialog", None)
    try:
        return dialog is not None and isinstance(dialog, MoviePlayer)
    except Exception:
        return False


def _service_center():
    factory = enigma_attribute("eServiceCenter")
    if factory is None:
        return None
    try:
        return factory.getInstance()
    except Exception:
        return None


def _shown(center, reference):
    """Whether the receiver's own History Zap screen would show this entry."""
    if center is None:
        return True
    try:
        return bool(center.info(reference))
    except Exception:
        return False


class ZapHistoryPublisher(Publisher):
    """`zap_history` - the receiver's list, newest first - and its two commands.

    Claimed by reading, like `bouquet_context`: the info bar is built after the
    session starts on some images, and a capability named before the first
    successful read would promise an entity nothing updates.
    """

    name = TOPIC

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._ticker = Ticker(self.refresh, "zap history")
        self._bound = False
        self._clear = False
        self._attempts = 0
        self._slow = False
        self._key = None
        self.published = None

    # ----------------------------------------------------------- capabilities --

    def claimed(self):
        return self._bound

    def extra_capabilities(self):
        return [CLEAR_CAPABILITY] if self._bound and self._clear else []

    def clear_available(self):
        return self._bound and self._clear

    def start(self):
        self.refresh()
        if not self._slow:
            self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()

    # ---------------------------------------------------------------- reading --

    def _read(self):
        """`(infobar, servicelist, history, position)`, or None when it cannot be read."""
        infobar = infobar_instance()
        servicelist = getattr(infobar, "servicelist", None) if infobar is not None else None
        if servicelist is None:
            return None
        try:
            history = servicelist.history
            position = int(servicelist.history_pos)
        except Exception:
            return None
        if not isinstance(history, list):
            return None
        if any(not callable(getattr(servicelist, name, None)) for name in HISTORY_METHODS):
            missing("the channel selection's history methods")
            return None
        return infobar, servicelist, history, position

    def _clear_possible(self, infobar):
        """Everything the 0 key's own path needs, or False with the reason logged once."""
        for name in CLEAR_METHODS:
            if not callable(getattr(infobar, name, None)):
                missing("InfoBar." + name)
                return False
        if _usage("panicbutton") is None:
            missing("config.usage.panicbutton")
            return False
        return True

    @staticmethod
    def _key_of(history, position, panic):
        parts = []
        for entry in history:
            try:
                service = reference_string(entry[-1])
                bouquet = reference_string(entry[-2]) if len(entry) > 1 else ""
            except Exception:
                service, bouquet = "", ""
            parts.append((service, bouquet))
        return (tuple(parts), position, panic)

    def _bouquet_name(self, bouquet):
        channels = self.bridge.publisher("channels") if self.bridge is not None else None
        if not bouquet or channels is None:
            return None
        for published in getattr(channels, "bouquets", None) or ():
            if reference_string(published.get("sref")) == bouquet:
                return published.get("name")
        return None

    def build(self, history, position, panic):
        """The payload: the entries the receiver's screen shows, newest first."""
        center = _service_center()
        entries = []
        current = None
        for index in range(len(history) - 1, -1, -1):
            try:
                entry = history[index]
                reference = entry[-1]
            except Exception:
                continue
            if not _shown(center, reference):
                continue
            sref = reference_string(reference)
            if not sref:
                continue
            bouquet = None
            try:
                if len(entry) > 1:
                    bouquet = reference_string(entry[-2]) or None
            except Exception:
                bouquet = None
            if index == position:
                current = len(entries)
            entries.append({
                "sref": sref,
                "name": self._name(center, reference, sref),
                "bouquet": bouquet,
                "bouquet_name": self._bouquet_name(bouquet),
            })
        return {"entries": entries, "current": current, "limit": history_size(),
                "panic_button": panic}

    @staticmethod
    def _name(center, reference, sref):
        """The name `service` gives the same channel, so one channel has one spelling."""
        name = service_name(sref)
        if not name and center is not None:
            try:
                info = center.info(reference)
                name = str(info.getName(reference) or "") if info else ""
            except Exception:
                name = ""
        return name or None

    def refresh(self, force=False):
        read = self._read()
        if read is None:
            self._not_yet()
            return None
        infobar, _servicelist, history, position = read
        panic = panic_button()
        clear = self._clear_possible(infobar)
        key = self._key_of(history, position, panic)
        if force or key != self._key or self.published is None:
            payload = self.build(history, position, panic)
            self.publish(TOPIC, payload)
            self.published = payload
            self._key = key
        self._bind(clear)
        return self.published

    def snapshot(self):
        read = self._read()
        if read is None:
            return {}
        _infobar, _servicelist, history, position = read
        panic = panic_button()
        payload = self.build(history, position, panic)
        self.published = payload
        self._key = self._key_of(history, position, panic)
        return {TOPIC: payload}

    def _bind(self, clear):
        self._attempts = 0
        if self._slow:
            self._slow = False
            self._ticker.start(POLL_MILLISECONDS)
        if self._bound and clear == self._clear:
            return
        first = not self._bound
        self._bound = True
        self._clear = clear
        if first:
            LOG.info("the receiver's zap history is readable; zap_history is available")
        if self.bridge is not None:
            self.bridge.announce_capabilities()

    def _not_yet(self):
        if self._bound or self._slow:
            return
        self._attempts += 1
        if self._attempts < BIND_ATTEMPTS:
            return
        self._slow = True
        self._ticker.start(SLOW_POLL_MILLISECONDS)
        LOG.warning(
            "no readable zap history after %d seconds; zap_history is not claimed and the "
            "list is now checked once a minute",
            BIND_ATTEMPTS * POLL_MILLISECONDS // 1000,
        )

    # ---------------------------------------------------------------- commands --

    def find(self, sref):
        """`(index, reference object)` of the entry for `sref`, by identity, or None.

        Only among the entries the receiver's own screen would show.
        """
        read = self._read()
        wanted = identity(sref)
        if read is None or not wanted:
            return None
        center = _service_center()
        for index, entry in enumerate(read[2]):
            try:
                reference = entry[-1]
            except Exception:
                continue
            if identity(reference_string(reference)) == wanted and _shown(center, reference):
                return index, reference
        return None

    def zap_to(self, sref, on_zap=None):
        """Zap to one entry the way the receiver's History Zap screen does. None, or why not.

        By identity, among the entries the screen shows; the call is the
        screen's own, `historyMenuClosed`, with the entry's own reference
        object, which moves the entry to the front and plays it. One case it
        leaves alone: the chosen entry is already the current one but something
        else is playing - a zap the history does not know about - and then
        `setHistoryPath()` plays it.
        """
        from .service import infobar_on_screen

        read = self._read()
        if read is None:
            return UNREADABLE
        _infobar, servicelist, _history, position = read
        found = self.find(sref)
        if found is None:
            return GONE
        index, reference = found
        playing = current_service_reference(self.session)
        if not infobar_on_screen(self.session):
            # A screen is open over the info bar; the history's own calls
            # would move the channel list under it. Played directly, and the
            # list is left as it is.
            # Asked the way `service.zap` asks it: a session without a player
            # says so on `last_error` rather than raising out of the command.
            nav = navigation(self.session)
            player = getattr(nav, "playService", None) if nav is not None else None
            if player is None:
                return NO_PLAYER
            player(reference)
        elif index == position and not same_service(playing, reference_string(reference)):
            servicelist.setHistoryPath()
        else:
            servicelist.historyMenuClosed(reference)
        if on_zap is not None:
            on_zap(reference_string(reference))
        self.refresh()
        return None

    def clear(self):
        """`cmd/history_clear`: the 0 key's own handler, after every case it would not clear."""
        from .power import in_standby
        from .service import infobar_on_screen, timeshift_active

        if not self.clear_available():
            return CLEAR_NOT_AVAILABLE
        read = self._read()
        if read is None:
            return UNREADABLE
        infobar, servicelist, history, _position = read
        if in_standby():
            return refusal(STANDBY)
        if panic_button() is not True:
            return refusal(PANIC_OFF)
        if len(history) < 2:
            # The key's own condition, on the list as it is - not on the entries
            # a screen would show.
            return refusal(TOO_SHORT)
        if timeshift_active(infobar):
            return refusal(TIMESHIFT)
        if self._zap_blocked(infobar):
            return refusal(ZAP_BLOCKED)
        if self._pip_takes_zero(infobar):
            return refusal(PIP)
        if playing_back(self.session):
            return refusal(PLAYBACK)
        if not infobar_on_screen(self.session):
            # The key reaches `keyNumberGlobal` only on the info bar; with the
            # channel list, the EPG or a menu open, 0 means something else.
            return refusal(SCREEN_OPEN)
        infobar.keyNumberGlobal(0)
        read = self._read()
        if read is not None and len(read[2]) > 1:
            self.refresh()
            return refusal(NOT_CLEARED)
        self.refresh()
        return None

    @staticmethod
    def _zap_blocked(infobar):
        timer = getattr(infobar, "pts_blockZap_timer", None)
        if timer is None:
            return False
        try:
            return bool(timer.isActive())
        except Exception:
            return True

    @staticmethod
    def _pip_takes_zero(infobar):
        """The key's own test: PiP shown and 0 not left to its standard meaning."""
        handles = getattr(infobar, "pipHandles0Action", None)
        if not callable(handles):
            return False
        try:
            return bool(handles())
        except Exception:
            return True
