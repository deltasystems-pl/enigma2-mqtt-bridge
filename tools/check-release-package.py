#!/usr/bin/env python3
"""Read a release package back and refuse it unless it is this release, with the release keys.

    tools/check-release-package.py dist/<package>.ipk [--commit SHA --time EPOCH]

The release workflow runs this after the build and before anything is published, instead of
trusting the build's exit status. It unpacks the package and asks **the package's own modules**,
imported from the unpacked tree in a separate interpreter - exactly what a receiver will import -
two questions:

- which build is this? It must be the commit being released (HEAD, unless `--commit` says
  otherwise), with that commit's time, a clean tree and the `release` flavour;
- what does it trust? `trust.configured` must answer the published origin and the embedded
  release keys. `buildinfo.py` must not even name a test origin or test keys: only an
  `acceptance` build may carry them (`tools/make-buildinfo.py` refuses them for any other), and a
  release that carried one would be a test build in a release's clothes.

Standard library only.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from MQTTBridge import buildid, trust  # noqa: E402 - this checkout's, to compare against

EXTENSIONS = "usr/lib/enigma2/python/Plugins/Extensions"

# Run inside the unpacked package: its modules, its buildinfo.py, nothing from this checkout.
_ASK = """
import json
from MQTTBridge import buildid, trust
origin, keys = trust.configured(buildid.LOADED, buildid.LOADED_OVERRIDES)
print(json.dumps({"build": buildid.LOADED, "origin": origin,
                  "keys": [key.key_id for key in keys], "fingerprint": trust.fingerprint(keys)}))
"""


class Refused(Exception):
    pass


def _data_member(ipk: Path) -> bytes:
    blob = ipk.read_bytes()
    if not blob.startswith(b"!<arch>\n"):
        raise Refused(f"{ipk}: not an ar archive")
    pos = 8
    while pos + 60 <= len(blob):
        header = blob[pos:pos + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        size = int(header[48:58].decode("ascii").strip())
        if name == "data.tar.gz":
            return blob[pos + 60:pos + 60 + size]
        pos += 60 + size + (size % 2)
    raise Refused(f"{ipk}: no data.tar.gz")


def check(ipk: Path, commit: str, when: int, python: str = sys.executable) -> dict:
    with tempfile.TemporaryDirectory() as work:
        with tarfile.open(fileobj=io.BytesIO(_data_member(ipk)), mode="r:gz") as tar:
            members = [member for member in tar.getmembers()
                       if member.name.lstrip("./").startswith(EXTENSIONS + "/MQTTBridge/")
                       and (member.isfile() or member.isdir())]
            # Names filtered above; the data filter as well where this Python has it.
            extra = {"filter": "data"} if hasattr(tarfile, "data_filter") else {}
            tar.extractall(work, members=members, **extra)
        extensions = Path(work) / EXTENSIONS
        text = (extensions / "MQTTBridge" / "buildinfo.py").read_text(encoding="utf-8")
        print(text, end="")
        if buildid.carries_overrides(text):
            raise Refused("the package names a test origin or test index keys")
        answer = subprocess.run(
            [python, "-I", "-c", f"import sys; sys.path.insert(0, {str(extensions)!r})\n" + _ASK],
            capture_output=True, text=True, check=False,
        )
        if answer.returncode:
            raise Refused(f"the package's own modules did not answer: {answer.stderr.strip()}")
        found = json.loads(answer.stdout)
    expected = {"commit": commit, "time": when, "dirty": False, "flavour": "release"}
    if found["build"] != expected:
        raise Refused(f"the package's build id is {found['build']}, expected {expected}")
    if found["origin"] != trust.ORIGIN:
        raise Refused(f"the package fetches from {found['origin']}, not {trust.ORIGIN}")
    if found["fingerprint"] != trust.fingerprint(trust.EMBEDDED):
        raise Refused(f"the package trusts keys {found['keys']}, not the release keys "
                      f"{[key.key_id for key in trust.EMBEDDED]}")
    return found


def _git(*args):
    return subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True,
                          check=True).stdout.strip()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("ipk", type=Path)
    parser.add_argument("--commit", help="the commit being released (default: HEAD)")
    parser.add_argument("--time", type=int, help="its time (default: HEAD's)")
    args = parser.parse_args(argv)
    try:
        commit = args.commit or _git("rev-parse", "HEAD^{commit}")
        when = args.time if args.time is not None else int(
            _git("show", "-s", "--format=%ct", commit))
        found = check(args.ipk, commit, when)
    except (Refused, OSError, ValueError, subprocess.CalledProcessError) as error:
        print(f"::error::check-release-package.py: {error}", file=sys.stderr)
        return 1
    print("build id:", found["build"])
    print("origin:", found["origin"], "keys:", ", ".join(found["keys"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
