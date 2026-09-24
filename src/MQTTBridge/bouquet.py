"""The receiver's active bouquet context and its guarded selector."""

from .channels import bouquet_roots
from .enigma2 import Ticker, identity, reference_string, service_reference
from .log import get_logger
from .publisher import Publisher
from .service import navigation

LOG = get_logger("bouquet")
POLL_MILLISECONDS = 2000

# Once a minute, for a box that has not produced a service list yet. A cold
# boot can spend a while before enigma2 builds its InfoBar, and an image that
# never does should not be asked every two seconds for the rest of its uptime -
# but it must still be asked, because „never so far" is not „never".
SLOW_POLL_MILLISECONDS = 60000

# How many fast turns that gets before the polling slows down: a minute.
BIND_ATTEMPTS = 30

# What one look at the receiver's own service list found.
UNREADABLE = "unreadable"
# Readable, but the root is not one of the configured bouquets: the box is on
# the radio list, in the movie list, or in a bouquet `bouquets_for_select`
# excludes. That is ordinary operation, not a missing hook.
FOREIGN_ROOT = "foreign_root"
READABLE = "readable"

# The `bouquet` payload for a root that is not one of ours. The fields stay,
# because a consumer templating `value_json.name` should get a null rather than
# an error, and because the topic is how „not in a configured bouquet" is said.
NO_BOUQUET = {"name": None, "sref": None}

TIMESHIFT = "timeshift is active; the receiver would ask on screen whether to leave it"


def _servicelist():
    try:
        from Screens.InfoBar import InfoBar
    except Exception:
        return None
    infobar = getattr(InfoBar, "instance", None)
    return getattr(infobar, "servicelist", None) if infobar is not None else None


def _current_service(session):
    nav = navigation(session)
    getter = getattr(nav, "getCurrentlyPlayingServiceReference", None)
    if getter is None:
        return None
    try:
        return getter()
    except Exception:
        return None


def _tv_mode(servicelist):
    try:
        from Screens.ChannelSelection import ChannelSelection

        expected = getattr(ChannelSelection, "MODE_TV", 0)
    except Exception:
        expected = 0
    return getattr(servicelist, "mode", expected) == expected


class BouquetPublisher(Publisher):
    """`bouquet` - the active channel-list root used by channel up/down.

    The capability is claimed by reading, not by registering. A box whose
    InfoBar this plugin never gets to see produces no `bouquet` topic and no
    working `cmd/bouquet`, so naming `bouquet_context` in `info.capabilities`
    before the first successful read would promise a consumer an entity that
    nothing would ever update.
    """

    name = "bouquet_context"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._ticker = Ticker(self.refresh, "bouquet context")
        self._bound = False
        self._attempts = 0
        self._slow = False

    def claimed(self):
        return self._bound

    def start(self):
        if self.bridge.publisher("channels") is None:
            return False
        # InfoBar is created after session-start on some images.  The ticker is
        # the bounded retry: it binds by lookup on every turn without creating
        # another timer or logging on every miss.
        self.refresh()
        if not self._slow:
            self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()

    def _bind(self):
        """The list was readable: claim the capability and say so, once."""
        self._attempts = 0
        if self._slow:
            self._slow = False
            self._ticker.start(POLL_MILLISECONDS)
        if self._bound:
            return
        self._bound = True
        LOG.info("the receiver's service list is readable; bouquet_context is available")
        if self.bridge is not None:
            self.bridge.announce_capabilities()

    def _not_yet(self):
        """No usable service list this turn: keep watching, but stop hurrying.

        Only a missing hook gets here. A list that reads perfectly well and
        happens to be showing something this plugin does not publish is not a
        failure to bind, and counting it as one used to retire the whole
        feature a few seconds after somebody opened the radio list.
        """
        if self._bound or self._slow:
            return
        self._attempts += 1
        if self._attempts < BIND_ATTEMPTS:
            return
        self._slow = True
        self._ticker.start(SLOW_POLL_MILLISECONDS)
        LOG.warning(
            "no readable service list after %d seconds; bouquet_context is not claimed "
            "and the list is now checked once a minute",
            BIND_ATTEMPTS * POLL_MILLISECONDS // 1000,
        )

    def _root(self):
        """The root the channel list was read from, or the one it starts with."""
        channels = self.bridge.publisher("channels") if self.bridge is not None else None
        return getattr(channels, "root", None) or bouquet_roots()[0]

    def _known(self, sref):
        channels = self.bridge.publisher("channels")
        if channels is None:
            return None
        wanted = reference_string(sref)
        for bouquet in channels.bouquets:
            if reference_string(bouquet.get("sref")) == wanted:
                return bouquet
        return None

    def _read(self):
        """`(state, payload)` for one look at the receiver's own service list.

        Three different things used to come back as None from here, and the
        caller could not tell them apart: an image with no service list, a
        lookup that raised, and a perfectly readable list showing a root this
        plugin does not publish. Only the first two mean the hooks are missing.
        """
        servicelist = _servicelist()
        get_root = getattr(servicelist, "getRoot", None)
        if get_root is None:
            return UNREADABLE, None
        try:
            root = get_root()
        except Exception:
            return UNREADABLE, None
        if root is None:
            return UNREADABLE, None
        try:
            bouquet = self._known(root)
        except Exception:
            return UNREADABLE, None
        if bouquet is None:
            return FOREIGN_ROOT, dict(NO_BOUQUET)
        return READABLE, {"name": bouquet.get("name"), "sref": bouquet.get("sref")}

    def _payload(self):
        """The active configured bouquet, or None when it is not one of ours."""
        state, payload = self._read()
        return payload if state == READABLE else None

    def refresh(self, force=False):
        state, payload = self._read()
        if state == UNREADABLE:
            self._not_yet()
            return None
        if force:
            self.bridge.publish_json(self.bridge.topic("bouquet"), payload)
        else:
            self.publish("bouquet", payload)
        self._bind()
        return payload if state == READABLE else None

    def snapshot(self):
        state, payload = self._read()
        return {} if state == UNREADABLE else {"bouquet": payload}

    def select(self, sref):
        """Activate one published bouquet; tune first channel only when needed."""
        bouquet = self._known(sref)
        if bouquet is None:
            return "the bouquet reference is not in the configured channel list"
        services = bouquet.get("channels") or []
        if not services:
            return "the selected bouquet has no playable channels"

        # The channel list's `zap()` asks `checkTimeshiftRunning` first, which
        # during timeshift opens a question on the television with no timeout -
        # and the synchronous check below would then see nothing tuned and
        # "restore" with `playService` while that question is still on screen.
        # Refused before anything is touched.
        from .service import infobar_instance, timeshift_active

        if timeshift_active(infobar_instance()):
            return TIMESHIFT

        servicelist = _servicelist()
        required = (
            "clearPath",
            "enterPath",
            "getRoot",
            "setCurrentSelection",
            "getCurrentSelection",
        )
        if servicelist is None or any(
            not callable(getattr(servicelist, name, None)) for name in required
        ):
            return "this image has no usable active service list"
        if not _tv_mode(servicelist):
            return "the active service list is not in television mode"

        old_path = list(
            getattr(servicelist, "servicePath", None)
            or getattr(servicelist, "path", None)
            or ()
        )
        old_root = servicelist.getRoot()
        if not old_path and old_root is not None:
            old_path = [old_root]
        old_selection = servicelist.getCurrentSelection()
        current = _current_service(self.session)
        chosen = next(
            (
                channel
                for channel in services
                if current is not None
                and identity(channel.get("sref")) == identity(reference_string(current))
            ),
            services[0],
        )
        preserve = current is not None and identity(chosen.get("sref")) == identity(
            reference_string(current)
        )
        tuned_changed = False

        try:
            servicelist.clearPath()
            # `channels` contains TV bouquets only.  Do not inherit a radio
            # service-list root merely because that happened to be on screen -
            # and enter the bouquet under the root it was actually read from,
            # which on a box with „multiple bouquets" off is the favourites
            # list rather than `bouquets.tv`.
            root = service_reference(self._root())
            bouquet_ref = service_reference(bouquet["sref"])
            selected_ref = service_reference(chosen["sref"])
            if root is None or bouquet_ref is None or selected_ref is None:
                raise RuntimeError("invalid service-list reference")
            if reference_string(root) != reference_string(bouquet_ref):
                servicelist.enterPath(root)
            servicelist.enterPath(bouquet_ref)
            if reference_string(servicelist.getRoot()) != reference_string(bouquet_ref):
                raise RuntimeError("the service list did not enter the requested bouquet")
            servicelist.setCurrentSelection(selected_ref)
            if identity(reference_string(servicelist.getCurrentSelection())) != identity(
                chosen["sref"]
            ):
                raise RuntimeError("the service list did not select a playable channel")
            if not preserve:
                zapper = getattr(servicelist, "zap", None)
                if not callable(zapper):
                    raise RuntimeError("the service list cannot tune the first channel")
                zapper()
                tuned_changed = True
                tuned = _current_service(self.session)
                if tuned is None or identity(reference_string(tuned)) != identity(
                    chosen["sref"]
                ):
                    raise RuntimeError("the receiver did not tune the first channel")
            if self._payload() is None:
                raise RuntimeError("the active bouquet could not be read back")
            save_root = getattr(servicelist, "saveRoot", None)
            if callable(save_root):
                save_root()
            self.refresh(force=True)
        except Exception as error:
            LOG.warning("could not activate bouquet: %s", error)
            try:
                servicelist.clearPath()
                for previous in old_path:
                    servicelist.enterPath(previous)
                now = _current_service(self.session)
                changed = tuned_changed or identity(reference_string(now)) != identity(
                    reference_string(current)
                )
                if changed:
                    nav = navigation(self.session)
                    if current is not None and callable(getattr(nav, "playService", None)):
                        nav.playService(current)
                    elif current is None and callable(getattr(nav, "stopService", None)):
                        nav.stopService()
                    restored = _current_service(self.session)
                    if identity(reference_string(restored)) != identity(
                        reference_string(current)
                    ):
                        raise RuntimeError("the previous service was not restored")
                if old_selection is not None:
                    servicelist.setCurrentSelection(old_selection)
                save_root = getattr(servicelist, "saveRoot", None)
                if callable(save_root):
                    save_root()
            except Exception:
                LOG.exception("could not restore the previous bouquet context")
            return str(error)

        return None
