"""The receiver's active bouquet context and its guarded selector."""

from .channels import bouquet_roots
from .enigma2 import Ticker, identity, reference_string, service_reference
from .log import get_logger
from .publisher import Publisher
from .service import navigation

LOG = get_logger("bouquet")
POLL_MILLISECONDS = 2000

# How many turns of the ticker the service list gets to appear before the
# publisher stops asking. Five of them is ten seconds, which is longer than any
# image measured here takes to build its InfoBar, and short enough that a box
# that will never have one is not polled for the rest of its uptime.
BIND_ATTEMPTS = 5


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
    """`bouquet` — the active channel-list root used by channel up/down.

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
        self._gave_up = False

    def claimed(self):
        return self._bound

    def start(self):
        if self.bridge.publisher("channels") is None:
            return False
        # InfoBar is created after session-start on some images.  The ticker is
        # the bounded retry: it binds by lookup on every turn without creating
        # another timer or logging on every miss.
        self.refresh()
        if not self._gave_up:
            self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()

    def _bind(self):
        """The root was readable: claim the capability and say so, once."""
        self._attempts = 0
        if self._gave_up:
            self._gave_up = False
            self._ticker.start(POLL_MILLISECONDS)
        if self._bound:
            return
        self._bound = True
        LOG.info("the receiver's service list is readable; bouquet_context is available")
        if self.bridge is not None:
            self.bridge.announce_capabilities()

    def _not_yet(self):
        """The root was not readable: keep waiting, but not forever."""
        if self._bound or self._gave_up:
            return
        self._attempts += 1
        if self._attempts < BIND_ATTEMPTS:
            return
        self._gave_up = True
        self._ticker.stop()
        LOG.warning(
            "this image gave no readable service list in %d attempts; "
            "bouquet_context is not claimed",
            BIND_ATTEMPTS,
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

    def _payload(self):
        servicelist = _servicelist()
        get_root = getattr(servicelist, "getRoot", None)
        if get_root is None:
            return None
        try:
            bouquet = self._known(get_root())
        except Exception:
            return None
        if bouquet is None:
            return None
        return {"name": bouquet.get("name"), "sref": bouquet.get("sref")}

    def refresh(self, force=False):
        payload = self._payload()
        if payload is None:
            self._not_yet()
            return None
        if force:
            self.bridge.publish_json(self.bridge.topic("bouquet"), payload)
        else:
            self.publish("bouquet", payload)
        self._bind()
        return payload

    def snapshot(self):
        payload = self._payload()
        return {} if payload is None else {"bouquet": payload}

    def select(self, sref):
        """Activate one published bouquet; tune first channel only when needed."""
        bouquet = self._known(sref)
        if bouquet is None:
            return "the bouquet reference is not in the configured channel list"
        services = bouquet.get("channels") or []
        if not services:
            return "the selected bouquet has no playable channels"

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
            # service-list root merely because that happened to be on screen —
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
