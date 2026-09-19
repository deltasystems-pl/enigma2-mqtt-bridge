"""OSCam telemetry is read-only, bounded and strips receiver identities."""

import io
import json
import threading
import time
from urllib.error import HTTPError

from Components.config import configfile

from MQTTBridge import config as settings_module
from MQTTBridge import log as log_module
from MQTTBridge import oscam

SALT = b"s" * 32
NODE = "vuuno4kse_005301"
TOPIC = "enigma2/" + NODE + "/oscam"


def status(readonly="1", clients=None):
    return {
        "oscam": {
            "version": "1.20_svn build r11718",
            "apiruntime": "120",
            "readonly": readonly,
            "status": {"client": clients or []},
        }
    }


def readers(rows=None):
    return {"oscam": {"readers": rows or []}}


def test_oscam_is_absent_until_explicitly_enabled(live_bridge):
    assert live_bridge.publisher("oscam") is None
    assert "oscam" not in live_bridge.capabilities()


def test_disabled_oscam_retracts_a_previous_process_topic_on_connect(
    live_bridge, factory
):
    live_bridge.publish_json(TOPIC, {"software": "OSCam"})
    factory.client.fire_connect()
    entry = factory.client.last(TOPIC)
    assert entry.payload == b""
    assert entry.retain is True


def test_info_exposes_only_the_opt_in_not_oscam_credentials(live_bridge, settings):
    settings.oscam_username.value = "private-user"
    settings.oscam_password.value = "private-password"
    settings.oscam_identity_salt.value = "12" * 32
    info = live_bridge.build_info()
    assert info["settings"]["oscam_telemetry"] is False
    encoded = json.dumps(info)
    assert "private-user" not in encoded
    assert "private-password" not in encoded
    assert settings.oscam_identity_salt.value not in encoded


def test_normalize_publishes_neutral_stable_handles_and_honest_counts():
    clients = [
        {
            "type": "p",
            "rname_enc": "Private%20Server",
            "protocol": "cccam",
            "connection": {
                "status": "CONNECTED",
                "ip": "192.0.2.1",
                "entitlements": [{"cccount": "12", "caid": "secret"}],
            },
        },
        {
            "type": "r",
            "rname_enc": "Living%20Room%20Card",
            "protocol": "internal",
            "connection": {"status": "CARDOK", "entitlements": [{"caid": "secret"}]},
        },
    ]
    configured = [
        {"label": "Private Server", "type": "p", "enabled": "1", "protocol": "cccam"},
        {"label": "Living Room Card", "type": "r", "enabled": "1", "protocol": "internal"},
    ]
    payload = oscam.normalize(status(clients=clients), readers(configured), SALT)
    assert payload["software"] == "OSCam"
    assert payload["software_running"] is True
    assert payload["cards_ready"] == 1
    assert payload["servers_connected"] == 1
    assert payload["shared_cards"] == 12
    assert all(row["id"].startswith(("reader_", "server_")) for row in payload["readers"])
    encoded = json.dumps(payload)
    for private in ("Private", "Living", "192.0.2.1", "caid", "secret"):
        assert private not in encoded

    reversed_payload = oscam.normalize(
        status(clients=list(reversed(clients))), readers(list(reversed(configured))), SALT
    )
    assert reversed_payload["readers"] == payload["readers"]


def test_a_build_suffix_on_the_revision_is_still_a_version():
    """🔴 A receiver here reports `1.20_svn build r11718-079`, and the whole
    version used to be dropped because of the three digits after the hyphen."""
    assert oscam._safe_version("1.20_svn build r11718-079") == "1.20_svn build r11718-079"
    assert oscam._safe_version("1.20_svn build r11718") == "1.20_svn build r11718"
    assert oscam._safe_version("1.20_svn") == "1.20_svn"
    # And the allowlist still refuses anything that is not a version.
    assert oscam._safe_version("private operator build") is None
    assert oscam._safe_version("1.20_svn build r11718-079 <script>") is None
    assert oscam._safe_version("1.20_svn build r11718-0123456789") is None


def test_unknown_protocol_status_and_huge_counts_are_bounded():
    clients = [
        {
            "type": "p",
            "rname_enc": "x",
            "protocol": "private-protocol",
            "connection": {"status": "private-state", "entitlements": {"cccount": "9" * 99}},
        }
    ]
    payload = oscam.normalize(
        status(clients=clients),
        readers([{"label": "x", "type": "p", "enabled": "1"}]),
        SALT,
    )
    assert payload["readers"][0] == {
        "id": payload["readers"][0]["id"],
        "kind": "server",
        "enabled": True,
        "status": "unknown",
        "protocol": None,
        "shared_cards": None,
    }


def test_disabled_stale_rows_do_not_count_as_healthy_or_cards():
    live = [{"type": "r", "rname_enc": "card", "connection": {"status": "CARDOK"}}]
    configured = [{"label": "card", "type": "r", "enabled": "0", "protocol": "internal"}]
    payload = oscam.normalize(status(clients=live), readers(configured), SALT)
    assert payload["readers"][0]["status"] == "disabled"
    assert payload["readers_healthy"] == 0
    assert payload["cards_ready"] == 0


def test_oversized_reader_inventory_is_rejected_instead_of_undercounted():
    configured = [
        {"label": "reader" + str(index), "type": "r", "enabled": "1"}
        for index in range(oscam.MAX_READERS + 1)
    ]
    try:
        oscam.normalize(status(), readers(configured), SALT)
    except ValueError:
        pass
    else:
        raise AssertionError("oversized inventory was accepted")


def test_fractional_counts_and_arbitrary_versions_are_not_coerced_or_echoed():
    document = status(
        clients=[
            {
                "type": "p",
                "rname_enc": "server",
                "protocol": "cccam",
                "connection": {"status": "CONNECTED", "entitlements": {"cccount": 1.5}},
            }
        ]
    )
    document["oscam"]["version"] = "private operator build"
    payload = oscam.normalize(
        document,
        readers([{"label": "server", "type": "p", "enabled": "1", "protocol": "cccam"}]),
        SALT,
    )
    assert payload["version"] is None
    assert payload["shared_cards"] is None


class Response:
    def __init__(self, data, url, delay=0):
        self.data = data
        self.url = url
        self.delay = delay
        self.offset = 0

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def geturl(self):
        return self.url

    def read1(self, size):
        if self.delay:
            time.sleep(self.delay)
        found = self.data[self.offset : self.offset + size]
        self.offset += len(found)
        return found

    read = read1


class Opener:
    def __init__(self, answers):
        self.answers = list(answers)
        self.requests = []

    def open(self, request, timeout=None):
        self.requests.append((request.full_url, timeout))
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return Response(answer, request.full_url)


def test_probe_uses_only_two_fixed_loopback_read_views(tmp_path):
    client = Opener(
        [json.dumps(status()).encode(), json.dumps(readers()).encode()]
    )
    payload = oscam.probe(8888, salt=SALT, opener=client, proc_root=str(tmp_path))
    assert payload["api_access"] == "granted"
    assert [url for url, _timeout in client.requests] == [
        "http://127.0.0.1:8888/oscamapi.json?part=status",
        "http://127.0.0.1:8888/oscamapi.json?part=readerlist",
    ]
    assert all("action=" not in url for url, _timeout in client.requests)
    assert all(
        timeout <= oscam.TIMEOUT_SECONDS / oscam.MAX_DIGEST_REQUESTS
        for _, timeout in client.requests
    )


def test_opener_explicitly_disables_environment_proxies(monkeypatch):
    captured = []
    monkeypatch.setenv("http_proxy", "http://proxy.example.invalid:8080")
    monkeypatch.setattr(oscam, "build_opener", lambda *handlers: captured.extend(handlers))
    oscam._opener("http://127.0.0.1:8888/oscamapi.json?part=", "user", "secret")
    proxy = next(handler for handler in captured if isinstance(handler, oscam.ProxyHandler))
    assert proxy.proxies == {}


def test_auth_failure_is_reachable_but_not_authenticated(tmp_path):
    error = HTTPError("http://127.0.0.1/", 401, "private", {}, io.BytesIO())
    payload = oscam.probe(8888, salt=SALT, opener=Opener([error]), proc_root=str(tmp_path))
    assert payload["api_reachable"] is True
    assert payload["api_access"] == "denied"
    assert payload["readers"] == []


def test_large_malformed_redirected_and_slow_responses_fail_closed(tmp_path):
    base = "http://127.0.0.1:8888/oscamapi.json?part=status"
    cases = [
        Response(b"x" * (oscam.MAX_RESPONSE_BYTES + 1), base),
        Response(b"not-json", base),
        Response(b"{}", "http://example.invalid/private"),
        Response(b"{}", base, delay=0.02),
    ]
    for response in cases:
        class One:
            def __init__(self, found):
                self.found = found

            def open(self, _request, timeout=None):
                return self.found

        timeout = 0.01 if response.delay else 1
        payload = oscam.probe(
            8888, salt=SALT, opener=One(response), proc_root=str(tmp_path), timeout=timeout
        )
        assert payload["api_access"] is None
        assert payload["readers"] == []


def test_process_scan_reads_comm_only_and_normalizes_the_name(tmp_path):
    process = tmp_path / "123"
    process.mkdir()
    (process / "comm").write_text("oscam-private-build\n", encoding="ascii")
    assert oscam.process_running(str(tmp_path)) is True


def test_identity_salt_is_hidden_persisted_and_reused(live_bridge, settings):
    settings.oscam_identity_salt.value = ""
    settings.oscam_identity_salt.saved_value = ""
    found = oscam.OscamPublisher(live_bridge)
    first = found._identity_salt()
    second = found._identity_salt()
    assert first == second
    assert len(first) == 32
    assert settings.oscam_identity_salt.saved_value == settings.oscam_identity_salt.value
    assert configfile.save_calls == 1
    assert "oscam_identity_salt" not in settings_module.SETTING_NAMES
    assert "oscam_identity_salt" not in settings_module.REMOTE_SETTING_NAMES


def test_each_started_instance_registers_its_password_before_probe(
    live_bridge, settings, monkeypatch
):
    settings.oscam_telemetry.value = True
    settings.oscam_identity_salt.value = "11" * 32
    monkeypatch.setattr(oscam.OscamPublisher, "_start_probe", lambda _self: True)
    settings.oscam_password.value = "first-private-value"
    first = oscam.OscamPublisher(live_bridge)
    assert first.start() is True
    settings.oscam_password.value = "second-private-value"
    second = oscam.OscamPublisher(live_bridge)
    assert second.start() is True
    assert "first-private-value" not in log_module.redact("first-private-value")
    assert "second-private-value" not in log_module.redact("second-private-value")


def test_unexpected_http_exception_never_logs_credentials(
    live_bridge, settings, monkeypatch, plugin_log
):
    settings.oscam_password.value = "private-webif-password"
    found = oscam.OscamPublisher(live_bridge)
    found._stopped = False
    found._generation = 1
    found._latest_serial = 1

    def fail(*_args, **_kwargs):
        raise RuntimeError("private-webif-password appeared in a third-party error")

    monkeypatch.setattr(oscam, "probe", fail)
    ticket = oscam._PROBE_SLOT.acquire()
    assert ticket is not None
    found._ticket = ticket
    found._probe(1, 1, ticket, 8888, "private-user", "private-webif-password", SALT)
    assert oscam._PROBE_SLOT.held is False
    written = plugin_log()
    assert "OSCam health probe failed" in written
    assert "private-webif-password" not in written
    assert "private-user" not in written


def test_a_configured_password_is_registered_even_with_telemetry_off(live_bridge, settings):
    """The credential is in the settings whether or not anything probes with it."""
    settings.oscam_telemetry.value = False
    settings.oscam_password.value = "disabled-private-value"
    found = oscam.OscamPublisher(live_bridge)

    assert found.start() is False
    assert "disabled-private-value" not in log_module.redact("disabled-private-value")


def test_a_listener_that_is_not_an_http_server_is_unavailable(tmp_path):
    """🔴 A non-HTTP answer raises `HTTPException`, which is not an `OSError`."""
    from http.client import BadStatusLine

    payload = oscam.probe(
        8888, salt=SALT, opener=Opener([BadStatusLine("\\x16\\x03\\x01")]),
        proc_root=str(tmp_path),
    )
    assert payload["api_reachable"] is False
    assert payload["api_access"] is None
    assert payload["readers"] == []


def test_stopped_or_disabled_completion_cannot_republish(live_bridge, factory, settings):
    settings.oscam_telemetry.value = True
    found = oscam.OscamPublisher(live_bridge)
    found._stopped = False
    found._generation = 1
    found._latest_serial = 1
    payload = oscam.unavailable(True, reachable=True, access="granted")
    found._finish_probe(1, 1, payload)
    topic = live_bridge.topic("oscam")
    assert factory.client.last(topic).json() == payload

    before = len(factory.client.published)
    found.stop()
    found._finish_probe(1, 1, payload)
    settings.oscam_telemetry.value = False
    found._stopped = False
    found._generation = 3
    found._latest_serial = 2
    found._finish_probe(3, 2, payload)
    assert len(factory.client.published) == before


def test_hung_probe_expires_the_last_success_and_lets_the_next_one_start(
    live_bridge, factory, settings, monkeypatch, wait_until
):
    """🔴 One stuck worker used to hold the process-wide slot until a restart."""
    settings.oscam_telemetry.value = True
    calls = []
    entered = threading.Event()
    release = threading.Event()

    def blocked_probe(*_args, **_kwargs):
        calls.append("probe")
        entered.set()
        release.wait(5)
        return oscam.unavailable(True, reachable=True, access="granted")

    monkeypatch.setattr(oscam, "probe", blocked_probe)
    found = oscam.OscamPublisher(live_bridge)
    found._stopped = False
    found._salt = SALT
    found._cached = oscam.unavailable(True, reachable=True, access="granted")
    found._last_completion = time.monotonic()
    assert found._start_probe() is True
    assert entered.wait(1)
    stuck_generation = found._generation
    stuck_serial = found._serial

    found._last_completion = time.monotonic() - oscam.STALE_SECONDS - 1
    found._poll()

    state = factory.client.last(live_bridge.topic("oscam")).json()
    assert state["software_running"] is None
    assert state["api_reachable"] is False
    # The slot was taken back, so a replacement probe is already running.
    assert wait_until(lambda: calls == ["probe", "probe"])
    assert found._generation != stuck_generation

    # Whatever the abandoned worker eventually answers is not published.
    before = len(factory.client.published)
    found._finish_probe(
        stuck_generation, stuck_serial, oscam.unavailable(True, reachable=True, access="granted")
    )
    assert len(factory.client.published) == before

    release.set()
    assert wait_until(lambda: not found._running)
    found.stop()

    before = len(factory.client.published)
    found._poll()
    assert len(factory.client.published) == before


def test_a_replacement_instance_does_not_wait_for_a_stuck_retired_probe(
    live_bridge, factory, settings, monkeypatch, wait_until
):
    """🔴 Saving the setup screen replaces this publisher; a slot the retired
    instance still held was telemetry that never came back."""
    settings.oscam_telemetry.value = True
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def blocked_probe(*_args, **_kwargs):
        calls.append("probe")
        entered.set()
        release.wait(5)
        return oscam.unavailable(True, reachable=True, access="granted")

    monkeypatch.setattr(oscam, "probe", blocked_probe)
    first = oscam.OscamPublisher(live_bridge)
    first._stopped = False
    first._salt = SALT
    assert first._start_probe() is True
    assert entered.wait(1)

    second = oscam.OscamPublisher(live_bridge)
    second._stopped = False
    second._salt = SALT
    # While the first one is alive and unstopped, one probe at a time holds.
    assert second._start_probe() is False
    assert calls == ["probe"]

    first.stop()

    # The retired instance is gone, so the replacement probes straight away —
    # with the old worker still blocked.
    assert second._start_probe() is True
    assert wait_until(lambda: calls == ["probe", "probe"])

    before = len(factory.client.published)
    release.set()
    assert wait_until(lambda: not second._running)
    # The retired instance's own answer publishes nothing.
    assert wait_until(lambda: not first._running)
    published = len(factory.client.published)
    assert published - before <= 1
    second.stop()


def test_abandoned_workers_are_capped_so_threads_cannot_accumulate(
    live_bridge, settings, monkeypatch, wait_until, plugin_log
):
    settings.oscam_telemetry.value = True
    calls = []
    release = threading.Event()

    def blocked_probe(*_args, **_kwargs):
        calls.append("probe")
        release.wait(5)
        return oscam.unavailable(True, reachable=True, access="granted")

    monkeypatch.setattr(oscam, "probe", blocked_probe)
    found = oscam.OscamPublisher(live_bridge)
    found._stopped = False
    found._salt = SALT
    found._last_completion = time.monotonic()
    assert found._start_probe() is True
    assert wait_until(lambda: len(calls) == 1)

    # Two stale windows: the first abandonment starts a replacement, the second
    # must not, because two workers are already out there and unreachable.
    for _ in range(2):
        found._last_completion = time.monotonic() - oscam.STALE_SECONDS - 1
        found._poll()

    assert calls == ["probe", "probe"]
    assert len(found._abandoned) == oscam.MAX_ABANDONED_PROBES
    assert found._running is False
    assert plugin_log().count("starting no more until they do") == 1

    # And it starts probing again once they finally return.
    release.set()
    assert wait_until(lambda: not found._abandoned)
    found._poll()
    assert wait_until(lambda: len(calls) == 3)
    found.stop()


def test_start_publishes_unknown_before_the_first_probe(
    live_bridge, settings, factory, monkeypatch
):
    settings.oscam_telemetry.value = True
    settings.oscam_identity_salt.value = "11" * 32
    monkeypatch.setattr(oscam.OscamPublisher, "_start_probe", lambda _self: False)
    found = oscam.OscamPublisher(live_bridge)
    assert found.start() is True
    payload = factory.client.last(live_bridge.topic("oscam")).json()
    assert payload["api_reachable"] is False
    assert payload["readers"] == []
