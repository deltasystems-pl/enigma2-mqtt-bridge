"""The entry points enigma2 actually calls.

The one rule this file exists to hold: **the graphical interface must never fail
to start because of the bridge.** Every entry point swallows what it catches, so
an image the plugin does not understand gets a receiver that works and a log line
that says why the plugin does not.
"""

from Plugins.Plugin import PluginDescriptor

from MQTTBridge import plugin as plugin_module


def descriptors():
    return {descriptor.where: descriptor for descriptor in plugin_module.Plugins()}


def test_three_descriptors_where_the_contract_says():
    found = descriptors()
    assert set(found) == {
        PluginDescriptor.WHERE_SESSIONSTART,
        PluginDescriptor.WHERE_AUTOSTART,
        PluginDescriptor.WHERE_PLUGINMENU,
    }


def test_the_menu_entry_is_named_and_has_an_icon():
    entry = descriptors()[PluginDescriptor.WHERE_PLUGINMENU]
    assert entry.name == "MQTT Bridge"
    assert entry.description
    assert entry.icon == "plugin.png"
    assert entry.fnc is plugin_module.open_setup


def test_the_session_start_descriptor_starts_the_bridge():
    entry = descriptors()[PluginDescriptor.WHERE_SESSIONSTART]
    assert entry.fnc is plugin_module.sessionstart
    assert entry.needsRestart is False


def test_session_start_builds_and_starts_a_bridge(monkeypatch):
    built = []

    class FakeBridge:
        def __init__(self, session=None):
            self.session = session
            self.started = 0
            built.append(self)

        def start(self):
            self.started += 1

        def stop(self):
            pass

    monkeypatch.setattr(plugin_module, "_bridge", None)
    monkeypatch.setattr("MQTTBridge.bridge.Bridge", FakeBridge)

    session = object()
    plugin_module.sessionstart(0, session=session)

    assert len(built) == 1
    assert built[0].session is session
    assert built[0].started == 1
    monkeypatch.setattr(plugin_module, "_bridge", None)


def test_session_start_ignores_the_other_reasons(monkeypatch):
    monkeypatch.setattr(plugin_module, "_bridge", None)
    plugin_module.sessionstart(1)
    assert plugin_module.get_bridge() is None


def test_a_bridge_that_cannot_start_does_not_take_the_gui_down(monkeypatch, plugin_log):
    class Explosive:
        def __init__(self, session=None):
            raise RuntimeError("this image is unusual")

    monkeypatch.setattr(plugin_module, "_bridge", None)
    monkeypatch.setattr("MQTTBridge.bridge.Bridge", Explosive)

    plugin_module.sessionstart(0, session=object())

    assert "could not be started" in plugin_log()


def test_autostart_reason_one_stops_the_bridge(monkeypatch):
    class Recorder:
        def __init__(self):
            self.stops = 0

        def stop(self):
            self.stops += 1

    recorder = Recorder()
    monkeypatch.setattr(plugin_module, "_bridge", recorder)

    plugin_module.autostart(0)
    assert recorder.stops == 0

    plugin_module.autostart(1)
    assert recorder.stops == 1


def test_autostart_with_no_bridge_is_harmless(monkeypatch):
    monkeypatch.setattr(plugin_module, "_bridge", None)
    plugin_module.autostart(1)


def test_a_stop_that_raises_is_swallowed(monkeypatch, plugin_log):
    class Stubborn:
        def stop(self):
            raise RuntimeError("the socket is wedged")

    monkeypatch.setattr(plugin_module, "_bridge", Stubborn())
    plugin_module.autostart(1)
    assert "could not be stopped cleanly" in plugin_log()


def test_open_setup_opens_the_screen():
    from MQTTBridge.setup import MQTTBridgeSetup

    class FakeSession:
        def __init__(self):
            self.opened = []

        def open(self, screen, *args, **kwargs):
            self.opened.append(screen)

    session = FakeSession()
    plugin_module.open_setup(session)
    assert session.opened == [MQTTBridgeSetup]


def test_open_setup_swallows_a_failure(plugin_log):
    class BrokenSession:
        def open(self, screen, *args, **kwargs):
            raise RuntimeError("no skin")

    plugin_module.open_setup(BrokenSession())
    assert "could not be opened" in plugin_log()


def test_plugins_returns_an_empty_list_when_the_image_has_no_descriptor(monkeypatch, plugin_log):
    import sys

    monkeypatch.delitem(sys.modules, "Plugins.Plugin")
    monkeypatch.setattr(sys.modules["Plugins"], "__path__", [])
    assert plugin_module.Plugins() == []
    assert "no Plugins.Plugin" in plugin_log()
