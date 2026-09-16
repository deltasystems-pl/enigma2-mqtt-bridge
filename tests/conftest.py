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

import sys
import types
from pathlib import Path

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


# ------------------------------------------------------------------- enigma --

enigma = _module("enigma")


class eTimer:
    def __init__(self):
        self.callback = []
        self.started = None
        self.stopped = False

    def start(self, interval, single=False):
        self.started = (interval, single)

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


config = ConfigSubsection()
config.plugins = ConfigSubsection()
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
notifications_module.AddPopup = lambda *args, **kwargs: None
notifications_module.RemovePopup = lambda *args, **kwargs: None


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
            return bytes(self.payload).decode("utf-8")
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
        }
        options.update(overrides)
        bridge = Bridge(**options)
        created.append(bridge)
        return bridge

    build.factory = factory
    yield build


@pytest.fixture
def connected_bridge(make_bridge, factory, settings):
    """A started, connected bridge with a plausible broker configured."""
    settings.host.value = "10.0.0.5"
    settings.node_id.value = "vuuno4kse_005301"
    settings.friendly_name.value = "Living room receiver"
    bridge = make_bridge()
    bridge.start()
    factory.client.fire_connect()
    return bridge
