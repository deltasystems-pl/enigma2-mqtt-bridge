"""The node id, and the rest of what the box says about itself.

The node id is the identity everything downstream keys on: change it and every
retained topic is orphaned and every Home Assistant entity is replaced. It has
to come out the same on every boot of the same hardware, and it has to come out
of *something* even on an image that ships none of the usual sources.

The MAC used throughout is 00:00:5e:00:53:01, from the range IANA reserves for
documentation. No real box has it.
"""

from MQTTBridge import boxinfo

DOC_MAC = "00:00:5e:00:53:01"


def test_node_id_is_boxtype_and_the_last_six_mac_digits(monkeypatch):
    monkeypatch.setattr(boxinfo, "mac_address", lambda: DOC_MAC)
    assert boxinfo.derive_node_id() == "vuuno4kse_005301"


def test_boxtype_comes_from_boxbranding_first():
    assert boxinfo.box_type() == "vuuno4kse"


def test_boxtype_falls_back_to_proc(monkeypatch, tmp_path):
    monkeypatch.setattr(boxinfo, "_boxbranding", lambda: None)
    proc = tmp_path / "boxtype"
    proc.write_text("VuUno4KSE\n", encoding="utf-8")

    def read(path):
        return proc.read_text(encoding="utf-8").strip() if path == "/proc/stb/info/boxtype" else ""

    monkeypatch.setattr(boxinfo, "_read_text", read)
    assert boxinfo.box_type() == "vuuno4kse"


def test_boxtype_is_unknown_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(boxinfo, "_boxbranding", lambda: None)
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "")
    assert boxinfo.box_type() == "unknown"


def test_a_boxtype_with_punctuation_is_sanitised(monkeypatch):
    monkeypatch.setattr(boxinfo, "_boxbranding", lambda: None)
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "Vu+ Uno 4K SE!")
    monkeypatch.setattr(boxinfo, "mac_address", lambda: DOC_MAC)
    assert boxinfo.box_type() == "vu_uno_4k_se"
    assert boxinfo.derive_node_id() == "vu_uno_4k_se_005301"


def test_the_node_id_never_comes_out_empty(monkeypatch):
    """An image that answers nothing still gets a usable, stable identity."""
    monkeypatch.setattr(boxinfo, "_boxbranding", lambda: None)
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "")
    monkeypatch.setattr(boxinfo, "mac_address", lambda: "")
    assert boxinfo.derive_node_id() == "unknown"

    monkeypatch.setattr(boxinfo, "box_type", lambda: "")
    assert boxinfo.derive_node_id() == "enigma2"


def test_mac_is_read_from_eth0(monkeypatch):
    def read(path):
        return DOC_MAC if path == "/sys/class/net/eth0/address" else ""

    monkeypatch.setattr(boxinfo, "_read_text", read)
    assert boxinfo.mac_address() == DOC_MAC


def test_mac_falls_back_to_the_first_real_interface(monkeypatch):
    def read(path):
        if path == "/sys/class/net/eth0/address":
            return ""
        if path == "/sys/class/net/lo/address":
            return "00:00:00:00:00:00"
        if path == "/sys/class/net/enp1s0/address":
            return DOC_MAC
        return ""

    monkeypatch.setattr(boxinfo, "_read_text", read)
    monkeypatch.setattr(boxinfo.os, "listdir", lambda path: ["lo", "enp1s0"])
    assert boxinfo.mac_address() == DOC_MAC


def test_an_all_zero_mac_is_not_a_mac(monkeypatch):
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "00:00:00:00:00:00")
    monkeypatch.setattr(boxinfo.os, "listdir", lambda path: ["lo"])
    assert boxinfo.mac_address() == ""


def test_mac_suffix_ignores_the_separators():
    assert boxinfo.mac_suffix(DOC_MAC) == "005301"
    assert boxinfo.mac_suffix("00005e005301") == "005301"
    assert boxinfo.mac_suffix("short") == ""


def test_image_version_from_boxbranding():
    assert boxinfo.image_version() == "openvix 6.6.007"


def test_image_version_falls_back_to_etc(monkeypatch):
    monkeypatch.setattr(boxinfo, "_boxbranding", lambda: None)
    monkeypatch.setattr(
        boxinfo,
        "_read_text",
        lambda path: "distro=openpli\nimageversion=9.1\n" if path == "/etc/image-version" else "",
    )
    assert boxinfo.image_version() == "openpli 9.1"


def test_image_version_is_unknown_when_nothing_answers(monkeypatch):
    monkeypatch.setattr(boxinfo, "_boxbranding", lambda: None)
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "")
    assert boxinfo.image_version() == "unknown"


def test_enigma_version_comes_from_the_binary():
    assert boxinfo.enigma_version() == "5.4"


def test_uptime_is_whole_seconds(monkeypatch):
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "384210.62 1523344.11")
    assert boxinfo.uptime_seconds() == 384210


def test_uptime_survives_an_unreadable_proc(monkeypatch):
    monkeypatch.setattr(boxinfo, "_read_text", lambda path: "")
    assert boxinfo.uptime_seconds() == 0


def test_local_ip_never_resolves_a_hostname(monkeypatch):
    """A DNS lookup on the main thread would stall the television."""
    seen = []

    class Probe:
        def settimeout(self, value):
            pass

        def connect(self, address):
            seen.append(address)

        def getsockname(self):
            return ("192.0.2.77", 9)

        def close(self):
            pass

    monkeypatch.setattr(boxinfo.socket, "socket", lambda family, kind: Probe())
    assert boxinfo.local_ip("broker.example.invalid") == "192.0.2.77"
    assert seen == [(boxinfo.UNROUTED_PROBE, 9)]

    seen.clear()
    assert boxinfo.local_ip("10.0.0.5") == "192.0.2.77"
    assert seen == [("10.0.0.5", 9)]


def test_local_ip_is_empty_when_there_is_no_route(monkeypatch):
    class Probe:
        def settimeout(self, value):
            pass

        def connect(self, address):
            raise OSError("no route to host")

        def close(self):
            pass

    monkeypatch.setattr(boxinfo.socket, "socket", lambda family, kind: Probe())
    assert boxinfo.local_ip("10.0.0.5") == ""
