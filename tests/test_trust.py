"""The release keys, and the rule every reader of the signed release index applies.

The rule's cases are the shared vectors (`tests/vectors/release-index.json`): a sequence of signed
indexes per scenario, each with the key set the reader holds and the verdict it must reach. The
companion integration runs the same file through its own implementation, so a verdict changed here
without the file is a failure, and the file changed without the generator is one too.
"""

import base64
import hashlib
import importlib.util
import json
import shutil
import subprocess
from pathlib import Path

import pytest

from MQTTBridge import trust

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS_PATH = REPO_ROOT / "tests" / "vectors" / "release-index.json"
VECTORS = json.loads(VECTORS_PATH.read_text("ascii"))
SCENARIOS = VECTORS["scenarios"]


def _keysets():
    return {name: trust.keys_from_data(item["keys"]) for name, item in VECTORS["keysets"].items()}


# -------------------------------------------------------------- the release keys --


def test_the_release_keys_are_pinned():
    # Changing one of these is a key rotation: a release of both halves, never a quiet edit.
    assert [(key.key_id, key.rank, base64.b64encode(key.public).decode(), key.baseline)
            for key in trust.EMBEDDED] == [
        ("5de3b24c97e88660", 1, "39Ndn8vAkeWAhYIYWvNubezKuF5F/5aY5E1uo+hSnqQ=", 0),
        ("c72fd83e3e514a25", 2, "1F2ajhsDoTuqAGdV2QOHdRl8hV4B0kNE0xwVGrpHdfs=", 0),
    ]
    assert trust.main_key(trust.EMBEDDED) is trust.MAIN


def test_a_key_id_is_the_first_16_hex_digits_of_the_public_keys_sha256():
    for key in trust.EMBEDDED:
        assert key.key_id == hashlib.sha256(key.public).hexdigest()[:16] == trust.key_id(key.public)


def test_the_vectors_carry_the_release_keys_as_embedded():
    assert VECTORS["release_keys"]["keys"] == trust.keys_to_data(trust.EMBEDDED)
    assert VECTORS["release_keys"]["fingerprint"] == trust.fingerprint(trust.EMBEDDED)


def test_the_fingerprint_is_the_documented_one():
    lines = "".join(
        f"{key.key_id} {key.rank} {base64.b64encode(key.public).decode()}\n"
        for key in sorted(trust.EMBEDDED, key=lambda key: key.key_id)
    )
    assert trust.fingerprint(trust.EMBEDDED) == hashlib.sha256(lines.encode()).hexdigest()
    # Order-independent, and different for a different set.
    assert trust.fingerprint(tuple(reversed(trust.EMBEDDED))) == trust.fingerprint(trust.EMBEDDED)
    assert trust.fingerprint(trust.EMBEDDED[:1]) != trust.fingerprint(trust.EMBEDDED)


def test_the_published_origin_is_a_constant():
    assert trust.ORIGIN == "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/"
    assert trust.is_origin(trust.ORIGIN)


# ------------------------------------------------------------------- the rule --


@pytest.mark.parametrize("scenario", SCENARIOS, ids=[scenario["name"] for scenario in SCENARIOS])
def test_every_shared_scenario(scenario):
    sets = _keysets()
    memory = {}
    for number, step in enumerate(scenario["steps"], 1):
        try:
            accepted = trust.accept(base64.b64decode(step["index"]), base64.b64decode(step["sig"]),
                                    sets[step["keyset"]], memory)
        except trust.Refused as error:
            verdict = error.reason
        else:
            verdict = "accept"
            memory = accepted.memory
        assert verdict == step["expect"], f"step {number}: {step['note']}"


def test_every_reason_is_exercised_by_the_vectors():
    seen = {step["expect"] for scenario in SCENARIOS for step in scenario["steps"]}
    assert seen == {"accept", *trust.REASONS}
    assert VECTORS["reasons"] == list(trust.REASONS)


def test_the_vectors_key_sets_derive_their_ids_and_fingerprints():
    for name, item in VECTORS["keysets"].items():
        keys = trust.keys_from_data(item["keys"])
        assert trust.fingerprint(keys) == item["fingerprint"], name
    for key in VECTORS["test_keys"].values():
        assert trust.key_id(base64.b64decode(key["public"])) == key["key_id"]


def test_accepting_does_not_change_the_memory_it_was_given():
    step = SCENARIOS[0]["steps"][0]
    other = "0" * 16
    memory = {"serials": {other: 3}, "silenced": []}
    accepted = trust.accept(base64.b64decode(step["index"]), base64.b64decode(step["sig"]),
                            _keysets()["test"], memory)
    assert memory == {"serials": {other: 3}, "silenced": []}
    assert accepted.memory == {"serials": {other: 3, accepted.key.key_id: 1}, "silenced": []}


def test_the_rank_floor_counts_only_keys_of_this_set():
    keys = _keysets()["test"]
    main, spare = keys
    assert trust.rank_floor(keys, None) == 0
    assert trust.rank_floor(keys, {"serials": {main.key_id: 5}}) == 1
    assert trust.rank_floor(keys, {"serials": {main.key_id: 5, spare.key_id: 1}}) == 2
    assert trust.rank_floor(keys, {"serials": {"0" * 16: 9}}) == 0


def test_accepting_a_key_silences_every_key_ranked_below_it_in_the_set():
    main, spare = _keysets()["test"]
    after_main = trust.remember(None, main, 3, _keysets()["test"])
    assert after_main == {"serials": {main.key_id: 3}, "silenced": []}
    after_spare = trust.remember(after_main, spare, 1, _keysets()["test"])
    assert after_spare == {"serials": {main.key_id: 3, spare.key_id: 1},
                           "silenced": [main.key_id]}
    # A silenced key is refused whatever set it is judged under, and the memory is plain data.
    with pytest.raises(trust.Refused, match="silenced"):
        trust.judge({"serial": 4}, main, (main,), after_spare)
    assert json.loads(json.dumps(after_spare)) == after_spare


def test_the_vectors_are_what_the_generator_writes():
    # The file is generated, never edited: regenerate it and compare, where OpenSSL can sign.
    if shutil.which("openssl") is None:
        pytest.skip("openssl is not installed")
    probe = subprocess.run(["openssl", "pkeyutl", "-help"], capture_output=True, text=True)
    if "-rawin" not in probe.stdout + probe.stderr:
        pytest.skip("this OpenSSL cannot sign Ed25519 (no -rawin)")
    spec = importlib.util.spec_from_file_location(
        "make_index_vectors", REPO_ROOT / "tools" / "make-index-vectors.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert module.generate() == VECTORS_PATH.read_bytes()


# ------------------------------------------------------------------ key sets --


def _data(**changes):
    item = trust.keys_to_data([trust.MAIN])[0]
    item.update(changes)
    return [item]


@pytest.mark.parametrize("items, words", [
    ([], "at least one key"),
    (_data(key_id="0" * 16), "not derived"),
    (_data(rank=0), "rank"),
    (_data(rank=True), "rank"),
    (_data(baseline=-1), "baseline"),
    (_data(public=base64.b64encode(b"\x00" * 31).decode()), "not an Ed25519 public key"),
    (_data(public="not base64!"), "not a key set"),
    (_data() + _data(), "share an id"),
    (_data() + trust.keys_to_data([trust.SPARE._replace(rank=1)]), "share a rank"),
    ([{"key_id": "x"}], "not a key set"),
])
def test_a_key_set_that_is_not_one_is_refused(items, words):
    with pytest.raises(trust.Refused) as refused:
        trust.keys_from_data(items)
    assert words in refused.value.detail


def test_a_key_set_round_trips_and_is_ordered_by_rank():
    data = trust.keys_to_data(reversed(trust.EMBEDDED))
    assert trust.keys_from_data(data) == trust.EMBEDDED


# ---------------------------------------------------------------- the format --


def _index(**changes):
    body = {
        "schema": 1, "package": trust.PACKAGE, "serial": 3, "issued": 1790500000,
        "key_id": trust.MAIN.key_id, "floor": "0.2.0",
        "releases": [{
            "version": "0.3.0", "filename": f"{trust.PACKAGE}_0.3.0_all.ipk", "size": 259730,
            "sha256": "12" * 32, "commit": "26" * 20, "commit_time": 1790324861, "contract": 1,
            "min_integration": None, "depends": ["python3-core"], "self_update": False,
            "withdrawn": None,
        }],
    }
    body.update(changes)
    return json.dumps(body).encode()


def test_a_well_formed_index_parses():
    assert trust.parse_index(_index())["serial"] == 3


@pytest.mark.parametrize("version", ["0.4.0rc1", "0.4", "v0.4.0", "0.04.0", "0.4.0\n", " 0.4.0",
                                     "0." + chr(0x0664) + ".0", "", None, 4])
def test_a_version_is_a_plain_ascii_n_n_n(version):
    assert not trust.is_version(version)


@pytest.mark.parametrize("version", ["0.0.0", "0.4.0", "10.20.30"])
def test_plain_versions_are_versions(version):
    assert trust.is_version(version)


def test_versions_sort_as_numbers():
    assert sorted(["0.10.0", "0.9.0", "0.9.10", "1.0.0"], key=trust.version_key) == [
        "0.9.0", "0.9.10", "0.10.0", "1.0.0"]


def test_a_strict_reader_refuses_what_a_tolerant_one_ignores():
    extra = json.loads(_index())
    extra["mirror"] = "https://example.invalid/"
    raw = json.dumps(extra).encode()
    assert trust.parse_index(raw)["mirror"] == "https://example.invalid/"
    with pytest.raises(trust.Refused, match="unknown members"):
        trust.parse_index(raw, strict=True)


def test_a_strict_reader_wants_the_newest_release_first():
    body = json.loads(_index())
    older = dict(body["releases"][0], version="0.2.0",
                 filename=f"{trust.PACKAGE}_0.2.0_all.ipk")
    body["releases"] = [older, body["releases"][0]]
    with pytest.raises(trust.Refused, match="newest first"):
        trust.parse_index(json.dumps(body).encode(), strict=True)
    body["releases"].reverse()
    assert trust.parse_index(json.dumps(body).encode(), strict=True)


@pytest.mark.parametrize("origin", [
    "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/",
    "https://192.0.2.12:8443/feed/",
    "https://lab.example/",
])
def test_an_origin_is_https_with_a_final_slash(origin):
    assert trust.is_origin(origin)


@pytest.mark.parametrize("origin", [
    "http://example.invalid/feed/", "https://example.invalid/feed", "https://user@host/feed/",
    "https://host/feed/?x=1", "https://host/../feed/", "", None,
])
def test_anything_else_is_not_an_origin(origin):
    assert not trust.is_origin(origin)


# ---------------------------------------------- what an acceptance build may carry --

ACCEPTANCE = {"commit": "1" * 40, "time": 1, "dirty": False, "flavour": "acceptance"}
TEST_KEYS = VECTORS["keysets"]["test"]["keys"]
OVERRIDES = {"origin": "https://lab.example/feed/", "index_keys": TEST_KEYS}


def test_an_acceptance_build_uses_its_own_origin_and_keys():
    origin, keys = trust.configured(ACCEPTANCE, OVERRIDES)
    assert origin == "https://lab.example/feed/"
    assert trust.fingerprint(keys) == VECTORS["keysets"]["test"]["fingerprint"]


@pytest.mark.parametrize("flavour", ["release", "development", "nightly", None])
def test_no_other_build_honours_an_override(flavour):
    build = dict(ACCEPTANCE, flavour=flavour)
    assert trust.configured(build, OVERRIDES) == (trust.ORIGIN, trust.EMBEDDED)


def test_without_overrides_or_a_build_the_release_values_apply():
    assert trust.configured(None, OVERRIDES) == (trust.ORIGIN, trust.EMBEDDED)
    assert trust.configured(ACCEPTANCE, None) == (trust.ORIGIN, trust.EMBEDDED)
    assert trust.configured(ACCEPTANCE, {"origin": None, "index_keys": TEST_KEYS})[0] == \
        trust.ORIGIN


def test_the_signature_file_is_one_line_in_a_fixed_order():
    sig = trust.signature_file(trust.MAIN.key_id, b"\x01" * 64)
    assert sig.endswith(b"\n") and sig.count(b"\n") == 1
    assert list(json.loads(sig)) == ["key_id", "algorithm", "signature"]
    assert trust.parse_signature(sig) == (trust.MAIN.key_id, b"\x01" * 64)
