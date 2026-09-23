"""Asking the image's EPG importer for an import, and following every import it runs.

The importer is the image's, not this plugin's. EPG-Importer is loaded by
enigma2's own plugin loader under `Plugins.Extensions.EPGImport.plugin`, and that
module holds a singleton importer and the scheduler's state. 🔴 This module never
imports it. A second `import` under any other name would build a second module
with a second importer and a second scheduler, and the two would not know about
each other. So the importer is looked up in `sys.modules`, where the loader left
it, and when it is not there the capability is simply not claimed.

**What an import costs, and what this plugin cannot change.** Downloads and
parsing run off the main loop, but the importer ends by saving the EPG cache on
the main loop: measured, two to three seconds with the picture's menus frozen.
That is the image's code. What this module promises is that it adds nothing
blocking of its own: it starts the import the way the importer's own „Manual"
button does, and then only asks `isImportRunning()` on a timer.

**Completion is observed, never hooked.** `startImport()` overwrites the
importer's completion callback on every run, the scheduler's runs included, so
replacing it would be undone by the next scheduled import — and patching the
module's completion function would change the image's behaviour for everybody.
Instead a timer asks `isImportRunning()` every two seconds while an import runs,
the same test at the same period the importer's own screen uses. The importer
clears its running flag, saves, and records `lastImportResult` in one main-loop
callback, so by the next tick the result is already there.

**The importer has no failure signal.** Its completion is called with a count of
zero when every download failed, and its per-source errors go to a stdout that
is `/dev/null` on the images measured. So the topic can say three things went
wrong — the import did not start, it finished with no events, or it did not
finish within the watchdog — and never which source failed.

**The topic follows every import**, not only the ones asked for here: an idle
poll once a minute notices an import the image's schedule or the importer's own
screen started, so that „already running" is never an unexplained refusal.
"""

import sys
import time

from . import recording
from .enigma2 import Ticker, enigma_attribute
from .log import get_logger
from .origin import MQTT, granted
from .publisher import Publisher

LOG = get_logger("epgimport")

# The name enigma2's plugin loader gives the importer's module:
# `'.'.join(['Plugins', category, name, 'plugin'])`.
IMPORTER_MODULE = "Plugins.Extensions.EPGImport.plugin"

IDLE = "idle"
RUNNING = "running"
DONE = "done"
FAILED = "failed"

# The importer's own screen polls at this period; so does this.
RUNNING_POLL_MILLISECONDS = 2000
# While nothing runs, only often enough to notice an import somebody else began.
IDLE_POLL_MILLISECONDS = 60000
# A normal run takes 80 to 100 seconds on the receiver this was measured on.
WATCHDOG_SECONDS = 30 * 60
# The image's scheduler starts an import without asking whether one runs, so an
# import started here close to its time would be restarted underneath itself.
SCHEDULE_WINDOW_SECONDS = 10 * 60
DAY_SECONDS = 24 * 60 * 60

PERMISSION = "importing the EPG is switched off in the plugin's settings"
NOT_RESOLVED = "this receiver's image has no EPG-Importer this plugin can start"
ALREADY_RUNNING = "an EPG import is already running"
NO_SOURCES = "EPG-Importer has no sources selected on the receiver"
NO_EVENTS = (
    "EPG-Importer finished without importing any events; its sources may be unreachable"
)
NOT_FINISHED = "EPG-Importer has not finished after 30 minutes"
DID_NOT_RUN = "EPG-Importer did not run the import"
UNKNOWN_RUNNING = "EPG-Importer will not say whether an import is running; refusing to guess"
UNKNOWN_SCHEDULE = "EPG-Importer will not say when its own import is due; refusing to guess"

_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
           "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def importer_module():
    """The importer's module as enigma2 loaded it, or None. Never imported here."""
    return sys.modules.get(IMPORTER_MODULE)


def running():
    """True only when the image's importer says an import is running.

    For the power commands' guard. A broken or absent importer answers False,
    because refusing every reboot for a plugin that is not there would be a
    worse failure than the one the guard prevents.
    """
    module = importer_module()
    importer = getattr(module, "epgimport", None) if module is not None else None
    checker = getattr(importer, "isImportRunning", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except Exception:
        LOG.debug("isImportRunning() raised while guarding a power command")
        return False


def _image_setting(section, name):
    """One of the image's settings (`config.<section>.<name>`), or None."""
    try:
        from Components.config import config
    except Exception:  # pragma: no cover - only on a broken image
        return None
    owner = config
    for part in section.split("."):
        owner = getattr(owner, part, None)
        if owner is None:
            return None
    element = getattr(owner, name, None)
    if element is None:
        return None
    try:
        return element.value
    except Exception:
        return None


def parse_last_import(text):
    """`"<asctime>, <count>"` as `(epoch seconds, count)`, or `(None, None)`.

    Parsed by hand rather than with `strptime`: `asctime` always writes English
    names, and `strptime` reads them in whatever locale the image set.
    """
    try:
        stamp, count = str(text).rsplit(",", 1)
        fields = stamp.split()
        _weekday, month, day, clock, year = fields
        hours, minutes, seconds = (int(part) for part in clock.split(":"))
        moment = time.mktime((int(year), _MONTHS.index(month) + 1, int(day),
                              hours, minutes, seconds, 0, 0, -1))
        return int(moment), int(count.strip())
    except (ValueError, TypeError, OverflowError):
        return None, None


class Importer:
    """The names this plugin needs from the loaded importer, all resolved at once."""

    def __init__(self, module):
        self.module = module
        self.importer = module.epgimport
        self.config = module.EPGConfig

    @property
    def last_result(self):
        # Rebound by the importer on every completion, so read it every time.
        return getattr(self.module, "lastImportResult", None)

    def is_running(self):
        return bool(self.importer.isImportRunning())

    def schedule(self):
        return getattr(self.module, "autoStartTimer", None)


def resolve():
    """`(Importer, None)`, or `(None, the reason)` when anything is missing.

    🔴 Every name must be there, and so must `importEvents` or `importEvent` on
    the image's EPG cache: without them the importer writes a file instead and
    ends by asking for a GUI restart, with a dialog whose default answer is yes
    and whose timeout presses it.
    """
    module = importer_module()
    if module is None:
        return None, "EPG-Importer is not loaded on this receiver"
    importer = getattr(module, "epgimport", None)
    if importer is None or not callable(getattr(importer, "isImportRunning", None)):
        return None, "EPG-Importer has no importer with isImportRunning()"
    if not hasattr(importer, "sources"):
        return None, "EPG-Importer's importer has no source list"
    if not callable(getattr(module, "startImport", None)):
        return None, "EPG-Importer has no startImport()"
    config = getattr(module, "EPGConfig", None)
    for name in ("loadUserSettings", "enumSources"):
        if not callable(getattr(config, name, None)):
            return None, "EPG-Importer has no EPGConfig." + name + "()"
    if not hasattr(module, "CONFIG_PATH"):
        return None, "EPG-Importer has no CONFIG_PATH"
    if not hasattr(module, "lastImportResult"):
        return None, "EPG-Importer has no lastImportResult"
    factory = enigma_attribute("eEPGCache")
    try:
        cache = factory.getInstance() if factory is not None else None
    except Exception:
        cache = None
    if cache is None or not (hasattr(cache, "importEvents") or hasattr(cache, "importEvent")):
        return None, (
            "this image's EPG cache cannot take imported events, and EPG-Importer "
            "would end by restarting the user interface"
        )
    return Importer(module), None


class EpgImportPublisher(Publisher):
    """`epg_import` — the state of the image's EPG import, whoever started it."""

    name = "epg_import"

    def __init__(self, bridge=None):
        Publisher.__init__(self, bridge)
        self._importer = None
        self._state = IDLE
        self._started = None
        self._finished = None
        self._events = None
        self._error = None
        self._tracking = False
        self._deadline = 0.0
        self._watchdog_fired = False
        self._baseline = None
        self._poll = Ticker(self._tick, "epg import")

    # ---------------------------------------------------------------- lifecycle --

    def start(self):
        importer, reason = resolve()
        if importer is None:
            # Said once per start, at info: most receivers simply do not have it.
            LOG.info("EPG import on demand is unavailable: %s", reason)
            self.switched_off = True
            return False
        self._importer = importer
        self._baseline = importer.last_result
        finished, events = parse_last_import(
            _image_setting("plugins.extra_epgimport", "last_import")
        )
        self._finished, self._events = finished, events
        if self._running() is True:
            self._begin_tracking()
        else:
            self._poll.start(IDLE_POLL_MILLISECONDS)
        return True

    def stop(self):
        self._poll.stop()
        self._tracking = False

    # -------------------------------------------------------------------- state --

    def _payload(self):
        return {
            "state": self._state,
            "started": self._started,
            "finished": self._finished,
            "events": self._events,
            "error": self._error,
        }

    def snapshot(self):
        return {"epg_import": self._payload()}

    def _publish(self):
        self.publish("epg_import", self._payload())

    def _running(self):
        """True, False, or None when the importer would not say."""
        try:
            return self._importer.is_running()
        except Exception:
            LOG.exception("isImportRunning() raised")
            return None

    def _begin_tracking(self, started=None):
        self._state = RUNNING
        self._started = int(time.time()) if started is None else started
        self._finished = None
        self._events = None
        self._error = None
        self._tracking = True
        self._watchdog_fired = False
        self._deadline = time.monotonic() + WATCHDOG_SECONDS
        self._poll.start(RUNNING_POLL_MILLISECONDS)

    # --------------------------------------------------------------- the polls --

    def _tick(self):
        if self._importer is None:
            return
        now_running = self._running()
        if self._tracking:
            if now_running is False:
                self._conclude()
                return
            if not self._watchdog_fired and time.monotonic() >= self._deadline:
                # The importer cannot be cancelled. The poll carries on, and the
                # running guard keeps refusing for as long as it really runs.
                self._watchdog_fired = True
                self._state = FAILED
                self._error = NOT_FINISHED
                LOG.warning("EPG-Importer has not finished after %d minutes",
                            WATCHDOG_SECONDS // 60)
                self._publish()
            return
        if now_running is True:
            LOG.info("an EPG import the plugin did not start is running")
            self._baseline = self._importer.last_result
            self._begin_tracking()
            self._publish()
            return
        # An import that began and ended between two idle polls is still an
        # import, and its result is on the importer's record.
        if self._importer.last_result != self._baseline:
            self._started = None
            self._conclude()

    def _conclude(self):
        """The import is over: read its result and say so."""
        self._tracking = False
        self._poll.start(IDLE_POLL_MILLISECONDS)
        result = self._importer.last_result
        if result is None or result == self._baseline:
            self._state = FAILED
            self._finished = None
            self._events = None
            self._error = DID_NOT_RUN
            self._publish()
            return
        self._baseline = result
        try:
            self._finished = int(result[0])
            self._events = int(result[1])
        except (TypeError, ValueError, IndexError):
            self._state = FAILED
            self._error = DID_NOT_RUN
            self._publish()
            return
        if self._events > 0:
            self._state = DONE
            self._error = None
            LOG.info("EPG-Importer imported %d event(s)", self._events)
            self._publish()
            grid = self.bridge.publisher("epg_grid") if self.bridge is not None else None
            if grid is not None:
                # Each bouquet is published only if it changed (ADR-0006).
                grid.regenerate()
            return
        self._state = FAILED
        self._error = NO_EVENTS
        self._publish()

    # -------------------------------------------------------------- the command --

    def _schedule_refusal(self):
        schedule = self._importer.schedule()
        # Unreadable is not „off": `getWakeTime()` answers -1 by itself when the
        # schedule is switched off, so it is asked whenever the setting is not
        # plainly false.
        if schedule is None or _image_setting("plugins.epgimport", "enabled") is False:
            return None
        try:
            wake = schedule.getWakeTime()
        except Exception:
            LOG.exception("autoStartTimer.getWakeTime() raised")
            return UNKNOWN_SCHEDULE
        try:
            wake = int(wake)
        except (TypeError, ValueError):
            return UNKNOWN_SCHEDULE
        if wake <= 0:
            return None
        remaining = wake - int(time.time())
        if remaining < 0:
            # `getWakeTime` answers today's clock time; once it has passed, the
            # next run is the same time tomorrow.
            remaining += DAY_SECONDS
        if 0 <= remaining <= SCHEDULE_WINDOW_SECONDS:
            return "EPG-Importer's own scheduled import starts in " + str(remaining) + " s"
        return None

    def _refusal(self, origin):
        if not granted(self.value, "epg_import_allowed", origin):
            return PERMISSION
        if self._importer is None:
            return NOT_RESOLVED
        state = self._running()
        if state is None:
            return UNKNOWN_RUNNING
        if state:
            return ALREADY_RUNNING
        refusal = recording.guard(self.session)
        if refusal:
            return refusal
        return self._schedule_refusal()

    def _reset_channel_cache(self):
        """The same test the scheduler's `runImport` makes, without writing its state."""
        schedule = self._importer.schedule()
        only_bouquet = _image_setting("plugins.epgimport", "import_onlybouquet")
        multi = _image_setting("usage", "multibouquet")
        if schedule is not None:
            unchanged = (
                getattr(schedule, "prev_onlybouquet", None) == only_bouquet
                and getattr(schedule, "prev_multibouquet", None) == multi
            )
            if unchanged:
                return
        self._importer.config.channelCache = {}

    def request(self, origin=MQTT):
        """Start an import. None when it began, a sentence otherwise."""
        refusal = self._refusal(origin)
        if refusal:
            return refusal
        module = self._importer.module
        try:
            self._reset_channel_cache()
            settings = self._importer.config.loadUserSettings()
            sources = list(self._importer.config.enumSources(
                module.CONFIG_PATH, filter=settings["sources"]
            ))
        except Exception as error:
            LOG.exception("could not read EPG-Importer's sources")
            return self._failed_to_start(error)
        if not sources:
            return NO_SOURCES
        sources.reverse()
        baseline = self._importer.last_result
        try:
            self._importer.importer.sources = sources
            module.startImport()
        except Exception as error:
            LOG.exception("EPG-Importer could not start")
            return self._failed_to_start(error)
        self._baseline = baseline
        LOG.info("EPG import started with %d source(s)", len(sources))
        self._begin_tracking()
        self._publish()
        return None

    def _failed_to_start(self, error):
        sentence = "EPG-Importer could not start: " + type(error).__name__
        self._state = FAILED
        self._started = int(time.time())
        self._finished = None
        self._events = None
        self._error = sentence
        self._publish()
        return sentence
