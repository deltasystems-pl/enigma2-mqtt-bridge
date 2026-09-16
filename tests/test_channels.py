"""The bouquets, what is in them, and how they are addressed."""

import conftest
from conftest import MARKER, POLSAT, TVN, TVP1

from MQTTBridge import channels as channels_module

NODE = "vuuno4kse_005301"
CHANNELS = "enigma2/" + NODE + "/channels"


def test_the_channel_list_has_the_documented_shape(live_bridge, factory):
    payload = factory.client.last(CHANNELS).json()
    assert set(payload) == {"generated", "bouquets"}
    assert isinstance(payload["generated"], int)
    bouquet = payload["bouquets"][0]
    assert set(bouquet) == {"name", "sref", "channels"}
    assert set(bouquet["channels"][0]) == {"sref", "name"}


def test_every_configured_bouquet_is_published(live_bridge, factory):
    names = [b["name"] for b in factory.client.last(CHANNELS).json()["bouquets"]]
    assert names == ["Ulubione TV", "Sport (HD)"]


def test_markers_are_not_channels(live_bridge, factory):
    """A marker offered as a channel is an option in a select box that cannot tune."""
    channels = factory.client.last(CHANNELS).json()["bouquets"][0]["channels"]
    assert [c["sref"] for c in channels] == [TVP1, TVN]
    assert MARKER not in [c["sref"] for c in channels]


def test_hidden_and_numbered_markers_are_not_channels():
    assert channels_module.is_playable(TVP1)
    assert not channels_module.is_playable("1:64:0:0:0:0:0:0:0:0::Heading")
    assert not channels_module.is_playable("1:320:0:0:0:0:0:0:0:0::Numbered")
    assert not channels_module.is_playable("1:832:0:0:0:0:0:0:0:0::Hidden")
    assert not channels_module.is_playable('1:7:1:0:0:0:0:0:0:0:FROM BOUQUET "x"')
    assert not channels_module.is_playable("")


def test_a_reference_with_no_flags_field_is_not_a_channel():
    assert not channels_module.is_playable("nonsense")


def test_the_bouquet_selection_filters_by_name(make_bridge, factory, settings, receiver):
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.bouquets_for_select.value = "Sport (HD)"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    names = [b["name"] for b in factory.client.last(CHANNELS).json()["bouquets"]]
    assert names == ["Sport (HD)"]


def test_the_bouquet_selection_also_takes_a_slug(make_bridge, factory, settings, receiver):
    """A name with brackets and spaces is awkward to type into a settings screen."""
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    settings.bouquets_for_select.value = "sport_hd"
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    names = [b["name"] for b in factory.client.last(CHANNELS).json()["bouquets"]]
    assert names == ["Sport (HD)"]


def test_several_bouquets_are_separated_by_commas():
    assert channels_module.selection_from("One, Two ,Three") == ["One", "Two", "Three"]
    assert channels_module.selection_from("") == []
    assert channels_module.selection_from(None) == []


def test_an_empty_selection_means_every_bouquet(live_bridge, factory):
    assert len(factory.client.last(CHANNELS).json()["bouquets"]) == 2


def test_a_box_with_no_bouquet_list_falls_back_to_its_favourites(make_bridge, factory, settings,
                                                                 receiver):
    """„Multiple bouquets" off means one favourites list and no bouquet list at all."""
    favourites = channels_module.bouquet_roots()[1]
    receiver.service_center.contents = {favourites: [(TVP1, "TVP 1 HD")]}
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    payload = factory.client.last(CHANNELS).json()
    assert payload["bouquets"][0]["name"] == channels_module.FAVOURITES_NAME
    assert payload["bouquets"][0]["channels"][0]["sref"] == TVP1


def test_the_bouquet_root_carries_the_images_own_type_filter():
    """The list of service types differs between images and between releases."""
    root = channels_module.bouquet_roots()[0]
    assert root.startswith(conftest.channel_selection_module.service_types_tv)
    assert 'FROM BOUQUET "bouquets.tv"' in root


def test_the_built_in_type_filter_is_used_when_the_image_exports_none(monkeypatch):
    monkeypatch.delattr(conftest.channel_selection_module, "service_types_tv")
    assert channels_module.service_types_tv() == channels_module.SERVICE_TYPES_TV


def test_which_bouquet_a_service_came_from(live_bridge):
    lookup = live_bridge.publisher("channels").bouquet_for
    assert lookup(TVP1) == "Ulubione TV"
    assert lookup(POLSAT) == "Sport (HD)"
    assert lookup("1:0:19:9999:3FB:1:C00000:0:0:0:") is None


def test_the_first_bouquet_wins_a_service_that_is_in_two(live_bridge, receiver):
    receiver.service_center.contents[conftest.SECOND_BOUQUET].append((TVP1, "TVP 1 HD"))
    live_bridge.publisher("channels").refresh()
    assert live_bridge.publisher("channels").bouquet_for(TVP1) == "Ulubione TV"


def test_a_name_resolves_to_one_service(live_bridge):
    sref, error = live_bridge.publisher("channels").find_by_name("TVN HD")
    assert (sref, error) == (TVN, None)


def test_a_name_is_not_case_sensitive(live_bridge):
    sref, error = live_bridge.publisher("channels").find_by_name("  tvn hd ")
    assert (sref, error) == (TVN, None)


def test_an_unknown_name_is_refused(live_bridge):
    sref, error = live_bridge.publisher("channels").find_by_name("Nonexistent")
    assert sref is None
    assert "no channel called 'Nonexistent'" in error


def test_a_name_in_two_bouquets_is_refused_with_its_count(live_bridge, receiver):
    """🔴 Four „Sport" channels on four satellites is a coin toss nobody asked for."""
    receiver.service_center.contents[conftest.SECOND_BOUQUET].append((TVN, "TVN HD"))
    receiver.service_center.contents[conftest.SECOND_BOUQUET][-1] = (
        "1:0:19:1234:3FB:1:C00000:0:0:0:", "TVN HD"
    )
    live_bridge.publisher("channels").refresh()
    sref, error = live_bridge.publisher("channels").find_by_name("TVN HD")
    assert sref is None
    assert "not unique (2 matches)" in error


def test_the_same_service_in_two_bouquets_is_still_unique(live_bridge, receiver):
    receiver.service_center.contents[conftest.SECOND_BOUQUET].append((TVN, "TVN HD"))
    live_bridge.publisher("channels").refresh()
    sref, error = live_bridge.publisher("channels").find_by_name("TVN HD")
    assert (sref, error) == (TVN, None)


def test_an_empty_name_is_refused(live_bridge):
    sref, error = live_bridge.publisher("channels").find_by_name("  ")
    assert sref is None and "no channel name" in error


def test_the_list_is_re_read_when_a_bouquet_file_changes(live_bridge, factory, receiver,
                                                         monkeypatch):
    """There is no event for this: the files are compared once a minute."""
    stamps = [(("bouquets.tv", 1.0),)]
    monkeypatch.setattr(channels_module, "bouquet_mtimes", lambda directory=None: stamps[0])
    publisher = live_bridge.publisher("channels")
    publisher._poll()
    factory.client.clear()
    receiver.service_center.contents[conftest.FIRST_BOUQUET].append((POLSAT, "Polsat Sport"))
    stamps[0] = (("bouquets.tv", 2.0),)
    publisher._poll()
    channels = factory.client.last(CHANNELS).json()["bouquets"][0]["channels"]
    assert [c["sref"] for c in channels] == [TVP1, TVN, POLSAT]


def test_the_list_is_left_alone_when_nothing_changed(live_bridge, factory, monkeypatch):
    monkeypatch.setattr(channels_module, "bouquet_mtimes", lambda directory=None: (("x", 1.0),))
    publisher = live_bridge.publisher("channels")
    publisher._poll()
    factory.client.clear()
    publisher._poll()
    assert factory.client.all_for(CHANNELS) == []


def test_the_poll_runs_every_minute(live_bridge):
    assert live_bridge.publisher("channels")._ticker.timer.started == (60000, False)


def test_a_bouquet_change_tells_whoever_asked(live_bridge):
    seen = []
    live_bridge.publisher("channels").when_changed(lambda: seen.append(1))
    live_bridge.publisher("channels").refresh()
    assert seen == [1]


def test_a_listener_that_raises_does_not_stop_the_refresh(live_bridge, factory, plugin_log):
    publisher = live_bridge.publisher("channels")
    publisher.when_changed(lambda: 1 / 0)
    factory.client.clear()
    assert publisher.refresh() is not None
    assert "bouquet-change listener raised" in plugin_log()


def test_no_service_centre_means_no_channel_list(make_bridge, factory, settings, receiver,
                                                 monkeypatch):
    monkeypatch.setattr(channels_module, "_service_center", lambda: None)
    settings.host.value = "10.0.0.5"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    assert "channels" not in bridge.capabilities()


def test_the_mtimes_of_a_directory_that_is_not_there(tmp_path):
    assert channels_module.bouquet_mtimes(str(tmp_path / "nowhere")) == ()


def test_the_mtimes_cover_the_bouquet_files(tmp_path):
    (tmp_path / "bouquets.tv").write_text("x")
    (tmp_path / "userbouquet.favourites.tv").write_text("y")
    (tmp_path / "settings").write_text("not a bouquet")
    names = [name for name, _stamp in channels_module.bouquet_mtimes(str(tmp_path))]
    assert names == ["bouquets.tv", "userbouquet.favourites.tv"]


def test_picons_are_not_published(live_bridge, factory):
    """They are tens of kilobytes each and OpenWebif is on the same address."""
    assert "picon" not in factory.client.last(CHANNELS).text
