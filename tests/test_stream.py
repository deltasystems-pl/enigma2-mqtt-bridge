"""What only the receiver knows about streaming: where, who, stop one, and could it.

The stream server in `conftest.py` follows `lib/dvb/streamserver.cpp` as
OpenViX, OpenATV and OpenPLi have it, and is not in the fake image unless a
test puts it there - so the rest of the suite sees a receiver without one.
"""

import json

import pytest
from Components.config import ConfigInteger, ConfigSubsection, ConfigYesNo, config
from conftest import (
    POLSAT,
    TVN,
    TVP1,
    ServiceCenter,
    StreamServer,
    install_stream_server,
)

from MQTTBridge import stream, webif
from MQTTBridge.commands import CommandDispatcher
from MQTTBridge.origin import PAGE

NODE = "vuuno4kse_005301"
ROOT = "enigma2/" + NODE
STREAM = ROOT + "/stream"
CLIENTS = ROOT + "/stream_clients"
PLAYABLE = ROOT + "/stream_playable"
INFO = ROOT + "/info"
LAST_ERROR = ROOT + "/last_error"

PHONE = "::ffff:192.0.2.20"
TABLET = "2001:db8::17"
ELSEWHERE = "1:0:19:9999:3FB:1:C00000:0:0:0:"


@pytest.fixture
def server(monkeypatch):
    return install_stream_server(monkeypatch)


@pytest.fixture
def box(server, live_bridge):
    """A connected bridge on a receiver whose image has a stream server."""
    return live_bridge


@pytest.fixture
def image(monkeypatch):
    """The image's own settings a stream is described from; absent from the shared stub."""
    streaming = ConfigSubsection()
    streaming.authentication = ConfigYesNo(default=False)
    openwebif = ConfigSubsection()
    openwebif.streamport = ConfigInteger(default=8001, limits=(1, 65535))
    transcoding = ConfigSubsection()
    transcoding.port = ConfigInteger(default=8002, limits=(8002, 9999))
    monkeypatch.setattr(config, "streaming", streaming, raising=False)
    monkeypatch.setattr(config, "OpenWebif", openwebif, raising=False)
    monkeypatch.setattr(config.plugins, "transcodingsetup", transcoding, raising=False)
    return config


def start(make_bridge, factory, settings, receiver):
    settings.host.value = "192.0.2.2"
    settings.node_id.value = NODE
    bridge = make_bridge(session=receiver.session)
    bridge.start()
    factory.client.fire_connect()
    return bridge


def tick(bridge):
    bridge.publisher("stream")._ticker.timer.fire()


def send(factory, name, payload):
    if not isinstance(payload, (bytes, str)):
        payload = json.dumps(payload)
    factory.client.fire_message(ROOT + "/cmd/" + name, payload)


def capabilities(factory):
    return factory.client.last(INFO).json()["capabilities"]


# ------------------------------------------------------------- the descriptor --


def test_the_stream_topic_says_where_the_stream_server_is(box, factory):
    published = factory.client.last(STREAM)
    assert published.retain is True
    assert published.json() == {
        "port": 8001, "path": "/{sref}", "authentication": None,
        "encoder": False, "encoder_port": None,
    }


def test_the_port_is_the_one_openwebif_is_set_to(image, server, make_bridge, factory, settings,
                                                 receiver):
    image.OpenWebif.streamport.value = 18001
    start(make_bridge, factory, settings, receiver)
    assert factory.client.last(STREAM).json()["port"] == 18001


@pytest.mark.parametrize("value", [0, 65536, "eight thousand", None, True])
def test_a_port_that_is_not_one_is_the_default(monkeypatch, value):
    section = ConfigSubsection()
    section.streamport = type("Element", (), {"value": value})()
    monkeypatch.setattr(config, "OpenWebif", section, raising=False)
    assert stream.stream_port() == 8001


def test_a_port_kept_as_text_is_read_as_a_number(monkeypatch):
    section = ConfigSubsection()
    section.streamport = type("Element", (), {"value": " 8003 "})()
    monkeypatch.setattr(config, "OpenWebif", section, raising=False)
    assert stream.stream_port() == 8003


@pytest.mark.parametrize("port", [1, 65535])
def test_the_ends_of_the_port_range_are_ports(port):
    assert stream._port(port) == port


def test_a_publisher_that_never_started_claims_nothing():
    publisher = stream.StreamPublisher()
    assert publisher.extra_capabilities() == []
    assert publisher.stop_available() is False
    assert publisher.playable_available() is False
    assert publisher.descriptor()["encoder"] is False
    assert publisher.stop_client("192.0.2.20", TVP1) == (
        "stopping a stream client is not available on this image")
    assert publisher.playable(TVP1) == "this image cannot say whether a service can be played"


def test_a_setting_that_cannot_be_read_is_no_setting(monkeypatch):
    class Trap:
        def __getattr__(self, name):
            raise RuntimeError("this image's settings object is not what it was")

    monkeypatch.setattr(config, "OpenWebif", Trap(), raising=False)
    assert stream.stream_port() == 8001


def test_whether_the_stream_wants_a_login_is_the_images_setting(image, box, factory):
    assert factory.client.last(STREAM).json()["authentication"] is False
    image.streaming.authentication.value = True
    factory.client.clear()
    tick(box)
    assert factory.client.last(STREAM).json()["authentication"] is True


def test_a_login_setting_that_is_no_boolean_is_unknown(monkeypatch):
    section = ConfigSubsection()
    section.authentication = type("Element", (), {"value": "yes"})()
    monkeypatch.setattr(config, "streaming", section, raising=False)
    assert stream.authentication() is None


def test_the_descriptor_carries_no_credential(image, box, factory):
    """Whether a login is wanted is a boolean. Nothing else about it is on the broker."""
    image.streaming.authentication.value = True
    tick(box)
    assert set(factory.client.last(STREAM).json()) == {
        "port", "path", "authentication", "encoder", "encoder_port"}


def test_the_descriptor_is_published_only_when_it_changed(image, box, factory):
    factory.client.clear()
    tick(box)
    tick(box)
    assert factory.client.all_for(STREAM) == []
    image.OpenWebif.streamport.value = 8003
    tick(box)
    assert [one.json()["port"] for one in factory.client.all_for(STREAM)] == [8003]


@pytest.mark.parametrize("which", [0, 1, 2])
def test_a_receiver_with_an_encoder_says_so_and_names_its_port(
    image, server, make_bridge, factory, settings, receiver, monkeypatch, tmp_path, which
):
    paths = [str(tmp_path / "bcm_enc0"), str(tmp_path / "encoder-proc"), str(tmp_path / "enc0")]
    (tmp_path / paths[which].rsplit("/", 1)[1]).write_text("")
    monkeypatch.setattr(stream, "ENCODER_PATHS", tuple(paths))
    start(make_bridge, factory, settings, receiver)
    payload = factory.client.last(STREAM).json()
    assert payload["encoder"] is True
    assert payload["encoder_port"] == 8002


def test_the_encoders_port_is_left_out_where_the_image_does_not_say(
    server, make_bridge, factory, settings, receiver, monkeypatch, tmp_path
):
    """Never a guessed 8002: without the transcoding plugin's setting the port is null."""
    (tmp_path / "bcm_enc0").write_text("")
    monkeypatch.setattr(stream, "ENCODER_PATHS", (str(tmp_path / "bcm_enc0"),))
    start(make_bridge, factory, settings, receiver)
    payload = factory.client.last(STREAM).json()
    assert (payload["encoder"], payload["encoder_port"]) == (True, None)


def test_a_receiver_without_an_encoder_names_no_encoder_port(image, box, factory):
    """The transcoding plugin's setting can be there on a receiver that cannot transcode."""
    payload = factory.client.last(STREAM).json()
    assert (payload["encoder"], payload["encoder_port"]) == (False, None)


def test_the_encoder_is_looked_for_once_and_not_on_the_tick(box, monkeypatch):
    looked = []
    monkeypatch.setattr(stream.os.path, "exists", lambda path: looked.append(path) or False)
    tick(box)
    tick(box)
    assert looked == []


def test_the_stream_areas_are_capabilities(box, factory):
    names = capabilities(factory)
    assert names[names.index("tuner") + 1:names.index("recording")] == [
        "stream", "stream_clients", "stream_stop", "stream_playable"]


def test_an_image_without_a_stream_server_claims_nothing(live_bridge, factory):
    assert live_bridge.publisher("stream") is None
    assert not [name for name in capabilities(factory) if name.startswith("stream")]
    assert factory.client.last(STREAM) is None
    assert factory.client.last(CLIENTS) is None


def test_an_image_whose_server_lists_no_clients_claims_only_the_descriptor(
    monkeypatch, make_bridge, factory, settings, receiver
):
    class Bare:
        _instance = None

        @classmethod
        def getInstance(cls):
            return cls()

    install_stream_server(monkeypatch, Bare)
    bridge = start(make_bridge, factory, settings, receiver)
    names = capabilities(factory)
    assert "stream" in names and "stream_playable" in names
    assert "stream_clients" not in names and "stream_stop" not in names
    assert factory.client.last(STREAM) is not None
    tick(bridge)
    assert factory.client.last(CLIENTS) is None
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.STOP_NOT_AVAILABLE


def test_an_image_that_cannot_stop_a_client_still_lists_them(
    monkeypatch, make_bridge, factory, settings, receiver
):
    class ReadOnly(StreamServer):
        stopStreamClient = None

    server = install_stream_server(monkeypatch, ReadOnly)
    server.clients = [(PHONE, TVP1, 0)]
    start(make_bridge, factory, settings, receiver)
    names = capabilities(factory)
    assert "stream_clients" in names and "stream_stop" not in names
    assert factory.client.last(CLIENTS).json()["count"] == 1
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.STOP_NOT_AVAILABLE


def test_an_image_that_can_stop_and_cannot_list_cannot_name_a_client_to_stop(
    monkeypatch, make_bridge, factory, settings, receiver
):
    class Blind:
        _instance = None
        stopped = []

        @classmethod
        def getInstance(cls):
            return cls()

        def stopStreamClient(self, host, sref):
            self.stopped.append((host, sref))
            return True

    install_stream_server(monkeypatch, Blind)
    start(make_bridge, factory, settings, receiver)
    assert "stream_stop" not in capabilities(factory)
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.STOP_NOT_AVAILABLE
    assert Blind.stopped == []


def test_a_server_that_cannot_be_reached_claims_nothing(monkeypatch, make_bridge, factory,
                                                        settings, receiver):
    class Broken:
        @classmethod
        def getInstance(cls):
            raise RuntimeError("not constructed yet")

    monkeypatch.setattr(stream, "enigma_attribute",
                        lambda name: Broken if name == "eStreamServer" else None)
    bridge = start(make_bridge, factory, settings, receiver)
    assert bridge.publisher("stream") is None


# ---------------------------------------------------------------- the clients --


def test_no_clients_is_an_empty_list(box, factory):
    published = factory.client.last(CLIENTS)
    assert published.retain is True
    assert published.json() == {"count": 0, "clients": []}


def test_a_client_is_listed_with_its_host_its_service_and_how_it_is_served(box, server, factory):
    server.clients = [(PHONE, TVP1, 0)]
    tick(box)
    assert factory.client.last(CLIENTS).json() == {
        "count": 1,
        "clients": [{"host": "192.0.2.20", "sref": TVP1, "name": "TVP 1 HD",
                     "bouquet": "Ulubione TV", "transcoded": False}],
    }


@pytest.mark.parametrize(("held", "shown"), [
    ("::ffff:192.0.2.20", "192.0.2.20"),
    ("::FFFF:192.0.2.20", "192.0.2.20"),
    ("192.0.2.20", "192.0.2.20"),
    ("2001:db8::17", "2001:db8::17"),
    ("::ffff:c000:214", "::ffff:c000:214"),
    ("::ffff:192.0.2", "::ffff:192.0.2"),
    ("::ffff:192.0.2.x", "::ffff:192.0.2.x"),
    ("  192.0.2.20 ", "192.0.2.20"),
    ("", ""),
])
def test_an_ipv4_client_is_shown_without_its_ipv6_wrapping(held, shown):
    assert stream.normalise_host(held) == shown


def test_a_host_longer_than_an_address_is_cut():
    assert stream.normalise_host("h" * 200) == "h" * 64


def test_a_transcoded_client_says_so(box, server, factory):
    server.clients = [(PHONE, TVP1, 1)]
    tick(box)
    assert factory.client.last(CLIENTS).json()["clients"][0]["transcoded"] is True


def test_a_client_that_connects_or_leaves_is_published_at_the_next_look(box, server, factory):
    factory.client.clear()
    tick(box)
    assert factory.client.all_for(CLIENTS) == []
    server.clients = [(PHONE, TVP1, 0)]
    tick(box)
    tick(box)
    assert [one.json()["count"] for one in factory.client.all_for(CLIENTS)] == [1]
    server.clients = []
    tick(box)
    assert factory.client.last(CLIENTS).json() == {"count": 0, "clients": []}


def test_the_look_is_every_five_seconds(box):
    assert box.publisher("stream")._ticker.timer.started == (5000, False)


def test_names_are_resolved_only_when_the_list_changed(box, server, factory, monkeypatch):
    resolved = []
    real = stream.service_name
    monkeypatch.setattr(stream, "service_name", lambda sref: resolved.append(sref) or real(sref))
    server.clients = [(PHONE, TVP1, 0)]
    tick(box)
    tick(box)
    tick(box)
    assert resolved == [TVP1]


def test_a_service_of_a_bouquet_that_is_not_published_is_listed_like_any_other(
    box, server, factory
):
    """As `service` and `zap_history` do: the reference and the name, and a null bouquet."""
    server.clients = [(PHONE, ELSEWHERE, 0)]
    tick(box)
    row = factory.client.last(CLIENTS).json()["clients"][0]
    assert row["sref"] == ELSEWHERE
    assert row["bouquet"] is None
    assert row["host"] == "192.0.2.20"


def test_a_client_with_no_service_has_nulls(box, server, factory):
    server.clients = [(PHONE, "", 0)]
    tick(box)
    assert factory.client.last(CLIENTS).json()["clients"] == [
        {"host": "192.0.2.20", "sref": None, "name": None, "bouquet": None,
         "transcoded": False}]


def test_the_list_is_cut_at_thirty_two_rows_and_the_count_is_not(box, server, factory):
    server.clients = [("::ffff:192.0.2." + str(number), TVP1, 0) for number in range(1, 41)]
    tick(box)
    payload = factory.client.last(CLIENTS).json()
    assert payload["count"] == 40
    assert [row["host"] for row in payload["clients"]] == [
        "192.0.2." + str(number) for number in range(1, 33)]


def test_a_list_that_cannot_be_read_publishes_nothing_and_raises_nothing(box, server, factory,
                                                                        plugin_log):
    server.clients = [(PHONE, TVP1, 0)]
    tick(box)
    factory.client.clear()
    server.raises = True
    tick(box)
    assert factory.client.all_for(CLIENTS) == []
    assert "Traceback" not in plugin_log()
    server.raises = False
    server.clients = []
    tick(box)
    assert factory.client.last(CLIENTS).json()["count"] == 0


@pytest.mark.parametrize(("rows", "read"), [
    ([("only-a-host",)], []),
    ([None], []),
    ([(PHONE, TVP1)], []),
    ([("only-a-host",), (PHONE, TVP1, 0)], [(PHONE, TVP1, False)]),
    ([(None, None, 1)], [("", "", True)]),
    (None, []),
    (7, None),
])
def test_a_row_the_image_shaped_differently_is_skipped(monkeypatch, rows, read):
    server = install_stream_server(monkeypatch)
    monkeypatch.setattr(server, "getConnectedClients", lambda: rows)
    assert stream.read_clients() == read


def test_a_reconnect_publishes_both_topics_again(box, server, factory):
    server.clients = [(PHONE, TVP1, 0)]
    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.last(STREAM).json()["port"] == 8001
    assert factory.client.last(CLIENTS).json()["count"] == 1


def test_both_topics_are_the_nodes_to_take_back(box):
    assert {STREAM, CLIENTS} <= set(box.state.retained_topics)
    assert PLAYABLE not in box.state.retained_topics


def test_the_look_stops_with_the_publisher(box):
    publisher = box.publisher("stream")
    publisher._after_stop.start(2000, True)
    publisher.stop()
    assert publisher._ticker.timer is None
    assert publisher._after_stop.timer is None


# ------------------------------------------------------------ cmd/stream_stop --


def test_one_client_is_stopped_by_its_published_host_and_service(box, server, factory):
    """The image is handed its own spelling of the host back, not the one on the topic."""
    server.clients = [(PHONE, TVP1, 0), (TABLET, TVN, 0)]
    tick(box)
    factory.client.clear()
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert server.stopped == [(PHONE, TVP1)]
    assert factory.client.last(LAST_ERROR) is None
    assert [client[0] for client in server.clients] == [TABLET]


def test_stopping_needs_no_permission(box, server, factory, settings):
    """Always allowed, as a zap and standby are: there is no switch for it."""
    for name in ("deep_standby_allowed", "softcam_restart_allowed", "epg_import_allowed",
                 "uninstall_allowed", "update_allowed"):
        assert getattr(settings, name).value is False
    server.clients = [(PHONE, TVP1, 0)]
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert server.stopped == [(PHONE, TVP1)]
    assert not [name for name in box.published_settings() if "stream" in name]


def test_the_list_is_read_again_two_seconds_after_a_stop(box, server, factory):
    """The connection ends a moment after the image said yes; the topic follows it."""
    server.linger = True
    server.clients = [(PHONE, TVP1, 0)]
    tick(box)
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    again = box.publisher("stream")._after_stop.timer
    assert again.started == (2000, True)
    assert factory.client.last(CLIENTS).json()["count"] == 1
    server.clients = []
    again.fire()
    assert factory.client.last(CLIENTS).json()["count"] == 0


def test_a_reference_the_image_holds_with_a_name_is_the_same_service(box, server, factory):
    held = TVP1 + ":TVP 1 HD"
    server.clients = [(PHONE, held, 0)]
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert server.stopped == [(PHONE, held)]


def test_the_host_may_be_sent_as_the_image_holds_it(box, server, factory):
    server.clients = [(PHONE, TVP1, 0)]
    send(factory, "stream_stop", {"host": "::FFFF:192.0.2.20", "sref": TVP1})
    assert server.stopped == [(PHONE, TVP1)]


def test_an_ipv6_host_is_matched_whatever_its_case(box, server, factory):
    server.clients = [("2001:DB8::17", TVN, 0)]
    send(factory, "stream_stop", {"host": "2001:db8::17", "sref": TVN})
    assert server.stopped == [("2001:DB8::17", TVN)]
    server.clients = [("2001:db8::17", TVN, 0)]
    send(factory, "stream_stop", {"host": "2001:DB8::17", "sref": TVN})
    assert server.stopped[-1] == ("2001:db8::17", TVN)


def test_spaces_around_the_host_and_the_reference_do_not_hide_the_client(box, server, factory):
    server.clients = [(PHONE, TVP1, 0)]
    send(factory, "stream_stop", {"host": " 192.0.2.20 ", "sref": " " + TVP1 + " "})
    assert server.stopped == [(PHONE, TVP1)]


def test_only_the_named_service_of_a_host_is_stopped(box, server, factory):
    server.clients = [(PHONE, TVP1, 0), (PHONE, TVN, 0)]
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVN})
    assert server.stopped == [(PHONE, TVN)]
    assert server.clients == [(PHONE, TVP1, 0)]


@pytest.mark.parametrize(("host", "sref"), [
    ("192.0.2.21", TVP1),
    ("192.0.2.20", POLSAT),
])
def test_a_client_that_is_not_connected_is_a_refusal(box, server, factory, host, sref):
    server.clients = [(PHONE, TVP1, 0)]
    send(factory, "stream_stop", {"host": host, "sref": sref})
    error = factory.client.last(LAST_ERROR).json()
    assert (error["cmd"], error["error"], error["reason"]) == (
        "stream_stop", "that stream client is not connected", "not_connected")
    assert "id" not in error
    assert server.stopped == []


def test_the_refusal_says_the_commands_id_back(box, server, factory):
    send(factory, "stream_stop",
         {"id": "stop-17", "payload": {"host": "192.0.2.20", "sref": TVP1}})
    error = factory.client.last(LAST_ERROR).json()
    assert (error["reason"], error["id"]) == ("not_connected", "stop-17")


@pytest.mark.parametrize("payload", [
    b"", b"PRESS", b"all", b"{}", b"[]", b"null",
    json.dumps({"host": "192.0.2.20"}).encode(),
    json.dumps({"sref": TVP1}).encode(),
    json.dumps({"host": "192.0.2.20", "sref": TVP1, "all": True}).encode(),
    json.dumps({"host": "", "sref": TVP1}).encode(),
    json.dumps({"host": "   ", "sref": TVP1}).encode(),
    json.dumps({"host": "192.0.2.20", "sref": ""}).encode(),
    json.dumps({"host": "192.0.2.20", "sref": "  "}).encode(),
    json.dumps({"host": None, "sref": TVP1}).encode(),
    json.dumps({"host": "192.0.2.20", "sref": 7}).encode(),
])
def test_nothing_is_stopped_by_a_payload_that_does_not_name_one_client(box, server, factory,
                                                                      payload):
    """Never "all": an empty or partial payload stops nobody."""
    server.clients = [(PHONE, TVP1, 0), (TABLET, TVN, 0)]
    send(factory, "stream_stop", payload)
    assert server.stopped == []
    assert len(server.clients) == 2
    assert factory.client.last(LAST_ERROR).json()["error"].startswith("cmd/stream_stop ")


@pytest.mark.parametrize(("payload", "sentence"), [
    (b"all", "cmd/stream_stop takes a JSON object"),
    (b"{}", "cmd/stream_stop takes exactly the host and sref fields"),
    (json.dumps({"host": 7, "sref": TVP1}).encode(),
     "cmd/stream_stop host must be a non-empty string"),
    (json.dumps({"host": "192.0.2.20", "sref": ""}).encode(),
     "cmd/stream_stop sref must be a non-empty string"),
])
def test_the_refusal_says_what_the_payload_lacked(box, factory, payload, sentence):
    send(factory, "stream_stop", payload)
    assert factory.client.last(LAST_ERROR).json()["error"] == sentence


def test_an_image_that_says_no_is_a_refusal(box, server, factory, monkeypatch):
    server.clients = [(PHONE, TVP1, 0)]
    monkeypatch.setattr(server, "stopStreamClient", lambda host, sref: False)
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    error = factory.client.last(LAST_ERROR).json()
    assert (error["error"], error["reason"]) == (
        "the receiver did not stop that stream client", "not_stopped")
    assert box.publisher("stream")._after_stop.timer is None


def test_a_stop_that_raises_is_a_refusal_and_not_a_traceback_on_screen(box, server, factory,
                                                                      monkeypatch):
    def broken(host, sref):
        raise RuntimeError("the client went away")

    server.clients = [(PHONE, TVP1, 0)]
    monkeypatch.setattr(server, "stopStreamClient", broken)
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert factory.client.last(LAST_ERROR).json()["error"] == "RuntimeError: the client went away"


def test_a_list_that_cannot_be_read_refuses_the_stop(box, server, factory):
    server.clients = [(PHONE, TVP1, 0)]
    server.raises = True
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.UNREADABLE
    assert server.stopped == []


def test_a_stop_without_a_stream_server_is_refused(live_bridge, factory):
    send(factory, "stream_stop", {"host": "192.0.2.20", "sref": TVP1})
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.STOP_NOT_AVAILABLE


# -------------------------------------------------------- cmd/stream_playable --


def test_the_question_is_asked_with_an_empty_second_reference(box, factory):
    """With the playing service there the image says yes to everything (ADR-0017)."""
    center = ServiceCenter.getInstance()
    center.playable[POLSAT] = 0
    send(factory, "stream_playable", POLSAT)
    assert center.asked == [(POLSAT, "", False)]
    assert factory.client.last(PLAYABLE).json()["playable"] is False


def test_the_answer_is_an_event_that_is_not_retained(box, factory):
    send(factory, "stream_playable", TVN)
    published = factory.client.last(PLAYABLE)
    assert (published.retain, published.qos) == (False, 0)
    payload = published.json()
    assert set(payload) == {"sref", "playable", "ts"}
    assert (payload["sref"], payload["playable"]) == (TVN, True)
    assert isinstance(payload["ts"], int)
    assert factory.client.last(LAST_ERROR) is None


def test_the_answer_says_the_commands_id_back(box, factory):
    send(factory, "stream_playable", {"id": "q-1", "payload": {"sref": TVN}})
    assert factory.client.last(PLAYABLE).json()["id"] == "q-1"
    send(factory, "stream_playable", {"id": "q-2", "payload": TVP1})
    answer = factory.client.last(PLAYABLE).json()
    assert (answer["id"], answer["sref"]) == ("q-2", TVP1)


def test_two_questions_get_two_answers_even_when_they_are_the_same(box, factory):
    send(factory, "stream_playable", TVN)
    send(factory, "stream_playable", TVN)
    assert len(factory.client.all_for(PLAYABLE)) == 2


def test_an_answer_is_not_replayed_by_a_reconnect(box, factory):
    send(factory, "stream_playable", TVN)
    factory.client.clear()
    factory.client.fire_connect()
    assert factory.client.all_for(PLAYABLE) == []


@pytest.mark.parametrize("payload", [b"", b"   ", b"{}", b"[]",
                                     json.dumps({"sref": ""}).encode(),
                                     json.dumps({"sref": "  "}).encode(),
                                     json.dumps({"sref": 5}).encode(),
                                     json.dumps({"name": "TVN HD"}).encode()])
def test_a_question_about_nothing_is_refused(box, factory, payload):
    send(factory, "stream_playable", payload)
    error = factory.client.last(LAST_ERROR).json()
    assert (error["cmd"], error["error"]) == ("stream_playable", "no service reference given")
    assert factory.client.last(PLAYABLE) is None
    assert ServiceCenter.getInstance().asked == []


def test_spaces_around_the_reference_are_not_part_of_the_question(box, factory):
    send(factory, "stream_playable", "  " + TVN + " ")
    assert factory.client.last(PLAYABLE).json()["sref"] == TVN
    send(factory, "stream_playable", {"sref": " " + TVP1 + "  "})
    assert factory.client.last(PLAYABLE).json()["sref"] == TVP1
    assert [asked[0] for asked in ServiceCenter.getInstance().asked] == [TVN, TVP1]


def test_a_refused_question_says_its_id_back_on_last_error(box, factory):
    send(factory, "stream_playable", {"id": "q-3", "payload": ""})
    assert factory.client.last(LAST_ERROR).json()["id"] == "q-3"


def test_a_question_the_image_cannot_answer_is_a_refusal(box, factory):
    ServiceCenter.getInstance().playable_raises = True
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == "RuntimeError: no resource manager"
    assert factory.client.last(PLAYABLE) is None


@pytest.mark.parametrize("answer", [None, "yes", 1.0])
def test_an_answer_that_is_no_number_is_a_refusal(box, factory, monkeypatch, answer):
    from conftest import ServiceInfo

    monkeypatch.setattr(ServiceInfo, "isPlayable", lambda self, ref, ignore: answer)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == (
        "the receiver did not say whether that service can be played")
    assert factory.client.last(PLAYABLE) is None


@pytest.mark.parametrize(("answer", "playable"), [(0, False), (1, True), (3, True),
                                                  (True, True), (False, False)])
def test_any_number_but_zero_is_yes(box, factory, monkeypatch, answer, playable):
    from conftest import ServiceInfo

    monkeypatch.setattr(ServiceInfo, "isPlayable", lambda self, ref, ignore: answer)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(PLAYABLE).json()["playable"] is playable


def test_a_service_the_receiver_has_no_information_for_is_a_refusal(box, factory, monkeypatch):
    monkeypatch.setattr(ServiceCenter, "info", lambda self, reference: None)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.NO_INFORMATION


def test_text_that_is_no_reference_is_a_refusal(box, factory, monkeypatch):
    from conftest import eServiceReference

    monkeypatch.setattr(eServiceReference, "valid", lambda self: False)
    send(factory, "stream_playable", "not-a-reference")
    assert factory.client.last(LAST_ERROR).json()["error"] == (
        "'not-a-reference' is not a service reference")
    assert ServiceCenter.getInstance().asked == []


def test_a_reference_that_cannot_be_judged_is_a_refusal(box, factory, monkeypatch):
    from conftest import eServiceReference

    def broken(self):
        raise RuntimeError("not a reference object")

    monkeypatch.setattr(eServiceReference, "valid", broken)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == (
        "'" + TVN + "' is not a service reference")


def test_a_reference_that_cannot_be_built_is_a_refusal(box, factory, monkeypatch):
    monkeypatch.setattr(stream, "service_reference", lambda sref: None)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == (
        "'" + TVN + "' is not a service reference")


def test_a_reference_without_a_validity_test_is_asked_about(box, factory, monkeypatch):
    class Plain:
        def __init__(self, text=""):
            self.text = text

        def toString(self):
            return self.text

    monkeypatch.setattr(stream, "service_reference", Plain)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(PLAYABLE).json()["playable"] is True


def test_a_service_centre_that_raises_is_a_refusal(box, factory, monkeypatch):
    def broken(self, reference):
        raise RuntimeError("no service handler")

    monkeypatch.setattr(ServiceCenter, "info", broken)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.NO_INFORMATION


def test_a_question_without_a_stream_server_is_refused(live_bridge, factory):
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.PLAYABLE_NOT_AVAILABLE


def test_an_image_whose_service_centre_gives_no_information_does_not_claim_the_question(
    server, make_bridge, factory, settings, receiver, monkeypatch
):
    monkeypatch.setattr(ServiceCenter, "info", None)
    start(make_bridge, factory, settings, receiver)
    assert "stream_playable" not in capabilities(factory)
    send(factory, "stream_playable", TVN)
    assert factory.client.last(LAST_ERROR).json()["error"] == stream.PLAYABLE_NOT_AVAILABLE


# ------------------------------------------------------------------- the page --


def test_both_commands_are_in_the_dispatchers_table():
    handlers = CommandDispatcher(None).handlers
    assert "stream_stop" in handlers and "stream_playable" in handlers


def test_the_page_stops_a_client_after_a_question(box, server):
    server.clients = [(PHONE, TVP1, 0)]
    action = {one.key: one for one in webif.actions()}["stream_stop"]
    values = {"host": "192.0.2.20", "sref": TVP1}
    assert action.confirm(values)
    assert box.run_command(action.command, action.build(values), PAGE) is None
    assert server.stopped == [(PHONE, TVP1)]


def test_the_page_says_the_answer_it_was_given(box, monkeypatch):
    monkeypatch.setattr(webif, "_bridge", lambda: box)
    action = {one.key: one for one in webif.actions()}["stream_playable"]
    payload = action.build({"sref": TVN})
    assert box.run_command(action.command, payload, PAGE) is None
    assert action.done(payload) == "The receiver can play that channel now."
    ServiceCenter.getInstance().playable[POLSAT] = 0
    payload = action.build({"sref": POLSAT})
    assert box.run_command(action.command, payload, PAGE) is None
    assert action.done(payload) == "The receiver cannot play that channel now."


def test_an_answer_asked_for_on_the_page_carries_no_id(box, factory):
    box.run_command("stream_playable", TVN, PAGE)
    assert "id" not in factory.client.last(PLAYABLE).json()
