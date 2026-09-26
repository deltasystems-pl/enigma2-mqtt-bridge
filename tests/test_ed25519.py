"""The plugin's own Ed25519 verifier: RFC 8032's vectors pass, and every forgery in them fails.

The vectors live in `tests/vectors/release-index.json`, shared with the companion integration,
whose verifier is OpenSSL's; `tools/make-index-vectors.py` checked every verdict there against
OpenSSL when it wrote the file. What is added here is what only this implementation can be asked:
its strict decoding, its refusal of anything that is not bytes, and its speed.
"""

import hashlib
import json
import time
from pathlib import Path

import pytest

from MQTTBridge import ed25519

VECTORS = json.loads(
    (Path(__file__).resolve().parent / "vectors" / "release-index.json").read_text("ascii")
)
SIGNATURES = VECTORS["signatures"]

TEST_2 = next(vector for vector in SIGNATURES if vector["name"] == "rfc8032 test 2")
PUBLIC = bytes.fromhex(TEST_2["public"])
MESSAGE = bytes.fromhex(TEST_2["message"])
SIGNATURE = bytes.fromhex(TEST_2["signature"])


@pytest.mark.parametrize("vector", SIGNATURES, ids=[vector["name"] for vector in SIGNATURES])
def test_every_shared_signature_vector(vector):
    verdict = ed25519.verify(
        bytes.fromhex(vector["public"]),
        bytes.fromhex(vector["message"]),
        bytes.fromhex(vector["signature"]),
    )
    assert verdict is vector["valid"]


def test_the_rfc_vectors_are_all_there():
    names = {vector["name"] for vector in SIGNATURES if vector["valid"]}
    assert names == {"rfc8032 test 1", "rfc8032 test 2", "rfc8032 test 3",
                     "rfc8032 test sha(abc)"}
    sha_abc = next(vector for vector in SIGNATURES if vector["name"] == "rfc8032 test sha(abc)")
    assert bytes.fromhex(sha_abc["message"]) == hashlib.sha512(b"abc").digest()


def test_the_forgeries_that_need_each_check_are_there():
    # Each of these is accepted by a verifier missing exactly one check: S < L, a canonical y,
    # both coordinates compared. Without them the corresponding mutant survives.
    names = {vector["name"] for vector in SIGNATURES if not vector["valid"]}
    assert "S + L, the same point: S must be below L" in names
    assert "R spelled non-canonically: y = p + 1" in names
    assert "R + [k]A and [S]B share x, not y" in names


@pytest.mark.parametrize("value", [None, "", "a" * 64, 0, [1, 2]])
def test_anything_but_bytes_is_false_and_never_an_exception(value):
    assert ed25519.verify(value, MESSAGE, SIGNATURE) is False
    assert ed25519.verify(PUBLIC, value, SIGNATURE) is False
    assert ed25519.verify(PUBLIC, MESSAGE, value) is False


def test_bytearrays_are_bytes():
    assert ed25519.verify(bytearray(PUBLIC), bytearray(MESSAGE), bytearray(SIGNATURE)) is True


@pytest.mark.parametrize("encoded", [
    # y = p: the canonical spelling of y = 0.
    ed25519.P.to_bytes(32, "little"),
    # y = 2^255 - 1, above p.
    ((1 << 255) - 1).to_bytes(32, "little"),
    # y = 1 is the identity, x = 0; its "negative" spelling sets the sign bit.
    (1 | (1 << 255)).to_bytes(32, "little"),
    # y = 2 is on no point of the curve.
    (2).to_bytes(32, "little"),
    b"\x00" * 31,
])
def test_a_point_has_one_spelling_and_must_be_on_the_curve(encoded):
    assert ed25519.decode_point(encoded) is None


def test_the_identity_and_the_base_point_decode():
    identity = ed25519.decode_point((1).to_bytes(32, "little"))
    assert identity is not None and identity[0] == 0
    base = ed25519.decode_point(ed25519._B_Y.to_bytes(32, "little"))
    assert ed25519._equal(base, ed25519.B)


def test_a_signature_by_another_key_fails():
    other = next(vector for vector in SIGNATURES if vector["name"] == "rfc8032 test 3")
    assert ed25519.verify(bytes.fromhex(other["public"]), MESSAGE, SIGNATURE) is False


def test_it_is_fast_enough_for_the_receiver():
    # 3 ms here; 40 ms measured on the armv7 receiver. The bound is loose on purpose - this only
    # catches an accidental quadratic, not a slow runner.
    started = time.perf_counter()
    for _ in range(5):
        assert ed25519.verify(PUBLIC, MESSAGE, SIGNATURE)
    assert (time.perf_counter() - started) / 5 < 0.25
