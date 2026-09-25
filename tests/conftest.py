"""A receiver, faked well enough to test the parts that matter.

`enigma` is compiled into the enigma2 binary and cannot be imported anywhere
else, so the only way to run this plugin's logic on a PC is to put a stub in
`sys.modules` before the package is imported. That is what happens here, at
import time, before any test module is collected.

The stubs are deliberately faithful rather than minimal where the real behaviour
is load-bearing - `ConfigYesNo` really does hold a bool, `configfile.save()`
really is counted - because a stub that is kinder than the real thing tests
nothing. Where the real behaviour is irrelevant (drawing a screen) the stub is a
shell.

`FakeMQTTClient` stands in for paho's `Client`. It records what was published,
with its QoS and retain flag, and lets a test fire the callbacks paho would fire
from its network thread. Every assertion about the contract in `docs/TOPICS.md`
is an assertion about what it recorded.
"""

import json
import sys
import types
from pathlib import Path

import hatemplate
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

# The discovery payloads carry Jinja templates that Home Assistant renders.
# Tests render them - never compare them as strings, which is how a
# `timestamp_utc` with a second `+00:00` survived review and a release - and
# they render them with `hatemplate`: Home Assistant's own `int`, `round` and
# `timestamp_utc`, and a guard that refuses any filter, test or global it does
# not reproduce. `test_discovery_templates.py` renders every template the plugin
# emits; this is the shorthand the feature modules use for their own.


def render_value_template(template, payload):
    """What Home Assistant would hand the entity for this `val_tpl` and payload."""
    return hatemplate.render(template, value=json.dumps(payload), value_json=payload)


# ------------------------------------------------------------------- enigma --

enigma = _module("enigma")


class eTimer:
    """enigma2's timer, which fires only when a test tells it to.

    Deliberately not a real timer: a test that waits for a second to pass is a
    test that will one day fail on a busy machine, and every timer in this
    plugin exists to do something a test can simply ask for.
    """

    instances = []
    # Only `MainLoop` reads these: when a started timer falls due, and in which
    # order two timers due at the same moment were started.
    _sequence = 0

    def __init__(self):
        self.callback = []
        self.started = None
        self.stopped = False
        self.due = None
        self.sequence = 0
        eTimer.instances.append(self)

    @property
    def running(self):
        return self.started is not None and not self.stopped

    def start(self, interval, single=False):
        self.started = (interval, single)
        self.stopped = False
        self.due = MainLoop.now + int(interval)
        eTimer._sequence += 1
        self.sequence = eTimer._sequence

    def stop(self):
        self.stopped = True

    def fire(self):
        for function in list(self.callback):
            function()


class MainLoop:
    """enigma2's main loop, as far as its timers go: a clock a test moves by hand.

    Most tests fire one timer they know about, which is enough. The CEC tests
    need the real thing, because what they test is **which turn of the loop**
    something happens on: `Session.close()` does not pop a screen, it starts a
    0 ms timer and returns, and the standby the screen was holding up runs on
    the turn that timer fires. `advance(0)` runs every timer that is already
    due - including ones started while it runs, as the real loop would - in
    the order they fall due, and a single-shot timer stops before its callback
    runs, as enigma2's does.
    """

    now = 0

    @classmethod
    def advance(cls, milliseconds=0, limit=1000):
        target = cls.now + int(milliseconds)
        for _ in range(limit):
            due = [
                timer for timer in eTimer.instances
                if timer.running and timer.due is not None and timer.due <= target
            ]
            if not due:
                break
            timer = min(due, key=lambda item: (item.due, item.sequence))
            cls.now = max(cls.now, timer.due)
            interval, single = timer.started
            if single:
                timer.stopped = True
            else:
                timer.due = cls.now + max(int(interval), 1)
            timer.fire()
        else:
            raise AssertionError("the main loop did not settle")
        cls.now = target


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


# ---------------------------------------------------- the desktop and its widgets --
#
# 🔴 Modelled on the C++ in `lib/gui/` at the commit the receiver's enigma2
# package was built from (OpenViX `cd4f9bc4ee`, named in the installed package
# version). The binary itself cannot be disassembled, so this is the source it
# was built from rather than a measurement of it. The details the discreet toast
# depends on, each read there:
#
# - `eWindow(desktop, z)` sets its z **before** `addRootWidget`, so a window's z is
#   fixed when it is created (`ewindow.cpp:28-34`).
# - `eWidgetDesktop::addRootWidget` keeps the root windows front to back in
#   descending z, and walks past every window whose z is not lower than the new
#   one's: **a new window of equal z goes behind every existing one**, so a tie
#   is won by the older window (`ewidgetdesktop.cpp:8-27`, immediate composition,
#   which is what the receiver runs).
# - A root window starts hidden and a child widget starts shown
#   (`ewidget.cpp:8, 16`).
# - `eListbox` binds `ListboxActions` at priority 0 **in its constructor**, with
#   no action map and no exec (`elistbox.cpp:26, 95`); `eLabel`, `ePixmap` and
#   `eWindow` bind nothing. `KeyActionMap.native` records those bindings.
# - `eLabel::calculateSize()` lays the text out in the label's **current** size
#   (`elabel.cpp:234-247`). The glyph metrics below are a model, not the font
#   renderer: half the font size per character and 1.2 lines, which is enough to
#   make a longer text taller and a wider label shorter.
#
# Destruction is not modelled - a window here lives as long as the test - so
# what a test can see is whether a window is *visible*, which is also what a
# household sees.


class eSize:
    def __init__(self, width=0, height=0):
        self._width = int(width)
        self._height = int(height)

    def width(self):
        return self._width

    def height(self):
        return self._height

    def __eq__(self, other):
        return (self.width(), self.height()) == (other.width(), other.height())

    def __repr__(self):
        return f"eSize({self._width}, {self._height})"


class ePoint:
    def __init__(self, x=0, y=0):
        self._x = int(x)
        self._y = int(y)

    def x(self):
        return self._x

    def y(self):
        return self._y

    def __repr__(self):
        return f"ePoint({self._x}, {self._y})"


class eWidget:
    def __init__(self, parent):
        self.parent = parent
        self.children = []
        self._position = ePoint(0, 0)
        self._size = eSize(0, 0)
        self.z = 0
        self.attributes = {}
        self.visible = parent is not None
        if parent is not None:
            parent.children.append(self)

    def show(self):
        self.visible = True

    def hide(self):
        self.visible = False

    def isVisible(self):
        return self.visible and (self.parent is None or self.parent.isVisible())

    def move(self, point):
        self._position = point

    def resize(self, size):
        self._size = size

    def position(self):
        return self._position

    def size(self):
        return self._size

    def setZPosition(self, z):
        # A root window is not re-sorted by this: its place was decided when it
        # was added to the desktop.
        self.z = int(z)

    def setFont(self, font):
        self.font = font

    def widgets(self):
        """This widget and everything under it."""
        found = [self]
        for child in self.children:
            found.extend(child.widgets())
        return found


class eWindow(eWidget):
    def __init__(self, desktop, z=0):
        eWidget.__init__(self, None)
        self.z = int(z)
        self.animation_mode = 0x11
        self.title = ""
        self.desktop = desktop
        desktop.addRootWidget(self)

    def setAnimationMode(self, mode):
        self.animation_mode = mode

    def setTitle(self, title):
        self.title = title


class eLabel(eWidget):
    def __init__(self, parent):
        eWidget.__init__(self, parent)
        self.text = ""
        self.font = ("Regular", 20)

    def setText(self, text):
        self.text = text

    def calculateSize(self):
        size = self.font[1]
        glyph = max(1, size // 2)
        line = int(size * 1.2)
        per_line = max(1, self._size.width() // glyph)
        lines = 0
        longest = 0
        for part in str(self.text).split("\n"):
            lines += max(1, -(-len(part) // per_line))
            longest = max(longest, min(len(part), per_line))
        return eSize(longest * glyph, lines * line)


class ePixmap(eWidget):
    def setPixmap(self, pixmap):
        self.pixmap = pixmap


class eListbox(eWidget):
    def __init__(self, parent):
        eWidget.__init__(self, parent)
        # `allowNativeKeys(true)`: bound here, exec or no exec.
        KeyActionMap.getInstance().native.append(("ListboxActions", 0, self))


class Desktop:
    """`eWidgetDesktop`, as far as the order of its root windows goes."""

    def __init__(self, width=1920, height=1080):
        self._size = eSize(width, height)
        self.roots = []

    def size(self):
        return self._size

    def resize(self, size):
        self._size = size

    def addRootWidget(self, root):
        for index, existing in enumerate(self.roots):
            if existing.z < root.z:
                self.roots.insert(index, root)
                return
        self.roots.append(root)

    def removeRootWidget(self, root):
        if root in self.roots:
            self.roots.remove(root)

    def front_to_back(self):
        """The visible root windows, the frontmost first."""
        return [root for root in self.roots if root.visible]


DESKTOP = Desktop()


def getDesktop(which):
    # Only the main frame buffer is modelled; the front display is another desktop.
    assert which == 0
    return DESKTOP


enigma.eSize = eSize
enigma.ePoint = ePoint
enigma.eWidget = eWidget
enigma.eWindow = eWindow
enigma.eLabel = eLabel
enigma.ePixmap = ePixmap
enigma.eListbox = eListbox
enigma.getDesktop = getDesktop


# ------------------------------------------------------- services and references --


class eServiceReference:
    """The string form is the reference; everything else is derived from it.

    The real class carries the fields as members and rebuilds the string on
    demand. Nothing in this plugin depends on that, and everything in it depends
    on the string round-tripping unchanged - including the flags in the second
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
        # {sref: [Event, ...]}, in time order.
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
        # The bindings a C++ widget makes for itself, with no action map: see
        # `eListbox` above.
        self.native = []

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
        a test proves that `cmd/key` reaches the plugin's own listener - exactly
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
        self.dataAvail = []
        self.stdoutAvail = []
        self.stderrAvail = []
        self.commands = []
        self.rejects = False
        ConsoleAppContainer.instances.append(self)

    def execute(self, command, *arguments):
        self.commands.append(command)
        return 1 if self.rejects else 0

    def send(self, data, stream="stdout"):
        """Output, as the image delivers it: every chunk on `dataAvail`, and again
        on `stdoutAvail` or `stderrAvail` (OpenViX `lib/base/console.cpp`)."""
        for function in list(self.dataAvail):
            function(data)
        for function in list(self.stdoutAvail if stream == "stdout" else self.stderrAvail):
            function(data)

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


# 🔴 The value classes below follow OpenViX 6.6's `Components/config.pyc`, read
# from the receiver's bytecode, in the two respects a plugin can trip over:
#
# - **A value is stored as it is given.** `ConfigElement.setValue` keeps the
#   object it was handed; `ConfigBoolean` (and so `ConfigYesNo`) does not
#   override it, `ConfigInteger` keeps `[value]` and hands `value` back, and
#   `ConfigText` keeps its `text` as given, `None` included. Nothing turns `1`
#   into `True` or `"5"` into `5`. These stubs used to, and a stub that
#   converts is a stub under which a plugin that writes the wrong type passes:
#   on the box the wrong type is what stays in memory until the next start
#   reads the settings file back.
# - **Notifiers are called only on a change.** `setValue` compares the old
#   value with the new one (`ConfigText` before storing, `ConfigSelection` by
#   the `str()` of the choice, `ConfigInteger` by the `str()` of the
#   one-element list it keeps) and calls `changed()` only when they differ;
#   `addNotifier` calls the new notifier at once unless told not to. So on an
#   integer `5` then `"5"` is a change (`[5]` against `['5']`), while on a
#   selection `2` then `"2"` is not.
#
# `ConfigSelection` never raises either: a value that is not one of its
# choices is replaced by the default, silently.
#
# Two things are deliberately left simpler than the image: `save()` keeps the
# value itself rather than its `tostring()` (and keeps it even when it equals
# the default), and `ConfigLocations` keeps a list of strings rather than the
# image's mount-point records. Neither is something the plugin writes through.
class ConfigElement:
    def __init__(self, default=None):
        self.default = default
        self._value = default
        self.saved_value = default
        self.save_calls = 0
        self.cancel_calls = 0
        self.notifiers = []
        self.notifiers_final = []

    def getValue(self):
        return self._value

    def setValue(self, value):
        previous = self._value
        self._value = value
        if previous != value:
            self.changed()

    # The image binds the property to the class's own accessors, and every
    # subclass that replaces one of them builds its own property again.
    value = property(getValue, setValue)

    def changed(self):
        for notifier in list(self.notifiers):
            notifier(self)

    def addNotifier(self, notifier, initial_call=True, immediate_feedback=True):
        if immediate_feedback:
            self.notifiers.append(notifier)
        else:
            self.notifiers_final.append(notifier)
        if initial_call:
            notifier(self)

    def removeNotifier(self, notifier):
        if notifier in self.notifiers:
            self.notifiers.remove(notifier)
        if notifier in self.notifiers_final:
            self.notifiers_final.remove(notifier)

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

    def setValue(self, value):
        # The image compares first and stores only a different value.
        if value != self._value:
            self._value = value
            self.changed()

    value = property(ConfigElement.getValue, setValue)


class ConfigPassword(ConfigText):
    pass


class ConfigInteger(ConfigElement):
    def __init__(self, default=0, limits=(0, 9999)):
        ConfigElement.__init__(self, default)
        self.limits = limits

    def setValue(self, value):
        # The image keeps `[value]` and compares the `str()` of that list, so
        # `5` and `"5"` differ (`[5]` against `['5']`) and the change notifies.
        previous = str([self._value])
        self._value = value
        if str([self._value]) != previous:
            self.changed()

    value = property(ConfigElement.getValue, setValue)


# In enigma2 ConfigYesNo is a ConfigBoolean, not a ConfigSelection, and a
# ConfigBoolean inherits `setValue`: it holds whatever it was last given. Only a
# start, which reads the settings file back, turns the value into a real bool.
class ConfigYesNo(ConfigElement):
    def __init__(self, default=False):
        ConfigElement.__init__(self, default)


class ConfigLocations(ConfigElement):
    """A list of **absolute paths**, which is what enigma2's own element holds.

    🔴 This stub was a `ConfigText` of bare names, and the plugin's own guards
    then agreed with it: the receiver stores `/usr/softcams/<name>`, the image's
    manager strips that prefix in its first line, and a plugin tested against
    basenames threw the only entry a real box has away before it looked at it.
    „Each side mocks the other", with the plugin mocking the receiver.
    """

    def __init__(self, default=None, visible_width=False):
        ConfigElement.__init__(self, list(default or []))

    def setValue(self, value):
        if isinstance(value, (list, tuple)):
            locations = [str(item) for item in value]
        else:
            locations = [str(value)] if value else []
        if locations != self._value:
            self._value = locations
            self.changed()

    value = property(ConfigElement.getValue, setValue)


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

    def setValue(self, value):
        # Matched by `str()`, as the image's `choicesList.index` does, and the
        # choice's own key is what gets stored; anything else becomes the default.
        previous = str(self._value)
        keys = [key for key, _label in self.choices]
        texts = [str(key) for key in keys]
        if str(value) in texts:
            self._value = keys[texts.index(str(value))]
        else:
            self._value = self.default
        if str(self._value) != previous:
            self.changed()

    value = property(ConfigElement.getValue, setValue)


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

    def increment(self):
        # What the image's `Standby` does, and the change is what notifies.
        self.value += 1


config = ConfigSubsection()
config.plugins = ConfigSubsection()
config.misc = ConfigSubsection()
config.misc.standbyCounter = StandbyCounter()

# The image's own softcam manager, which is not this plugin's configuration and
# is only ever read. `softcams` selects between „the manager starts the bare
# binary" (`None`, and the only choice the measured image offers) and „an init
# script does", and the plugin behaves very differently in the two shapes.
config.misc.softcams = ConfigSelection(choices=["None", "oscam"], default="None")
config.softcammanager = ConfigSubsection()
config.softcammanager.softcams_autostart = ConfigLocations(default=[])
config.softcammanager.softcamtimerenabled = ConfigYesNo(default=False)
config.softcammanager.softcamtimer = ConfigInteger(default=6)

configfile = ConfigFile()

config_module.ConfigElement = ConfigElement
config_module.ConfigText = ConfigText
config_module.ConfigPassword = ConfigPassword
config_module.ConfigInteger = ConfigInteger
config_module.ConfigYesNo = ConfigYesNo
config_module.ConfigLocations = ConfigLocations
config_module.ConfigSelection = ConfigSelection
config_module.ConfigSubsection = ConfigSubsection
config_module.config = config
config_module.configfile = configfile
config_module.getConfigListEntry = getConfigListEntry


# ------------------------------------------------------- Components.SystemInfo --

system_info_module = _module("Components.SystemInfo")


class SystemInformation(dict):
    """The image's `SystemInfo`: a dict whose `get` answers for a key never set.

    Shaped after OpenViX 6.6's bytecode, read from a receiver: a `dict`
    subclass whose `get(item, default=None)` delegates to `BoxInfo`, and in
    which `WakeOnLAN` is always set - to the path of `/proc/stb/fp/wol` (or
    `/proc/stb/power/wol` on two machines) when the driver created it, and to
    `False` otherwise, which is what that receiver holds. `config.usage` is
    left as the rest of this file has it, with no `wakeOnLAN`, because the
    image builds that element only when the file exists.
    """


SystemInfo = SystemInformation()
SystemInfo["WakeOnLAN"] = False
system_info_module.SystemInfo = SystemInfo
components.SystemInfo = system_info_module


# ---------------------------------------------- Components / Screens / Plugins --

action_map_module = _module("Components.ActionMap")


class ActionMap:
    """Binds nothing until executed, which is the real one's rule and the toast's premise."""

    def __init__(self, contexts, actions=None, prio=0):
        self.contexts = contexts
        self.actions = actions or {}
        self.prio = prio

    def execBegin(self):
        pass

    def execEnd(self):
        pass

    def destroy(self):
        pass


action_map_module.ActionMap = ActionMap
action_map_module.NumberActionMap = ActionMap

config_list_module = _module("Components.ConfigList")


class ConfigList:
    def __init__(self, entries=None):
        self.list = list(entries or [])

    def destroy(self):
        pass


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


# ------------------------------------------- GUI components, sources and screens --
#
# 🔴 Modelled on the receiver's own `Components/GUIComponent.pyc`, `Label.pyc`,
# `Pixmap.pyc`, `MenuList.pyc`, `Sources/Source.pyc`, `Sources/StaticText.pyc` and
# `Screens/Screen.pyc`, each of which compiles byte for byte from OpenViX
# `cd4f9bc4ee` (every code object compared), and on `StartEnigma.py`, which the
# image ships as source and which is identical to that commit's. The details the
# discreet toast depends on:
#
# - A `GUIComponent` has no widget until `GUIcreate(parent)` makes one from its
#   `GUI_WIDGET`, and `destroy()` clears its `__dict__`.
# - `Label` is a `GUIComponent` whose widget is `eLabel`; `getSize()` is the
#   widget's `calculateSize()`. `Pixmap` is `ePixmap`. `MenuList` is `eListbox`,
#   which is the whole reason it may not be in the toast.
# - `StaticText` is a **source**, not a `GUIComponent`. `Screen.__init__` puts three
#   of them into every screen (`Title`, `ScreenPath`, `title`).
# - `Screen` is a `dict`. `show()` returns early when the screen is already shown
#   and has been shown once, or has no window; `hide()` when it is not shown.
#   `doClose()` hides, runs `onClose`, destroys every item and then sets **every
#   attribute to `None`**. `close()` on a screen that is not executing only
#   records `close_on_next_exec`.
# - `applySkin()` takes the `zPosition` from the skin, creates the `eWindow` with
#   it, applies the rest, and creates every component's widget in the window.
#   `setAnimationMode` passes through to the window.
#
# `setTitle` and `setImage` are simplified: the first records the title, the
# second is not modelled (an image the skin names for a screen would add a
# `Pixmap`, which the toast's widget rule allows anyway).

gui_component_module = _module("Components.GUIComponent")


class GUIComponent:
    def __init__(self):
        self.instance = None
        self.onVisibilityChange = []
        self.skinAttributes = []
        self.deprecationInfo = None

    def execBegin(self):
        pass

    def execEnd(self):
        pass

    def onShow(self):
        pass

    def onHide(self):
        pass

    def destroy(self):
        self.__dict__.clear()

    def applySkin(self, desktop, parent):
        if self.skinAttributes:
            skin_module.applyAllAttributes(self.instance, desktop, self.skinAttributes,
                                           parent.scale)
            return True
        return False

    def move(self, x, y=None):
        if y is None:
            self.instance.move(x)
        else:
            self.instance.move(ePoint(int(x), int(y)))

    def resize(self, x, y=None):
        self.width = x
        self.height = y
        if y is None:
            self.instance.resize(x)
        else:
            self.instance.resize(eSize(int(x), int(y)))

    def GUIcreate(self, parent):
        self.instance = self.createWidget(parent)
        self.postWidgetCreate(self.instance)

    def GUIdelete(self):
        self.preWidgetRemove(self.instance)
        self.instance = None

    def createWidget(self, parent):
        return self.GUI_WIDGET(parent)

    def postWidgetCreate(self, instance):
        pass

    def preWidgetRemove(self, instance):
        pass


gui_component_module.GUIComponent = GUIComponent

label_module = _module("Components.Label")


class Label(GUIComponent):
    GUI_WIDGET = eLabel

    def __init__(self, text=""):
        GUIComponent.__init__(self)
        self.message = ""
        self.onChanged = []
        self.setText(text)

    def setText(self, text):
        self.message = text
        if self.instance:
            self.instance.setText(self.message or "")
        for function in self.onChanged:
            function()

    def getText(self):
        return self.message

    text = property(getText, setText)

    def postWidgetCreate(self, instance):
        instance.setText(str(self.message) or "")

    def getSize(self):
        size = self.instance.calculateSize()
        return size.width(), size.height()


label_module.Label = Label

pixmap_module = _module("Components.Pixmap")


class Pixmap(GUIComponent):
    GUI_WIDGET = ePixmap


pixmap_module.Pixmap = Pixmap

menu_list_module = _module("Components.MenuList")


class MenuList(GUIComponent):
    GUI_WIDGET = eListbox

    def __init__(self, entries=None, enableWrapAround=True, content=None):
        GUIComponent.__init__(self)
        self.list = list(entries or [])


menu_list_module.MenuList = MenuList

source_module = _module("Components.Sources.Source")


class Source:
    def __init__(self):
        self.downstream_elements = []

    def execBegin(self):
        pass

    def execEnd(self):
        pass

    def onShow(self):
        pass

    def onHide(self):
        pass

    def destroy(self):
        pass


source_module.Source = Source

static_text_module = _module("Components.Sources.StaticText")


class StaticText(Source):
    def __init__(self, text=""):
        Source.__init__(self)
        self.text = text

    def setText(self, text):
        self.text = text

    def getText(self):
        return self.text


static_text_module.StaticText = StaticText

screens = _module("Screens", package=True)
screen_module = _module("Screens.Screen")


class Screen(dict):
    def __init__(self, session, parent=None, mandatoryWidgets=None):
        dict.__init__(self)
        self.skinName = self.__class__.__name__
        self.session = session
        self.parent = parent
        self.mandatoryWidgets = mandatoryWidgets
        self.onClose = []
        self.onFirstExecBegin = []
        self.onExecBegin = []
        self.onExecEnd = []
        self.onLayoutFinish = []
        self.onShown = []
        self.onShow = []
        self.onHide = []
        self.execing = False
        self.shown = True
        self.already_shown = False
        self.renderer = []
        self.close_on_next_exec = None
        self.stand_alone = False
        self.desktop = None
        self.instance = None
        self.title = ""
        # Not the image's: what the setup-screen tests read back.
        self.closed_with = None
        self["Title"] = StaticText()
        self["ScreenPath"] = StaticText()
        self["title"] = StaticText()

    @property
    def widgets(self):
        """The screen's items. The setup-screen tests predate the dict model."""
        return self

    def setTitle(self, title):
        self.title = title

    def close(self, *retval):
        self.closed_with = retval
        if not self.execing:
            self.close_on_next_exec = retval
        else:
            self.session.close(self, *retval)

    def show(self):
        if (self.shown and self.already_shown) or not self.instance:
            return
        self.shown = True
        self.already_shown = True
        self.instance.show()
        for function in self.onShow:
            function()
        for value in list(self.values()) + self.renderer:
            if isinstance(value, (GUIComponent, Source)):
                value.onShow()

    def hide(self):
        if not self.shown or not self.instance:
            return
        self.shown = False
        self.instance.hide()
        for function in self.onHide:
            function()
        for value in list(self.values()) + self.renderer:
            if isinstance(value, (GUIComponent, Source)):
                value.onHide()

    def doClose(self):
        self.hide()
        for function in self.onClose:
            function()
        self.deleteGUIScreen()
        del self.session
        for name, value in list(self.items()):
            value.destroy()
            del self[name]
        self.renderer = []
        for name in self.__dict__:
            setattr(self, name, None)

    def setDesktop(self, desktop):
        self.desktop = desktop

    def setAnimationMode(self, mode):
        if self.instance:
            self.instance.setAnimationMode(mode)

    def applySkin(self):
        z_position = 0
        for key, value in self.skinAttributes:
            if key == "zPosition":
                z_position = int(value)
        self.scale = ((720, 720), (576, 576))
        if not self.instance:
            self.instance = eWindow(self.desktop, z_position)
        skin_module.applyAllAttributes(self.instance, self.desktop, self.skinAttributes,
                                       self.scale)
        self.createGUIScreen(self.instance, self.desktop)

    def createGUIScreen(self, parent, desktop):
        for key in self:
            value = self[key]
            if isinstance(value, GUIComponent):
                value.GUIcreate(parent)
                value.applySkin(desktop, self)
        for function in self.onLayoutFinish:
            function()

    def deleteGUIScreen(self):
        for _name, value in list(self.items()):
            if isinstance(value, GUIComponent):
                value.GUIdelete()


screen_module.Screen = Screen


# ------------------------------------------------------------------------ skin --
#
# 🔴 From the receiver's `skin.pyc`, which compiles from `cd4f9bc4ee` except for
# three `assert`s the image strips. `addOnLoadCallback`/`removeOnLoadCallback` are
# membership-guarded appends and removes on the module's `onLoadCallbacks`, and
# `InitSkins(booting=False)` - a skin reload without a restart - resizes the
# desktop **before** it calls every callback (a test resizes `DESKTOP` first).
# `readSkin` prefers a screen the skin defines under the screen's `skinName`,
# falls back to the embedded `skin` string, and refuses a named widget the screen
# does not have.
#
# The model reads plain integers only for `position` and `size`: the toast
# promises to format them itself, so a coordinate expression here is a failure.

skin_module = _module("skin")
skin_module.GUI_SKIN_ID = 0
skin_module.onLoadCallbacks = []
# name -> the XML of a screen the active skin defines.
skin_module.domScreens = {}


class SkinError(Exception):
    pass


def addOnLoadCallback(callback):
    if callback not in skin_module.onLoadCallbacks:
        skin_module.onLoadCallbacks.append(callback)


def removeOnLoadCallback(callback):
    if callback in skin_module.onLoadCallbacks:
        skin_module.onLoadCallbacks.remove(callback)


def InitSkins(booting=True):
    if not booting:
        for method in skin_module.onLoadCallbacks:
            if method:
                method()


def _pair(value):
    first, second = str(value).split(",")
    return int(first), int(second)


def applyAllAttributes(guiObject, desktop, attributes, scale):
    for attribute, value in attributes:
        if attribute == "position":
            guiObject.move(ePoint(*_pair(value)))
        elif attribute == "size":
            guiObject.resize(eSize(*_pair(value)))
        elif attribute == "font":
            face, size = str(value).split(";")
            guiObject.setFont((face, int(size)))
        elif attribute == "zPosition":
            guiObject.setZPosition(int(value))
        else:
            guiObject.attributes[attribute] = value


def readSkin(screen, skin, names, desktop):
    import xml.etree.ElementTree as ElementTree

    if not isinstance(names, list):
        names = [names]
    source = None
    for name in names:
        if name in skin_module.domScreens:
            source = skin_module.domScreens[name]
            break
    if source is None:
        source = getattr(screen, "skin", None) or "<screen></screen>"
    element = ElementTree.fromstring(source)
    screen.skinAttributes = [
        (key, value) for key, value in element.attrib.items() if key != "name"
    ]
    screen.additionalWidgets = []
    screen.renderer = []
    for widget in element:
        if widget.tag != "widget" or "name" not in widget.attrib:
            raise SkinError(f"the model reads named widgets only, not <{widget.tag}>")
        name = widget.attrib["name"]
        if name not in screen:
            raise SkinError(f"Component with name '{name}' was not found in skin")
        screen[name].skinAttributes = [
            (key, value) for key, value in widget.attrib.items() if key != "name"
        ]


skin_module.SkinError = SkinError
skin_module.addOnLoadCallback = addOnLoadCallback
skin_module.removeOnLoadCallback = removeOnLoadCallback
skin_module.InitSkins = InitSkins
skin_module.applyAllAttributes = applyAllAttributes
skin_module.readSkin = readSkin

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


# 🔴 The queue is modelled on the receiver's own `Tools/Notifications.pyc`
# (OpenViX 6.6, disassembled under Python 3.12, which is what that image runs),
# not on what a queue ought to look like - the CEC workaround is tested against
# exactly these details and would pass against a kinder stub while doing nothing
# on a real box:
#
# - `notifications` and `notificationAdded` are **module-level lists** that are
#   only ever mutated in place (the info bar does `del notifications[0]`), so a
#   reference taken once stays the queue.
# - An entry is the five-tuple `(fnc, screen, args, kwargs, id)`, and
#   `AddNotification(screen)` queues `(None, screen, (), {}, None)`.
# - `__AddNotification` appends **first** and then calls every entry of
#   `notificationAdded` with **no arguments**, iterating the live list.
# - `RemovePopup(id)` skips every entry whose id is falsy - which is why a
#   queued standby cannot be removed through it.
notifications = []
notificationAdded = []
current_notifications = []


class Notifications:
    """What was put on the screen, and in which order.

    `notifications` is the module's queue itself, not a copy: the tests that
    predate the model read it from here.
    """

    popups = []
    removed = []
    notifications = notifications
    raises = False


def AddPopup(text, type=1, timeout=10, id=None):
    if Notifications.raises:
        raise RuntimeError("no screen to put it on")
    Notifications.popups.append({"text": text, "type": type, "timeout": timeout, "id": id})


def RemovePopup(id):
    Notifications.removed.append(id)
    for entry in list(notifications):
        if entry[4] and entry[4] == id:
            notifications.remove(entry)


def _add_notification(fnc, screen, id, *args, **kwargs):
    """`__AddNotification`: append, then tell every listener, with no arguments."""
    notifications.append((fnc, screen, args, kwargs, id))
    for listener in notificationAdded:
        listener()


def AddNotificationWithCallback(fnc, screen, *args, **kwargs):
    _add_notification(fnc, screen, None, *args, **kwargs)


def AddNotification(screen, *args, **kwargs):
    AddNotificationWithCallback(None, screen, *args, **kwargs)


def AddNotificationWithID(id, screen, *args, **kwargs):
    _add_notification(None, screen, id, *args, **kwargs)


notifications_module.notifications = notifications
notifications_module.notificationAdded = notificationAdded
notifications_module.current_notifications = current_notifications
notifications_module.AddPopup = AddPopup
notifications_module.RemovePopup = RemovePopup
notifications_module.AddNotification = AddNotification
notifications_module.AddNotificationWithCallback = AddNotificationWithCallback
notifications_module.AddNotificationWithID = AddNotificationWithID
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


class RestoringStandbyScreen:
    """The standby screen as a zap from standby meets it, closing a turn late.

    From `Screens/Standby.pyc` (`Standby2`) and `StartEnigma.py`: `Power()` is
    `self.close(True)`, and `Session.close` only starts a 0 ms timer - the screen
    is closed, and its `onClose` walked, on the next turn of the main loop
    (`finish_close` here). The first listener is the screen's own `__onClose`,
    appended in its constructor [Standby.py 70-...], which sets `inStandby` to
    None and `playService`s the service the box slept on [136-...]. `doClose`
    walks the list itself, not a copy.
    """

    def __init__(self, nav=None, restore=None):
        self.nav = nav
        self.restore = restore
        self.onClose = [self._on_close]
        self.power_calls = 0
        self.closing = False

    def Power(self):
        self.power_calls += 1
        self.closing = True

    def finish_close(self):
        for function in self.onClose:
            function()

    def _on_close(self):
        standby_module.inStandby = None
        if self.nav is not None and self.restore is not None:
            self.nav.playService(eServiceReference(self.restore))


# ------------------------------------------------- the session, as StartEnigma --
#
# 🔴 `ModalSession` and `ModelScreen` follow `/usr/lib/enigma2/python/StartEnigma.py`
# and `Screens/Screen.pyc` on the receiver (StartEnigma is the one file that image
# ships as source). The details the CEC workaround depends on, each read there:
#
# - `Session.close(screen)` returns silently when `in_exec` is false, **asserts**
#   `screen == current_dialog`, then starts `delay_timer` at 0 ms single-shot and
#   calls `execEnd()`. It does **not** pop the dialog: `processDelay` does, on the
#   turn that timer fires, and it is that pop - `execBegin(first=False)` on the
#   screen underneath - which lets the info bar drain the notification queue.
# - `execDialog` (how the info bar shows the channel list) makes a non-temporary
#   dialog; `open` makes a temporary one and refuses a modal open from a screen
#   that is not executing.
# - `Screen.close()` hands itself to `session.close` only while `execing`, and
#   otherwise remembers the close for the next `execBegin`.
# - `Screen.execBegin` runs `onExecBegin` and then the one-off `onFirstExecBegin`,
#   returns early if one of them opened another dialog, and only then sets
#   `execing` - so the info bar is *not* executing while it drains its queue.


class ModelScreen:
    def __init__(self, session, *args, **kwargs):
        self.session = session
        self.execing = False
        self.onExecBegin = []
        self.onFirstExecBegin = []
        self.onClose = []
        self.shown = True
        self.returnValue = None
        self.callback = None
        self.isTmp = False
        self.close_on_next_exec = None
        self.stand_alone = False

    def execBegin(self):
        if self.close_on_next_exec is not None:
            pending = self.close_on_next_exec
            self.close_on_next_exec = None
            self.execing = True
            self.close(*pending)
            return
        single = self.onFirstExecBegin
        self.onFirstExecBegin = []
        for function in self.onExecBegin + single:
            function()
            if not self.stand_alone and self.session.current_dialog != self:
                return
        self.execing = True

    def execEnd(self):
        self.execing = False

    def doClose(self):
        for function in list(self.onClose):
            function()

    def close(self, *retval):
        if not self.execing:
            self.close_on_next_exec = retval
            return
        self.session.close(self, *retval)


class ModalSession:
    """`StartEnigma.Session`: a dialog stack, one dialog executing, deferred pops."""

    def __init__(self, nav=None):
        self.nav = nav
        self.delay_timer = enigma.eTimer()
        self.delay_timer.callback.append(self.processDelay)
        self.current_dialog = None
        self.dialog_stack = []
        self.in_exec = False

    def processDelay(self):
        callback = self.current_dialog.callback
        retval = self.current_dialog.returnValue
        if self.current_dialog.isTmp:
            self.current_dialog.doClose()
        else:
            self.current_dialog.callback = None
        self.popCurrent()
        if callback is not None:
            callback(*retval)

    def execBegin(self, first=True, do_show=True):
        assert not self.in_exec
        self.in_exec = True
        self.current_dialog.execBegin()

    def execEnd(self, last=True):
        assert self.in_exec
        self.in_exec = False
        self.current_dialog.execEnd()

    def instantiateDialog(self, screen, *arguments, **kwargs):
        return screen(self, *arguments, **kwargs)

    def pushCurrent(self):
        if self.current_dialog is not None:
            self.dialog_stack.append((self.current_dialog, self.current_dialog.shown))
            self.execEnd(last=False)

    def popCurrent(self):
        if self.dialog_stack:
            (self.current_dialog, do_show) = self.dialog_stack.pop()
            self.execBegin(first=False, do_show=do_show)
        else:
            self.current_dialog = None

    def execDialog(self, dialog):
        self.pushCurrent()
        self.current_dialog = dialog
        self.current_dialog.isTmp = False
        self.current_dialog.callback = None
        self.execBegin()

    def open(self, screen, *arguments, **kwargs):
        if self.dialog_stack and not self.in_exec:
            raise RuntimeError("Modal open are allowed only from a screen which is modal!")
        self.pushCurrent()
        dialog = self.current_dialog = self.instantiateDialog(screen, *arguments, **kwargs)
        dialog.isTmp = True
        dialog.callback = None
        self.execBegin()
        return dialog

    def openWithCallback(self, callback, screen, *arguments, **kwargs):
        dialog = self.open(screen, *arguments, **kwargs)
        dialog.callback = callback
        return dialog

    def close(self, screen, *retval):
        if not self.in_exec:
            return
        assert screen == self.current_dialog
        self.current_dialog.returnValue = retval
        self.delay_timer.start(0, 1)
        self.execEnd()


class NotifiableInfoBar(ModelScreen):
    """The info bar's `InfoBarNotifications` mixin - the only thing that drains the queue.

    From `Screens/InfoBarGenerics.pyc`: it registers `checkNotificationsIfExecing`
    on `notificationAdded` (which does nothing unless the info bar is executing)
    and `checkNotifications` on `onExecBegin`, and `checkNotifications` takes
    **one** entry off the front of the queue per call and opens its screen.
    """

    def __init__(self, session, *args, **kwargs):
        ModelScreen.__init__(self, session)
        self.onExecBegin.append(self.checkNotifications)
        notificationAdded.append(self.checkNotificationsIfExecing)
        self.onClose.append(self._remove_notification)

    def _remove_notification(self):
        if self.checkNotificationsIfExecing in notificationAdded:
            notificationAdded.remove(self.checkNotificationsIfExecing)

    def checkNotificationsIfExecing(self):
        if self.execing:
            self.checkNotifications()

    def checkNotifications(self):
        if notifications:
            entry = notifications[0]
            del notifications[0]
            callback = entry[0]
            if callback:
                self.session.openWithCallback(callback, entry[1], *entry[2], **entry[3])
            else:
                self.session.open(entry[1], *entry[2], **entry[3])


class Standby(ModelScreen):
    """The class enigma2 hands to `AddNotification` to *enter* standby.

    As `Screens/Standby.pyc` has it: on its first `execBegin` the screen becomes
    `inStandby` and increments `config.misc.standbyCounter` - which is the
    moment everything listening for „the receiver entered standby" hears it,
    `HdmiCec` included.
    """

    def __init__(self, session=None, *args, **kwargs):
        ModelScreen.__init__(self, session)
        self.power_calls = 0
        self.onFirstExecBegin.append(self._first_exec)

    def _first_exec(self):
        standby_module.inStandby = self
        config.misc.standbyCounter.increment()

    def Power(self):
        self.power_calls += 1
        standby_module.inStandby = None
        for function in list(self.onClose):
            function()


class TryQuitMainloop:
    """The screen that shuts the box down; its argument is what kind of down."""


standby_module.inStandby = None
# A module-level boolean on the receiver's `Screens/Standby.pyc`, set while the
# shutdown screen is on its way out of the main loop.
standby_module.inTryQuitMainloop = False
standby_module.Standby = Standby
standby_module.StandbyScreen = StandbyScreen
standby_module.TryQuitMainloop = TryQuitMainloop


# --------------------------------------------------------- Components.HdmiCec --
#
# 🔴 Modelled on the receiver's `Components/HdmiCec.pyc` (source lines in
# brackets), and only as far as the standby path goes:
#
# - `HdmiCec.instance` is a **class attribute**, set in `__init__` [342]; the
#   image builds the singleton once, from `StartEnigma.py`. Until then it is None.
# - `__init__` registers `onEnterStandby` on `config.misc.standbyCounter` with
#   `initial_call=False` [368], and starts with `useStandby = True` and
#   `handlingStandbyFromTV = False` [353-354].
# - `messageReceived` does nothing unless `config.hdmicec.enabled` is on [385],
#   and for `<Standby>` (0x36) with `handle_tv_standby` on it is exactly
#   `handlingStandbyFromTV = True; self.standby(); ... = False` [454-457]. With
#   either setting off the television's standby is never queued at all. The
#   singleton is built either way: `__init__` sets `instance` [342] before it
#   looks at `enabled` [355].
# - The flag is read only as a truth test [618], never compared with `True`.
# - `standby()` queues `AddNotification(Screens.Standby.Standby)` unless already
#   in standby [635-637].
# - `onEnterStandby` appends to the standby screen's `onClose` and calls
#   `standbyMessages` [598-601]; that sends at once, unless `next_boxes_detect` is
#   on, in which case it waits a second on its own timer [607-613].
# - `sendStandbyMessages` sends `standby` to the television when `useStandby and
#   not handlingStandbyFromTV`, and `sourceinactive` otherwise [615-622] - the
#   flag is read there and nowhere else. `sent` records what went out.

config.hdmicec = ConfigSubsection()
config.hdmicec.enabled = ConfigYesNo(default=True)
config.hdmicec.handle_tv_standby = ConfigYesNo(default=True)
config.hdmicec.control_tv_standby = ConfigYesNo(default=True)
config.hdmicec.next_boxes_detect = ConfigYesNo(default=False)

hdmi_cec_module = _module("Components.HdmiCec")

STANDBY_OPCODE = 0x36


class HdmiCec:
    instance = None

    def __init__(self):
        HdmiCec.instance = self
        self.useStandby = True
        self.handlingStandbyFromTV = False
        self.sent = []
        self.delay = enigma.eTimer()
        self.delay.callback.append(self.sendStandbyMessages)
        config.misc.standbyCounter.addNotifier(self.onEnterStandby, initial_call=False)

    def messageReceived(self, cmd):
        if not config.hdmicec.enabled.value:
            return
        if cmd == STANDBY_OPCODE and config.hdmicec.handle_tv_standby.value:
            self.handlingStandbyFromTV = True
            self.standby()
            self.handlingStandbyFromTV = False

    def standby(self):
        if not standby_module.inStandby:
            notifications_module.AddNotification(standby_module.Standby)

    def onEnterStandby(self, configelement=None):
        standby_module.inStandby.onClose.append(self.onLeaveStandby)
        self.standbyMessages()

    def onLeaveStandby(self):
        pass

    def standbyMessages(self):
        if config.hdmicec.enabled.value:
            if config.hdmicec.next_boxes_detect.value:
                self.delay.start(1000, True)
            else:
                self.sendStandbyMessages()

    def sendStandbyMessages(self):
        if config.hdmicec.control_tv_standby.value:
            if self.useStandby and not self.handlingStandbyFromTV:
                self.sent.append("standby")
            else:
                self.sent.append("sourceinactive")
                self.useStandby = True


hdmi_cec_module.HdmiCec = HdmiCec


infobar_module = _module("Screens.InfoBar")


# 🔴 `InfoBar` and `ChannelList` below model the receiver's own
# `Screens/InfoBarGenerics.pyc` and `Screens/ChannelSelection.pyc` (OpenViX
# 6.6.007, disassembled with the image's own CPython 3.12; source lines in
# brackets) as far as the zap history goes:
#
# - `InfoBarNumberZap.selectAndStartService(service, bouquet)` [1255-1265]: when
#   `servicelist.getRoot() != bouquet`, `clearPath()`, then `enterPath
#   (bouquet_root)` unless the bouquet is the root, then `enterPath(bouquet)`;
#   `setCurrentSelection(service)`; `zap(enable_pipzap=True)`;
#   `correctChannelNumber()`; `startRoot = None`.
# - `ChannelSelection.addToHistory(ref)` [2372-2389]: only with a `servicePath`;
#   appends the path plus the service, deletes every *older* entry for the same
#   service, drops the oldest past `HISTORYSIZE` (20), and points `history_pos`
#   at the newest.
# - `historyMenuClosed(ref)` [2451-2465]: the entry whose last element equals
#   `ref` moves to the end and `setHistoryPath()` plays it - unless it is
#   already at `history_pos`, when nothing at all happens.
# - `setHistoryPath()` [2407-2423]: the entry's path becomes `servicePath`, the
#   root follows, and `session.nav.playService(ref, adjust=False)` - no
#   timeshift check on this path.
# - `ChannelSelection.zap` records through `addToHistory` unless the service is
#   `startServiceRef`; the stub tunes by setting `nav.sref`, never through
#   `playService`, because other tests count `playService` as the unrecorded path.

HISTORYSIZE = 20


class _ZapBlock:
    """`pts_blockZap_timer`, as far as the 0 key asks it."""

    def __init__(self):
        self.active = False

    def isActive(self):
        return self.active


class InfoBar:
    """The info bar, with the number-zap and timeshift mixins the 0 key runs through.

    Beyond `selectAndStartService` (above), from the same bytecode:

    - `InfoBarNumberZap.keyNumberGlobal(0)` [1136-1157]: nothing while
      `pts_blockZap_timer` is active; with `pipHandles0Action()` true, the PiP
      action and nothing else; with **more than one** history entry,
      `checkTimeshiftRunning(recallPrevService)`; otherwise nothing at all.
      (The timeshift seek-pointer branch before these is not modelled: the
      plugin refuses in timeshift before it gets there.)
    - `InfoBarTimeshift.checkTimeshiftRunning(fn)` [Timeshift.py 457-495]: in
      timeshift, or with a timeshift waiting to be saved, opens a question with
      no timeout (recorded in `questions`); otherwise `fn(True)`.
    - `recallPrevService(True)` with `config.usage.panicbutton` on [1165-1215]:
      the history is **replaced** by a new empty list, `history_pos = 0`, and
      channel 1 is started through `selectAndStartService` - which records it.
      With the setting off, the previous channel and nothing cleared.
    """

    instance = None

    def __init__(self, servicelist=None):
        self.servicelist = servicelist
        # Timeshift, as `checkTimeshiftRunning` reads it [Timeshift.py 457-495].
        self.seekable = False
        self.timeshift = False
        self.save_current_timeshift = False
        self.started = []
        self.pts_blockZap_timer = _ZapBlock()
        self.pip_zero = False
        self.pip_actions = 0
        self.questions = []
        self.keys = []
        # Channel 1: the first channel of the first bouquet.
        self.first_channel = (eServiceReference(TVP1), eServiceReference(FIRST_BOUQUET))
        # A defective panic, for the plugin's own check that the list emptied.
        self.panic_keeps = None

    def isSeekable(self):
        return self.seekable

    def timeshiftEnabled(self):
        return self.timeshift

    def pipHandles0Action(self):
        return self.pip_zero

    def pipDoHandle0Action(self):
        self.pip_actions += 1

    def keyNumberGlobal(self, number):
        self.keys.append(number)
        if self.pts_blockZap_timer.isActive():
            return
        if number == 0:
            if self.pipHandles0Action():
                self.pipDoHandle0Action()
                return
            if len(self.servicelist.history) > 1:
                self.checkTimeshiftRunning(self.recallPrevService)

    def checkTimeshiftRunning(self, returnFunction):
        if (self.isSeekable() and self.timeshiftEnabled()) or self.save_current_timeshift:
            self.questions.append(returnFunction)
            return
        returnFunction(True)

    def recallPrevService(self, reply):
        if not reply:
            return
        servicelist = self.servicelist
        if config.usage.panicbutton.value:
            servicelist.history_tv = []
            servicelist.history_radio = []
            servicelist.history = servicelist.history_tv
            servicelist.history_pos = 0
            service, bouquet = self.first_channel
            servicelist.clearPath()
            servicelist.enterPath(servicelist.bouquet_root)
            servicelist.enterPath(bouquet)
            servicelist.saveRoot()
            self.selectAndStartService(service, bouquet)
            if self.panic_keeps is not None:
                servicelist.history[:0] = self.panic_keeps
                servicelist.history_pos = len(servicelist.history) - 1
        elif len(servicelist.history) > 1:
            position = servicelist.history_pos
            other = position - 1 if position > 0 else position + 1
            history = servicelist.history
            history[position], history[other] = history[other], history[position]
            servicelist.setHistoryPath()

    def selectAndStartService(self, service, bouquet):
        self.started.append((service, bouquet))
        servicelist = self.servicelist
        if service:
            if servicelist.getRoot() != bouquet:
                servicelist.clearPath()
                if servicelist.bouquet_root != bouquet:
                    servicelist.enterPath(servicelist.bouquet_root)
                servicelist.enterPath(bouquet)
            servicelist.setCurrentSelection(service)
            servicelist.zap(enable_pipzap=True)
            servicelist.correctChannelNumber()
            servicelist.startRoot = None


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
        self.history = []
        self.history_pos = 0
        self.dopipzap = False
        self.startServiceRef = None
        self.startRoot = None
        self.corrected = 0
        self.history_paths = 0

    def setCurrentSelection(self, reference):
        wanted = getattr(reference, "reference", str(reference))
        if wanted in self.selectable:
            self.selection = wanted

    def getCurrentSelection(self):
        return None if self.selection is None else eServiceReference(self.selection)

    def zap(self, enable_pipzap=False, preview_zap=False, checkParentalControl=True, ref=None):
        self.zaps += 1
        if self.nav is not None and self.selection is not None:
            # ChannelSelection.zap owns the tune; do not model it as the
            # fallback Navigation.playService path whose use other tests detect.
            self.nav.sref = self.selection
            reference = eServiceReference(self.selection)
            if self.startServiceRef is None or reference != self.startServiceRef:
                self.addToHistory(reference)

    def addToHistory(self, ref):
        if self.servicePath is None:
            return
        entry = self.servicePath[:]
        entry.append(ref)
        self.history.append(entry)
        length = len(self.history)
        index = 0
        while index < length - 1:
            if self.history[index][-1] == ref:
                del self.history[index]
                length -= 1
            else:
                index += 1
        if length > HISTORYSIZE:
            del self.history[0]
            length -= 1
        self.history_pos = length - 1

    def historyMenuClosed(self, retval):
        if not retval:
            return
        length = len(self.history)
        position = 0
        for entry in self.history:
            if entry[-1] == retval:
                break
            position += 1
        if position < length and position != self.history_pos:
            entry = self.history[position]
            del self.history[position]
            self.history.append(entry)
            self.history_pos = len(self.history) - 1
            self.setHistoryPath()

    def setHistoryPath(self, doZap=True):
        self.history_paths += 1
        path = self.history[self.history_pos][:]
        ref = path.pop()
        del self.servicePath[:]
        self.servicePath += path
        self.saveRoot()
        root = path[-1]
        current = self.getRoot()
        if current and current != root:
            self.root = root
            services = self.bouquets.get(getattr(root, "reference", str(root)))
            if services is not None:
                self.selectable = list(services)
        if doZap:
            self.nav.playService(ref, adjust=False)
        self.setCurrentSelection(ref)

    def correctChannelNumber(self):
        self.corrected += 1

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


parental_module = _module("Components.ParentalControl")


class ParentalControl:
    """`Components/ParentalControl.pyc`, as far as `isProtected(ref)` goes."""

    def __init__(self):
        self.protected = set()
        self.raises = False

    def isProtected(self, ref):
        if self.raises:
            raise RuntimeError("the parental-control list could not be read")
        return getattr(ref, "reference", str(ref)) in self.protected


parental_module.parentalControl = ParentalControl()


class MoviePlayer(ModelScreen):
    """`Screens/InfoBar.pyc`'s player for recordings, the current dialog while one plays."""


infobar_module.InfoBar = InfoBar
infobar_module.ChannelList = ChannelList
infobar_module.MoviePlayer = MoviePlayer

channel_selection_module = _module("Screens.ChannelSelection")
channel_selection_module.service_types_tv = (
    "1:7:1:0:0:0:0:0:0:0:(type == 1) || (type == 17) || (type == 22)"
)
# A module constant on the receiver [ChannelSelection.py 2006].
channel_selection_module.HISTORYSIZE = HISTORYSIZE


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
    """paho's `MQTTMessageInfo`: `is_published()` is the broker's acknowledgement.

    As in paho, a QoS 0 publish counts as published once it is handed over, and
    a QoS 1 publish only when its PUBACK is in - which here is when a test says
    so, with `FakeMQTTClient.acknowledge()`. A rejected publish raises from
    `is_published()`, as paho's does.
    """

    def __init__(self, rc=0, qos=0):
        self.waited = None
        self.rc = rc
        self.qos = qos
        self.acknowledged = qos == 0

    def wait_for_publish(self, timeout=None):
        self.waited = timeout

    def is_published(self):
        if self.rc:
            raise RuntimeError("Message publish failed: rc=" + str(self.rc))
        return self.acknowledged


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
        self.infos = []
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
        info = FakeMessageInfo(rc=self.publish_rc, qos=qos)
        self.infos.append(info)
        return info

    def acknowledge(self, count=None):
        """The broker's PUBACKs, oldest first: `count` of them, or all outstanding."""
        waiting = [info for info in self.infos if not info.acknowledged]
        for info in waiting if count is None else waiting[:count]:
            info.acknowledged = True

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

    def playService(self, reference, checkParentalControl=True, forceRestart=False, adjust=True):
        # The receiver's signature, from `Navigation.pyc`: the channel list's
        # history zap calls it with `adjust=False`.
        self.sref = getattr(reference, "reference", str(reference))
        self.played.append(self.sref)

    def fire(self, event):
        for listener in list(self.event):
            listener(event)

    def fire_record(self, service=None, event=None):
        for listener in list(self.record_event):
            listener(service, event)


class Session:
    """`StartEnigma.Session` as far as the tests need it.

    `instantiateDialog` and `deleteDialog` are the real ones, from the source the
    image ships (`StartEnigma.py`, identical to OpenViX `cd4f9bc4ee`):
    construct, `readSkin`, `setDesktop`, `applySkin` - and never the dialog stack;
    and `hide()` then `doClose()`. `open` only records, and `execDialog` is not
    here, so a dialog handed to either is visible to a test.
    """

    def __init__(self, nav):
        self.nav = nav
        # The executing dialog, as `StartEnigma.Session` keeps it. The info bar
        # once `with_channel_list` has built one.
        self.current_dialog = None
        self.opened = []
        self.desktop = getDesktop(0)
        self.instantiated = []
        self.deleted = []

    def open(self, screen, *arguments):
        self.opened.append((screen, arguments))
        return screen

    def openWithCallback(self, callback, screen, *arguments):
        self.opened.append((screen, arguments))
        return screen

    def instantiateDialog(self, screen, *arguments, **kwargs):
        return self.doInstantiateDialog(screen, arguments, kwargs, self.desktop)

    def deleteDialog(self, screen):
        self.deleted.append(screen)
        screen.hide()
        screen.doClose()

    def doInstantiateDialog(self, screen, arguments, kwargs, desktop):
        dialog = screen(self, *arguments, **kwargs)
        if dialog is None:
            return None
        skin_module.readSkin(dialog, None, dialog.skinName, desktop)
        dialog.setDesktop(desktop)
        dialog.applySkin()
        self.instantiated.append(dialog)
        return dialog


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

    def enter_standby(self, restoring=False):
        """`restoring`: the screen closes a turn late and plays what the box slept on."""
        if restoring:
            standby_module.inStandby = RestoringStandbyScreen(self.nav, self.nav.sref)
        else:
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
        # StartEnigma opens the info bar as the session's first dialog.
        self.session.current_dialog = InfoBar.instance
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
    from MQTTBridge import service as service_module

    def reset():
        parental_module.parentalControl = ParentalControl()
        service_module.forget_unrecorded()
        if service_module._pending_wake is not None:
            service_module._pending_wake.cancel()
        service_module._pending_wake = None
        ServiceCenter._instance = None
        EPGCache._instance = None
        KeyActionMap._instance = None
        DVBVolumeControl._instance = None
        VolumeControl.instance = None
        InfoBar.instance = None
        standby_module.inStandby = None
        standby_module.inTryQuitMainloop = False
        DESKTOP.resize(eSize(1920, 1080))
        DESKTOP.roots = []
        del skin_module.onLoadCallbacks[:]
        skin_module.domScreens = {}
        ServiceReference.names = {}
        ConsoleAppContainer.instances = []
        eTimer.instances = []
        Notifications.popups = []
        Notifications.removed = []
        # Emptied in place: the queue is one list for the life of the image.
        del notifications[:]
        del notificationAdded[:]
        del current_notifications[:]
        Notifications.raises = False
        HdmiCec.instance = None
        config.hdmicec.enabled.value = True
        config.hdmicec.handle_tv_standby.value = True
        config.hdmicec.control_tv_standby.value = True
        config.hdmicec.next_boxes_detect.value = False
        MainLoop.now = 0
        record_timer_module.margin_before = 0
        record_timer_module.margin_after = 0
        counter = config.misc.standbyCounter
        counter.notifiers = []
        counter._value = 0
        config.misc.softcams.value = "None"
        config.softcammanager.softcams_autostart.value = []
        config.softcammanager.softcamtimerenabled.value = False
        config.softcammanager.softcamtimer.value = 6
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
def no_package_manager(tmp_path, monkeypatch):
    """The `uninstall` capability reads opkg's files: none here, unless a test builds them.

    Without this the capability would depend on the machine the suite runs on -
    a developer's box with an `/usr/bin/opkg` would claim it and every
    capability list asserted anywhere would change.
    """
    from MQTTBridge.uninstall import Uninstaller

    monkeypatch.setattr(Uninstaller, "root", str(tmp_path / "no-package-manager"))


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
    logger, and what this project promises about its log - that a password never
    reaches it - is a promise about the file, not about a capture handler.
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

    No session, so no feature area registers: this is the bridge itself - the
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


# ------------------------------------------------- the image's EPG importer --
#
# EPG-Importer as enigma2's plugin loader leaves it: a module in `sys.modules`
# under `Plugins.Extensions.EPGImport.plugin`, holding a singleton importer, the
# scheduler, `startImport()` and `lastImportResult`. Shaped on the bytecode of
# the OpenViX 6.6 build (`1.0+git286`), and deliberately **not** installed by
# default: most receivers do not have it, and a test that wants it says so with
# `install_epg_importer()`, which is also how it is kept out of every capability
# list the other tests assert.

EPG_IMPORTER_MODULE = "Plugins.Extensions.EPGImport.plugin"


class FakeEpgImporter:
    """`EPGImport.EPGImport`: running while `source` is set, as the image's own test says."""

    def __init__(self, epgcache):
        self.epgcache = epgcache
        self.sources = []
        self.source = None
        self.onDone = None
        self.eventCount = None
        self.longDescUntil = None
        self.raises = False
        self.began = []

    def isImportRunning(self):
        if self.raises:
            raise RuntimeError("the importer is broken")
        return self.source is not None

    def beginImport(self, longDescUntil=None):
        self.eventCount = 0
        self.longDescUntil = longDescUntil
        self.began.append([getattr(source, "description", source) for source in self.sources])
        self.nextImport()

    def nextImport(self):
        if not self.sources:
            self.closeImport()
            return
        self.source = self.sources.pop()

    def finish(self, events):
        """What the end of the download chain does: clear, save, call back - one turn."""
        self.sources = []
        self.eventCount = events
        self.closeImport()

    def closeImport(self):
        self.source = None
        if self.eventCount is not None:
            if self.onDone:
                self.onDone(reboot=False, epgfile=None)
            self.eventCount = None


class FakeEpgSource:
    def __init__(self, description):
        self.description = description


class FakeAutoStartTimer:
    """The importer's scheduler: `getWakeTime()` answers today's clock time, or -1."""

    def __init__(self, wake=-1):
        self.wake = wake
        self.prev_onlybouquet = False
        self.prev_multibouquet = True

    def getWakeTime(self):
        if isinstance(self.wake, Exception):
            raise self.wake
        return self.wake


def install_epg_importer(monkeypatch, import_events=True, import_event=False,
                         sources=("Polska - Podstawowy", "Deutschland - Basis"),
                         scheduler=True):
    """Put a fake EPG-Importer where enigma2's plugin loader would have, and return it."""
    import time as time_module

    module = types.ModuleType(EPG_IMPORTER_MODULE)
    cache = EPGCache.getInstance()
    for name, wanted in (("importEvents", import_events), ("importEvent", import_event)):
        if wanted:
            setattr(cache, name, lambda *arguments: None)
        elif name in cache.__dict__:
            delattr(cache, name)

    module.epgimport = FakeEpgImporter(cache)
    module.lastImportResult = None
    module.CONFIG_PATH = "/etc/epgimport"
    module.started = 0

    selection = {"sources": list(sources)}
    epg_config = types.SimpleNamespace(channelCache={"cached": True})
    epg_config.loaded = 0

    def loadUserSettings(filename="/etc/enigma2/epgimport.conf"):
        epg_config.loaded += 1
        return {"sources": list(selection["sources"])}

    def enumSources(path, filter=None, categories=False):
        for name in filter or ():
            yield FakeEpgSource(name)

    epg_config.loadUserSettings = loadUserSettings
    epg_config.enumSources = enumSources
    epg_config.selection = selection
    module.EPGConfig = epg_config

    def doneImport(reboot=False, epgfile=None):
        now = time_module.time()
        count = module.epgimport.eventCount
        module.lastImportResult = (now, count)
        stamp = time_module.asctime(time_module.localtime(now))
        config.plugins.extra_epgimport.last_import.value = f"{stamp}, {count}"

    def startImport():
        module.started += 1
        module.epgimport.onDone = doneImport
        module.epgimport.beginImport(longDescUntil=time_module.time() + 5 * 24 * 3600)

    module.doneImport = doneImport
    module.startImport = startImport
    module.autoStartTimer = FakeAutoStartTimer() if scheduler else None

    # The image's own settings for it, taken away again with the module.
    importer_settings = ConfigSubsection()
    importer_settings.enabled = ConfigYesNo(default=True)
    importer_settings.import_onlybouquet = ConfigYesNo(default=False)
    extra = ConfigSubsection()
    extra.last_import = ConfigText(default="none")
    usage = ConfigSubsection()
    usage.multibouquet = ConfigYesNo(default=True)
    monkeypatch.setattr(config.plugins, "epgimport", importer_settings, raising=False)
    monkeypatch.setattr(config.plugins, "extra_epgimport", extra, raising=False)
    monkeypatch.setattr(config, "usage", usage, raising=False)

    monkeypatch.setitem(sys.modules, EPG_IMPORTER_MODULE, module)
    return module
