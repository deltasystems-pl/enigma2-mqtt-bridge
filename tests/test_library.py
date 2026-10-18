"""The recording library: what it reads, what it publishes, and when.

The image is `pvrimage.PvrImage`: movie locations on a temporary disk, the image's
own move-to-trash, its trash switch and a `/proc/mounts` of the test's own. The
worker runs inline unless a test hands it a `Background`, which makes the test the
thread - and what runs on the main loop visibly apart from what runs in the worker.
"""

import hashlib
import json
import os
import time

import pytest
from conftest import POLSAT, TVN, TVP1, MainLoop, RecordTimerEntry, ServiceReference, eTimer
from pvrimage import Background, PvrImage, access_points, inline, meta_text

from MQTTBridge import library
from MQTTBridge.library import LibraryPublisher

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE + "/"
INDEX = ROOT + "recordings"
LAST_ERROR = ROOT + "last_error"
INFO = ROOT + "info"
CMD = ROOT + "cmd/"
OUTSIDE = "1:0:19:2EE3:441:1:C00000:0:0:0:"


@pytest.fixture
def image(tmp_path, monkeypatch):
    image = PvrImage(tmp_path, monkeypatch).install()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(inline))
    return image


@pytest.fixture
def start(image, make_bridge, factory, settings, receiver):
    """Start a bridge on the receiver - after the test has put its recordings on the disk."""

    def run(connect=True, player=True):
        settings.host.value = "10.0.0.5"
        settings.node_id.value = NODE
        if player:
            image.with_player(receiver)
        bridge = make_bridge(session=receiver.session)
        bridge.start()
        if connect:
            factory.client.fire_connect()
        return bridge

    return run


def last(factory, topic):
    entry = factory.client.last(topic)
    return None if entry is None or not entry.text else entry.json()


def folder(factory, slug):
    return last(factory, ROOT + "recordings/" + slug)


def send(factory, command, payload):
    factory.client.fire_message(CMD + command, json.dumps(payload) if not isinstance(
        payload, str) else payload)


def error(factory):
    return last(factory, LAST_ERROR)


# ------------------------------------------------------------------ the id --


def test_the_id_is_sixteen_hex_digits_of_the_sha256_of_the_path():
    path = "/media/hdd/movie/20261017 2015 - Channel - Title.ts"
    expected = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
    assert library.recording_id(path) == expected
    assert library.valid_id(expected)


def test_the_id_is_over_the_file_systems_bytes_and_a_name_is_shown_with_replacements():
    raw = b"/media/hdd/movie/Kana\xb3.ts"
    path = os.fsdecode(raw)
    assert library.recording_id(path) == hashlib.sha256(raw).hexdigest()[:16]
    assert library.shown(path) == "/media/hdd/movie/Kana�.ts"


@pytest.mark.parametrize("value", ["", "3f9c2e4a1b7d5c6", "3F9C2E4A1B7D5C60", "3f9c2e4a1b7d5c60a",
                                   "../../etc/passwd", 16, None, "3f9c2e4a1b7d5cg0"])
def test_anything_but_sixteen_lower_hex_digits_is_no_id(value):
    assert not library.valid_id(value)


@pytest.mark.parametrize("text", ['{"id": "3f9c2e4a1b7d5c60", "path": "/etc"}',
                                  '{"path": "/media/hdd/movie/x.ts"}', '"3f9c2e4a1b7d5c60"',
                                  "3f9c2e4a1b7d5c60", "", "[]", '{"id": "/media/hdd/x.ts"}'])
def test_a_payload_that_is_not_exactly_one_id_names_nothing(text):
    assert library.payload_id(text) is None


# ---------------------------------------------------------- the files beside --


def test_a_meta_file_gives_the_service_the_name_the_description_and_the_length():
    meta = library.parse_meta(meta_text(length=90000 * 3600).encode())
    assert meta == {"sref": TVP1, "name": "Wiadomosci", "description": "Serwis",
                    "created": 1792050000, "length": 90000 * 3600}


def test_a_garbled_meta_file_gives_what_it_can():
    assert library.parse_meta(b"") == {}
    assert library.parse_meta(b"\xff\xfe\x00garbage") == {}
    meta = library.parse_meta(b"not a reference\n" + b"x" * 5000 + b"\n\nyesterday\n")
    assert "sref" not in meta and "created" not in meta
    assert len(meta["name"]) == library.TEXT_LIMIT
    assert library.parse_meta(b"1:0:19:283D:3FB:1:C00000:0:0:0:\r\nTitel\r\n")["name"] == "Titel"


def test_the_length_from_the_access_points_and_across_the_wrap():
    path_data = access_points(1000, 1000 + 90000 * 1800)
    assert library.ap_duration_bytes(path_data) == 1800
    wrapped = access_points((1 << 33) - 90000 * 60, (1 << 33) + 90000 * 60)
    assert library.ap_duration_bytes(wrapped) == 120


@pytest.mark.parametrize("data", [b"", b"\x00" * 16, b"\x00" * 33, b"\x00" * 64])
def test_an_access_point_file_too_short_or_without_a_length_gives_none(data):
    assert library.ap_duration_bytes(data) is None


def test_the_image_file_name_gives_the_channel_and_the_title():
    assert library.split_name("20261017 2015 - TVP 1 HD - Wiadomosci") == (
        "TVP 1 HD", "Wiadomosci")
    assert library.split_name("20261017 - Wiadomosci") == (None, "Wiadomosci")
    assert library.split_name("Holiday video") == (None, None)


# ---------------------------------------------------------------- the mounts --


def test_mounts_are_read_with_their_escapes(tmp_path):
    path = tmp_path / "mounts"
    path.write_text("server:/v /media/net/My\\040Share nfs4 rw 0 0\n"
                    "systemd-1 /media/autofs autofs rw 0 0\n", encoding="utf-8")
    assert library.read_mounts(str(path)) == [
        ("/media/net/My Share", "nfs4"), ("/media/autofs", "autofs")]
    assert library.read_mounts(str(tmp_path / "absent")) == []


def test_a_location_is_mounted_on_its_own_file_system_and_not_on_an_automounter():
    mounts = [("/", "rootfs"), ("/media/hdd", "ext4"), ("/media/autofs", "autofs"),
              ("/media/autofs/nas", "nfs")]
    assert library.is_mounted("/media/hdd/movie", mounts)
    assert not library.is_mounted("/media/usb/movie", mounts)
    assert not library.is_mounted("/media/autofs/other/movie", mounts)
    assert library.is_mounted("/media/autofs/nas/movie", mounts)
    assert library.covering_mount("/media/hddx", mounts) == ("/", "rootfs")


def test_a_link_on_the_flash_is_followed_and_nothing_inside_a_mount_is(tmp_path):
    disk = tmp_path / "media" / "hdd"
    (disk / "movie").mkdir(parents=True)
    os.symlink(str(disk), str(tmp_path / "hdd"))
    os.symlink(str(disk / "movie"), str(disk / "inside"))
    mounts = [("/", "rootfs"), (str(disk), "ext4")]
    assert library.resolve(str(tmp_path / "hdd" / "movie"), mounts) == str(disk / "movie")
    # A link inside the mounted disk is the disk's: reading it would be a disk access.
    assert library.resolve(str(disk / "inside"), mounts) == str(disk / "inside")


def test_free_space_is_asked_only_where_a_location_is_mounted(image, monkeypatch):
    asked = []
    monkeypatch.setattr(library, "free_megabytes", lambda path: asked.append(path) or 5)
    absent = str(image.tmp / "media" / "usb" / "movie")
    result = library.space([str(image.movie), absent], str(image.mounts))
    assert result == {str(image.movie): (True, 5), absent: (False, None)}
    assert asked == [str(image.movie)]


# ------------------------------------------------------------------ the walk --


def test_the_walk_lists_subfolders_and_leaves_out_the_trash_hidden_files_and_other_media(image):
    image.record("20261017 2015 - TVP 1 HD - Wiadomosci.ts", meta=meta_text())
    image.record("a.ts", folder=image.movie / "Series")
    image.record("deleted.ts", folder=image.movie / ".Trash")
    image.record("hidden.ts", folder=image.movie / ".hidden")
    image.record(".dot.ts")
    image.record("holiday.mkv")
    image.record("radio.stream", folder=image.movie / "IPTV")
    (image.movie / "Empty").mkdir()
    os.symlink(str(image.movie / "Series"), str(image.movie / "Loop"))
    folders = library.walk(str(image.movie))
    assert [(relative, [record["file"] for record in records]) for relative, records in folders] \
        == [
        ("", ["20261017 2015 - TVP 1 HD - Wiadomosci.ts"]),
        ("IPTV", ["IPTV/radio.stream"]),
        ("Series", ["Series/a.ts"]),
    ]


def test_a_recording_without_a_meta_file_is_named_from_its_file(image):
    path = image.record("20261017 2015 - TVN HD - Fakty.ts", size=3 * 1024 * 1024)
    record = library.read_recording(str(image.movie), os.path.basename(path), str(image.movie))
    assert record["name"] == "Fakty" and record["_channel"] == "TVN HD"
    assert record["sref"] is None and record["description"] is None
    assert record["size_mb"] == 3
    assert record["id"] == library.recording_id(path)


def test_the_length_comes_from_the_meta_file_and_then_from_the_access_points(image):
    image.record("a.ts", meta=meta_text(length=90000 * 600))
    image.record("b.ts", meta=meta_text(), ap=access_points(0, 90000 * 300))
    image.record("c.ts", meta=b"\x00\x01garbled")
    records = {record["file"]: record for _folder, rows in library.walk(str(image.movie))
               for record in rows}
    assert records["a.ts"]["duration"] == 600
    assert records["b.ts"]["duration"] == 300
    assert records["c.ts"]["duration"] is None
    assert records["c.ts"]["name"] == "c"


# ------------------------------------------------------------ the published --


def test_the_index_and_one_topic_per_folder(image, start, factory):
    image.record("20261017 2015 - TVP 1 HD - Wiadomosci.ts", meta=meta_text(created=1792050000),
                 size=2 * 1024 * 1024)
    image.record("20261018 2015 - TVN HD - Fakty.ts",
                 meta=meta_text(sref=TVN, name="Fakty", description="", created=1792136400))
    image.record("20261001 2000 - Polsat Sport - Mecz.ts", folder=image.movie / "Sport",
                 meta=meta_text(sref=POLSAT, name="Mecz", created=1790000000))
    ServiceReference.names.update({TVP1: "TVP 1 HD", TVN: "TVN HD", POLSAT: "Polsat Sport"})
    bridge = start()
    index = last(factory, INDEX)
    assert index["reason"] == "start" and isinstance(index["read"], int)
    assert index["locations"] == [{
        "path": str(image.movie),
        "mounted": True,
        "free_mb": index["locations"][0]["free_mb"],
        "folders": [
            {"slug": "movie", "path": "", "count": 2, "size_mb": 3, "cut": 0},
            {"slug": "movie_sport", "path": "Sport", "count": 1, "size_mb": 1, "cut": 0},
        ],
    }]
    assert isinstance(index["locations"][0]["free_mb"], int)
    movie = folder(factory, "movie")["recordings"]
    # Newest first.
    assert [entry["name"] for entry in movie] == ["Fakty", "Wiadomosci"]
    assert movie[1] == {
        "id": library.recording_id(str(image.movie / "20261017 2015 - TVP 1 HD - Wiadomosci.ts")),
        "file": "20261017 2015 - TVP 1 HD - Wiadomosci.ts",
        "name": "Wiadomosci",
        "description": "Serwis",
        "service_name": "TVP 1 HD",
        "sref": TVP1,
        "bouquet": "Ulubione TV",
        "begin": 1792050000,
        "duration": None,
        "size_mb": 2,
    }
    assert movie[0]["description"] is None
    sport = folder(factory, "movie_sport")["recordings"]
    assert sport[0]["file"] == "Sport/20261001 2000 - Polsat Sport - Mecz.ts"
    assert sport[0]["bouquet"] == "Sport (HD)"
    assert "recordings" in bridge.capabilities()
    assert factory.client.last(INDEX).retain is True


def test_the_receiver_lists_every_recording_and_says_its_bouquet_for_home_assistant(
        image, start, factory, settings):
    """Hidden bouquets are Home Assistant's: the receiver lists all, with `sref` and `bouquet`."""
    settings.bouquets_for_select.value = "Ulubione TV"
    image.record("a.ts", meta=meta_text(sref=POLSAT, name="Sport", created=2))
    image.record("b.ts", meta=meta_text(sref=OUTSIDE, name="Other", created=1))
    image.record("c.ts")
    start()
    entries = {entry["name"]: entry for entry in folder(factory, "movie")["recordings"]}
    assert entries["Sport"]["sref"] == POLSAT and entries["Sport"]["bouquet"] is None
    assert entries["Other"]["sref"] == OUTSIDE and entries["Other"]["bouquet"] is None
    assert entries["c"]["sref"] is None and entries["c"]["bouquet"] is None
    assert last(factory, INDEX)["locations"][0]["folders"][0]["count"] == 3


def test_a_change_of_the_bouquets_says_the_bouquets_again_without_reading_a_disk(
        image, start, factory, settings, receiver, monkeypatch):
    image.record("a.ts", meta=meta_text(sref=POLSAT, created=2))
    settings.bouquets_for_select.value = "Ulubione TV"
    bridge = start()
    assert folder(factory, "movie")["recordings"][0]["bouquet"] is None
    read = last(factory, INDEX)["read"]
    monkeypatch.setattr(library, "walk", lambda location: pytest.fail("the disk was read"))
    settings.bouquets_for_select.value = ""
    bridge.publisher("channels").refresh()
    assert folder(factory, "movie")["recordings"][0]["bouquet"] == "Sport (HD)"
    assert last(factory, INDEX)["read"] == read


def test_two_locations_with_the_same_name_get_two_slugs(image, start, factory, tmp_path):
    usb = tmp_path / "media" / "usb" / "movie"
    usb.mkdir(parents=True)
    image.mount_lines.append("/dev/sdb1 " + str(usb.parent) + " vfat rw 0 0")
    image.write_mounts()
    image.movielist.videodirs.value = [str(image.movie) + "/", str(usb) + "/"]
    image.record("a.ts")
    image.record("b.ts", folder=usb)
    start()
    slugs = [one["slug"] for location in last(factory, INDEX)["locations"]
             for one in location["folders"]]
    assert slugs == ["movie", "movie_2"]
    assert folder(factory, "movie_2")["recordings"][0]["file"] == "b.ts"


def test_a_location_inside_another_is_listed_once(image, start, factory):
    image.movielist.videodirs.value = [str(image.movie) + "/", str(image.movie / "Series")]
    image.record("a.ts", folder=image.movie / "Series")
    start()
    locations = last(factory, INDEX)["locations"]
    assert [location["path"] for location in locations] == [str(image.movie)]
    assert locations[0]["folders"][0]["path"] == "Series"


def test_a_location_reached_through_a_link_has_the_ids_of_its_real_path(image, start, factory):
    os.symlink(str(image.disk), str(image.tmp / "hdd"))
    image.movielist.videodirs.value = [str(image.tmp / "hdd" / "movie") + "/"]
    image.usage.default_path.value = str(image.tmp / "hdd" / "movie") + "/"
    path = image.record("a.ts")
    start()
    assert folder(factory, "movie")["recordings"][0]["id"] == library.recording_id(path)


def test_a_folder_over_the_packet_bound_is_cut_oldest_first(image, start, factory, monkeypatch):
    for index in range(40):
        image.record(f"{index:04d}.ts", meta=meta_text(name="x" * 150, description="y" * 150,
                                                      created=1790000000 + index), size=0)
    from MQTTBridge import bridge as bridge_module

    monkeypatch.setattr(bridge_module, "MAX_PACKET_BYTES", 10000)
    start()
    row = last(factory, INDEX)["locations"][0]["folders"][0]
    published = factory.client.last(ROOT + "recordings/movie")
    entries = published.json()["recordings"]
    assert row["cut"] > 0 and row["count"] == len(entries) == 40 - row["cut"]
    assert len(published.text.encode()) + len(published.topic) + 7 <= 10000
    # The newest stay: the cut ones are the oldest.
    assert entries[0]["begin"] == 1790000039
    assert min(entry["begin"] for entry in entries) == 1790000000 + row["cut"]


def test_five_thousand_recordings_are_read_and_published_within_the_bound(
        image, start, factory, monkeypatch):
    for index in range(5000):
        image.record(f"20261017 {index:04d} - Channel {index % 37} - Title {index}.ts",
                     meta=meta_text(sref=f"1:0:19:{index + 1:X}:3FB:1:C00000:0:0:0:",
                                    name=f"Title {index}", description="Text " * 20,
                                    created=1790000000 + index), size=0)
    background = Background()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(background))
    slices = []
    real = LibraryPublisher._run_slices

    def timed(self, job):
        def wrapped(inner):
            def run():
                began = time.perf_counter()
                after = inner()
                slices.append(time.perf_counter() - began)
                return wrapped(after) if after is not None else None
            return run
        return real(self, wrapped(job))

    monkeypatch.setattr(LibraryPublisher, "_run_slices", timed)
    began = time.perf_counter()
    bridge = start()
    for _ in range(500):
        background.run()
        MainLoop.advance(0)
        if last(factory, INDEX) is not None and not background.jobs:
            break
    elapsed = time.perf_counter() - began
    index = last(factory, INDEX)
    row = index["locations"][0]["folders"][0]
    assert row["count"] + row["cut"] == 5000 and row["cut"] > 0
    published = factory.client.last(ROOT + "recordings/movie")
    assert len(published.text.encode()) + len(published.topic) + 7 <= 1000000
    # The main loop's share comes in slices; none holds the picture up for 50 ms.
    assert len(slices) > 1
    assert max(slices) < 0.05, max(slices)
    assert elapsed < 60
    assert bridge.publisher("recordings").find(published.json()["recordings"][0]["id"])


def test_a_folder_that_goes_is_retracted_and_the_topics_follow_the_index(image, start, factory):
    image.record("a.ts")
    image.record("b.ts", folder=image.movie / "Series")
    bridge = start()
    assert folder(factory, "movie_series") is not None
    os.remove(str(image.movie / "Series" / "b.ts"))
    bridge.publisher("recordings").request(library.REFRESH)
    retraction = factory.client.last(ROOT + "recordings/movie_series")
    assert retraction.text == "" and retraction.retain is True
    assert [one["slug"] for one in last(factory, INDEX)["locations"][0]["folders"]] == ["movie"]
    assert ROOT + "recordings/movie_series" not in bridge.state.retained_topics


def test_a_folder_an_earlier_run_left_is_retracted_at_the_connect(image, start, factory,
                                                                    state_path):
    from MQTTBridge.discovery import StateStore

    store = StateStore(path=state_path)
    store.remember(ROOT + "recordings/gone")
    store.save(force=True)
    image.record("a.ts")
    start()
    assert factory.client.last(ROOT + "recordings/gone").text == ""


def test_an_image_without_a_movie_list_claims_nothing_and_takes_back_an_earlier_runs_topics(
        make_bridge, factory, settings, receiver, state_path):
    from MQTTBridge.discovery import StateStore

    store = StateStore(path=state_path)
    for suffix in ("recordings", "recordings/movie", "timeshift"):
        store.remember(ROOT + suffix)
    store.save(force=True)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert bridge.publisher("recordings") is None
    assert "recordings" not in bridge.capabilities()
    for suffix in ("recordings", "recordings/movie", "timeshift"):
        assert factory.client.last(ROOT + suffix).text == "", suffix


def test_the_snapshot_publishes_the_library_again_at_every_connect(image, start, factory):
    image.record("a.ts")
    start()
    factory.client.clear()
    factory.client.fire_connect()
    assert last(factory, INDEX) is not None
    assert folder(factory, "movie")["recordings"][0]["file"] == "a.ts"


def test_nothing_is_published_again_when_nothing_changed(image, start, factory):
    image.record("a.ts")
    bridge = start()
    factory.client.clear()
    bridge.publisher("recordings")._bouquets_changed()
    assert factory.client.all_for(ROOT + "recordings/movie") == []
    assert factory.client.all_for(INDEX) == []


# ------------------------------------------------------------- the triggers --


def test_the_library_is_never_read_on_a_timer(image, start, factory, monkeypatch):
    image.record("a.ts")
    bridge = start()
    publisher = bridge.publisher("recordings")
    reads = []
    monkeypatch.setattr(publisher, "request", lambda reason: reads.append(reason))
    # Two hours of the main loop: every timer the bridge has fires, many times over.
    MainLoop.advance(2 * 3600 * 1000, limit=100000)
    assert reads == []
    assert not any(
        timer.running and not timer.started[1]
        for timer in eTimer.instances
        if any(getattr(function, "__self__", None) in (publisher._slices, publisher._settle,
                                                       publisher._delete_poll)
               for function in timer.callback)
    )


def test_a_recording_that_starts_or_ends_reads_the_library_once_the_burst_is_over(
        image, start, factory, receiver):
    from conftest import iRecordableService

    bridge = start()
    assert folder(factory, "movie") is None
    image.record("20261017 2015 - TVP 1 HD - Wiadomosci.ts")
    receiver.nav.fire_record(None, iRecordableService.evStart)
    receiver.nav.fire_record(None, iRecordableService.evStart)
    assert folder(factory, "movie") is None
    MainLoop.advance(library.RECORD_SETTLE_MILLISECONDS)
    assert last(factory, INDEX)["reason"] == "recording"
    assert len(folder(factory, "movie")["recordings"]) == 1
    publisher = bridge.publisher("recordings")
    asked = []
    publisher.request = asked.append
    receiver.nav.fire_record(None, iRecordableService.evRecordWriteError)
    MainLoop.advance(library.RECORD_SETTLE_MILLISECONDS)
    assert asked == []
    receiver.nav.fire_record(None, iRecordableService.evEnd)
    MainLoop.advance(library.RECORD_SETTLE_MILLISECONDS)
    assert asked == ["recording"]


def test_a_location_that_is_mounted_is_read_and_one_that_goes_keeps_its_list(
        image, start, factory):
    image.record("a.ts")
    bridge = start()
    publisher = bridge.publisher("recordings")
    location = str(image.movie)
    factory.client.clear()
    publisher.on_space({location: (False, None)})
    index = last(factory, INDEX)
    assert index["locations"][0]["mounted"] is False
    assert index["locations"][0]["free_mb"] is None
    assert index["reason"] == "start"
    assert factory.client.all_for(ROOT + "recordings/movie") == []
    image.record("b.ts")
    publisher.on_space({location: (True, 77)})
    index = last(factory, INDEX)
    assert index["reason"] == "mount" and index["locations"][0]["mounted"] is True
    assert len(folder(factory, "movie")["recordings"]) == 2


def test_free_space_alone_goes_out_on_the_index(image, start, factory):
    image.record("a.ts")
    bridge = start()
    publisher = bridge.publisher("recordings")
    factory.client.clear()
    publisher.on_space({str(image.movie): (True, 123)})
    assert last(factory, INDEX)["locations"][0]["free_mb"] == 123
    assert last(factory, INDEX)["reason"] == "start"
    factory.client.clear()
    publisher.on_space({str(image.movie): (True, 123)})
    assert factory.client.all_for(INDEX) == []


def test_an_unmounted_location_is_not_walked_but_by_a_refresh(image, start, factory,
                                                              monkeypatch):
    image.mount_lines = ["rootfs / rootfs rw 0 0"]
    image.write_mounts()
    image.record("a.ts")
    walked = []
    real = library.walk
    monkeypatch.setattr(library, "walk", lambda location: walked.append(location) or real(
        location))
    bridge = start()
    assert walked == []
    assert last(factory, INDEX)["locations"][0]["folders"] == []
    # Nothing is asked of a location that is not mounted - not its free space either.
    assert last(factory, INDEX)["locations"][0]["free_mb"] is None
    send(factory, "recordings_refresh", "PRESS")
    assert walked == [str(image.movie)]
    assert last(factory, INDEX)["reason"] == "refresh"
    assert len(folder(factory, "movie")["recordings"]) == 1
    assert bridge.publisher("recordings") is not None


def test_the_recording_disks_minute_brings_the_mounts_and_the_space(image, start, factory,
                                                                     monkeypatch, wait_until):
    from MQTTBridge import hdd

    image.record("a.ts")
    bridge = start()
    monkeypatch.setattr(library, "free_megabytes", lambda path: 4242)
    monkeypatch.setattr(hdd, "read", lambda path=None: {"mounted": True, "path": "/media/hdd",
                                                        "free_mb": 1})
    bridge.publisher("hdd")._poll()
    assert wait_until(lambda: last(factory, INDEX)["locations"][0]["free_mb"] == 4242)


def test_one_read_at_a_time_a_refresh_during_a_read_is_answered_by_it(image, start, factory,
                                                                      monkeypatch):
    image.record("a.ts")
    background = Background()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(background))
    walks = []
    real = LibraryPublisher._walk
    monkeypatch.setattr(LibraryPublisher, "_walk", staticmethod(
        lambda *arguments: walks.append(arguments[2]) or real(*arguments)))
    bridge = start()
    background.run()
    publisher = bridge.publisher("recordings")
    assert walks == ["start"]
    assert publisher.request(library.REFRESH) is True
    assert publisher.request(library.REFRESH) is False
    send(factory, "recordings_refresh", "PRESS")
    assert len(background.jobs) == 1
    assert error(factory) is None
    background.run()
    assert last(factory, INDEX)["reason"] == "refresh"
    assert background.jobs == []
    # One read answered all three refreshes.
    assert walks == ["start", "refresh"]


def test_an_event_during_a_read_is_read_again_after_it(image, start, factory, monkeypatch):
    image.record("a.ts")
    background = Background()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(background))
    bridge = start()
    background.run()
    publisher = bridge.publisher("recordings")
    publisher.request(library.REFRESH)
    publisher.request(library.MOUNT)
    background.run()
    assert last(factory, INDEX)["reason"] == "mount"


def test_a_read_that_fails_leaves_the_last_list_and_reads_again_later(image, start, factory,
                                                                      monkeypatch):
    image.record("a.ts")
    bridge = start()

    def broken(*_arguments):
        raise OSError("the disk went away")

    monkeypatch.setattr(LibraryPublisher, "_walk", staticmethod(broken))
    bridge.publisher("recordings").request(library.REFRESH)
    assert last(factory, INDEX)["reason"] == "start"
    assert bridge.publisher("recordings").request(library.REFRESH) is True


# ------------------------------------------------------------------- play --


def an_id(path):
    return library.recording_id(path)


def test_play_opens_the_images_movie_player_and_is_verified_on_service(
        image, start, factory, receiver):
    path = image.record("a.ts")
    start()
    send(factory, "play_recording", {"id": an_id(path)})
    assert image.player_opened == ["1:0:0:0:0:0:0:0:0:0:" + path]
    assert receiver.nav.sref == "1:0:0:0:0:0:0:0:0:0:" + path
    assert error(factory) is None


def test_play_hands_a_second_recording_to_the_player_already_open(image, start, factory,
                                                                  receiver):
    first = image.record("a.ts")
    second = image.record("b.ts")
    start()
    send(factory, "play_recording", {"id": an_id(first)})
    player = receiver.session.current_dialog
    chosen = []
    player.movieSelected = lambda ref: chosen.append(ref.reference) or receiver.nav.playService(
        ref)
    send(factory, "play_recording", {"id": an_id(second)})
    assert chosen == ["1:0:0:0:0:0:0:0:0:0:" + second]
    assert image.player_opened == ["1:0:0:0:0:0:0:0:0:0:" + first]


def test_an_image_without_open_movie_player_goes_through_movie_selected(image, start, factory,
                                                                        receiver):
    from conftest import InfoBar

    path = image.record("a.ts")
    start(player=False)
    receiver.with_channel_list()
    infobar = InfoBar.instance
    selected = []

    def movie_selected(service):
        selected.append((service.reference, infobar.lastservice))
        receiver.nav.playService(service)

    infobar.movieSelected = movie_selected
    send(factory, "play_recording", {"id": an_id(path)})
    assert selected[0][0] == "1:0:0:0:0:0:0:0:0:0:" + path


def test_play_that_does_not_start_is_reported(image, start, factory, receiver):
    path = image.record("a.ts")
    bridge = start()
    infobar = __import__("conftest").InfoBar.instance
    infobar.openMoviePlayer = lambda ref, LastService=None: None
    send(factory, "play_recording", {"id": "deadbeefdeadbeef"})
    assert error(factory)["reason"] == "not_in_library"
    send(factory, "play_recording", {"id": an_id(path), })
    bridge.publisher("service")._verify.timer.fire()
    refusal = error(factory)
    assert refusal["cmd"] == "play_recording"
    assert "did not start playing the recording" in refusal["error"]


def test_an_unknown_id_is_refused_before_a_sleeping_receiver_is_woken(image, start, factory,
                                                                      receiver):
    image.record("a.ts")
    start()
    screen = receiver.enter_standby()
    send(factory, "play_recording", {"id": "0123456789abcdef"})
    assert error(factory) == {"cmd": "play_recording", "error":
                              "that recording is not in the receiver's library",
                              "reason": "not_in_library", "ts": error(factory)["ts"]}
    from conftest import standby_module

    assert standby_module.inStandby is screen


def test_play_from_standby_wakes_the_receiver_first(image, start, factory, receiver):
    path = image.record("a.ts")
    start()
    receiver.enter_standby()
    send(factory, "play_recording", {"id": an_id(path)})
    assert image.player_opened == []
    MainLoop.advance(1000)
    assert image.player_opened == ["1:0:0:0:0:0:0:0:0:0:" + path]


@pytest.mark.parametrize("payload", ['{"path": "/etc/passwd"}', "a.ts", '{"id": 5}', ""])
def test_play_takes_an_id_and_nothing_else(image, start, factory, payload):
    image.record("a.ts")
    start()
    send(factory, "play_recording", payload)
    assert error(factory)["error"] == library.NO_ID
    assert image.player_opened == []


def test_play_of_a_file_that_is_gone_is_refused(image, start, factory):
    path = image.record("a.ts")
    start()
    os.remove(path)
    send(factory, "play_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "file_gone"
    assert image.player_opened == []


def test_play_is_claimed_with_an_info_bar_to_open_the_player_from(image, start, factory):
    image.record("a.ts")
    bridge = start(player=False)
    assert "recording_play" not in bridge.capabilities()


# ------------------------------------------------------------------- delete --


@pytest.fixture
def allowed(settings):
    settings.recording_delete_allowed.value = True
    return settings


def test_delete_moves_the_recording_and_its_files_into_the_trash(image, start, factory,
                                                                 allowed):
    path = image.record("a.ts", meta=meta_text(), ap=access_points(0, 90000), eit=True,
                        cuts=True)
    image.record("b.ts")
    start()
    send(factory, "delete_recording", {"id": an_id(path)})
    trash = image.movie / ".Trash"
    assert sorted(os.listdir(str(trash))) == ["a.eit", "a.ts", "a.ts.ap", "a.ts.cuts",
                                              "a.ts.meta"]
    assert not os.path.exists(path)
    assert image.resume_points[0].reference == "1:0:0:0:0:0:0:0:0:0:" + path
    index = last(factory, INDEX)
    assert index["reason"] == "delete"
    assert [entry["file"] for entry in folder(factory, "movie")["recordings"]] == ["b.ts"]
    assert error(factory) is None


def test_delete_is_verified_when_the_images_job_has_moved_the_file(image, start, factory,
                                                                   allowed):
    path = image.record("a.ts")
    start()
    image.deferred = True
    send(factory, "delete_recording", {"id": an_id(path)})
    assert last(factory, INDEX)["reason"] == "start"
    MainLoop.advance(library.DELETE_POLL_MILLISECONDS)
    assert last(factory, INDEX)["reason"] == "start"
    image.jobs.finish()
    MainLoop.advance(library.DELETE_POLL_MILLISECONDS)
    assert last(factory, INDEX)["reason"] == "delete"
    assert folder(factory, "movie") is None


def test_a_move_that_never_happens_is_reported(image, start, factory, allowed):
    path = image.record("a.ts")
    start()
    image.deferred = True
    factory.client.fire_message(CMD + "delete_recording",
                                json.dumps({"id": "c1", "payload": {"id": an_id(path)}}))
    MainLoop.advance(library.DELETE_WAIT_SECONDS * 1000 + library.DELETE_POLL_MILLISECONDS,
                     limit=1000)
    refusal = error(factory)
    assert refusal["error"] == library.NOT_MOVED and refusal["id"] == "c1"
    assert "reason" not in refusal
    assert os.path.exists(path)


def test_delete_is_refused_unless_the_permission_is_on(image, start, factory, settings):
    path = image.record("a.ts")
    start()
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "not_permitted"
    assert os.path.exists(path) and image.moves == []


def test_the_permission_is_never_writable_over_mqtt(image, start, factory, settings):
    image.record("a.ts")
    start()
    send(factory, "config", {"recording_delete_allowed": True})
    assert "recording_delete_allowed" in error(factory)["error"]
    assert settings.recording_delete_allowed.value is False


def test_the_page_needs_no_permission_as_for_every_box_side_permission(image, start, factory):
    path = image.record("a.ts")
    bridge = start()
    assert bridge.run_command("delete_recording", json.dumps({"id": an_id(path)}),
                              "page") is None
    assert not os.path.exists(path)


def test_delete_with_the_trash_switched_off_deletes_nothing(image, start, factory, allowed):
    path = image.record("a.ts")
    bridge = start()
    image.usage.movielist_trashcan.value = False
    send(factory, "delete_recording", {"id": an_id(path)})
    refusal = error(factory)
    assert refusal["error"] == "the receiver's trash is switched off; nothing is deleted for good"
    assert refusal["reason"] == "trash_off"
    assert os.path.exists(path) and image.moves == []
    assert "recording_delete" not in bridge.capabilities()
    assert bridge.build_info()["recording_trash"] is False


def test_an_image_without_a_trash_deletes_nothing(tmp_path, monkeypatch, make_bridge, factory,
                                                  settings, receiver):
    image = PvrImage(tmp_path, monkeypatch).install(trash_function=False)
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(inline))
    settings.recording_delete_allowed.value = True
    path = image.record("a.ts")
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["error"] == library.NO_TRASH and "reason" not in error(factory)
    assert os.path.exists(path)
    assert bridge.build_info()["recording_trash"] is None
    assert "recording_delete" not in bridge.capabilities()


def test_delete_of_an_unknown_id_or_a_path_is_refused(image, start, factory, allowed):
    path = image.record("a.ts")
    start()
    send(factory, "delete_recording", {"id": "0123456789abcdef"})
    assert error(factory)["reason"] == "not_in_library"
    send(factory, "delete_recording", {"path": path})
    assert error(factory)["error"] == library.NO_ID
    assert image.moves == []


def test_a_recording_being_recorded_is_not_deleted(image, start, factory, allowed, receiver):
    path = image.record("20261017 2015 - TVP 1 HD - Wiadomosci.ts")
    start()
    timer = receiver.add_timer(state=RecordTimerEntry.StateRunning)
    timer.Filename = path[:-3]
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "in_use"
    assert os.path.exists(path)


def test_a_recording_being_played_is_not_deleted(image, start, factory, allowed, receiver):
    path = image.record("a.ts")
    start()
    receiver.nav.sref = "1:0:0:0:0:0:0:0:0:0:" + path
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "in_use"
    assert os.path.exists(path)


def test_a_move_the_image_refuses_is_a_refusal_and_never_a_delete_for_good(
        image, start, factory, allowed):
    path = image.record("a.ts")
    start()
    image.move_raises = OSError("cross-device")
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["error"] == library.MOVE_FAILED
    assert os.path.exists(path)
    image.move_raises = None
    image.trash_makes = False
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["error"] == library.MOVE_FAILED
    assert os.path.exists(path)
    # Without a trash folder the move is never asked for.
    assert image.moves == []


def test_delete_of_a_file_that_is_gone_is_refused(image, start, factory, allowed):
    path = image.record("a.ts")
    start()
    os.remove(path)
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "file_gone"


def test_delete_is_claimed_while_the_permission_is_on_and_the_trash_is_on(image, start,
                                                                          factory, settings):
    image.record("a.ts")
    bridge = start()
    assert "recording_delete" not in bridge.capabilities()
    assert bridge.build_info()["recording_trash"] is True
    settings.recording_delete_allowed.value = True
    assert "recording_delete" in bridge.capabilities()
    image.usage.movielist_trashcan.value = False
    assert "recording_delete" not in bridge.capabilities()


def test_a_save_of_the_permission_applies_it_live_without_a_reconnect(image, start, factory,
                                                                      settings):
    image.record("a.ts")
    bridge = start()
    clients = len(factory.clients)
    assert bridge.apply_settings({"recording_delete_allowed": True}) is None
    assert bridge.last_save_reconnected is False
    assert len(factory.clients) == clients
    info = last(factory, INFO)
    assert info["settings"]["recording_delete_allowed"] is True
    assert "recording_delete" in info["capabilities"]
    assert bridge.apply_settings({"recording_delete_allowed": False}) is None
    assert "recording_delete" not in last(factory, INFO)["capabilities"]


def test_commands_wait_for_the_first_read(image, start, factory, monkeypatch, allowed):
    background = Background()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(background))
    start()
    send(factory, "play_recording", {"id": "0123456789abcdef"})
    assert error(factory)["error"] == library.NOT_AVAILABLE
    send(factory, "delete_recording", {"id": "0123456789abcdef"})
    assert error(factory)["error"] == library.NOT_AVAILABLE


def test_the_commands_refuse_during_an_update(image, start, factory, monkeypatch, allowed):
    path = image.record("a.ts")
    bridge = start()
    monkeypatch.setattr(bridge.self_update, "doors_refusal",
                        lambda name=None: "an update is being applied")
    for command in ("play_recording", "delete_recording", "recordings_refresh"):
        send(factory, command, {"id": an_id(path)})
        assert error(factory)["error"] == "an update is being applied"
        assert error(factory)["reason"] == "busy"
    assert os.path.exists(path) and image.player_opened == []


def test_a_stopped_library_drops_a_result_from_before(image, start, factory, monkeypatch):
    image.record("a.ts")
    background = Background()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(background))
    bridge = start()
    publisher = bridge.publisher("recordings")
    publisher.stop()
    # The walk that was running comes back, and nothing more is started from it.
    assert background.run() == 1
    assert publisher.snapshot() == {}
    assert last(factory, INDEX) is None


def test_a_recording_played_through_a_link_on_the_flash_is_not_deleted(image, start, factory,
                                                                       allowed, receiver):
    os.symlink(str(image.disk), str(image.tmp / "hdd"))
    path = image.record("a.ts")
    start()
    receiver.nav.sref = "1:0:0:0:0:0:0:0:0:0:" + str(image.tmp / "hdd" / "movie" / "a.ts")
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "in_use"
    assert os.path.exists(path)


def test_a_recording_written_through_a_link_on_the_flash_is_not_deleted(image, start, factory,
                                                                        allowed, receiver):
    os.symlink(str(image.disk), str(image.tmp / "hdd"))
    path = image.record("a.ts")
    start()
    timer = receiver.add_timer(state=RecordTimerEntry.StateRunning)
    timer.Filename = str(image.tmp / "hdd" / "movie" / "a")
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory)["reason"] == "in_use"


def test_a_justplay_timer_on_the_same_file_does_not_block_a_delete(image, start, factory,
                                                                   allowed, receiver):
    path = image.record("a.ts")
    start()
    timer = receiver.add_timer(state=RecordTimerEntry.StateRunning, justplay=True)
    timer.Filename = path[:-3]
    send(factory, "delete_recording", {"id": an_id(path)})
    assert error(factory) is None
    assert not os.path.exists(path)


def test_a_second_delete_while_the_first_waits_for_its_move_is_refused(image, start, factory,
                                                                       allowed):
    first = image.record("a.ts")
    second = image.record("b.ts")
    start()
    image.deferred = True
    send(factory, "delete_recording", {"id": an_id(first)})
    send(factory, "delete_recording", {"id": an_id(second)})
    assert error(factory)["reason"] == "busy"
    assert os.path.exists(second)


def test_the_permission_is_asked_first_and_by_the_library_itself(image, start, factory):
    path = image.record("a.ts")
    bridge = start()
    send(factory, "delete_recording", "not an id")
    assert error(factory)["reason"] == "not_permitted"
    publisher = bridge.publisher("recordings")
    assert publisher.delete(an_id(path)).reason == "not_permitted"
    assert os.path.exists(path)


def test_the_main_loops_share_is_spread_over_turns(image, start, factory, monkeypatch):
    for index in range(30):
        image.record(f"{index:02d}.ts", meta=meta_text(sref=f"1:0:19:{index + 1:X}:1:1:0:0:0:0:",
                                                      created=1790000000 + index))
    monkeypatch.setattr(library, "SLICE_SECONDS", 0)
    monkeypatch.setattr(library, "NAMES_PER_SLICE", 5)
    background = Background()
    monkeypatch.setattr(LibraryPublisher, "run_in_background", staticmethod(background))
    bridge = start()
    publisher = bridge.publisher("recordings")
    background.jobs.pop(0)()
    # Five names on this turn; the rest wait for the next turns of the main loop.
    assert background.jobs == []
    assert publisher._slices.timer is not None and publisher._slices.timer.running
    assert last(factory, INDEX) is None
    for _ in range(100):
        MainLoop.advance(0)
        background.run()
        MainLoop.advance(0)
        if last(factory, INDEX) is not None:
            break
    assert len(folder(factory, "movie")["recordings"]) == 30
    assert all(entry["sref"] for entry in folder(factory, "movie")["recordings"])


def test_the_default_movie_location_comes_first_then_the_images_order(image, start, factory,
                                                                       tmp_path):
    usb = tmp_path / "media" / "usb" / "movie"
    usb.mkdir(parents=True)
    image.mount_lines.append("/dev/sdb1 " + str(usb.parent) + " vfat rw 0 0")
    image.write_mounts()
    image.movielist.videodirs.value = [str(usb) + "/", str(image.movie) + "/"]
    image.usage.default_path.value = str(image.movie) + "/"
    image.record("a.ts")
    image.record("b.ts", folder=usb)
    start()
    assert [location["path"] for location in last(factory, INDEX)["locations"]] == [
        str(image.movie), str(usb)]


def test_the_codes_a_refusal_carries_are_the_contracts(image, start, factory, allowed,
                                                       receiver):
    path = image.record("a.ts")
    start()
    codes = set()
    for command, payload in (("play_recording", {"id": "0123456789abcdef"}),
                             ("delete_recording", {"id": "0123456789abcdef"})):
        send(factory, command, payload)
        codes.add((command, error(factory)["reason"]))
    receiver.nav.sref = "1:0:0:0:0:0:0:0:0:0:" + path
    send(factory, "delete_recording", {"id": an_id(path)})
    codes.add(("delete_recording", error(factory)["reason"]))
    assert codes == {("play_recording", "not_in_library"), ("delete_recording", "not_in_library"),
                     ("delete_recording", "in_use")}
