"""A receiver, faked well enough to test the parts that matter.

`enigma` is compiled into the enigma2 binary and cannot be imported anywhere
else, so the only way to run this plugin's logic on a PC is to put a stub in
`sys.modules` before the package is imported. That is what happens here, at
import time, before any test module is collected.

The stubs are deliberately faithful rather than minimal where the real behaviour
is load-bearing — `ConfigYesNo` really does hold a bool, `configfile.save()`
really is counted — because a stub that is kinder than the real thing tests
nothing. Where the real behaviour is irrelevant (drawing a screen) the stub is a
shell.

`FakeMQTTClient` stands in for paho's `Client`. It records what was published,
with its QoS and retain flag, and lets a test fire the callbacks paho would fire
from its network thread. Every assertion about the contract in `docs/TOPICS.md`
is an assertion about what it recorded.
"""

import datetime
import sys
import types
from pathlib import Path

import jinja2
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC = REPO_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def _module(name, package=False):
    module = types.ModuleType(name)
    if package:
        module.__path__ = []
    sys.modules[name] = module
    return module


# ------------------------------------------------------- value templates --

# The discovery payloads carry Jinja templates that Home Assistant renders, and
# for a long time these tests only compared them as strings. That is how a
# `timestamp_utc` with `+00:00` appended to it survived review and a release:
# the filter already ends in the offset, every assertion matched the string it
# expected to see, and no assertion ever looked at what the string produces.
#
# 🔴 `_timestamp_utc` below is a COPY of Home Assistant's own filter, from
# `homeassistant/helpers/template/extensions/datetime.py` in 2026.9.2:
#
#     return dt_util.utc_from_timestamp(value).isoformat()
#
# where `dt_util.utc_from_timestamp(value)` is `datetime.fromtimestamp(value,
# UTC)`. Checked by rendering the same template through Home Assistant's own
# engine on 2026.9.2 and comparing the output byte for byte. Being a copy it can
# go stale with nothing here noticing, which is exactly why this covers the two
# timestamp templates and is not extended to filters nobody has run: a shim that
# guesses looks like coverage and is worse than a string compare.
#
# `int` is left as Jinja's own filter. Home Assistant replaces it with a
# forgiving one, and the two differ only on input these templates never reach it
# with — every use sits behind an `{% if %}` that excludes null and absent, and
# the plugin publishes a whole number or nothing.


def _timestamp_utc(value):
    return datetime.datetime.fromtimestamp(value, datetime.timezone.utc).isoformat()


def render_value_template(template, payload):
    """What Home Assistant would hand the entity for this `val_tpl`."""
    environment = jinja2.Environment()
    environment.filters["timestamp_utc"] = _timestamp_utc
    return environment.from_string(template).render(value_json=payload)


# ------------------------------------------------------------------- enigma --

enigma = _module("enigma")


class eTimer:
    """enigma2's timer, which fires only when a test tells it to.

    Deliberately not a real timer: a test that waits for a second to pass is a
    test that will one day fail on a busy machine, and every timer in this
    plugin exists to do something a test can simply ask for.
    """

    instances = []

    def __init__(self):
        self.callback = []
        self.started = None
        self.stopped = False
        eTimer.instances.append(self)

    @property
    def running(self):
        return self.started is not None and not self.stopped

    def start(self, interval, single=False):
        self.started = (interval, single)
        self.stopped = False

    def stop(self):
        self.stopped = True

    def fire(self):
        for function in list(self.callback):
            function()


class _Signal:
    def __init__(self):
        self._listeners = []

    def get(self):
        return self._listeners


class ePythonMessagePump:
    def __init__(self):
        self.recv_msg = _Signal()
        self.sent = []

    def send(self, value):
        self.sent.append(value)
        for listener in list(self.recv_msg.get()):
            listener(value)


enigma.eTimer = eTimer
enigma.ePythonMessagePump = ePythonMessagePump
# What OE-Alliance images actually return: a build date, not a version number.
enigma.getEnigmaVersionString = lambda: "2024-09-11-Release"


# ------------------------------------------------------- services and references --


class eServiceReference:
    """The string form is the reference; everything else is derived from it.

    The real class carries the fields as members and rebuilds the string on
    demand. Nothing in this plugin depends on that, and everything in it depends
    on the string round-tripping unchanged — including the flags in the second
    field, which are how a marker is told from a channel.
    """

    isDirectory = 1
    mustDescent = 2
    canDescent = 4
    flagDirectory = 7
    shouldSort = 8
    hasSortKey = 16
    sort1 = 32
    isMarker = 64
    isGroup = 128
    isNumberedMarker = 256
    isInvisible = 512

    def __init__(self, reference=""):
        self.reference = str(reference)

    @property
    def flags(self):
        parts = self.reference.split(":")
        try:
            return int(parts[1])
        except (IndexError, ValueError):
            return 0

    def toString(self):
        return self.reference

    def valid(self):
        return bool(self.reference)

    def __eq__(self, other):
        return self.reference == getattr(other, "reference", None)

    def __hash__(self):
        return hash(self.reference)

    def __repr__(self):
        return f"eServiceReference({self.reference!r})"


enigma.eServiceReference = eServiceReference


class iServiceInformation:
    # The values are arbitrary and that is the point: nothing in the plugin may
    # hard-code them, so the stub uses numbers no image would.
    sProvider = 901
    sVideoWidth = 902
    sVideoHeight = 903
    sServiceref = 904
    sIsCrypted = 905


class iPlayableService:
    evStart = 1
    evEnd = 2
    evTunedIn = 3
    evTuneFailed = 4
    evUpdatedInfo = 5
    evUpdatedEventInfo = 6
    evNewProgramInfo = 7


class iRecordableService:
    evStart = 11
    evEnd = 12
    evRecordWriteError = 13


class iFrontendInformation:
    bitErrorRate = 0
    signalPower = 1
    signalQuality = 2
    lockState = 3
    syncState = 4
    frontendNumber = 5


enigma.iServiceInformation = iServiceInformation
enigma.iPlayableService = iPlayableService
enigma.iRecordableService = iRecordableService
enigma.iFrontendInformation = iFrontendInformation


class Event:
    """`eServiceEvent`: a programme."""

    def __init__(self, event_id=1, begin=1000, duration=1800, title="Programme",
                 short="", extended=""):
        self.event_id = event_id
        self.begin = begin
        self.duration = duration
        self.title = title
        self.short = short
        self.extended = extended

    def getEventId(self):
        return self.event_id

    def getBeginTime(self):
        return self.begin

    def getDuration(self):
        return self.duration

    def getEventName(self):
        return self.title

    def getShortDescription(self):
        return self.short

    def getExtendedDescription(self):
        return self.extended


class ServiceInfo:
    def __init__(self, name="TVP 1 HD", provider="Cyfrowy Polsat", width=1920, height=1080,
                 events=None, encrypted=False):
        self.name = name
        self.provider = provider
        self.width = width
        self.height = height
        self.events = list(events or [])
        self.encrypted = encrypted
        self.raises_on_event = False

    def getName(self):
        return self.name

    def getInfoString(self, key):
        if key == iServiceInformation.sProvider:
            return self.provider
        return ""

    def getInfo(self, key):
        if key == iServiceInformation.sVideoWidth:
            return self.width
        if key == iServiceInformation.sVideoHeight:
            return self.height
        if key == iServiceInformation.sIsCrypted:
            return int(self.encrypted)
        return -1

    def getEvent(self, index):
        if self.raises_on_event:
            raise RuntimeError("no EPG here")
        try:
            return self.events[index]
        except IndexError:
            return None


class Frontend:
    def __init__(self, quality=52428, power=40000, ber=0, tuner_number=0, status=True):
        self.quality = quality
        self.power = power
        self.ber = ber
        self.tuner_number = tuner_number
        self.status = status

    def getFrontendStatus(self):
        if not self.status:
            return None
        # Note what is *not* here: `tuner_signal_quality_db` is absent rather
        # than negative when the driver has no reading, which is the real
        # behaviour and the reason the plugin uses `.get`.
        return {
            "tuner_signal_quality": self.quality,
            "tuner_signal_power": self.power,
            "tuner_bit_error_rate": self.ber,
            "tuner_locked": 1,
        }

    def getFrontendData(self):
        return {"tuner_number": self.tuner_number, "tuner_type": "DVB-S"}

    def getFrontendInfo(self, key):
        return {
            iFrontendInformation.signalQuality: self.quality,
            iFrontendInformation.signalPower: self.power,
            iFrontendInformation.bitErrorRate: self.ber,
        }.get(key)


class Service:
    def __init__(self, info=None, frontend=None):
        self._info = info if info is not None else ServiceInfo()
        self._frontend = frontend

    def info(self):
        return self._info

    def frontendInfo(self):
        return self._frontend


class ServiceList:
    def __init__(self, entries):
        self.entries = list(entries)

    def getContent(self, fmt="SN", sorted=True):
        rows = []
        for sref, name in self.entries:
            row = []
            for letter in fmt:
                if letter == "S":
                    row.append(sref)
                elif letter == "C":
                    row.append(":".join(sref.split(":")[:10]) + ":")
                elif letter in ("N", "n"):
                    row.append(name)
            rows.append(row[0] if len(row) == 1 else row)
        return rows


class ServiceCenter:
    """One instance, with a dictionary of what each reference contains."""

    _instance = None

    def __init__(self):
        self.contents = {}
        self.listed = []

    @classmethod
    def getInstance(cls):
        if cls._instance is None:
            cls._instance = ServiceCenter()
        return cls._instance

    def list(self, reference):
        sref = getattr(reference, "reference", str(reference))
        self.listed.append(sref)
        entries = self.contents.get(sref)
        return None if entries is None else ServiceList(entries)

    def info(self, reference):
        return ServiceInfo()


enigma.eServiceCenter = ServiceCenter


class EPGCache:
    """`lookupEventId` and `lookupEvent`, over a dictionary of events."""

    _instance = None

    def __init__(self):
        # {sref: [Event, …]}, in time order.
        self.events = {}
        self.queries = []
        self.multi_service = True
        self.raises = False

    @classmethod
    def getInstance(cls):
        if cls._instance is None:
            cls._instance = EPGCache()
        return cls._instance

    def lookupEventId(self, reference, event_id):
        sref = getattr(reference, "reference", str(reference))
        for event in self.events.get(sref, []):
            if event.getEventId() == int(event_id):
                return event
        return None

    def lookupEvent(self, query):
        self.queries.append(query)
        if self.raises:
            raise RuntimeError("this image does not like that query")
        fields = query[0]
        entries = query[1:]
        if len(entries) > 1 and not self.multi_service:
            # What an image that will not take a multi-service query does: it
            # answers None rather than raising.
            return None
        rows = []
        for entry in entries:
            sref = entry[0]
            minutes = entry[3] if len(entry) > 3 else 0
            window = (minutes or 0) * 60
            begin_from = entry[2]
            for event in self.events.get(sref, []):
                if window and begin_from != -1 and event.getBeginTime() > begin_from + window:
                    continue
                row = []
                for letter in fields:
                    if letter == "R":
                        row.append(sref)
                    elif letter == "I":
                        row.append(event.getEventId())
                    elif letter == "B":
                        row.append(event.getBeginTime())
                    elif letter == "D":
                        row.append(event.getDuration())
                    elif letter == "T":
                        row.append(event.getEventName())
                    elif letter == "N":
                        row.append("")
                rows.append(row)
        return rows


enigma.eEPGCache = EPGCache


class KeyActionMap:
    _instance = None

    def __init__(self):
        self.bound = []
        self.pressed = []

    @classmethod
    def getInstance(cls):
        if cls._instance is None:
            cls._instance = KeyActionMap()
        return cls._instance

    def bindAction(self, context, priority, function):
        self.bound.append((context, priority, function))

    def unbindAction(self, context, function):
        self.bound = [entry for entry in self.bound if entry[2] is not function]

    def keyPressed(self, device, key, flags):
        """The three-argument form every current image has.

        Injected presses are delivered to whatever bound a handler, which is how
        a test proves that `cmd/key` reaches the plugin's own listener — exactly
        as it does on the box.
        """
        self.pressed.append((device, key, flags))
        for _context, _priority, function in list(self.bound):
            function(key, flags)


enigma.eActionMap = KeyActionMap


class ConsoleAppContainer:
    """`eConsoleAppContainer`: runs nothing, remembers everything."""

    instances = []

    def __init__(self):
        self.appClosed = []
        self.stdoutAvail = []
        self.commands = []
        self.rejects = False
        ConsoleAppContainer.instances.append(self)

    def execute(self, command, *arguments):
        self.commands.append(command)
        return 1 if self.rejects else 0

    def finish(self, retval=0):
        for function in list(self.appClosed):
            function(retval)

    def kill(self):
        pass


enigma.eConsoleAppContainer = ConsoleAppContainer


class DVBVolumeControl:
    _instance = None

    def __init__(self):
        self.volume = 35
        self.muted = False

    @classmethod
    def getInstance(cls):
        if cls._instance is None:
            cls._instance = DVBVolumeControl()
        return cls._instance

    def getVolume(self):
        return self.volume

    def setVolume(self, left, right):
        self.volume = int(left)

    def isMuted(self):
        return self.muted

    def volumeToggleMute(self):
        self.muted = not self.muted


enigma.eDVBVolumecontrol = DVBVolumeControl


# -------------------------------------------------------------- boxbranding --

boxbranding = _module("boxbranding")
boxbranding.getBoxType = lambda: "vuuno4kse"
boxbranding.getImageDistro = lambda: "openvix"
boxbranding.getImageVersion = lambda: "6.6"
boxbranding.getImageBuild = lambda: "007"


# ------------------------------------------------------------ Components.config --

components = _module("Components", package=True)
_module("Components.Sources", package=True)
config_module = _module("Components.config")


class ConfigElement:
    def __init__(self, default=None):
        self.default = default
        self._value = default
        self.saved_value = default
        self.save_calls = 0
        self.cancel_calls = 0

    def _get(self):
        return self._value

    def _set(self, value):
        self._value = value

    value = property(_get, _set)

    def getValue(self):
        return self._value

    def setValue(self, value):
        self.value = value

    def save(self):
        self.saved_value = self._value
        self.save_calls += 1

    def load(self):
        self._value = self.saved_value

    def cancel(self):
        self.cancel_calls += 1
        self.load()

    def getText(self):
        return str(self._value)

    def __repr__(self):
        return f"<{type(self).__name__} {self._value!r}>"


class ConfigText(ConfigElement):
    def __init__(self, default="", fixed_size=True, visible_width=False):
        ConfigElement.__init__(self, default)
        self.fixed_size = fixed_size
        self.visible_width = visible_width

    def _set(self, value):
        self._value = "" if value is None else str(value)

    value = property(ConfigElement._get, _set)


class ConfigPassword(ConfigText):
    pass


class ConfigInteger(ConfigElement):
    def __init__(self, default=0, limits=(0, 9999)):
        ConfigElement.__init__(self, int(default))
        self.limits = limits

    def _set(self, value):
        self._value = int(value)

    value = property(ConfigElement._get, _set)


# In enigma2 ConfigYesNo is a ConfigBoolean, not a ConfigSelection; what matters
# here is that its value is a bool and nothing else.
class ConfigYesNo(ConfigElement):
    def __init__(self, default=False):
        ConfigElement.__init__(self, bool(default))

    def _set(self, value):
        self._value = bool(value)

    value = property(ConfigElement._get, _set)


class ConfigSelection(ConfigElement):
    def __init__(self, choices=None, default=None):
        normalised = []
        for choice in choices or []:
            if isinstance(choice, (tuple, list)):
                normalised.append((choice[0], choice[1]))
            else:
                normalised.append((choice, choice))
        self.choices = normalised
        ConfigElement.__init__(self, default if default is not None else normalised[0][0])

    def _set(self, value):
        if value not in [key for key, _label in self.choices]:
            raise ValueError(f"not a choice: {value!r}")
        self._value = value

    value = property(ConfigElement._get, _set)


class _Content:
    def __init__(self):
        self.items = {}


class ConfigSubsection:
    def __init__(self):
        object.__setattr__(self, "content", _Content())

    def __setattr__(self, name, value):
        self.content.items[name] = value
        object.__setattr__(self, name, value)


class ConfigFile:
    def __init__(self):
        self.save_calls = 0

    def save(self):
        self.save_calls += 1

    def load(self):
        pass


def getConfigListEntry(*args):
    return tuple(args)


class StandbyCounter(ConfigInteger):
    """`config.misc.standbyCounter`: a number that goes up when the box sleeps.

    Its notifier list is the only event enigma2 offers for *entering* standby,
    which is why the plugin hangs off a counter rather than off a state.
    """

    def __init__(self):
        ConfigInteger.__init__(self, default=0)
        self.notifiers = []

    def addNotifier(self, notifier, initial_call=True, immediate_feedback=True):
        self.notifiers.append(notifier)
        if initial_call:
            notifier(self)

    def removeNotifier(self, notifier):
        if notifier in self.notifiers:
            self.notifiers.remove(notifier)

    def increment(self):
        self._value += 1
        for notifier in list(self.notifiers):
            notifier(self)


config = ConfigSubsection()
config.plugins = ConfigSubsection()
config.misc = ConfigSubsection()
config.misc.standbyCounter = StandbyCounter()
configfile = ConfigFile()

config_module.ConfigElement = ConfigElement
config_module.ConfigText = ConfigText
config_module.ConfigPassword = ConfigPassword
config_module.ConfigInteger = ConfigInteger
config_module.ConfigYesNo = ConfigYesNo
config_module.ConfigSelection = ConfigSelection
config_module.ConfigSubsection = ConfigSubsection
config_module.config = config
config_module.configfile = configfile
config_module.getConfigListEntry = getConfigListEntry


# ---------------------------------------------- Components / Screens / Plugins --

action_map_module = _module("Components.ActionMap")


class ActionMap:
    def __init__(self, contexts, actions=None, prio=0):
        self.contexts = contexts
        self.actions = actions or {}
        self.prio = prio


action_map_module.ActionMap = ActionMap
action_map_module.NumberActionMap = ActionMap

config_list_module = _module("Components.ConfigList")


class ConfigList:
    def __init__(self, entries=None):
        self.list = list(entries or [])


class ConfigListScreen:
    def __init__(self, entries, session=None, on_change=None):
        self.entries = list(entries)
        self["config"] = ConfigList(self.entries)
        self.onChangedEntry = []

    def saveAll(self):
        for entry in self.entries:
            entry[1].save()

    def keyOK(self):
        pass


config_list_module.ConfigList = ConfigList
config_list_module.ConfigListScreen = ConfigListScreen

label_module = _module("Components.Label")


class Label:
    def __init__(self, text=""):
        self.text = text

    def setText(self, text):
        self.text = text


label_module.Label = Label

static_text_module = _module("Components.Sources.StaticText")
static_text_module.StaticText = Label

screens = _module("Screens", package=True)
screen_module = _module("Screens.Screen")


class Screen:
    def __init__(self, session):
        self.session = session
        self.widgets = {}
        self.title = ""
        self.closed_with = None
        self.onLayoutFinish = []
        self.onClose = []

    def __setitem__(self, name, widget):
        self.widgets[name] = widget

    def __getitem__(self, name):
        return self.widgets[name]

    def setTitle(self, title):
        self.title = title

    def close(self, *args):
        self.closed_with = args


screen_module.Screen = Screen

message_box_module = _module("Screens.MessageBox")


class MessageBox:
    # enigma2's own numbering: the yes/no dialog is 0.
    TYPE_YESNO = 0
    TYPE_INFO = 1
    TYPE_WARNING = 2
    TYPE_ERROR = 3

    def __init__(self, session=None, text="", type=TYPE_INFO, timeout=-1, **kwargs):
        self.text = text
        self.type = type
        self.timeout = timeout


message_box_module.MessageBox = MessageBox

plugins_package = _module("Plugins", package=True)
plugin_module = _module("Plugins.Plugin")


class PluginDescriptor:
    WHERE_MENU = 1
    WHERE_AUTOSTART = 2
    WHERE_PLUGINMENU = 3
    WHERE_EXTENSIONSMENU = 4
    WHERE_SESSIONSTART = 6

    def __init__(self, name="", description="", where=None, fnc=None, icon=None,
                 needsRestart=None, **kwargs):
        self.name = name
        self.description = description
        self.where = where
        self.fnc = fnc
        self.icon = icon
        self.needsRestart = needsRestart


plugin_module.PluginDescriptor = PluginDescriptor

tools = _module("Tools", package=True)
directories_module = _module("Tools.Directories")
directories_module.SCOPE_PLUGINS = "plugins"
directories_module.SCOPE_CONFIG = "config"


def resolveFilename(scope, path=""):
    return "/usr/lib/enigma2/python/Plugins/" + path


directories_module.resolveFilename = resolveFilename

notifications_module = _module("Tools.Notifications")


class Notifications:
    """What was put on the screen, and in which order."""

    popups = []
    removed = []
    notifications = []
    raises = False


def AddPopup(text, type=1, timeout=10, id=None):
    if Notifications.raises:
        raise RuntimeError("no screen to put it on")
    Notifications.popups.append({"text": text, "type": type, "timeout": timeout, "id": id})


def RemovePopup(id):
    Notifications.removed.append(id)


def AddNotification(screen, *arguments):
    Notifications.notifications.append((screen, arguments))


notifications_module.AddPopup = AddPopup
notifications_module.RemovePopup = RemovePopup
notifications_module.AddNotification = AddNotification
notifications_module.Notifications = Notifications


# ------------------------------------------------------------ Screens.Standby --

standby_module = _module("Screens.Standby")


class StandbyScreen:
    def __init__(self):
        self.onClose = []
        self.power_calls = 0

    def Power(self):
        self.power_calls += 1
        # Leaving standby is the screen closing, which is what the plugin
        # listens for.
        standby_module.inStandby = None
        for function in list(self.onClose):
            function()


class Standby:
    """The class enigma2 hands to `AddNotification` to *enter* standby."""


class TryQuitMainloop:
    """The screen that shuts the box down; its argument is what kind of down."""


standby_module.inStandby = None
standby_module.Standby = Standby
standby_module.StandbyScreen = StandbyScreen
standby_module.TryQuitMainloop = TryQuitMainloop


infobar_module = _module("Screens.InfoBar")


class InfoBar:
    instance = None

    def __init__(self, servicelist=None):
        self.servicelist = servicelist


class ChannelList:
    """Enough of the receiver's channel list to prove the zap is safe."""

    def __init__(self, selectable=(), bouquets=None, nav=None):
        self.selectable = [str(s) for s in selectable]
        self.selection = None
        self.zaps = 0
        self.bouquet_root = eServiceReference(BOUQUET_ROOT)
        self.root = eServiceReference(FIRST_BOUQUET)
        self.path = [self.bouquet_root, self.root]
        self.servicePath = self.path
        self.bouquets = bouquets or {}
        self.nav = nav
        self.saved_roots = 0
        self.mode = 0

    def setCurrentSelection(self, reference):
        wanted = getattr(reference, "reference", str(reference))
        if wanted in self.selectable:
            self.selection = wanted

    def getCurrentSelection(self):
        return None if self.selection is None else eServiceReference(self.selection)

    def zap(self):
        self.zaps += 1
        if self.nav is not None and self.selection is not None:
            # ChannelSelection.zap owns the tune; do not model it as the
            # fallback Navigation.playService path whose use other tests detect.
            self.nav.sref = self.selection

    def clearPath(self):
        self.path.clear()
        self.root = None

    def enterPath(self, reference):
        self.path.append(reference)
        self.root = reference
        services = self.bouquets.get(getattr(reference, "reference", str(reference)))
        if services is not None:
            self.selectable = list(services)
            if self.selection not in self.selectable:
                self.selection = None

    def getRoot(self):
        return self.root

    def saveRoot(self):
        self.saved_roots += 1

    def channel_down(self):
        if not self.selectable:
            return
        index = self.selectable.index(self.selection) if self.selection in self.selectable else -1
        self.selection = self.selectable[(index + 1) % len(self.selectable)]
        self.zap()

    def channel_up(self):
        if not self.selectable:
            return
        index = self.selectable.index(self.selection) if self.selection in self.selectable else 0
        self.selection = self.selectable[(index - 1) % len(self.selectable)]
        self.zap()


infobar_module.InfoBar = InfoBar
infobar_module.ChannelList = ChannelList

channel_selection_module = _module("Screens.ChannelSelection")
channel_selection_module.service_types_tv = (
    "1:7:1:0:0:0:0:0:0:0:(type == 1) || (type == 17) || (type == 22)"
)


# ------------------------------------------------- ServiceReference / RecordTimer --

service_reference_module = _module("ServiceReference")


class ServiceReference(eServiceReference):
    """On current images this really is an `eServiceReference` subclass."""

    names = {}

    def __init__(self, reference=""):
        eServiceReference.__init__(self, getattr(reference, "reference", str(reference)))

    @property
    def ref(self):
        return self

    def getServiceName(self):
        return ServiceReference.names.get(self.reference, "")

    def __str__(self):
        return self.reference


service_reference_module.ServiceReference = ServiceReference

record_timer_module = _module("RecordTimer")


class RecordTimerEntry:
    StateWaiting = 0
    StatePrepared = 1
    StateRunning = 2
    StateEnded = 3

    def __init__(self, serviceref, begin, end, name, description, eit, disabled=False,
                 justplay=False, afterEvent=3, *args, **kwargs):
        # The real class asserts this, so a wrapper of the wrong kind would fail
        # on a box and pass here if the stub were kinder.
        assert isinstance(serviceref, eServiceReference)
        self.service_ref = serviceref
        self.begin = int(begin)
        self.end = int(end)
        self.name = name
        self.description = description
        self.eit = eit
        self.disabled = disabled
        self.justplay = justplay
        self.afterEvent = afterEvent
        self.state = RecordTimerEntry.StateWaiting
        self.repeated = 0
        self.dontSave = False

    def isRunning(self):
        return self.state == RecordTimerEntry.StateRunning


def parseEvent(event, description=True):
    """Exactly the five-tuple the real one returns, margins and all."""
    begin = event.getBeginTime() - record_timer_module.margin_before * 60
    end = event.getBeginTime() + event.getDuration() + record_timer_module.margin_after * 60
    name = event.getEventName() if description else ""
    short = (event.getShortDescription() or event.getExtendedDescription()) if description else ""
    return (begin, end, name, short, event.getEventId())


record_timer_module.RecordTimerEntry = RecordTimerEntry
record_timer_module.parseEvent = parseEvent
record_timer_module.margin_before = 0
record_timer_module.margin_after = 0


class RecordTimer:
    """`session.nav.RecordTimer`, with the two return values that matter."""

    def __init__(self):
        self.timer_list = []
        self.processed_timers = []
        self.save_calls = 0
        self.conflicts = None
        # The trap: `record()` answers None both for „accepted" and for
        # „dropped as a duplicate".
        self.swallow = False
        self.removed = []

    def record(self, entry, *args, **kwargs):
        if self.conflicts:
            return list(self.conflicts)
        if self.swallow:
            return None
        self.timer_list.append(entry)
        self.saveTimer()
        return None

    def removeEntry(self, entry):
        self.removed.append(entry)
        if entry in self.timer_list:
            self.timer_list.remove(entry)
        self.saveTimer()

    def saveTimer(self):
        self.save_calls += 1

    def isRecording(self):
        return any(timer.state == RecordTimerEntry.StateRunning for timer in self.timer_list)

    def getNextRecordingTime(self):
        upcoming = [
            timer.begin
            for timer in self.timer_list
            if timer.state == RecordTimerEntry.StateWaiting and not timer.disabled
        ]
        return min(upcoming) if upcoming else -1


# -------------------------------------------------------- Components.VolumeControl --

volume_control_module = _module("Components.VolumeControl")


class VolumeDialog:
    def __init__(self):
        self.shown = 0
        self.value = None

    def show(self):
        self.shown += 1

    def setValue(self, value):
        self.value = value


class VolumeControl:
    instance = None

    def __init__(self):
        self.volctrl = DVBVolumeControl.getInstance()
        self.volumeDialog = VolumeDialog()
        self.hideVolTimer = eTimer()
        self.saves = 0
        self.mute_calls = 0
        # The real one refuses to mute at volume 0 unless forced.
        self.refuses_mute_at_zero = True

    def volSave(self):
        self.saves += 1

    def volUp(self):
        self.volctrl.setVolume(min(100, self.volctrl.getVolume() + 1),
                               min(100, self.volctrl.getVolume() + 1))

    def volDown(self):
        self.volctrl.setVolume(max(0, self.volctrl.getVolume() - 1),
                               max(0, self.volctrl.getVolume() - 1))

    def volMute(self, showMuteSymbol=True, force=False):
        self.mute_calls += 1
        if self.refuses_mute_at_zero and not force and self.volctrl.getVolume() == 0:
            return
        self.volctrl.volumeToggleMute()


volume_control_module.VolumeControl = VolumeControl


# -------------------------------------------------------------------- keyids --

keyids_module = _module("keyids")
keyids_module.KEYIDS = {
    "KEY_OK": 352,
    "KEY_ENTER": 352,
    "KEY_RED": 398,
    "KEY_INFO": 358,
    # One this plugin's own table does not carry, to prove the merge happens.
    "KEY_PVR": 366,
}


# -------------------------------------------------------- twisted, immediately --

_module("twisted", package=True)
_module("twisted.internet", package=True)
reactor_module = _module("twisted.internet.reactor")


def callFromThread(function, *args, **kwargs):
    """The real one queues onto the main loop; here the test *is* the main loop."""
    return function(*args, **kwargs)


reactor_module.callFromThread = callFromThread
sys.modules["twisted.internet"].reactor = reactor_module


# ---------------------------------------------------------------- fake broker --


class Published:
    __slots__ = ("topic", "payload", "qos", "retain")

    def __init__(self, topic, payload, qos, retain):
        self.topic = topic
        self.payload = payload
        self.qos = qos
        self.retain = retain

    @property
    def text(self):
        if isinstance(self.payload, (bytes, bytearray)):
            # `replace` rather than strict: one of the payloads on this contract
            # is a JPEG, and a test that only wants to know whether a topic was
            # retracted should not blow up on it.
            return bytes(self.payload).decode("utf-8", "replace")
        return "" if self.payload is None else str(self.payload)

    def json(self):
        import json

        return json.loads(self.text)

    def __repr__(self):
        return (
            f"Published({self.topic!r}, {self.text!r}, "
            f"qos={self.qos!r}, retain={self.retain!r})"
        )


class FakeMessageInfo:
    def __init__(self, rc=0):
        self.waited = None
        self.rc = rc

    def wait_for_publish(self, timeout=None):
        self.waited = timeout


class FakeMessage:
    def __init__(self, topic, payload, qos=1, retain=False):
        self.topic = topic
        self.payload = payload if isinstance(payload, bytes) else str(payload).encode("utf-8")
        self.qos = qos
        self.retain = retain


class FakeMQTTClient:
    """paho's Client, reduced to what it records and what it fires."""

    def __init__(self, client_id="", **kwargs):
        self.client_id = client_id
        self.kwargs = kwargs
        self.will = None
        self.credentials = None
        self.tls = None
        self.reconnect_delay = None
        self.connect_calls = []
        self.subscriptions = []
        self.published = []
        self.loop_started = False
        self.disconnect_calls = 0
        self.max_queued = None
        self.max_inflight = None
        # Set to a paho error code to make every later publish come back rejected.
        self.publish_rc = 0
        self.on_connect = None
        self.on_message = None
        self.on_disconnect = None

    # --- the paho surface the plugin uses -----------------------------------

    def will_set(self, topic, payload=None, qos=0, retain=False):
        self.will = Published(topic, payload, qos, retain)

    def username_pw_set(self, username, password=None):
        self.credentials = (username, password)

    def tls_set(self, ca_certs=None, **kwargs):
        self.tls = ca_certs if ca_certs else True

    def reconnect_delay_set(self, min_delay=1, max_delay=120):
        self.reconnect_delay = (min_delay, max_delay)

    def max_queued_messages_set(self, queue_size):
        self.max_queued = queue_size
        return self

    def max_inflight_messages_set(self, inflight):
        self.max_inflight = inflight

    def connect_async(self, host, port=1883, keepalive=60):
        self.connect_calls.append((host, port, keepalive))

    def loop_start(self):
        self.loop_started = True

    def loop_stop(self):
        self.loop_started = False

    def disconnect(self):
        self.disconnect_calls += 1

    def subscribe(self, topic, qos=0):
        self.subscriptions.append((topic, qos))
        return (0, 1)

    def publish(self, topic, payload=None, qos=0, retain=False):
        self.published.append(Published(topic, payload, qos, retain))
        return FakeMessageInfo(rc=self.publish_rc)

    # --- what paho's network thread would do --------------------------------

    def fire_connect(self, reason_code=0):
        self.on_connect(self, None, {}, reason_code, None)

    def fire_message(self, topic, payload, retain=False, qos=1):
        self.on_message(self, None, FakeMessage(topic, payload, qos=qos, retain=retain))

    def fire_disconnect(self, reason_code=0):
        self.on_disconnect(self, None, {}, reason_code, None)

    # --- what a test asks it ------------------------------------------------

    def client_disconnected(self):
        return self.disconnect_calls > 0

    def topics(self):
        return [entry.topic for entry in self.published]

    def last(self, topic):
        for entry in reversed(self.published):
            if entry.topic == topic:
                return entry
        return None

    def all_for(self, topic):
        return [entry for entry in self.published if entry.topic == topic]

    def clear(self):
        self.published = []
        self.subscriptions = []


class ClientFactory:
    """Handed to the bridge in place of paho; keeps every client it made."""

    def __init__(self):
        self.clients = []

    def __call__(self, client_id):
        client = FakeMQTTClient(client_id=client_id)
        self.clients.append(client)
        return client

    @property
    def client(self):
        return self.clients[-1] if self.clients else None


# ------------------------------------------------------------ a whole receiver --


class Navigation:
    """`session.nav`: what is playing, what is recording, and who is watching."""

    def __init__(self, service=None, sref=""):
        self.event = []
        self.record_event = []
        self.RecordTimer = RecordTimer()
        self.service = service
        self.sref = sref
        self.played = []

    def getCurrentService(self):
        return self.service

    def getCurrentlyPlayingServiceReference(self):
        return eServiceReference(self.sref) if self.sref else None

    def playService(self, reference):
        self.sref = getattr(reference, "reference", str(reference))
        self.played.append(self.sref)

    def fire(self, event):
        for listener in list(self.event):
            listener(event)

    def fire_record(self, service=None, event=None):
        for listener in list(self.record_event):
            listener(service, event)


class Session:
    def __init__(self, nav):
        self.nav = nav
        self.opened = []

    def open(self, screen, *arguments):
        self.opened.append((screen, arguments))
        return screen

    def openWithCallback(self, callback, screen, *arguments):
        self.opened.append((screen, arguments))
        return screen


BOUQUET_ROOT = (
    channel_selection_module.service_types_tv
    + ' FROM BOUQUET "bouquets.tv" ORDER BY bouquet'
)
FIRST_BOUQUET = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.ulubione.tv" ORDER BY bouquet'
SECOND_BOUQUET = '1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "userbouquet.sport.tv" ORDER BY bouquet'

TVP1 = "1:0:19:283D:3FB:1:C00000:0:0:0:"
TVN = "1:0:19:283E:3FB:1:C00000:0:0:0:"
POLSAT = "1:0:19:283F:3FB:1:C00000:0:0:0:"
MARKER = "1:64:0:0:0:0:0:0:0:0::A heading"


class Receiver:
    """A whole fake box: bouquets, EPG, a tuner, a volume and a session."""

    def __init__(self):
        self.service_center = ServiceCenter.getInstance()
        self.epg = EPGCache.getInstance()
        self.volume = DVBVolumeControl.getInstance()
        self.actions = KeyActionMap.getInstance()
        self.info = ServiceInfo(
            events=[
                Event(27431, 1789459200, 1500, "Wiadomości", "Serwis informacyjny"),
                Event(27432, 1789460700, 300, "Pogoda"),
            ]
        )
        self.frontend = Frontend()
        self.service = Service(self.info, self.frontend)
        self.nav = Navigation(self.service, TVP1)
        self.session = Session(self.nav)

        self.service_center.contents = {
            BOUQUET_ROOT: [
                (FIRST_BOUQUET, "Ulubione TV"),
                (SECOND_BOUQUET, "Sport (HD)"),
            ],
            FIRST_BOUQUET: [
                (MARKER, "A heading"),
                (TVP1, "TVP 1 HD"),
                (TVN, "TVN HD"),
            ],
            SECOND_BOUQUET: [
                (POLSAT, "Polsat Sport"),
            ],
        }
        ServiceReference.names.update({
            TVP1: "TVP 1 HD",
            TVN: "TVN HD",
            POLSAT: "Polsat Sport",
        })
        self.epg.events = {
            TVP1: [
                Event(27431, 1789459200, 1500, "Wiadomości"),
                Event(27432, 1789460700, 300, "Pogoda"),
                Event(27433, 1789461000, 3600, "Film"),
            ],
            TVN: [Event(31001, 1789459200, 1800, "Fakty")],
            POLSAT: [],
        }
        VolumeControl.instance = VolumeControl()
        self.volume_control = VolumeControl.instance

    # --- what a test does to it ---------------------------------------------

    def enter_standby(self):
        standby_module.inStandby = StandbyScreen()
        config.misc.standbyCounter.increment()
        return standby_module.inStandby

    def leave_standby(self):
        screen = standby_module.inStandby
        if screen is not None:
            screen.Power()

    def add_timer(self, sref=TVP1, begin=1789459200, end=1789460700, name="Wiadomości",
                  state=RecordTimerEntry.StateWaiting, justplay=False, repeated=0):
        timer = RecordTimerEntry(ServiceReference(sref), begin, end, name, "", 0)
        timer.state = state
        timer.justplay = justplay
        timer.repeated = repeated
        self.nav.RecordTimer.timer_list.append(timer)
        return timer

    def with_channel_list(self, selectable=(TVP1, TVN)):
        InfoBar.instance = InfoBar(
            ChannelList(
                selectable,
                bouquets={
                    FIRST_BOUQUET: [TVP1, TVN],
                    SECOND_BOUQUET: [POLSAT],
                },
                nav=self.nav,
            )
        )
        return InfoBar.instance.servicelist


@pytest.fixture
def receiver():
    return Receiver()


@pytest.fixture(autouse=True)
def fresh_receiver():
    """Every singleton the receiver stubs keep, put back between tests."""
    from MQTTBridge import enigma2 as enigma2_module
    from MQTTBridge import keys as keys_module
    from MQTTBridge import remote as remote_module

    def reset():
        ServiceCenter._instance = None
        EPGCache._instance = None
        KeyActionMap._instance = None
        DVBVolumeControl._instance = None
        VolumeControl.instance = None
        InfoBar.instance = None
        standby_module.inStandby = None
        ServiceReference.names = {}
        ConsoleAppContainer.instances = []
        eTimer.instances = []
        Notifications.popups = []
        Notifications.removed = []
        Notifications.notifications = []
        Notifications.raises = False
        record_timer_module.margin_before = 0
        record_timer_module.margin_after = 0
        counter = config.misc.standbyCounter
        counter.notifiers = []
        counter._value = 0
        enigma2_module.forget_missing()
        keys_module.forget_image_keys()
        remote_module.forget_rate_limit()

    reset()
    yield
    reset()


# --------------------------------------------------------------- fixtures --


@pytest.fixture(autouse=True)
def isolated_log(tmp_path, monkeypatch):
    """No test writes to /home/root or /tmp, and no secret survives a test."""
    from MQTTBridge import log as log_module

    log_module.close()
    log_module.forget_secrets()
    monkeypatch.setattr(log_module, "PRIMARY_LOG_PATH", str(tmp_path / "mqttbridge.log"))
    monkeypatch.setattr(log_module, "FALLBACK_LOG_PATH", str(tmp_path / "fallback.log"))
    yield tmp_path / "mqttbridge.log"
    log_module.close()
    log_module.forget_secrets()


@pytest.fixture(autouse=True)
def fresh_settings():
    """Config elements are module-level singletons; put them back between tests."""
    from MQTTBridge import config as settings_module

    for name in settings_module.SETTING_NAMES:
        element = getattr(settings_module.settings, name)
        element.value = element.default
        element.saved_value = element.default
        element.save_calls = 0
        element.cancel_calls = 0
    configfile.save_calls = 0
    yield
    configfile.save_calls = 0


@pytest.fixture
def plugin_log(isolated_log):
    """The plugin's own log file, at debug, read back as a string.

    Deliberately not `caplog`: the package logger does not propagate to the root
    logger, and what this project promises about its log — that a password never
    reaches it — is a promise about the file, not about a capture handler.
    """
    from MQTTBridge import log as log_module

    log_module.configure("debug", str(isolated_log))

    def read():
        try:
            return isolated_log.read_text(encoding="utf-8")
        except OSError:
            return ""

    read.path = isolated_log
    return read


@pytest.fixture
def settings():
    from MQTTBridge import config as settings_module

    return settings_module.settings


@pytest.fixture
def wait_until():
    """Wait for something a background thread does, without sleeping blindly.

    The shutdown path hands paho's thread join to a thread of its own, so what
    used to be true the instant `stop()` returned is now true shortly after.
    """
    import time

    def wait(predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.005)
        return bool(predicate())

    return wait


@pytest.fixture
def factory():
    return ClientFactory()


@pytest.fixture
def state_path(tmp_path):
    return str(tmp_path / "mqttbridge-state.json")


@pytest.fixture
def make_bridge(factory, state_path, tmp_path, isolated_log):
    """A bridge wired to the fake broker, a temporary state file and no real files."""
    from MQTTBridge.bridge import Bridge
    from MQTTBridge.discovery import StateStore

    created = []

    def build(**overrides):
        options = {
            "client_factory": factory,
            "dispatcher": callFromThread,
            "state_store": StateStore(path=state_path),
            "provisioning_path": str(tmp_path / "absent-mqttbridge.json"),
            "log_path": str(isolated_log),
            "loop_monitor": None,
        }
        options.update(overrides)
        bridge = Bridge(**options)
        created.append(bridge)
        return bridge

    build.factory = factory
    yield build


@pytest.fixture
def connected_bridge(make_bridge, factory, settings):
    """A started, connected bridge with a plausible broker configured.

    No session, so no feature area registers: this is the bridge itself — the
    connection, `info`, the announcement and the commands that need nothing from
    the receiver.
    """
    settings.host.value = "10.0.0.5"
    settings.node_id.value = "vuuno4kse_005301"
    settings.friendly_name.value = "Living room receiver"
    bridge = make_bridge()
    bridge.start()
    factory.client.fire_connect()
    return bridge


def settle(bridge, rounds=20):
    """Let the work that is spread across timer ticks finish.

    The EPG grid builds one bouquet per turn of the main loop, so on a real box
    it is complete a fraction of a second after the connect. A test that wants
    the settled state has to turn that handle itself.
    """
    grid = bridge.publisher("epg_grid")
    if grid is None:
        return bridge
    for _ in range(rounds):
        if not grid._queue:
            break
        grid._step.timer.fire()
    return bridge


@pytest.fixture
def live_bridge(make_bridge, factory, settings, receiver):
    """A bridge on a working receiver: every publisher registered and started."""
    settings.host.value = "10.0.0.5"
    settings.node_id.value = "vuuno4kse_005301"
    settings.friendly_name.value = "Living room receiver"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    return settle(bridge)
