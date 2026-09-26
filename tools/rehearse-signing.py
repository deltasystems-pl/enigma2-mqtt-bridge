#!/usr/bin/env python3
"""Run the sign job's steps with a throwaway key and prove they produce a signature that verifies.

    tools/rehearse-signing.py

`index.yml` runs this on every pull request, on a real runner, under the sign job's own
permissions (none), so a change to the sign step - or to the runner's OpenSSL - is found before the
maintainer approves a real run. The tests run it too. What it does:

1. makes a throwaway Ed25519 seed for this run only, and its public key through OpenSSL, and a
   one-key test set around it - never the release keys, whose private halves are nowhere near a
   pull request;
2. writes a small, well-formed unsigned index for that key and runs `make-index.py check` on it,
   as the sign job does;
3. takes the sign step's `run` text **out of publish-index.yml** - the text the real job will run,
   not a copy - and runs it with `bash -eo pipefail` (what `shell: bash` means) and the throwaway
   seed in `INDEX_SIGNING_KEY`, in a scratch directory;
4. checks that the step wrote the raw signature and nothing else, and that the seed appears in no
   file and in none of its output;
5. runs `make-index.py wrap` and `verify` on the result, as the sign and publish jobs do - and
   shows that a signature by any other key is refused by `wrap`.

Nothing is published and the scratch directory is removed.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from MQTTBridge import trust  # noqa: E402

SECRET = "INDEX_SIGNING_KEY"


def _tool(name):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_"),
                                                  REPO_ROOT / "tools" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def public_key(seed, openssl="openssl"):
    """The raw public key of a seed, through OpenSSL: the last 32 bytes of its public key info."""
    sign_index = _tool("sign-index")
    result = subprocess.run([openssl, "pkey", "-inform", "DER", "-pubout", "-outform", "DER"],
                            input=sign_index.PKCS8_PREFIX + seed, capture_output=True, check=True)
    return result.stdout[-32:]


def throwaway_key(openssl="openssl"):
    seed = os.urandom(32)
    public = public_key(seed, openssl)
    return seed, trust.Key(trust.key_id(public), 1, public, 0)


def sample_index(key, serial=1):
    """A small index that `check` accepts: one made-up release, signed by nobody yet."""
    make_index = _tool("make-index")
    entry = {
        "version": "0.0.1", "filename": f"{trust.PACKAGE}_0.0.1_all.ipk", "size": 1,
        "sha256": "0" * 64, "commit": "0" * 40, "commit_time": 1, "contract": 1,
        "min_integration": None, "depends": [], "self_update": False, "withdrawn": None,
    }
    return make_index.render({
        "schema": trust.SCHEMA, "package": trust.PACKAGE, "serial": serial, "issued": 1,
        "key_id": key.key_id, "floor": "0.0.1", "releases": [entry],
    })


def run_step(work, script, environment, bash="bash"):
    """Run a step's text in `work` as `shell: bash` does: (returncode, output)."""
    step = Path(work) / "step.sh"
    step.write_text(script, encoding="utf-8")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), **environment}
    result = subprocess.run([bash, "--noprofile", "--norc", "-eo", "pipefail", str(step)],
                            cwd=work, env=env, capture_output=True, check=False)
    step.unlink()
    return result.returncode, result.stdout + result.stderr


def run_sign_step(work, seed, script=None, bash="bash"):
    """Run the sign step's text in `work` with `seed` as the secret: (returncode, output)."""
    if script is None:
        script = _tool("check-workflows").sign_step_script()
    return run_step(work, script, {SECRET: base64.b64encode(seed).decode()}, bash)


def run_hash_step(work, expected, script=None, bash="bash"):
    """Run the hash step's text in `work` with `expected` as build's sha256."""
    if script is None:
        script = _tool("check-workflows").hash_step_script()
    return run_step(work, script, {"EXPECTED": expected}, bash)


def rehearse(openssl="openssl", log=print):
    make_index = _tool("make-index")
    seed, key = throwaway_key(openssl)
    other_seed, _other = throwaway_key(openssl)
    keys = trust.check_keys([key])
    with tempfile.TemporaryDirectory() as work:
        work = Path(work)
        (work / "index").mkdir()
        index_path = work / "index" / trust.INDEX_FILE
        index_path.write_bytes(sample_index(key))
        make_index.check_unsigned(index_path.read_bytes(), keys, key.key_id, (None, None))
        log(f"throwaway key {key.key_id}: unsigned index checked")

        # The hash step, as the sign job runs it before the key: the right hash passes, any other
        # stops the job.
        right = hashlib.sha256(index_path.read_bytes()).hexdigest()
        code, output = run_hash_step(work, right)
        if code:
            raise SystemExit("the hash step refused the right hash: "
                             + output.decode("utf-8", "replace"))
        code, _output = run_hash_step(work, "0" * 64)
        if code == 0:
            raise SystemExit("the hash step accepted a wrong hash")
        log("hash step: the right sha256 passes, a wrong one stops the job")

        code, output = run_sign_step(work, seed)
        if code:
            raise SystemExit(f"the sign step failed ({code}): {output.decode('utf-8', 'replace')}")
        written = sorted(path.name for path in (work / "index").iterdir())
        if written != [trust.INDEX_FILE, "releases.json.raw-sig"]:
            raise SystemExit(f"the sign step left {written}, not just the raw signature")
        encoded = base64.b64encode(seed)
        for path in (work / "index").iterdir():
            content = path.read_bytes()
            if seed in content or encoded in content:
                raise SystemExit(f"the seed is in {path.name}")
        if seed in output or encoded in output:
            raise SystemExit("the seed is in the sign step's output")
        raw_signature = (work / "index" / "releases.json.raw-sig").read_bytes()
        log(f"sign step: {len(raw_signature)}-byte signature, nothing else written, the seed "
            "in no file and no output")

        sig = make_index.wrap(index_path.read_bytes(), raw_signature, keys, key.key_id)
        accepted = make_index.verify(index_path.read_bytes(), sig, keys, (None, None))
        log(f"wrap and verify: accepted, serial {accepted.index['serial']}, key "
            f"{accepted.key.key_id}")

        # The same step with another key's seed - a wrong secret - must not get past wrap.
        (work / "index" / "releases.json.raw-sig").unlink()
        code, output = run_sign_step(work, other_seed)
        wrong = (work / "index" / "releases.json.raw-sig").read_bytes() if code == 0 else b""
        try:
            make_index.wrap(index_path.read_bytes(), wrong, keys, key.key_id)
        except make_index.IndexError_ as error:
            log(f"a signature by another key: refused - {error}")
        else:
            raise SystemExit("a signature by another key was accepted")
    version = subprocess.run([openssl, "version"], capture_output=True, text=True).stdout.strip()
    log(f"rehearsal passed with {version}")
    return json.dumps({"key_id": key.key_id, "openssl": version})


def main():
    rehearse()
    return 0


if __name__ == "__main__":
    sys.exit(main())
