"""Signed release indexes, a fake origin and a synchronous worker, for the update check's tests.

The update check is the receiver's reader of the signed release index: it fetches the index from
the origin, or takes it relayed over MQTT, and judges it by the rule in `trust.py`. Its tests need
indexes signed by keys they control, an origin that answers the way a test says, and a worker that
runs when the test says - so that a check is one call and its answer the next line.

Signing is RFC 8032 in a few lines on top of the plugin's own verifier's arithmetic, so the tests
need no OpenSSL. The keys are the vectors' throwaway test keys, never a release key.
"""

import base64
import hashlib
import json
from pathlib import Path

from MQTTBridge import ed25519, trust

REPO_ROOT = Path(__file__).resolve().parents[1]
VECTORS = json.loads((REPO_ROOT / "tests" / "vectors" / "release-index.json").read_text("ascii"))

PACKAGE = trust.PACKAGE


def _encode(point):
    x_z, y_z, z = point[0], point[1], point[2]
    inverse = pow(z, ed25519.P - 2, ed25519.P)
    x, y = x_z * inverse % ed25519.P, y_z * inverse % ed25519.P
    return (y | ((x & 1) << 255)).to_bytes(32, "little")


def sign(seed, message):
    """An Ed25519 signature over `message` by the key whose 32-byte seed is `seed` (RFC 8032)."""
    digest = hashlib.sha512(seed).digest()
    scalar = bytearray(digest[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    secret = int.from_bytes(scalar, "little")
    public = _encode(ed25519._multiply(secret, ed25519.B))
    r = int.from_bytes(hashlib.sha512(digest[32:] + message).digest(), "little") % ed25519.L
    r_bytes = _encode(ed25519._multiply(r, ed25519.B))
    k = int.from_bytes(hashlib.sha512(r_bytes + public + message).digest(), "little") % ed25519.L
    return r_bytes + ((r + k * secret) % ed25519.L).to_bytes(32, "little")


def seed_of(name):
    return bytes.fromhex(VECTORS["test_keys"][name]["seed"])


def key_id_of(name):
    return VECTORS["test_keys"][name]["key_id"]


def keyset(name="test"):
    """One of the vectors' test key sets, as the reader holds it."""
    return trust.keys_from_data(VECTORS["keysets"][name]["keys"])


TEST_KEYS = keyset("test")


def release(version, *, contract=1, min_integration=None, depends=("python3-core",),
            withdrawn=None, self_update=True):
    return {
        "version": version,
        "filename": f"{PACKAGE}_{version}_all.ipk",
        "size": 300000,
        "sha256": "ab" * 32,
        "commit": "cd" * 20,
        "commit_time": 1790400000,
        "contract": contract,
        "min_integration": min_integration,
        "depends": list(depends),
        "self_update": self_update,
        "withdrawn": withdrawn,
    }


def index_bytes(serial, releases=None, *, key="t1", floor="0.2.0", issued=1790500000):
    """The exact bytes of an index - the ones that get signed."""
    body = {
        "schema": 1,
        "package": PACKAGE,
        "serial": serial,
        "issued": issued,
        "key_id": key_id_of(key),
        "floor": floor,
        "releases": releases if releases is not None else [release("0.4.0")],
    }
    return (json.dumps(body, indent=1) + "\n").encode("utf-8")


def signed(serial, releases=None, *, key="t1", floor="0.2.0", issued=1790500000):
    """`(index bytes, signature file bytes)` of an index signed by the test key `key`."""
    raw = index_bytes(serial, releases, key=key, floor=floor, issued=issued)
    return raw, trust.signature_file(key_id_of(key), sign(seed_of(key), raw))


def relay_payload(index_raw, signature_raw):
    """What the companion integration publishes, retained, on `enigma2mqtt/release_index`."""
    return json.dumps({
        "index": base64.b64encode(index_raw).decode("ascii"),
        "sig": base64.b64encode(signature_raw).decode("ascii"),
    }).encode("ascii")


class FakeOrigin:
    """The origin as `UpdateChecker.fetch` sees it: `fetch(url, cap, timeout)` -> (status, body).

    `serve(index, sig)` puts a pair up; `down()` makes every request fail the way a box without
    internet fails; `answer(name, status, body)` makes one file answer something else. Every
    request is recorded, with its cap and its timeout.
    """

    def __init__(self, origin=trust.ORIGIN):
        self.origin = origin
        self.files = {}
        self.calls = []
        self.unreachable = False
        self.queued = {}

    def serve(self, index_raw, signature_raw):
        self.files[trust.INDEX_FILE] = (200, index_raw)
        self.files[trust.SIGNATURE_FILE] = (200, signature_raw)

    def answer(self, name, status, body=b""):
        self.files[name] = (status, body)

    def then(self, name, status, body):
        """The answer after the next one for `name`: a publication landing between two reads."""
        self.queued.setdefault(name, []).append((status, body))

    def down(self):
        self.unreachable = True

    def names(self):
        return [call[0][len(self.origin):] for call in self.calls]

    def __call__(self, url, cap, timeout):
        from MQTTBridge import updatecheck

        self.calls.append((url, cap, timeout))
        if self.unreachable:
            raise updatecheck.Unreachable("no route to host")
        assert url.startswith(self.origin), url
        name = url[len(self.origin):]
        status, body = self.files.get(name, (404, b"not found"))
        if self.queued.get(name):
            self.files[name] = self.queued[name].pop(0)
        return status, body[: cap + 1]


def inline(job):
    """The worker, run now: the test is the main loop and the thread at once."""
    job()
