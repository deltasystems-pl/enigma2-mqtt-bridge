#!/usr/bin/env python3
"""Sign a release index offline - the spare key's emergency path, never the routine one.

    tools/sign-index.py --index releases.json --key-file SEED_FILE [--out releases.json.sig]

Every ordinary index is signed by `publish-index.yml`'s sign job, in CI, with the main key, after
the maintainer approves it. This is for the day the main key is leaked or lost
(docs/RELEASE-INDEX.md, "When the main key is lost or leaked"): on a machine that never held the
main key, the maintainer builds the index with `tools/make-index.py build --key-id <spare>`, signs
it here with the sealed spare key, and opens a pull request adding both files under
`release-index/emergency/`. Using the spare cannot be undone - every reader that accepts one index
signed with it ignores the main key for good - so it is never used to test anything.

`SEED_FILE` holds one line: the raw 32-byte Ed25519 seed in base64 (`-` reads it from stdin). The
seed goes to OpenSSL's stdin as DER and is never written anywhere; the signature is checked with
the embedded public key of the key the index names before the signature file is written, so a
wrong seed produces an error, not a file.

Needs OpenSSL 3 (`openssl pkeyutl -rawin`).
"""

from __future__ import annotations

import argparse
import base64
import binascii
import importlib.util
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from MQTTBridge import trust  # noqa: E402

# The fixed PKCS#8 header of an Ed25519 private key, before its 32-byte seed (RFC 8410).
PKCS8_PREFIX = bytes.fromhex("302e020100300506032b657004220420")


def _make_index():
    spec = importlib.util.spec_from_file_location("make_index", REPO_ROOT / "tools" /
                                                  "make-index.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def seed_from(text):
    try:
        seed = base64.b64decode(text.strip(), validate=True)
    except (binascii.Error, ValueError) as error:
        raise ValueError("the key file is not one line of base64") from error
    if len(seed) != 32:
        raise ValueError("the key file is not a 32-byte Ed25519 seed")
    return seed


def openssl_sign(seed, message_path, openssl="openssl"):
    """The raw 64-byte signature, the key passed to OpenSSL on stdin only."""
    with tempfile.TemporaryDirectory() as work:
        out = Path(work) / "signature"
        result = subprocess.run(
            [openssl, "pkeyutl", "-sign", "-rawin", "-keyform", "DER", "-inkey", "/dev/stdin",
             "-in", str(message_path), "-out", str(out)],
            input=PKCS8_PREFIX + seed, capture_output=True, check=False,
        )
        if result.returncode:
            raise ValueError(f"openssl refused: {result.stderr.decode('utf-8', 'replace').strip()}")
        return out.read_bytes()


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--index", type=Path, required=True)
    parser.add_argument("--key-file", required=True, help="one line of base64, or - for stdin")
    parser.add_argument("--out", type=Path, help="default: <index>.sig")
    parser.add_argument("--keyset", help="a test key set (tests only)")
    args = parser.parse_args(argv)
    tool = _make_index()
    try:
        keys = tool.load_keys(args.keyset)
        raw = args.index.read_bytes()
        index = trust.parse_index(raw, strict=True)
        text = sys.stdin.read() if args.key_file == "-" else Path(args.key_file).read_text()
        signature = openssl_sign(seed_from(text), args.index)
        sig = tool.wrap(raw, signature, keys, index["key_id"])
    except (OSError, ValueError, trust.Refused, tool.IndexError_) as error:
        print(f"sign-index.py: {error}", file=sys.stderr)
        return 1
    out = args.out or args.index.with_name(args.index.name + ".sig")
    out.write_bytes(sig)
    print(f"signed serial {index['serial']} with key {index['key_id']}, verified, written to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
