"""Wake-on-LAN: what the image says it can do, and the image's own switch.

The image here is the stub in `conftest`: `Components.SystemInfo` with
`WakeOnLAN` set to `False`, which is what the one measured receiver holds, and
no `config.usage.wakeOnLAN`, which that image builds only when the driver made
the front-processor file. The `image_wol` fixture below gives a test the other
kind of receiver — a file under `…/fp/wol` or `…/power/wol` and the image's own
setting, whose notifier writes the file the way the image's does.
"""

import ast
import os
import subprocess
from pathlib import Path

import pytest
from Components.config import ConfigSubsection, ConfigYesNo, config, configfile
from Components.SystemInfo import SystemInfo
from conftest import ConsoleAppContainer

from MQTTBridge import boxinfo, wol
from MQTTBridge import config as settings_module
from MQTTBridge import setup as setup_screen

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"
MAC = "00:00:5e:00:53:01"
NOT_AVAILABLE = "Wake-on-LAN is not available on this receiver"


class ImageWakeOnLan(ConfigYesNo):
    """`config.usage.wakeOnLAN` as the image builds it: a yes/no whose notifier writes the file.

    The notifier is the image's `wakeOnLANChanged`: `enable`/`disable` when the
    path has `fp` in it, `on`/`off` otherwise.

    🔴 Unlike the shared `ConfigYesNo` stub, this one stores **the object it is
    given**, because OpenViX 6.6's `ConfigElement.setValue` does: it keeps
    `value` as assigned and calls the notifiers only when it differs from the
    previous one. A stub that turned `1` into `True` would hide an `arm()` that
    wrote `1` — which the image keeps, writes `enable` for, and which then
    fails an `is True` test on every start until a reboot loads a real bool.
    """

    def __init__(self, path, default=False):
        self.path = path
        ConfigYesNo.__init__(self, default=default)

    def _set(self, value):
        previous = self._value
        self._value = value
        if previous != value:
            self._notify()

    def _notify(self):
        with open(self.path, "w", encoding="ascii") as handle:
            if "fp" in self.path:
                handle.write("enable" if self._value else "disable")
            else:
                handle.write("on" if self._value else "off")

    value = property(ConfigYesNo._get, _set)


@pytest.fixture
def interfaces(monkeypatch):
    """`/sys/class/net`, as a test says it is: name to address."""

    def install(found):
        def read(path):
            parts = path.split("/")
            if path.startswith("/sys/class/net/") and parts[-1] == "address":
                return found.get(parts[-2], "")
            return ""

        monkeypatch.setattr(boxinfo, "_read_text", read)
        monkeypatch.setattr(boxinfo.os, "listdir", lambda path: sorted(found) + ["lo"])

    install({"eth0": MAC})
    return install


@pytest.fixture
def usage(monkeypatch):
    """`config.usage` without a Wake-on-LAN element, which is the measured receiver."""
    section = ConfigSubsection()
    monkeypatch.setattr(config, "usage", section, raising=False)
    return section


@pytest.fixture
def image_wol(monkeypatch, tmp_path, usage):
    """A receiver whose driver made the front-processor file, and the image's setting for it."""

    def install(mechanism="fp", content=None, element=True, value=False):
        path = tmp_path / "stb" / mechanism / "wol"
        path.parent.mkdir(parents=True)
        monkeypatch.setitem(SystemInfo, "WakeOnLAN", str(path))
        setting = None
        if element:
            setting = ImageWakeOnLan(str(path), default=False)
            # What a start loads from the settings file: a real bool.
            setting.value = value
            setting.saved_value = value
            # The image's `addNotifier` calls the notifier at once, so the file
            # holds the setting from the moment the image has started.
            setting._notify()
            usage.wakeOnLAN = setting
        if content is not None:
            path.write_text(content, encoding="ascii")
        return path, setting

    return install


# -------------------------------------------------------------- not supported --


def test_a_receiver_without_the_file_reports_not_supported(interfaces, usage):
    assert wol.report() == {
        "supported": False, "armed": None, "iface": "eth0", "mechanism": None,
    }


UNKNOWN = {"supported": None, "armed": None, "iface": "eth0", "mechanism": None}


def test_an_image_without_system_info_is_unknown_not_unsupported(interfaces, monkeypatch):
    """`false` is the image's answer; no `SystemInfo` to ask is no answer at all."""
    monkeypatch.setattr(wol, "_system_info", lambda: None)
    assert wol.report() == UNKNOWN


def test_a_system_info_that_raises_is_unknown(interfaces, monkeypatch):
    class Broken:
        def get(self, item, default=None):
            raise RuntimeError("BoxInfo is not ready")

    monkeypatch.setattr(wol, "_system_info", lambda: Broken())
    assert wol.report() == UNKNOWN


def test_a_key_the_image_never_set_is_unknown(interfaces, monkeypatch):
    monkeypatch.delitem(SystemInfo, "WakeOnLAN")
    assert wol.report() == UNKNOWN


@pytest.mark.parametrize("value", [True, 1, None, ["/proc/stb/fp/wol"]])
def test_a_value_the_image_s_probe_never_produces_is_unknown(interfaces, monkeypatch, value):
    monkeypatch.setitem(SystemInfo, "WakeOnLAN", value)
    assert wol.report() == UNKNOWN


def test_a_failure_while_reading_is_unknown_and_keeps_the_interface(
    interfaces, image_wol, monkeypatch
):
    image_wol()

    def broken(path):
        raise RuntimeError("unexpected")

    monkeypatch.setattr(wol, "armed", broken)
    assert wol.report() == UNKNOWN


def test_an_unknown_image_is_never_armed(interfaces, usage, settings, monkeypatch):
    monkeypatch.setattr(wol, "_system_info", lambda: None)
    usage.wakeOnLAN = ConfigYesNo(default=False)
    settings.wol_arm.value = True

    assert wol.arm(settings) is False
    assert usage.wakeOnLAN.value is False
    assert configfile.save_calls == 0


def test_arming_where_the_image_has_no_switch_changes_and_writes_nothing(
    interfaces, usage, settings
):
    settings.wol_arm.value = True

    assert wol.arm(settings) is False
    assert not hasattr(usage, "wakeOnLAN")
    assert configfile.save_calls == 0


def test_an_element_without_the_image_s_file_is_not_touched(usage, settings):
    """The image builds the element only with the file; one without it is not the image's."""
    usage.wakeOnLAN = ConfigYesNo(default=False)
    settings.wol_arm.value = True

    assert wol.arm(settings) is False
    assert usage.wakeOnLAN.value is False
    assert usage.wakeOnLAN.save_calls == 0


def test_the_setup_screen_says_the_setting_does_nothing_here(usage):
    label = dict(setup_screen.setting_labels())["wol_arm"]
    assert NOT_AVAILABLE in label


def test_the_setup_screen_says_nothing_where_the_image_has_a_switch(image_wol):
    image_wol()
    label = dict(setup_screen.setting_labels())["wol_arm"]
    assert NOT_AVAILABLE not in label
    assert "Wake-on-LAN" in label


# ------------------------------------------------------------------ supported --


@pytest.mark.parametrize("mechanism", ["fp", "power"])
@pytest.mark.parametrize(
    "content, expected",
    [("enable", True), ("disable", False), ("on", True), ("off", False),
     ("enable\n", True), ("OFF\n", False)],
)
def test_armed_follows_the_file_in_both_vocabularies(
    interfaces, image_wol, mechanism, content, expected
):
    path, _setting = image_wol(mechanism=mechanism, element=False, content=content)

    assert wol.report() == {
        "supported": True, "armed": expected, "iface": "eth0", "mechanism": mechanism,
    }
    assert wol.switch_path() == str(path)


@pytest.mark.parametrize("value", [True, False])
def test_an_unreadable_file_falls_back_to_the_image_s_setting(image_wol, value):
    """A write-only file reads as nothing; the image's setting is what wrote it."""
    path, setting = image_wol(value=value)
    path.write_text("", encoding="ascii")

    assert wol.report()["armed"] is value


def test_a_missing_file_falls_back_to_the_image_s_setting(image_wol):
    path, _setting = image_wol(value=True)
    path.unlink()
    assert wol.report()["armed"] is True


def test_nothing_readable_at_all_is_null_not_a_guess(image_wol):
    path, _setting = image_wol(element=False, content="garbage")
    assert wol.report()["armed"] is None


def test_the_file_outranks_the_image_s_setting(image_wol):
    """The file is what the front processor was told; the setting is only what wrote it."""
    path, setting = image_wol(value=True)
    path.write_text("disable", encoding="ascii")
    assert wol.report()["armed"] is False


def test_armed_is_never_the_plugin_s_own_setting(image_wol, settings):
    image_wol(content="disable", element=False)
    settings.wol_arm.value = True
    assert wol.report()["armed"] is False


def test_a_path_elsewhere_is_read_the_way_the_image_reads_it():
    assert wol.mechanism("/proc/stb/fp/wol") == "fp"
    assert wol.mechanism("/proc/stb/power/wol") == "power"
    assert wol.mechanism("/proc/fp_wol") == "fp"
    assert wol.mechanism("/proc/wol") == "power"
    # The directory decides before the image's substring rule does.
    assert wol.mechanism("/mnt/fpga/stb/power/wol") == "power"
    assert wol.mechanism("") is None


# ---------------------------------------------------------------------- arming --


@pytest.mark.parametrize("mechanism", ["fp", "power"])
def test_arming_sets_and_saves_the_image_s_setting_exactly_once(
    interfaces, image_wol, settings, mechanism
):
    path, setting = image_wol(mechanism=mechanism)
    settings.wol_arm.value = True

    assert wol.arm(settings) is True
    assert wol.arm(settings) is False

    # Exactly `True`, not something truthy: the image keeps what it is given.
    assert setting.value is True
    # What reaches the settings file is the new value — saved after it was set.
    assert setting.saved_value is True
    assert setting.save_calls == 1
    assert configfile.save_calls == 1
    # The image's notifier wrote its file, and the report reads it back.
    assert path.read_text(encoding="ascii") == ("enable" if mechanism == "fp" else "on")
    assert wol.report()["armed"] is True


def test_a_setting_loaded_on_after_a_start_is_left_alone(image_wol, settings):
    """After a reboot the image loads a real `True`; arming again writes nothing."""
    path, setting = image_wol(value=True)
    settings.wol_arm.value = True

    assert wol.arm(settings) is False
    assert setting.save_calls == 0
    assert configfile.save_calls == 0
    assert path.read_text(encoding="ascii") == "enable"


@pytest.mark.parametrize("value", [True, False])
def test_switched_off_it_leaves_the_image_s_setting_alone(image_wol, settings, value):
    _path, setting = image_wol(value=value)
    settings.wol_arm.value = False

    assert wol.arm(settings) is False
    assert setting.value is value
    assert setting.save_calls == 0
    assert configfile.save_calls == 0


def test_a_setting_that_will_not_save_does_not_raise(image_wol, settings, monkeypatch):
    _path, setting = image_wol()
    settings.wol_arm.value = True

    def refuse():
        raise OSError("read-only file system")

    monkeypatch.setattr(configfile, "save", refuse)
    assert wol.arm(settings) is False


def test_the_bridge_arms_at_start_and_publishes_what_the_image_says(
    make_bridge, factory, settings, interfaces, image_wol
):
    _path, setting = image_wol()
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.wol_arm.value = True

    bridge = make_bridge()
    bridge.start()
    factory.client.fire_connect()
    bridge.reload()
    factory.client.fire_connect()

    assert setting.value is True
    assert setting.saved_value is True
    assert setting.save_calls == 1
    assert factory.client.last(INFO).json()["wol"] == {
        "supported": True, "armed": True, "iface": "eth0", "mechanism": "fp",
    }


def test_a_bridge_on_the_measured_receiver_publishes_not_supported(
    make_bridge, factory, settings, interfaces, usage
):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.wol_arm.value = True

    make_bridge().start()
    factory.client.fire_connect()

    assert factory.client.last(INFO).json()["wol"] == {
        "supported": False, "armed": None, "iface": "eth0", "mechanism": None,
    }
    assert not hasattr(usage, "wakeOnLAN")


def test_a_switched_off_plugin_changes_no_image_setting(make_bridge, settings, image_wol):
    _path, setting = image_wol()
    settings.enabled.value = False
    settings.wol_arm.value = True

    make_bridge().start()

    assert setting.value is False


# ------------------------------------------------------------- no process at all --


def test_nothing_here_starts_a_process(
    make_bridge, factory, settings, interfaces, image_wol, monkeypatch
):
    started = []

    def trap(name):
        def refuse(*args, **kwargs):
            started.append(name)
            raise AssertionError(name + " was called")

        return refuse

    monkeypatch.setattr(subprocess, "Popen", trap("subprocess.Popen"))
    for name in ("system", "popen", "fork", "execv", "execvp", "spawnv", "posix_spawn"):
        if hasattr(os, name):
            monkeypatch.setattr(os, name, trap("os." + name))
    image_wol()
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.wol_arm.value = True

    make_bridge().start()
    factory.client.fire_connect()
    wol.report()
    wol.arm(settings)

    assert started == []
    assert ConsoleAppContainer.instances == []


def test_the_module_names_no_way_of_starting_a_process():
    source = Path(wol.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    forbidden = {
        "subprocess", "eConsoleAppContainer", "ePopen", "system", "popen", "Popen",
        "spawnv", "posix_spawn", "execv", "execvp", "fork",
    }
    used = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, ast.Attribute):
            used.add(node.attr)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            used.update(alias.name for alias in node.names)
            if isinstance(node, ast.ImportFrom) and node.module:
                used.add(node.module)
    assert not used & forbidden


# ------------------------------------------------------------- the box-only rule --


def test_wol_arm_is_refused_over_mqtt(connected_bridge, factory, settings):
    assert "wol_arm" not in settings_module.REMOTE_SETTING_NAMES
    factory.client.fire_message(
        ROOT + "/cmd/config",
        b'{"publish_keys":true,"screenshot":"on_zap","screenshot_interval":60,'
        b'"wol_arm":true}',
    )
    assert factory.client.last(LAST_ERROR).json()["error"] == (
        "the config object contains unknown settings"
    )
    assert settings.wol_arm.value is False


def test_wol_arm_defaults_off_and_is_a_setting_like_any_other(settings):
    assert settings.wol_arm.default is False
    assert settings_module.SETTING_KINDS["wol_arm"] == "bool"
    names = settings_module.SETTING_NAMES
    assert names.index("wol_arm") == names.index("deep_standby_allowed") + 1


# ------------------------------------------------------------------ the interface --


def test_the_interface_is_eth0_when_it_has_an_address(interfaces):
    interfaces({"eth0": MAC, "eth1": "00:00:5e:00:53:02"})
    assert wol.report()["iface"] == "eth0"
    assert boxinfo.mac_address() == MAC


def test_the_interface_is_the_first_real_one_without_eth0(interfaces):
    interfaces({"eth0": "00:00:00:00:00:00", "wlan0": "00:00:5e:00:53:03",
                "sys0": "00:00:5e:00:53:04"})
    assert wol.report()["iface"] == "sys0"
    assert boxinfo.mac_address() == "00:00:5e:00:53:04"


def test_the_interface_is_null_when_there_is_none(interfaces):
    interfaces({})
    assert wol.report()["iface"] is None
