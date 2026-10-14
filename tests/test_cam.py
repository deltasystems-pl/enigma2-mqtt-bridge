"""Opt-in conditional-access telemetry stays current and privacy-bounded."""

import os
import time

from MQTTBridge import cam
from MQTTBridge.cam import MAX_BYTES, read_cam

NODE = "vuuno4kse_005301"
CAM = "enigma2/" + NODE + "/cam"


def write_ecm(path, text="system: Nagra\necm time: 0.123\n"):
    path.write_text(text, encoding="utf-8")


def test_cam_is_absent_until_explicitly_enabled(live_bridge):
    assert live_bridge.publisher("cam") is None
    assert "cam" not in live_bridge.capabilities()


def test_free_to_air_service_is_known_inactive(receiver, tmp_path):
    write_ecm(tmp_path / "ecm.info")
    assert read_cam(receiver.session, str(tmp_path / "ecm.info")) == {
        "system": None,
        "active": False,
        "encrypted": False,
        "ecm_ms": None,
    }


def test_fresh_ecm_for_encrypted_service_reports_only_safe_fields(receiver, tmp_path):
    receiver.info.encrypted = True
    path = tmp_path / "ecm.info"
    write_ecm(path, "system: Nagra\necm time: 123 msec\nreader: private\nserver: private\n")
    assert read_cam(receiver.session, str(path)) == {
        "system": "Nagra",
        "active": True,
        "encrypted": True,
        "ecm_ms": 123,
    }


def test_zap_invalidates_ecm_from_the_previous_service(receiver, tmp_path):
    receiver.info.encrypted = True
    path = tmp_path / "ecm.info"
    write_ecm(path)
    since = time.time() + 1
    state = read_cam(receiver.session, str(path), service_since=since, now=since)
    assert state["active"] is None
    assert state["system"] is None


def test_symlink_oversize_and_malformed_files_are_unknown(receiver, tmp_path):
    receiver.info.encrypted = True
    target = tmp_path / "target"
    write_ecm(target)
    link = tmp_path / "link"
    link.symlink_to(target)
    huge = tmp_path / "huge"
    huge.write_bytes(b"x" * (MAX_BYTES + 1))
    malformed = tmp_path / "malformed"
    malformed.write_text("reader: private\nserver: private\n", encoding="utf-8")
    for path in (link, huge, malformed):
        state = read_cam(receiver.session, str(path))
        assert state["active"] is None
        assert state["system"] is None
        assert state["ecm_ms"] is None


def test_host_like_system_and_unbounded_ecm_values_are_never_exposed(receiver, tmp_path):
    receiver.info.encrypted = True
    path = tmp_path / "ecm.info"
    for value in ("9" * 900, "999999 seconds"):
        write_ecm(path, "system: user@example.internal\necm time: " + value + "\n")
        state = read_cam(receiver.session, str(path))
        assert state["active"] is None
        assert state["system"] is None


def test_enabled_publisher_refreshes_and_invalidates_on_zap(
    make_bridge, factory, settings, receiver, tmp_path, monkeypatch
):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.cam_telemetry.value = True
    receiver.info.encrypted = True
    path = tmp_path / "ecm.info"
    write_ecm(path)
    monkeypatch.setattr("MQTTBridge.cam.ECM_PATH", str(path))
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    found = bridge.publisher("cam")
    found.path = str(path)
    found._poll.timer.fire()
    assert factory.client.last(CAM).json()["active"] is True

    receiver.nav.fire(1)
    assert factory.client.last(CAM).json()["active"] is None


def test_an_ecm_written_in_the_future_is_unknown(receiver, tmp_path):
    receiver.info.encrypted = True
    path = tmp_path / "ecm.info"
    write_ecm(path)
    stamp = time.time() + 5
    os.utime(path, (stamp, stamp))
    assert read_cam(receiver.session, str(path))["active"] is None


# ------------------------------------------------- a file gone for a moment --


def cam_publisher(make_bridge, settings, receiver, path, monkeypatch):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.cam_telemetry.value = True
    receiver.info.encrypted = True
    monkeypatch.setattr("MQTTBridge.cam.ECM_PATH", str(path))
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    found = bridge.publisher("cam")
    found.path = str(path)
    return found


def written(path, ago, text="system: Nagra\necm time: 0.123\n"):
    """An ECM file written `ago` seconds before now."""
    write_ecm(path, text)
    stamp = time.time() - ago
    os.utime(path, (stamp, stamp))


class SteppedClock:
    """The `time` module, `step` seconds later."""

    def __init__(self, step):
        self.step = step

    def __getattr__(self, name):
        return getattr(time, name)

    def time(self):
        return time.time() + self.step


def test_a_fresh_reading_is_kept_while_the_file_is_gone_for_a_moment(
    make_bridge, factory, settings, receiver, tmp_path, monkeypatch
):
    """Measured: the cam removes the file for a few seconds while still decoding.

    It does so when a descrambled stream to a stream client ends, with the
    television still decoding, and writes it again at the next ECM. A poll in
    that gap used to say `active: null`.
    """
    path = tmp_path / "ecm.info"
    found = cam_publisher(make_bridge, settings, receiver, path, monkeypatch)
    # Nothing read yet, nothing to keep: absent is unknown, as it always was.
    found._poll.timer.fire()
    assert factory.client.last(CAM).json()["active"] is None

    written(path, 2)
    found._poll.timer.fire()
    os.remove(path)
    found._poll.timer.fire()
    assert factory.client.last(CAM).json() == {
        "system": "Nagra", "active": True, "encrypted": True, "ecm_ms": 123,
    }


def test_a_kept_reading_ends_when_it_is_no_longer_fresh(
    make_bridge, factory, settings, receiver, tmp_path, monkeypatch
):
    path = tmp_path / "ecm.info"
    found = cam_publisher(make_bridge, settings, receiver, path, monkeypatch)
    written(path, 10)
    found._poll.timer.fire()
    os.remove(path)
    found._poll.timer.fire()
    assert factory.client.last(CAM).json()["active"] is True

    # Ten seconds on, the ECM is twenty seconds old: the old answer.
    monkeypatch.setattr(cam, "time", SteppedClock(10))
    found._poll.timer.fire()
    assert factory.client.last(CAM).json() == {
        "system": None, "active": None, "encrypted": True, "ecm_ms": None,
    }


def test_a_zap_ends_a_kept_reading(
    make_bridge, factory, settings, receiver, tmp_path, monkeypatch
):
    path = tmp_path / "ecm.info"
    found = cam_publisher(make_bridge, settings, receiver, path, monkeypatch)
    # Written a moment "after" the zap below, by the receiver's clock.
    written(path, -0.9)
    found._poll.timer.fire()
    os.remove(path)

    receiver.nav.fire(1)          # evStart
    assert found._held is None
    assert factory.client.last(CAM).json()["active"] is None


def test_a_file_that_is_there_but_not_usable_ends_a_kept_reading(
    make_bridge, factory, settings, receiver, tmp_path, monkeypatch
):
    path = tmp_path / "ecm.info"
    found = cam_publisher(make_bridge, settings, receiver, path, monkeypatch)
    written(path, 2)
    found._poll.timer.fire()
    os.remove(path)
    found._poll.timer.fire()
    assert factory.client.last(CAM).json()["active"] is True

    target = tmp_path / "target"
    written(target, 1)
    # Replaced by a link (not the file, and not absent either), malformed, or
    # stale: each answers as it always did, and the kept reading is gone with it.
    for make in (
        lambda: path.symlink_to(target),
        lambda: written(path, 1, "reader: private\n"),
        lambda: written(path, cam.FRESH_SECONDS + 5),
    ):
        written(path, 2)
        found._poll.timer.fire()
        os.remove(path)
        found._poll.timer.fire()
        assert factory.client.last(CAM).json()["active"] is True
        make()
        found._poll.timer.fire()
        assert factory.client.last(CAM).json()["active"] is None
        os.remove(path)
        found._poll.timer.fire()
        assert factory.client.last(CAM).json()["active"] is None


def test_free_to_air_is_inactive_and_ends_a_kept_reading(receiver, tmp_path):
    path = str(tmp_path / "ecm.info")
    held = ("Nagra", 123, time.time() - 1)
    receiver.info.encrypted = False
    payload, reading = cam.cam_state(receiver.session, path, held=held)
    assert payload == {"system": None, "active": False, "encrypted": False, "ecm_ms": None}
    assert reading is None
    # A service that cannot say whether it is encrypted ends it too.
    receiver.info.encrypted = None
    payload, reading = cam.cam_state(receiver.session, path, held=held)
    assert payload["active"] is None
    assert reading is None
    receiver.info.encrypted = True
    payload, reading = cam.cam_state(receiver.session, path, held=held)
    assert payload["active"] is True
    assert reading == held


def test_the_kept_reading_holds_the_parsed_values_only(
    make_bridge, settings, receiver, tmp_path, monkeypatch
):
    path = tmp_path / "ecm.info"
    found = cam_publisher(make_bridge, settings, receiver, path, monkeypatch)
    written(path, 2, "system: Nagra\necm time: 0.123\nreader: MARKERREADER\nfrom: MARKERHOST\n")
    found._poll.timer.fire()
    assert found._held[:2] == ("Nagra", 123)
    assert len(found._held) == 3
    assert "MARKER" not in repr(found._held)


def test_disabling_cam_retracts_retained_state(live_bridge, factory, settings):
    settings.cam_telemetry.value = True
    live_bridge._replace_configurable_publishers()
    live_bridge.publish_json(live_bridge.topic("cam"), {"active": True})
    old = live_bridge.publisher("cam")
    # enigma2 keeps the timer; the publisher lets go of it when it stops.
    old_timer = old._poll.timer
    settings.cam_telemetry.value = False
    live_bridge._replace_configurable_publishers()
    entry = factory.client.last(CAM)
    assert entry.payload == b""
    assert entry.retain is True

    # eTimer callbacks already queued by enigma2 can still arrive after stop.
    published = len(factory.client.published)
    old_timer.fire()
    assert len(factory.client.published) == published
    assert factory.client.last(CAM).payload == b""


def test_disabled_cam_retracts_a_previous_process_topic_on_connect(
    live_bridge, factory
):
    live_bridge.publish_json(live_bridge.topic("cam"), {"active": True})
    factory.client.fire_connect()
    entry = factory.client.last(CAM)
    assert entry.payload == b""
    assert entry.retain is True
