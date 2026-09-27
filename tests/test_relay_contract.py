"""Both halves of the relay handshake agree: the plugin against the integration's own code.

`integration_relay.py` is the companion integration's request parser, answer and address rule,
copied verbatim. The plugin's `relay_request` must be one the integration parses, the
integration's `cmd/relay` must be one the plugin takes - sent on the topic, at the QoS and with
the retain flag the integration uses - and the two address rules must say the same of every
address. When either side changes its half, this is the test that fails.
"""

import json
import secrets

import integration_relay as integration
import pytest
import test_selfupdate
from conftest import ConsoleAppContainer
from test_selfupdate import NOW, ROOT, directories
from test_selfupdate_relay import URL, offline

from MQTTBridge import updatehelper
from MQTTBridge.origin import SCREEN

# `test_selfupdate.py`'s receiver, by the names its fixtures are asked for.
box = test_selfupdate.box
tree = test_selfupdate.tree
mono = test_selfupdate.mono

BASE_TOPIC, NODE_ID = ROOT.split("/")
# The integration's own test fixture: Home Assistant on the receiver's subnet (test_relay.py).
HA_BASE = "http://192.0.2.5:8123"


def ask_offline(bridge, factory, version="0.4.0", downgrade=False):
    offline(bridge)
    assert bridge.self_update.request(json.dumps({"version": version}), origin=SCREEN,
                                      downgrade=downgrade) is None
    (entry,) = factory.client.all_for(ROOT + "/relay_request")
    return entry


def test_the_integration_parses_the_plugins_relay_request(box, factory):
    entry = ask_offline(box(), factory)
    # The integration drops a retained request, and parses nothing but the payload's bytes.
    assert entry.retain is False
    parsed = integration._parse_request(entry.payload)
    assert parsed == {"id": entry.json()["id"], "version": "0.4.0"}


def test_the_plugin_takes_the_integrations_answer(box, factory):
    bridge = box()
    entry = ask_offline(bridge, factory)
    request = integration._parse_request(entry.payload)
    token = secrets.token_urlsafe(32)
    expires = NOW + 600.25
    payload = integration.answer(request, request["version"], HA_BASE, token, expires)
    topic = integration.command_topic(BASE_TOPIC, NODE_ID, "relay")
    factory.client.fire_message(topic, payload.encode("utf-8"),
                                retain=integration.PUBLISH["retain"],
                                qos=integration.PUBLISH["qos"])
    (directory,) = directories(bridge.root)
    sent = json.loads((directory / "request.json").read_text())
    assert sent["relay"] == {"url": HA_BASE + integration.RELAY_PATH + token,
                             "expires": int(expires)}
    assert sent["started_by"] == "screen"
    assert len(ConsoleAppContainer.instances) == 1


def test_a_downgrade_the_receiver_asks_for_is_answered_and_taken(box, factory):
    bridge = box()
    entry = ask_offline(bridge, factory, "0.2.5", downgrade=True)
    request = integration._parse_request(entry.payload)
    payload = integration.answer(request, request["version"], HA_BASE, "b" * 43, NOW + 600)
    factory.client.fire_message(integration.command_topic(BASE_TOPIC, NODE_ID, "relay"),
                                payload.encode("utf-8"))
    sent = json.loads((directories(bridge.root)[0] / "request.json").read_text())
    assert (sent["target"], sent["downgrade"]) == ("0.2.5", True)


ADDRESSES = [
    URL,
    "https://192.0.2.5/api/enigma2_mqtt/relay/" + "-" * 43,
    "http://homeassistant.local:8123/api/enigma2_mqtt/relay/" + "_" * 43,
    "http://ha:1/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://ha:65535/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://ha:0/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://ha:65536/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://ha:123456/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://[2001:db8::5]:8123/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://[::ffff:192.0.2.5]/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://user:pass@192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://home_assistant/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 42,
    "http://192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 44,
    "http://192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 42 + "=",
    "http://192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 43 + "/",
    "http://192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 43 + "\n",
    "ftp://192.0.2.5/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://\u0661\u0669\u0662.0.2.5/api/enigma2_mqtt/relay/" + "z" * 43,
    "http://192.0.2.5:\u0668\u0661/api/enigma2_mqtt/relay/" + "z" * 43,
]


@pytest.mark.parametrize("url", ADDRESSES)
def test_both_halves_judge_every_address_alike(url):
    """What Home Assistant offers is what the receiver takes, and nothing else."""
    assert integration.relay_url_ok(url) is updatehelper.relay_ok(
        {"url": url, "expires": NOW + 60}, NOW)
