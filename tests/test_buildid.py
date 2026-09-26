"""`info.build` and `info.contract`: which build runs, which waits on disk, which contract.

The builder writes `buildinfo.py` (`tests/test_make_buildinfo.py` holds it to the truth); this side
only reads it. What is asserted here is what a consumer relies on: the running build is the one
this process loaded, a build staged on disk shows up without a restart and without anybody asking,
a copy of the source that nobody built says so rather than inventing an id, and a development build
never displays as the release it shares a number with.
"""

import importlib.util
import re
import sys
import types
from pathlib import Path

import pytest

from MQTTBridge import buildid
from MQTTBridge.version import CONTRACT, __version__

REPO_ROOT = Path(__file__).resolve().parents[1]
TOOL = REPO_ROOT / "tools" / "make-buildinfo.py"

NODE = "vuuno4kse_005301"
INFO = "enigma2/" + NODE + "/info"

RELEASE = {"commit": "1" * 40, "time": 1790000000, "dirty": False, "flavour": "release"}
DEVELOPMENT = {"commit": "2abcdef" + "0" * 33, "time": 1790000100, "dirty": False,
               "flavour": "development"}


def _render(info):
    spec = importlib.util.spec_from_file_location("make_buildinfo", TOOL)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.render(info)


def _write(path, info):
    path.write_text(_render(info), encoding="utf-8")
    return path


# ----------------------------------------------------------------- reading --


def test_what_the_builder_writes_is_what_the_plugin_reads():
    for info in (RELEASE, DEVELOPMENT, {**DEVELOPMENT, "commit": "", "time": 0, "dirty": True}):
        assert buildid.parse(_render(info)) == info


def test_the_file_is_read_without_running_it(tmp_path):
    # An import of a changed file would be a first import of new code into a process still
    # running the old code, so the text is read as literals - and a call is not a literal.
    marker = tmp_path / "ran"
    text = _render(RELEASE) + f"COMMIT = open({str(marker)!r}, 'w').name\n"
    assert buildid.parse(text) is None
    assert not marker.exists()


@pytest.mark.parametrize("change", [
    {"commit": "1" * 39},
    {"commit": "A" * 40},
    {"time": True},
    {"time": -1},
    {"time": "1790000000"},
    {"dirty": 0},
    {"flavour": ""},
    {"flavour": "Release"},
    {"flavour": 1},
])
def test_a_malformed_build_id_is_no_build_id(change):
    assert buildid.validated({**RELEASE, **change}) is None


def test_a_missing_member_is_no_build_id():
    text = "\n".join(line for line in _render(RELEASE).splitlines() if "DIRTY" not in line)
    assert buildid.parse(text) is None


def test_an_unknown_flavour_is_kept_for_the_consumer_to_judge():
    assert buildid.validated({**RELEASE, "flavour": "nightly"})["flavour"] == "nightly"


def test_an_absent_oversized_or_undecodable_file_is_no_build_id(tmp_path):
    assert buildid.read(str(tmp_path / "absent.py")) is None
    big = tmp_path / "big.py"
    big.write_text(_render(RELEASE) + "#" * buildid.MAX_BYTES, encoding="utf-8")
    assert buildid.read(str(big)) is None
    garbled = tmp_path / "garbled.py"
    garbled.write_bytes(b"COMMIT = '\xff'\n")
    assert buildid.read(str(garbled)) is None
    assert buildid.read(str(_write(tmp_path / "good.py", RELEASE))) == RELEASE


def test_a_checkout_carries_no_build_id():
    # The file exists only in a package. One in the source tree would give every test - and a
    # copy made by hand - a build id nobody built, so it is ignored by git and must not be here.
    assert not (REPO_ROOT / "src" / "MQTTBridge" / buildid.FILE_NAME).exists()
    assert "src/MQTTBridge/buildinfo.py" in (REPO_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert buildid.LOADED is None


def test_the_loaded_build_is_the_imported_module(monkeypatch):
    module = types.ModuleType("MQTTBridge.buildinfo")
    module.COMMIT, module.COMMIT_TIME = RELEASE["commit"], RELEASE["time"]
    module.DIRTY, module.FLAVOUR = RELEASE["dirty"], RELEASE["flavour"]
    monkeypatch.setitem(sys.modules, "MQTTBridge.buildinfo", module)
    assert buildid._loaded() == RELEASE
    module.DIRTY = "no"
    assert buildid._loaded() is None


# --------------------------------------------------------------- reporting --


def test_a_copy_nobody_built_reports_no_commit_and_nulls():
    assert buildid.report(None, None) == {
        "commit": "", "time": None, "dirty": None, "flavour": None, "on_disk": None,
    }


def test_the_running_build_is_reported_and_the_same_build_on_disk_is_not():
    assert buildid.report(RELEASE, dict(RELEASE)) == {**RELEASE, "on_disk": None}


def test_another_build_on_disk_is_named_by_its_commit():
    assert buildid.report(RELEASE, DEVELOPMENT)["on_disk"] == DEVELOPMENT["commit"]
    # The same commit built as another flavour is another build.
    other = {**RELEASE, "flavour": "acceptance"}
    assert buildid.report(RELEASE, other)["on_disk"] == RELEASE["commit"]
    # A built package on disk under a copy nobody built.
    assert buildid.report(None, RELEASE)["on_disk"] == RELEASE["commit"]


def test_a_file_that_cannot_be_read_is_not_evidence_of_another_build():
    assert buildid.report(RELEASE, None)["on_disk"] is None


def test_an_unknown_time_is_null():
    assert buildid.report({**DEVELOPMENT, "commit": "", "time": 0}, None)["time"] is None


# ---------------------------------------------------------------- display --


@pytest.mark.parametrize("build, shown", [
    (None, "0.3.0"),
    ({"commit": "", "dirty": None, "flavour": None}, "0.3.0"),
    (RELEASE, "0.3.0"),
    ({**RELEASE, "dirty": True}, "0.3.0+g1111111"),
    ({**RELEASE, "dirty": None}, "0.3.0+g1111111"),
    (DEVELOPMENT, "0.3.0+g2abcdef"),
    ({**RELEASE, "flavour": "acceptance"}, "0.3.0+g1111111"),
    ({**RELEASE, "flavour": "nightly"}, "0.3.0+g1111111"),
])
def test_the_display_string(build, shown):
    assert buildid.display_version("0.3.0", build) == shown


def test_a_development_build_never_looks_like_a_pre_release():
    shown = buildid.display_version("0.3.0", DEVELOPMENT)
    assert re.fullmatch(r"\d+\.\d+\.\d+\+g[0-9a-f]{7}", shown)
    assert "-" not in shown and "rc" not in shown


# ------------------------------------------------------------------ in info --


def test_info_says_no_build_id_for_a_copy_nobody_built(connected_bridge, factory):
    payload = factory.client.last(INFO).json()
    assert payload["build"] == {
        "commit": "", "time": None, "dirty": None, "flavour": None, "on_disk": None,
    }
    assert payload["contract"] == CONTRACT == 1
    assert payload["plugin"] == __version__


def _connected(make_bridge, factory, settings, **overrides):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(**overrides)
    bridge.start()
    factory.client.fire_connect()
    return bridge


def _build_timer(bridge):
    return bridge._build_ticker.timer


def test_info_carries_the_running_build(make_bridge, factory, settings, tmp_path):
    on_disk = _write(tmp_path / "buildinfo.py", RELEASE)
    _connected(make_bridge, factory, settings, build=RELEASE, build_path=str(on_disk))
    assert factory.client.last(INFO).json()["build"] == {**RELEASE, "on_disk": None}


def test_a_build_staged_on_disk_is_published_within_one_check(
    make_bridge, factory, settings, tmp_path
):
    on_disk = _write(tmp_path / "buildinfo.py", RELEASE)
    bridge = _connected(make_bridge, factory, settings, build=RELEASE, build_path=str(on_disk))
    timer = _build_timer(bridge)
    assert timer.started == (buildid.CHECK_MILLISECONDS, False)
    before = len(factory.client.all_for(INFO))

    timer.fire()
    assert len(factory.client.all_for(INFO)) == before, "nothing changed, nothing published"

    _write(on_disk, DEVELOPMENT)
    timer.fire()
    payloads = factory.client.all_for(INFO)
    assert len(payloads) == before + 1
    assert payloads[-1].json()["build"]["on_disk"] == DEVELOPMENT["commit"]
    assert payloads[-1].retain is True

    timer.fire()
    assert len(factory.client.all_for(INFO)) == before + 1, "said once, not every ten minutes"

    _write(on_disk, RELEASE)
    timer.fire()
    assert factory.client.last(INFO).json()["build"]["on_disk"] is None


def test_the_check_publishes_nothing_without_a_session(make_bridge, factory, settings, tmp_path):
    on_disk = _write(tmp_path / "buildinfo.py", RELEASE)
    bridge = _connected(make_bridge, factory, settings, build=RELEASE, build_path=str(on_disk))
    bridge.client.connected = False
    before = len(factory.client.published)
    _write(on_disk, DEVELOPMENT)
    assert bridge.check_build_on_disk() is False
    assert len(factory.client.published) == before


def test_the_check_stops_with_the_session(make_bridge, factory, settings, tmp_path):
    bridge = _connected(make_bridge, factory, settings, build=RELEASE,
                        build_path=str(tmp_path / "absent.py"))
    timer = _build_timer(bridge)
    assert timer.running
    bridge.stop()
    assert timer.stopped


def test_the_start_line_names_the_build(make_bridge, factory, settings, plugin_log):
    _connected(make_bridge, factory, settings, build=DEVELOPMENT)
    assert "build=" + __version__ + "+g2abcdef" in plugin_log()


def test_the_contract_major_is_the_documented_one():
    import json

    contract = json.loads((REPO_ROOT / "docs" / "contract.json").read_text(encoding="utf-8"))
    assert CONTRACT == contract["contract"]


def test_the_build_members_are_the_documented_ones():
    import json

    contract = json.loads((REPO_ROOT / "docs" / "contract.json").read_text(encoding="utf-8"))
    assert sorted(buildid.report(None, None)) == sorted(contract["info_build_members"])
