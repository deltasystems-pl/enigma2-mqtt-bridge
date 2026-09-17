"""The receiver's active bouquet context and its guarded selector."""

from .channels import bouquet_roots
from .enigma2 import Ticker, identity, reference_string, service_reference
from .log import get_logger
from .publisher import Publisher
from .service import navigation

LOG = get_logger("bouquet")
POLL_MILLISECONDS = 2000


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
    """`bouquet` — the active channel-list root used by channel up/down."""

    name = "bouquet_context"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._ticker = Ticker(self.refresh, "bouquet context")

    def start(self):
        if self.bridge.publisher("channels") is None:
            return False
        # InfoBar is created after session-start on some images.  The ticker is
        # the bounded retry: it binds by lookup on every turn without creating
        # another timer or logging on every miss.
        self.refresh()
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def stop(self):
        self._ticker.stop()

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
        if payload is not None:
            if force:
                self.bridge.publish_json(self.bridge.topic("bouquet"), payload)
            else:
                self.publish("bouquet", payload)
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
            # service-list root merely because that happened to be on screen.
            root = service_reference(bouquet_roots()[0])
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
