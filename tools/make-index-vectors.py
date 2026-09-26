#!/usr/bin/env python3
"""Write `tests/vectors/release-index.json`: the cases every reader of the index must agree on.

    tools/make-index-vectors.py [--out tests/vectors/release-index.json] [--check]

The plugin (`trust.py`) and the companion integration each implement the index's acceptance rule,
in their own code; the vectors are the one definition both are tested against - the integration
reads this file from the plugin's source archive it bundles. They carry:

- **signatures**: RFC 8032's test vectors 1, 2, 3 and SHA(abc), which must verify; and forgeries
  that must not - a tampered message, `R`, `S` and key, `S + L`, a non-canonical encoding of `R`,
  a signature whose two sides share `x` but not `y`, and wrong lengths. The two crafted ones are
  accepted by a verifier that skips one check and refused by one that makes it, which is what
  makes them worth having;
- **key sets**: throwaway test keys - their seeds are in the file, and nothing trusts them - with
  ranks and baselines, the `key_id` each derives to, and each set's fingerprint; and the release
  keys as embedded, with theirs;
- **scenarios**: sequences of signed indexes, each step with the key set the reader holds and the
  verdict it must reach - `accept` or a reason code (`trust.REASONS`). A reader starts every
  scenario with no memory and carries its memory from step to step in whatever form it stores it;
  only the verdicts are compared.

Signing needs OpenSSL 3 (`pkeyutl -rawin`); the crafted forgeries are computed here. `--check`
regenerates in memory and fails when the committed file differs: the file is this tool's output,
nothing edited by hand. When Python's `cryptography` is importable, every signature verdict is
also checked against it - which is OpenSSL's verifier, the integration's.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import importlib.util
import json
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from MQTTBridge import ed25519, trust  # noqa: E402

OUT = REPO_ROOT / "tests" / "vectors" / "release-index.json"
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")
ISSUED = 1790500000

RFC8032 = [
    ("rfc8032 test 1", "d75a980182b10ab7d54bfed3c964073a0ee172f3daa62325af021a68f707511a", "",
     "e5564300c360ac729086e2cc806e828a84877f1eb8e5d974d873e065224901555"
     "fb8821590a33bacc61e39701cf9b46bd25bf5f0595bbe24655141438e7a100b"),
    ("rfc8032 test 2", "3d4017c3e843895a92b70aa74d1b7ebc9c982ccf2ec4968cc0cd55f12af4660c", "72",
     "92a009a9f0d4cab8720e820b5f642540a2b27b5416503f8fb3762223ebdb69da0"
     "85ac1e43e15996e458f3613d0f11d8c387b2eaeb4302aeeb00d291612bb0c00"),
    ("rfc8032 test 3", "fc51cd8e6218a1a38da47ed00230f0580816ed13ba3303ac5deb911548908025",
     "af82",
     "6291d657deec24024827e69c3abe01a30ce548a284743a445e3680d7db5ac3ac1"
     "8ff9b538d16f290ae67f760984dc6594a7c15e9716ed28dc027beceea1ec40a"),
    ("rfc8032 test sha(abc)", "ec172b93ad5e563bf4932c70e1245034c35467ef2efd4d64ebf819683467e2bf",
     "ddaf35a193617abacc417349ae20413112e6fa4e89a97ea20a9eeee64b55d39a"
     "2192992a274fc1a836ba3c23a3feebbd454d4423643ce80e2a9ac94fa54ca49f",
     "dc2a4459e7369633a52b1bf277839a00201009a3efbf3ecb69bea2186c26b5890"
     "9351fc9ac90b3ecfdfbc7c66431e0303dca179c138ac17ad9bef1177331a704"),
]


def _make_index():
    spec = importlib.util.spec_from_file_location("make_index", REPO_ROOT / "tools" /
                                                  "make-index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# ------------------------------------------------------------ keys and signing --


def seed(number):
    return hashlib.sha256(f"enigma2-mqtt-bridge release index test key {number}".encode()).digest()


def _openssl(args, data):
    return subprocess.run(["openssl", *args], input=data, capture_output=True, check=True).stdout


def public_of(secret):
    return _openssl(["pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
                    PKCS8_PREFIX + secret)[-32:]


def sign(secret, message):
    with tempfile.TemporaryDirectory() as work:
        path = Path(work) / "message"
        path.write_bytes(message)
        return _openssl(["pkeyutl", "-sign", "-rawin", "-keyform", "DER", "-inkey", "/dev/stdin",
                         "-in", str(path)], PKCS8_PREFIX + secret)


def scalar_of(secret):
    """The secret scalar RFC 8032 derives from a seed: the clamped low half of its sha512."""
    digest = bytearray(hashlib.sha512(secret).digest()[:32])
    digest[0] &= 248
    digest[31] &= 127
    digest[31] |= 64
    return int.from_bytes(digest, "little")


def encode(point):
    x_z, y_z, z = point[0], point[1], point[2]
    inverse = pow(z, ed25519.P - 2, ed25519.P)
    x, y = x_z * inverse % ed25519.P, y_z * inverse % ed25519.P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def _k(r_bytes, public, message):
    return int.from_bytes(hashlib.sha512(r_bytes + public + message).digest(), "little") % ed25519.L


def forge_non_canonical_r(secret, public, message):
    """R = the identity, spelled with y = p + 1: a verifier that reduces y mod p accepts it."""
    r_bytes = (ed25519.P + 1).to_bytes(32, "little")
    s = _k(r_bytes, public, message) * scalar_of(secret) % ed25519.L
    return r_bytes + s.to_bytes(32, "little")


def forge_x_only(secret, public, message):
    """R = T2 + [r]B, T2 = (0, -1): then R + [k]A and [S]B share x and differ in y."""
    r = 12345
    order_two = (0, ed25519.P - 1, 1, 0)
    r_bytes = encode(ed25519._add(order_two, ed25519._multiply(r, ed25519.B)))
    s = (-r - _k(r_bytes, public, message) * scalar_of(secret)) % ed25519.L
    return r_bytes + s.to_bytes(32, "little")


def torsion_of_order_8():
    """A point of order 8: the torsion part [L]P of some curve point P that has one."""
    identity = ed25519._IDENTITY
    for y in range(2, 1000):
        point = ed25519.decode_point(y.to_bytes(32, "little"))
        if point is None:
            continue
        torsion = ed25519._multiply(ed25519.L, point)
        four = ed25519._multiply(4, torsion)
        if not ed25519._equal(four, identity):
            return torsion
    raise SystemExit("no point of order 8 found")


def mixed_order_signature(secret, public, message):
    """A key A' = A + T8 with a torsion part, and a signature that satisfies the cofactorless
    equation only when k is reduced mod L - as RFC 8032 and OpenSSL reduce it. A verifier that
    uses the unreduced hash accepts nothing here; OpenSSL and the plugin both accept it."""
    t8 = torsion_of_order_8()
    a_point = ed25519.decode_point(public)
    mixed = encode(ed25519._add(a_point, t8))
    a = scalar_of(secret)
    for r in range(1, 5000):
        for j in range(8):
            torsion = ed25519._multiply(j, t8) if j else ed25519._IDENTITY
            r_bytes = encode(ed25519._add(ed25519._multiply(r, ed25519.B), torsion))
            full = int.from_bytes(hashlib.sha512(r_bytes + mixed + message).digest(), "little")
            k = full % ed25519.L
            if (j + k) % 8 == 0 and full % 8 != k % 8:
                s = (r + k * a) % ed25519.L
                return mixed, r_bytes + s.to_bytes(32, "little")
    raise SystemExit("no mixed-order signature found")


def _flip(data, position):
    data = bytearray(data)
    data[position] ^= 0x01
    return bytes(data)


def signature_vectors(test_secret, test_public):
    vectors = []
    for name, public, message, signature in RFC8032:
        vectors.append({"name": name, "public": public, "message": message,
                        "signature": signature, "valid": True})
    _name, public, message, signature = RFC8032[1]
    sig = bytes.fromhex(signature)
    s_plus_l = sig[:32] + (int.from_bytes(sig[32:], "little") + ed25519.L).to_bytes(32, "little")
    negated = sig[:32] + (ed25519.L - int.from_bytes(sig[32:], "little")).to_bytes(32, "little")
    identity = (1).to_bytes(32, "little")
    tampered = [
        ("tampered message", public, "73", signature),
        ("tampered R", public, message, _flip(sig, 0).hex()),
        ("tampered S", public, message, _flip(sig, 40).hex()),
        ("tampered key", RFC8032[2][1], message, signature),
        ("S + L, the same point: S must be below L", public, message, s_plus_l.hex()),
        ("(R, L - S): [S]B negated, the same y", public, message, negated.hex()),
        ("S = L under the identity key, R the identity: S must be below L", identity.hex(),
         message, (identity + ed25519.L.to_bytes(32, "little")).hex()),
        ("signature of 63 bytes", public, message, signature[:-2]),
        ("key of 31 bytes", public[:-2], message, signature),
    ]
    message = b"release index"
    tampered.append(("R spelled non-canonically: y = p + 1", test_public.hex(), message.hex(),
                     forge_non_canonical_r(test_secret, test_public, message).hex()))
    tampered.append(("R + [k]A and [S]B share x, not y", test_public.hex(), message.hex(),
                     forge_x_only(test_secret, test_public, message).hex()))
    for name, public, message, signature in tampered:
        vectors.append({"name": name, "public": public, "message": message,
                        "signature": signature, "valid": False})
    message = b"release index"
    mixed, mixed_signature = mixed_order_signature(test_secret, test_public, message)
    vectors.append({"name": "a key with a torsion part: k is reduced mod L", "public": mixed.hex(),
                    "message": message.hex(), "signature": mixed_signature.hex(), "valid": True})
    return vectors


# ---------------------------------------------------------------- key sets --


def key_sets(test_keys):
    def item(name, rank, baseline=0):
        public = test_keys[name]["public"]
        return {"key": name, "key_id": trust.key_id(public), "rank": rank,
                "public": base64.b64encode(public).decode(), "baseline": baseline}

    sets = {
        "test": [item("t1", 1), item("t2", 2)],
        "test-plus": [item("t1", 1), item("t2", 2), item("t3", 3)],
        "test-dropped": [item("t2", 2), item("t3", 3)],
        "test-spare-dropped": [item("t1", 1), item("t3", 3)],
        "test-spare-only": [item("t2", 2)],
        "other": [item("t3", 1)],
        "test-baseline": [item("t1", 1, 5000), item("t2", 2)],
    }
    for name, items in list(sets.items()):
        keys = trust.keys_from_data(items)
        sets[name] = {"keys": items, "fingerprint": trust.fingerprint(keys)}
    return sets


# --------------------------------------------------------------- scenarios --


class Builder:
    def __init__(self, test_keys, render):
        self.keys = test_keys
        self.render = render

    def index(self, signer, serial, **changes):
        entry = {
            "version": "0.4.0", "filename": f"{trust.PACKAGE}_0.4.0_all.ipk", "size": 300000,
            "sha256": "ab" * 32, "commit": "cd" * 20, "commit_time": 1790400000,
            "contract": 1, "min_integration": None, "depends": ["python3-core"],
            "self_update": True, "withdrawn": None,
        }
        entry.update(changes.pop("entry", {}))
        body = {"schema": 1, "package": trust.PACKAGE, "serial": serial, "issued": ISSUED,
                "key_id": trust.key_id(self.keys[signer]["public"]), "floor": "0.2.0",
                "releases": [entry]}
        body.update(changes)
        return self.render(body)

    def sig(self, signer, raw, as_key=None):
        """The signature file for `raw` signed by `signer`, naming `as_key` (default the signer)."""
        named = trust.key_id(self.keys[as_key or signer]["public"])
        return trust.signature_file(named, sign(self.keys[signer]["secret"], raw))

    def step(self, keyset, signer, raw, expect, note, sig=None, lineage="release"):
        return {"keyset": keyset, "lineage": lineage, "note": note,
                "index": base64.b64encode(raw).decode(),
                "sig": base64.b64encode(sig if sig is not None else self.sig(signer, raw)).decode(),
                "expect": expect}


def scenarios(b):
    s = b.step
    raw = b.index
    out = []

    def scenario(name, *steps):
        out.append({"name": name, "steps": list(steps)})

    scenario("a first index within the baseline window is accepted",
             s("test", "t1", raw("t1", 1), "accept", "serial 1, baseline 0"))
    same = raw("t1", 1)
    scenario("an equal or lower serial is refused, however its bytes differ",
             s("test", "t1", same, "accept", "serial 1"),
             s("test", "t1", same, "replay", "the same bytes again"),
             s("test", "t1", raw("t1", 1, issued=ISSUED + 1), "replay", "serial 1, other bytes"),
             s("test", "t1", raw("t1", 2), "accept", "serial 2"),
             s("test", "t1", raw("t1", 1), "replay", "serial 1 after 2"))
    scenario("the next serial may be at most 1000 above the last",
             s("test", "t1", raw("t1", 1), "accept", "serial 1"),
             s("test", "t1", raw("t1", 1002), "jump", "+1001"),
             s("test", "t1", raw("t1", 1001), "accept", "+1000"))
    scenario("a key seen for the first time: within 1000 of its baseline",
             s("test", "t1", raw("t1", 1001), "first_sight", "baseline 0 + 1001"),
             s("test", "t1", raw("t1", 1000), "accept", "baseline 0 + 1000"))
    scenario("the baseline embedded at build time moves the first-sight window",
             s("test-baseline", "t1", raw("t1", 1), "first_sight", "baseline 5000, serial 1"),
             s("test-baseline", "t1", raw("t1", 6001), "first_sight", "baseline 5000 + 1001"),
             s("test-baseline", "t1", raw("t1", 5000), "accept", "the baseline itself"))
    scenario("after a rank-2 index, rank 1 is refused for good",
             s("test", "t2", raw("t2", 1), "accept", "the spare, serial 1"),
             s("test", "t1", raw("t1", 5), "rank", "the main key after the spare"),
             s("test", "t1", raw("t1", 6), "rank", "and again"),
             s("test", "t2", raw("t2", 2), "accept", "the spare goes on"))
    scenario("serials are per key: a high rank-1 serial does not block a rank-2 index",
             s("test", "t1", raw("t1", 900), "accept", "main key, serial 900"),
             s("test", "t2", raw("t2", 1), "accept", "spare key, its own serial 1"))
    scenario("the index names the key that signed it",
             s("test", "t2", raw("t1", 1), "key_mismatch", "index names t1, t2 signed it",
               sig=b.sig("t2", raw("t1", 1))))
    scenario("a key the reader does not hold is refused",
             s("other", "t1", raw("t1", 1), "unknown_key", "t1 is not in the set"))
    tampered = bytearray(raw("t1", 1))
    tampered[tampered.index(b'"size": 300000') + 9] = ord("4")
    scenario("the signature covers every byte, and only the named key's counts",
             s("test", "t1", bytes(tampered), "bad_signature", "one digit changed after signing",
               sig=b.sig("t1", raw("t1", 1))),
             s("test", "t2", raw("t1", 1), "bad_signature", "t2 signed, the file names t1",
               sig=b.sig("t2", raw("t1", 1), as_key="t1")),
             s("test", "t2", b"not an index at all\n", "bad_signature",
               "neither signed by the named key nor JSON: the signature is judged first",
               sig=b.sig("t2", b"not an index at all\n", as_key="t1")),
             s("test", "t1", raw("t1", 1), "accept", "and the genuine pair"))
    genuine = raw("t1", 1)
    good_sig = json.loads(b.sig("t1", genuine))
    malformed_sigs = [
        ("another algorithm", dict(good_sig, algorithm="rsa")),
        ("an upper-case key id", dict(good_sig, key_id=good_sig["key_id"].upper())),
        ("63 bytes of signature", dict(good_sig, signature=base64.b64encode(
            base64.b64decode(good_sig["signature"])[:63]).decode())),
        ("not base64", dict(good_sig, signature="!" * 88)),
    ]
    scenario("a signature file that is not well formed",
             *[s("test", "t1", genuine, "malformed_signature", note,
                 sig=(json.dumps(body) + "\n").encode()) for note, body in malformed_sigs],
             s("test", "t1", genuine, "malformed_signature", "not JSON", sig=b"key_id=t1\n"),
             s("test", "t1", genuine, "too_large", "a signature file over 1024 bytes",
               sig=(json.dumps(dict(good_sig, padding="x" * 1100)) + "\n").encode()))
    # Pad a valid index with a long withdrawal reason to exactly 64 KiB, and to one byte more.
    unpadded = len(raw("t1", 1, entry={"withdrawn": "x"})) - 1
    exact = raw("t1", 1, entry={"withdrawn": "x" * (trust.MAX_INDEX_BYTES - unpadded)})
    big = raw("t1", 2, entry={"withdrawn": "x" * (trust.MAX_INDEX_BYTES + 1 - unpadded)})
    assert len(exact) == trust.MAX_INDEX_BYTES and len(big) == trust.MAX_INDEX_BYTES + 1
    scenario("an index of exactly 64 KiB is accepted; one byte more is refused before anything "
             "else",
             s("test", "t1", exact, "accept", "65536 bytes, validly signed"),
             s("test", "t1", big, "too_large", "65537 bytes, validly signed"),
             s("test", "t1", big, "too_large", "65 KiB, signed by another key: the cap comes "
               "before the signature", sig=b.sig("t2", big, as_key="t1")))

    def signed_text(text, note, expect):
        data = text.encode()
        return s("test", "t1", data, expect, note)

    good = raw("t1", 1).decode()
    malformed = [
        ("a pre-release version", good.replace('"0.4.0"', '"0.4.0rc1"', 1)),
        ("a version with a leading zero", good.replace('"version": "0.4.0"',
                                                       '"version": "0.04.0"')),
        ("a version with a trailing newline", good.replace('"version": "0.4.0"',
                                                           '"version": "0.4.0\\n"')),
        ("a version in non-ASCII digits", good.replace('"version": "0.4.0"',
                                                       '"version": "0.\\u0664.0"')),
        ("a serial that is true", good.replace('"serial": 1', '"serial": true')),
        ("serial 0", good.replace('"serial": 1', '"serial": 0')),
        ("a member twice", good.replace('"serial": 1,', '"serial": 1,\n  "serial": 2,')),
        ("NaN", good.replace(f'"issued": {ISSUED}', '"issued": NaN')),
        ("schema 2", good.replace('"schema": 1', '"schema": 2')),
        ("another package", good.replace('"package": "enigma2-plugin-extensions-mqttbridge"',
                                         '"package": "other"')),
        ("a filename that is not the package's", good.replace(
            '_0.4.0_all.ipk"', '_0.4.1_all.ipk"')),
        ("depends that is not a list", good.replace('"depends": ["python3-core"]',
                                                    '"depends": "python3-core"')),
        ("a withdrawn reason that is empty", good.replace('"withdrawn": null',
                                                          '"withdrawn": ""')),
        ("a floor that is not N.N.N", good.replace('"floor": "0.2.0"', '"floor": "0.2"')),
        ("the same version twice", good.replace(
            '"releases": [\n',
            '"releases": [\n' + good.split('"releases": [\n')[1].split("\n")[0] + ",\n")),
    ]
    scenario("a signed index that is not well formed is refused",
             *[signed_text(text, note, "malformed_index") for note, text in malformed])
    scenario("a reader tolerates a member it does not know",
             signed_text(good.replace('"floor": "0.2.0",', '"floor": "0.2.0",\n  "mirror": "x",'),
                         "an unknown top-level member", "accept"),
             signed_text(raw("t1", 2, entry={"notes": "x"}).decode(),
                         "an unknown member in a release", "accept"))
    scenario("memory kept under another key set does not raise this set's rank",
             s("test", "t2", raw("t2", 1), "accept", "test set: the spare"),
             s("other", "t3", raw("t3", 1), "accept",
               "a disjoint set - another lineage, as release keys are to test keys - whose "
               "rank-1 key is t3"),
             s("test", "t1", raw("t1", 2), "rank", "back under the test set: still silenced"))
    scenario("a release that adds a key keeps what the reader knew about the others",
             s("test", "t1", raw("t1", 10), "accept", "serial 10"),
             s("test-plus", "t1", raw("t1", 10), "replay", "the serial came along"),
             s("test-plus", "t1", raw("t1", 11), "accept", "serial 11"),
             s("test-plus", "t2", raw("t2", 1), "accept", "the spare"),
             s("test-plus", "t1", raw("t1", 12), "rank", "the silenced key stays silenced"))
    scenario("a release that drops the spare but keeps the main key: the main key stays silenced",
             s("test", "t1", raw("t1", 1), "accept", "the main key"),
             s("test", "t2", raw("t2", 1), "accept", "the spare silences it"),
             s("test-spare-dropped", "t1", raw("t1", 2), "rank",
               "a release without the spare: the main key is still silenced"),
             s("test-spare-dropped", "t3", raw("t3", 1), "accept", "a higher key, first sight"))
    scenario("a key added below one already accepted is refused by the rank floor",
             s("test-spare-only", "t2", raw("t2", 1), "accept", "a set with the spare alone"),
             s("test", "t1", raw("t1", 1), "rank",
               "a release adds the main key below it: never silenced, still refused"))
    scenario("an acceptance build's memory is never read by a release build",
             s("test", "t1", raw("t1", 1), "accept", "release lineage: the main key"),
             s("test", "t2", raw("t2", 1), "accept",
               "acceptance lineage, the same keys: the spare silences the main key there",
               lineage="acceptance"),
             s("test", "t1", raw("t1", 1), "rank", "acceptance lineage: silenced there",
               lineage="acceptance"),
             s("test", "t1", raw("t1", 2), "accept",
               "release lineage: the main key goes on, nothing the test taught is read"))
    scenario("a release that drops a key refuses it",
             s("test", "t1", raw("t1", 3), "accept", "serial 3"),
             s("test-dropped", "t1", raw("t1", 4), "unknown_key", "t1 no longer embedded"),
             s("test-dropped", "t2", raw("t2", 1), "accept", "the spare, first sight"))
    return out


# ------------------------------------------------------------ stored shapes --


def memory_shapes(test_keys):
    """Memories a reader must load as they are, and ones it must refuse - never read as empty."""
    k1 = trust.key_id(test_keys["t1"]["public"])
    k2 = trust.key_id(test_keys["t2"]["public"])
    good = [
        ({"serials": {}, "silenced": []}, "nothing accepted yet"),
        ({"serials": {k1: 3, k2: 1}, "silenced": [k1]}, "two keys, one silenced"),
    ]
    bad = [
        ({k1: 5}, "the first shape: a bare map of serials"),
        ({"serials": {k1: "7"}, "silenced": []}, "a serial that is a string"),
        ({"serials": {k1: True}, "silenced": []}, "a serial that is true"),
        ({"serials": {k1: 0}, "silenced": []}, "serial 0"),
        ({"serials": [], "silenced": []}, "serials as a list"),
        ({"serials": {}, "silenced": 5}, "silenced as a number"),
        ({"serials": {}, "silenced": ["x"]}, "a silenced entry that is not a key id"),
        ({"serials": {"KEY": 1}, "silenced": []}, "a serial for something that is not a key id"),
        ({"serials": {}}, "silenced missing"),
        ({"serials": {}, "silenced": [], "rank": 2}, "a member no reader writes"),
        ([], "not an object"),
    ]
    return ([{"memory": memory, "valid": True, "note": note} for memory, note in good]
            + [{"memory": memory, "valid": False, "note": note} for memory, note in bad])


def state_shapes(test_keys):
    k1 = trust.key_id(test_keys["t1"]["public"])
    entry = {"keys": [k1], "serials": {k1: 2}, "silenced": []}
    fp = "ab" * 32
    good = [
        ({"schema": 1, "release": {}, "acceptance": {}}, "nothing stored"),
        ({"schema": 1, "release": {fp: entry}, "acceptance": {fp: entry}}, "one entry in each"),
    ]
    bad = [
        ({"serials": {k1: 2}, "silenced": []}, "a bare memory, not a state"),
        ({"schema": 2, "release": {}, "acceptance": {}}, "another schema"),
        ({"schema": 1, "release": {}}, "acceptance missing"),
        ({"schema": 1, "release": {"x": entry}, "acceptance": {}}, "a key that is no fingerprint"),
        ({"schema": 1, "release": {fp: dict(entry, keys=[])}, "acceptance": {}},
         "an empty key list"),
        ({"schema": 1, "release": {fp: dict(entry, serials={k1: -1})}, "acceptance": {}},
         "a negative serial"),
    ]
    return ([{"state": state, "valid": True, "note": note} for state, note in good]
            + [{"state": state, "valid": False, "note": note} for state, note in bad])


# --------------------------------------------------------------------- tool --


def generate():
    render = _make_index().render
    test_keys = {}
    for number in (1, 2, 3):
        secret = seed(number)
        test_keys[f"t{number}"] = {"secret": secret, "public": public_of(secret)}
    b = Builder(test_keys, render)
    data = {
        "about": "Shared vectors for the signed release index; see tools/make-index-vectors.py "
                 "and docs/RELEASE-INDEX.md. Generated - do not edit by hand. The t1-t3 seeds "
                 "are public test keys that nothing trusts.",
        "reasons": list(trust.REASONS),
        "max_jump": trust.MAX_JUMP,
        "max_index_bytes": trust.MAX_INDEX_BYTES,
        "release_keys": {
            "keys": trust.keys_to_data(trust.EMBEDDED),
            "fingerprint": trust.fingerprint(trust.EMBEDDED),
            "key_id": "first 16 hex digits of sha256 over the raw 32-byte public key",
        },
        "test_keys": {
            name: {"seed": key["secret"].hex(), "public": base64.b64encode(key["public"]).decode(),
                   "key_id": trust.key_id(key["public"])}
            for name, key in test_keys.items()
        },
        "keysets": key_sets(test_keys),
        "signatures": signature_vectors(test_keys["t1"]["secret"], test_keys["t1"]["public"]),
        "scenarios": scenarios(b),
        "memories": memory_shapes(test_keys),
        "states": state_shapes(test_keys),
    }
    return (json.dumps(data, indent=1, ensure_ascii=True) + "\n").encode("ascii")


def cross_check(data):
    """Every signature verdict against OpenSSL's verifier, when `cryptography` is here."""
    try:
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
    except ImportError:
        return "cryptography not installed: not cross-checked"
    for vector in json.loads(data)["signatures"]:
        public = bytes.fromhex(vector["public"])
        try:
            Ed25519PublicKey.from_public_bytes(public).verify(
                bytes.fromhex(vector["signature"]), bytes.fromhex(vector["message"]))
            verdict = True
        except (InvalidSignature, ValueError):
            verdict = False
        if verdict != vector["valid"]:
            raise SystemExit(f"OpenSSL says {verdict} for {vector['name']!r}")
        if ed25519.verify(public, bytes.fromhex(vector["message"]),
                          bytes.fromhex(vector["signature"])) != vector["valid"]:
            raise SystemExit(f"the plugin's verifier disagrees on {vector['name']!r}")
    return "every signature verdict equals OpenSSL's (cryptography)"


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", type=Path, default=OUT)
    parser.add_argument("--check", action="store_true",
                        help="fail when the committed file is not what this writes")
    args = parser.parse_args(argv)
    data = generate()
    print(cross_check(data))
    if args.check:
        if args.out.read_bytes() != data:
            print(f"{args.out} is not what tools/make-index-vectors.py writes", file=sys.stderr)
            return 1
        print(f"{args.out}: up to date")
        return 0
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_bytes(data)
    print(f"wrote {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
