#!/usr/bin/env python3
"""Build, check and verify the signed release index - everything but the signature itself.

    tools/make-index.py build   --repo OWNER/NAME --out releases.json [--summary FILE]
    tools/make-index.py check   --index releases.json [--sha256 HEX]
    tools/make-index.py wrap    --index releases.json --raw-signature SIG --out releases.json.sig
    tools/make-index.py verify  --index releases.json --sig releases.json.sig [--sha256 HEX]
    tools/make-index.py tag-for COMMIT
    tools/make-index.py tree

The index (`feed/releases.json` on the `gh-pages` branch, with `feed/releases.json.sig`) is the only
list of versions the plugin and the companion integration will ever install from
([ADR-0015](../docs/adr/0015-signed-self-update.md); the format and the chain are in
[RELEASE-INDEX.md](../docs/RELEASE-INDEX.md)). `.github/workflows/publish-index.yml` runs it in
three jobs, and each job uses the subcommands that fit what it may do:

- **build** (read-only, no key): `tag-for` turns the triggering release's commit into its version;
  `build` downloads every release's package, checks its size and sha256 against the release asset's
  own `digest` and against the copy the opkg feed serves, reads `COMPATIBILITY` at each release tag
  and `release-index/policy.json` on `main`, verifies the published index, and writes the next one,
  unsigned, with the sha256 the maintainer reads before approving;
- **sign** (the key, no write): `check` re-validates that file itself - the sha256 build reported,
  every field, the main key's id, the next serial - before OpenSSL signs it; `wrap` turns the raw
  signature into `releases.json.sig` and refuses one that does not verify with the embedded main
  key;
- **publish** (write, no key): `verify` accepts the pair exactly as a reader that has seen the
  published index would, before it is pushed.

`tree` is `index.yml`'s check of the working tree: policy, `COMPATIBILITY`, the embedded keys'
baselines against the published serial, and any emergency index waiting to be published.

Standard library only, and the plugin's own `trust` and `ed25519` modules: the sign job installs
nothing, and the receiver runs the same verifier.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import importlib.util
import io
import json
import os
import re
import subprocess
import sys
import tarfile
import time
import urllib.error
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(REPO_ROOT / "src"))

from MQTTBridge import buildid, trust  # noqa: E402 - the plugin's own modules, from this tree

POLICY = Path("release-index") / "policy.json"
EMERGENCY = Path("release-index") / "emergency"
COMPATIBILITY = "COMPATIBILITY"
FEED_DIR = "feed"
PUBLISHED_REF = "origin/gh-pages"
API = "https://api.github.com"
BUILDINFO_MEMBER = "./usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/buildinfo.py"

# The published serial may run this far past a key's embedded baseline before `tree` fails. A
# receiver meeting a key for the first time accepts a serial within trust.MAX_JUMP of the baseline
# its build carries, so a release built with a baseline that has fallen too far behind would lock
# out every receiver installed from it; the margin leaves room for the releases already in flight.
BASELINE_MARGIN = 100

_TAG = re.compile(r"v(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_DIGEST = re.compile(r"sha256:([0-9a-f]{64})", re.ASCII)


class IndexError_(Exception):
    """The index cannot be built or accepted truthfully, so nothing is written."""


def _fail(message):
    raise IndexError_(message)


# ------------------------------------------------------------------------- inputs --


def _git(*args, root=REPO_ROOT, check=True):
    result = subprocess.run(["git", *args], cwd=root, capture_output=True, check=False)
    if check and result.returncode:
        _fail(f"git {' '.join(args)}: {result.stderr.decode('utf-8', 'replace').strip()}")
    return result


def git_file(ref, path, root=REPO_ROOT):
    """The bytes of `path` at `ref`, or None when the ref or the file is not there."""
    result = _git("show", f"{ref}:{path}", root=root, check=False)
    return result.stdout if result.returncode == 0 else None


def load_keys(path):
    """The embedded keys, or a test key set from a file - for tests and the local rehearsal only;
    the workflows never pass one, and `check-workflows.py` refuses a workflow that does."""
    if path is None:
        return trust.EMBEDDED
    try:
        return trust.keys_from_data(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError, trust.Refused) as error:
        _fail(f"{path}: not a key set: {error}")


def _strict_json(raw, what):
    try:
        return json.loads(raw, object_pairs_hook=trust._no_duplicates,
                          parse_constant=trust._no_constants)
    except ValueError as error:
        _fail(f"{what}: not JSON: {error}")


def _declarations(value, what, required):
    """`contract`, `min_integration`, `self_update` - all of them when `required`."""
    names = ("contract", "min_integration", "self_update")
    if not isinstance(value, dict):
        _fail(f"{what}: not an object")
    unknown = set(value) - set(names)
    if unknown:
        _fail(f"{what}: unknown {', '.join(sorted(unknown))}")
    if required and set(value) != set(names):
        _fail(f"{what}: needs all of {', '.join(names)}")
    if "contract" in value and (
        not trust._whole(value["contract"]) or value["contract"] < 0
    ):
        _fail(f"{what}: contract must be a whole number >= 0")
    if "min_integration" in value and value["min_integration"] is not None and not trust.is_version(
        value["min_integration"]
    ):
        _fail(f"{what}: min_integration must be null or N.N.N")
    if "self_update" in value and not isinstance(value["self_update"], bool):
        _fail(f"{what}: self_update must be true or false")
    return dict(value)


def parse_policy(raw):
    """`release-index/policy.json`: the floor, the withdrawals and the corrections, checked."""
    data = _strict_json(raw, str(POLICY))
    if not isinstance(data, dict):
        _fail(f"{POLICY}: not an object")
    expected = {"schema", "floor", "withdrawn", "corrections"}
    if set(data) != expected:
        _fail(f"{POLICY}: needs exactly {', '.join(sorted(expected))}")
    if data["schema"] != 1 or isinstance(data["schema"], bool):
        _fail(f"{POLICY}: schema must be 1")
    if not trust.is_version(data["floor"]):
        _fail(f"{POLICY}: floor {data['floor']!r} is not a plain N.N.N")
    withdrawn = data["withdrawn"]
    if not isinstance(withdrawn, dict):
        _fail(f"{POLICY}: withdrawn is not an object")
    for version, reason in withdrawn.items():
        if not trust.is_version(version):
            _fail(f"{POLICY}: withdrawn {version!r} is not a plain N.N.N")
        if not isinstance(reason, str) or not reason.strip() or "\n" in reason or len(reason) > 300:
            _fail(f"{POLICY}: withdrawn {version}: the reason is one line of 1 to 300 characters")
    corrections = data["corrections"]
    if not isinstance(corrections, dict):
        _fail(f"{POLICY}: corrections is not an object")
    for version, value in corrections.items():
        if not trust.is_version(version):
            _fail(f"{POLICY}: corrections {version!r} is not a plain N.N.N")
        _declarations(value, f"{POLICY}: corrections {version}", required=False)
    return data


def parse_compatibility(raw, what=COMPATIBILITY):
    """A `COMPATIBILITY` file: `name = value` lines, `#` comments, all three names, nothing else."""
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as error:
        _fail(f"{what}: not UTF-8: {error}")
    values = {}
    for number, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name, equals, value = (part.strip() for part in line.partition("="))
        if not equals or name in values:
            _fail(f"{what} line {number}: expected one `name = value` per name")
        if name == "contract":
            if not re.fullmatch(r"(0|[1-9][0-9]*)", value, re.ASCII):
                _fail(f"{what} line {number}: contract is a whole number")
            values[name] = int(value)
        elif name == "min_integration":
            values[name] = None if value == "none" else value
        elif name == "self_update":
            if value not in ("true", "false"):
                _fail(f"{what} line {number}: self_update is true or false")
            values[name] = value == "true"
        else:
            _fail(f"{what} line {number}: unknown name {name!r}")
    return _declarations(values, what, required=True)


def _read_ar_member(data, wanted):
    spec = importlib.util.spec_from_file_location("make_feed", REPO_ROOT / "tools" / "make-feed.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.read_ar_member(data, wanted)


def package_facts(blob, filename):
    """`(Package, Version, [depends], buildinfo text or None)` read out of an IPK in memory."""
    try:
        control_tar = _read_ar_member(blob, "control.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(control_tar), mode="r:gz") as tar:
            member = next((tar.getmember(name) for name in ("./control", "control")
                           if name in tar.getnames()), None)
            if member is None:
                _fail(f"{filename}: no control file")
            control = tar.extractfile(member).read().decode("utf-8")
        data_tar = _read_ar_member(blob, "data.tar.gz")
        with tarfile.open(fileobj=io.BytesIO(data_tar), mode="r:gz") as tar:
            names = tar.getnames()
            info = (tar.extractfile(BUILDINFO_MEMBER).read().decode("utf-8")
                    if BUILDINFO_MEMBER in names else None)
    except (KeyError, ValueError, tarfile.TarError, OSError, gzip.BadGzipFile,
            UnicodeDecodeError) as error:
        _fail(f"{filename}: not a readable package: {error}")
    fields = {}
    for line in control.splitlines():
        if line and not line[0].isspace() and ":" in line:
            name, _, value = line.partition(":")
            fields[name.strip()] = value.strip()
    depends = []
    for item in filter(None, (part.strip() for part in fields.get("Depends", "").split(","))):
        name = re.split(r"[\s(]", item, maxsplit=1)[0]
        depends.append(name)
    return fields.get("Package"), fields.get("Version"), depends, info


class GitHub:
    """The few API calls the build needs. Downloads carry no token: the assets are public, and
    GitHub redirects them to a host that must never see one."""

    def __init__(self, repo, api=API, token=None):
        self.repo, self.api, self.token = repo, api.rstrip("/"), token

    def _get(self, url, token, limit):
        request = urllib.request.Request(url, headers={"User-Agent": "make-index"})
        if token:
            request.add_unredirected_header("Authorization", f"Bearer {token}")
            request.add_header("Accept", "application/vnd.github+json")
        try:
            with urllib.request.urlopen(request, timeout=60) as response:
                body = response.read(limit + 1)
        except (urllib.error.URLError, OSError) as error:
            _fail(f"GET {url}: {error}")
        if len(body) > limit:
            _fail(f"GET {url}: more than {limit} bytes")
        return body

    def releases(self):
        found, page = [], 1
        while True:
            body = self._get(f"{self.api}/repos/{self.repo}/releases?per_page=100&page={page}",
                             self.token, 16 * 1024 * 1024)
            batch = _strict_json(body, "the releases API")
            if not isinstance(batch, list):
                _fail("the releases API did not answer a list")
            found.extend(batch)
            if len(batch) < 100:
                return found
            page += 1

    def download(self, asset):
        return self._get(asset["browser_download_url"], None, trust.MAX_PACKAGE_BYTES)


class LocalAssets:
    """The API's answer and the packages from files: tests and the local rehearsal."""

    def __init__(self, releases_json, assets_dir):
        self._releases = _strict_json(Path(releases_json).read_bytes(), str(releases_json))
        self._dir = Path(assets_dir)

    def releases(self):
        return self._releases

    def download(self, asset):
        return (self._dir / asset["name"]).read_bytes()


# ------------------------------------------------------------------------- build --


def published_pair(ref=PUBLISHED_REF, directory=None, root=REPO_ROOT):
    """`(index bytes, signature bytes)` as published, or `(None, None)` before the first run."""
    if directory is not None:
        index = Path(directory) / trust.INDEX_FILE
        sig = Path(directory) / trust.SIGNATURE_FILE
        return ((index.read_bytes() if index.exists() else None),
                (sig.read_bytes() if sig.exists() else None))
    return (git_file(ref, f"{FEED_DIR}/{trust.INDEX_FILE}", root),
            git_file(ref, f"{FEED_DIR}/{trust.SIGNATURE_FILE}", root))


def verified_published(index_raw, sig_raw, keys):
    """The published index, its key and the memory of a reader that accepted it - or all None."""
    if index_raw is None and sig_raw is None:
        return None, None, {}
    if index_raw is None or sig_raw is None:
        _fail("the published index and its signature must both be there, or neither")
    try:
        index, key = trust.authenticate(index_raw, sig_raw, keys)
    except trust.Refused as error:
        _fail(f"the published index does not verify ({error.reason}): {error.detail}")
    return index, key, {key.key_id: index["serial"]}


def next_serial(key, published, published_key):
    """The serial the next index signed with `key` carries."""
    if published is None or published_key.key_id != key.key_id:
        if published is not None and published_key.rank > key.rank:
            _fail(
                f"the published index is signed with key {published_key.key_id} (rank "
                f"{published_key.rank}); every reader that has seen it refuses key {key.key_id} "
                f"(rank {key.rank}) for good"
            )
        return key.baseline + 1
    return published["serial"] + 1


def _release_version(release):
    tag = release.get("tag_name")
    if not isinstance(tag, str) or not _TAG.fullmatch(tag):
        return None
    return tag[1:]


def build(source, policy, keys, key_id, *, root=REPO_ROOT, published=(None, None),
          feed_ref=PUBLISHED_REF, feed_dir=None, issued=None, serial=None, require=None,
          log=print):
    """The unsigned index as bytes, with the published index it follows - or IndexError_."""
    key = trust.by_id(keys, key_id)
    if key is None:
        _fail(f"key {key_id} is not one of the keys")
    old, old_key, _memory = verified_published(*published, keys)

    entries = []
    for release in source.releases():
        version = _release_version(release)
        if version is None or release.get("draft") or release.get("prerelease"):
            if version is not None:
                log(f"skipped {release.get('tag_name')}: a draft or a pre-release")
            continue
        filename = f"{trust.PACKAGE}_{version}_all.ipk"
        assets = [asset for asset in release.get("assets", []) if asset.get("name") == filename]
        if len(assets) != 1:
            _fail(f"release v{version} does not carry exactly one {filename}")
        asset = assets[0]
        blob = source.download(asset)
        size, sha256 = len(blob), hashlib.sha256(blob).hexdigest()
        if size != asset.get("size"):
            _fail(f"{filename}: downloaded {size} bytes, the release says {asset.get('size')}")
        digest = _DIGEST.fullmatch(str(asset.get("digest") or ""))
        if not digest or digest.group(1) != sha256:
            _fail(f"{filename}: sha256 {sha256} is not the release asset's digest "
                  f"{asset.get('digest')}")
        # The receiver downloads from the feed, and checks what it got against this entry: the
        # feed's copy must be the release asset, byte for byte, or every install would fail.
        if feed_dir is not None:
            candidate = Path(feed_dir) / filename
            served = candidate.read_bytes() if candidate.exists() else None
        else:
            served = git_file(feed_ref, f"{FEED_DIR}/{filename}", root)
        if served is None or hashlib.sha256(served).hexdigest() != sha256:
            _fail(f"{filename}: the opkg feed does not serve the release asset's bytes")
        package, control_version, depends, info = package_facts(blob, filename)
        if package != trust.PACKAGE or control_version != version:
            _fail(f"{filename}: the package says {package} {control_version}")

        tag = f"v{version}"
        commit = _git("rev-parse", "--verify", f"{tag}^{{commit}}", root=root)
        commit = commit.stdout.decode().strip()
        commit_time = int(_git("show", "-s", "--format=%ct", commit, root=root).stdout.decode())
        if info is not None:
            # A package that says what it is must say it is this release.
            build_id = buildid.parse(info)
            expected = {"commit": commit, "time": commit_time, "dirty": False,
                        "flavour": "release"}
            if build_id != expected:
                _fail(f"{filename}: the package's build id is {build_id}, expected {expected}")
            if buildid.carries_overrides(info):
                _fail(f"{filename}: the package carries a test origin or test keys")

        declared = {}
        compatibility = git_file(tag, COMPATIBILITY, root)
        if compatibility is not None:
            declared = parse_compatibility(compatibility, f"{COMPATIBILITY} at {tag}")
        declared.update(policy["corrections"].get(version, {}))
        missing = [name for name in ("contract", "min_integration", "self_update")
                   if name not in declared]
        if missing:
            _fail(f"{tag}: no {', '.join(missing)} - neither {COMPATIBILITY} at the tag nor a "
                  f"correction in {POLICY} declares it")
        entries.append({
            "version": version,
            "filename": filename,
            "size": size,
            "sha256": sha256,
            "commit": commit,
            "commit_time": commit_time,
            "contract": declared["contract"],
            "min_integration": declared["min_integration"],
            "depends": depends,
            "self_update": declared["self_update"],
            "withdrawn": policy["withdrawn"].get(version),
        })

    listed = {entry["version"] for entry in entries}
    for section in ("withdrawn", "corrections"):
        stray = set(policy[section]) - listed
        if stray:
            _fail(f"{POLICY}: {section} names {', '.join(sorted(stray))}, which is not released")
    if require is not None and require not in listed:
        _fail(f"release v{require} has no published package yet - the index waits for it")
    entries.sort(key=lambda entry: trust.version_key(entry["version"]), reverse=True)

    index = {
        "schema": trust.SCHEMA,
        "package": trust.PACKAGE,
        "serial": serial if serial is not None else next_serial(key, old, old_key),
        "issued": int(time.time()) if issued is None else issued,
        "key_id": key.key_id,
        "floor": policy["floor"],
        "releases": entries,
    }
    raw = render(index)
    # An explicit serial is the emergency path's, or a test's: its form is checked, and the reader's
    # judgement is left to `verify`, which the emergency workflow runs before it publishes.
    check_unsigned(raw, keys, key_id, published, expect_next=serial is None)
    return raw, old


def render(index):
    """The exact bytes that get signed: one member per line and one release per line, so a diff
    of two indexes reads line by line and 64 KiB holds well over a hundred releases; ASCII, one
    final newline. `check` refuses a file in any other form, so the signed bytes are this tool's."""
    lines = [
        f"  {json.dumps(name)}: {json.dumps(value, ensure_ascii=True)},"
        for name, value in index.items()
        if name != "releases"
    ]
    releases = ",\n".join(
        "    " + json.dumps(entry, ensure_ascii=True) for entry in index["releases"]
    )
    body = "\n".join(lines) + '\n  "releases": [' + (f"\n{releases}\n  ]" if releases else "]")
    return ("{\n" + body + "\n}\n").encode("ascii")


def check_unsigned(raw, keys, key_id, published, expect_next=True):
    """Everything `sign` re-checks before it signs, on the file it was handed."""
    try:
        index = trust.parse_index(raw, strict=True)
    except trust.Refused as error:
        _fail(f"the index is not well formed ({error.reason}): {error.detail}")
    if raw != render(index):
        _fail("the index is not in the form this tool writes")
    key = trust.by_id(keys, key_id)
    if key is None or index["key_id"] != key.key_id:
        _fail(f"the index names key {index['key_id']}, and only {key_id} signs here")
    old, old_key, memory = verified_published(*published, keys)
    if not expect_next:
        return index
    expected = next_serial(key, old, old_key)
    if index["serial"] != expected:
        _fail(f"serial {index['serial']} is not the next one, {expected}")
    if old is not None and old_key.key_id == key.key_id and old["issued"] > index["issued"]:
        _fail("the index was issued before the one it follows")
    # The same judgement a reader makes, from what the published index taught it.
    try:
        trust.judge(index, key, keys, memory)
    except trust.Refused as error:
        _fail(f"a reader that has seen the published index would refuse this one "
              f"({error.reason}): {error.detail}")
    return index


def diff(old, new):
    """Markdown lines saying what changed between the published index and this one."""
    lines = []
    if old is None:
        lines.append("Nothing is published yet: this is the first index.")
        old = {"serial": None, "key_id": None, "floor": None, "releases": []}
    lines.append(f"- serial: {old['serial']} -> {new['serial']}")
    lines.append(f"- key: {old['key_id']} -> {new['key_id']}")
    if old["floor"] != new["floor"]:
        lines.append(f"- **floor: {old['floor']} -> {new['floor']}**")
    before = {entry["version"]: entry for entry in old["releases"]}
    after = {entry["version"]: entry for entry in new["releases"]}
    for version in sorted(set(before) | set(after), key=trust.version_key, reverse=True):
        if version not in before:
            entry = after[version]
            lines.append(f"- **added {version}**: {entry['size']} bytes, sha256 "
                         f"`{entry['sha256']}`, commit `{entry['commit'][:12]}`, contract "
                         f"{entry['contract']}, withdrawn: {entry['withdrawn'] or 'no'}")
        elif version not in after:
            lines.append(f"- **removed {version}**")
        else:
            changed = [name for name in after[version] if after[version][name] !=
                       before[version].get(name)]
            for name in changed:
                lines.append(f"- **{version} {name}: {before[version].get(name)!r} -> "
                             f"{after[version][name]!r}**")
    return lines


# ------------------------------------------------------------------------ verify --


def verify(index_raw, sig_raw, keys, published):
    """Accept the signed pair as a reader that has seen the published index would, or fail."""
    _old, _old_key, memory = verified_published(*published, keys)
    try:
        return trust.accept(index_raw, sig_raw, keys, memory)
    except trust.Refused as error:
        _fail(f"the index is refused ({error.reason}): {error.detail}")


def wrap(index_raw, raw_signature, keys, key_id):
    """`releases.json.sig` for a raw 64-byte signature - only when it verifies with that key."""
    key = trust.by_id(keys, key_id)
    if key is None:
        _fail(f"key {key_id} is not one of the keys")
    if not trust.ed25519.verify(key.public, index_raw, raw_signature):
        _fail(f"the signature does not verify with key {key_id}: the secret is not that key")
    sig = trust.signature_file(key_id, raw_signature)
    trust.authenticate(index_raw, sig, keys)
    return sig


def tag_for(commit, root=REPO_ROOT):
    """The version of the one `vN.N.N` tag on `commit`: what a release run was for."""
    if not _COMMIT.fullmatch(commit or ""):
        _fail(f"{commit!r} is not a 40-digit commit id")
    tags = _git("tag", "--points-at", commit, "--list", "v*", root=root).stdout.decode().split()
    versions = [tag[1:] for tag in tags if _TAG.fullmatch(tag)]
    if len(versions) != 1:
        _fail(f"commit {commit} carries {len(versions)} release tags ({', '.join(tags) or 'none'}),"
              " not exactly one")
    return versions[0]


def tree(root, keys, published, log=print):
    """`index.yml`'s check of the tree: policy, COMPATIBILITY, baselines, emergency index."""
    parse_policy((root / POLICY).read_bytes())
    parse_compatibility((root / COMPATIBILITY).read_bytes())
    old, old_key, memory = verified_published(*published, keys)
    if old is None:
        log("nothing is published yet")
    else:
        log(f"published: serial {old['serial']}, key {old_key.key_id}, verified")
        key = trust.by_id(keys, old_key.key_id)
        if old["serial"] > key.baseline + trust.MAX_JUMP - BASELINE_MARGIN:
            _fail(f"the published serial {old['serial']} is too far above key {key.key_id}'s "
                  f"embedded baseline {key.baseline}: raise it in src/MQTTBridge/trust.py")
    emergency = root / EMERGENCY
    index_file, sig_file = emergency / trust.INDEX_FILE, emergency / trust.SIGNATURE_FILE
    if index_file.exists() or sig_file.exists():
        if not (index_file.exists() and sig_file.exists()):
            _fail(f"{EMERGENCY}: needs both {trust.INDEX_FILE} and {trust.SIGNATURE_FILE}")
        index_raw, sig_raw = index_file.read_bytes(), sig_file.read_bytes()
        if (index_raw, sig_raw) == published:
            log("emergency index: it is the published one")
            return
        try:
            index, key = trust.authenticate(index_raw, sig_raw, keys)
        except trust.Refused as error:
            _fail(f"the emergency index does not verify ({error.reason}): {error.detail}")
        try:
            trust.judge(index, key, keys, memory)
        except trust.Refused as error:
            if error.reason != "replay":
                _fail(f"the emergency index would be refused ({error.reason}): {error.detail}")
            # Signed and genuine, but the published index has moved past it: history, not a
            # threat - and emergency-index.yml would refuse to publish it again.
            log(f"emergency index: serial {index['serial']}, already superseded")
            return
        log(f"emergency index: serial {index['serial']}, key {key.key_id}, accepted against "
            "the published one - it is published when it reaches main")


# --------------------------------------------------------------------- the tool --


def _sha256_arg(value):
    if value is not None and not re.fullmatch(r"[0-9a-f]{64}", value, re.ASCII):
        _fail(f"--sha256 {value!r} is not 64 lowercase hex digits")
    return value


def _check_sha(raw, expected):
    if expected is not None and hashlib.sha256(raw).hexdigest() != expected:
        _fail(f"the file's sha256 {hashlib.sha256(raw).hexdigest()} is not {expected}, the one "
              "build reported")


def _append(path, text):
    if path:
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, default=REPO_ROOT, help="the checkout")
    parser.add_argument("--keyset", help="a test key set (tests and the rehearsal only)")
    parser.add_argument("--published-ref", default=PUBLISHED_REF,
                        help="the git ref of the gh-pages branch")
    parser.add_argument("--published-dir", help="the published files from a directory instead")
    commands = parser.add_subparsers(dest="command", required=True)

    one = commands.add_parser("build")
    one.add_argument("--repo", help="OWNER/NAME, for the releases API")
    one.add_argument("--releases-json", help="the API's answer from a file (with --assets-dir)")
    one.add_argument("--assets-dir", help="the packages from a directory")
    one.add_argument("--feed-dir", help="the feed's packages from a directory")
    one.add_argument("--policy", type=Path)
    one.add_argument("--key-id", help="the signing key's id (default: the main key)")
    one.add_argument("--serial", type=int, help="an explicit serial (the emergency path)")
    one.add_argument("--issued", type=int)
    one.add_argument("--require-version", help="the release this run is for")
    one.add_argument("--out", type=Path, required=True)
    one.add_argument("--summary", help="append the hash and the diff here (Markdown)")
    one.add_argument("--github-output", help="append sha256=<hex> here")

    two = commands.add_parser("check")
    two.add_argument("--index", type=Path, required=True)
    two.add_argument("--sha256")
    two.add_argument("--key-id")

    three = commands.add_parser("wrap")
    three.add_argument("--index", type=Path, required=True)
    three.add_argument("--raw-signature", type=Path, required=True)
    three.add_argument("--out", type=Path, required=True)

    four = commands.add_parser("verify")
    four.add_argument("--index", type=Path, required=True)
    four.add_argument("--sig", type=Path, required=True)
    four.add_argument("--sha256")

    five = commands.add_parser("tag-for")
    five.add_argument("commit")

    commands.add_parser("tree")

    args = parser.parse_args(argv)
    try:
        keys = load_keys(args.keyset)
        published = (published_pair(directory=args.published_dir) if args.published_dir
                     else published_pair(args.published_ref, root=args.root))
        if args.command == "build":
            if args.releases_json:
                source = LocalAssets(args.releases_json, args.assets_dir)
            elif args.repo:
                token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
                source = GitHub(args.repo, token=token)
            else:
                _fail("build needs --repo, or --releases-json with --assets-dir")
            policy = parse_policy((args.policy or args.root / POLICY).read_bytes())
            key_id = args.key_id or trust.main_key(keys).key_id
            raw, old = build(source, policy, keys, key_id, root=args.root, published=published,
                             feed_ref=args.published_ref, feed_dir=args.feed_dir,
                             issued=args.issued, serial=args.serial,
                             require=args.require_version)
            args.out.write_bytes(raw)
            sha256 = hashlib.sha256(raw).hexdigest()
            new = trust.parse_index(raw)
            report = "\n".join([
                "## Release index - unsigned",
                "",
                f"**sha256 of the unsigned index: `{sha256}`**",
                "",
                f"Serial {new['serial']}, key `{new['key_id']}`, floor {new['floor']}, "
                f"{len(new['releases'])} releases. The sign job signs exactly these bytes, and "
                "only after it has checked this hash again.",
                "",
                *diff(old, new),
                "",
            ])
            print(report)
            _append(args.summary, report + "\n")
            _append(args.github_output, f"sha256={sha256}\n")
        elif args.command == "check":
            raw = args.index.read_bytes()
            _check_sha(raw, _sha256_arg(args.sha256))
            key_id = args.key_id or trust.main_key(keys).key_id
            index = check_unsigned(raw, keys, key_id, published)
            print(f"unsigned index checked: serial {index['serial']}, key {index['key_id']}, "
                  f"{len(index['releases'])} releases")
        elif args.command == "wrap":
            raw = args.index.read_bytes()
            index = trust.parse_index(raw)
            sig = wrap(raw, args.raw_signature.read_bytes(), keys, index["key_id"])
            args.out.write_bytes(sig)
            print(f"signed by {index['key_id']}, verified with the embedded key")
        elif args.command == "verify":
            raw = args.index.read_bytes()
            _check_sha(raw, _sha256_arg(args.sha256))
            accepted = verify(raw, args.sig.read_bytes(), keys, published)
            print(f"accepted: serial {accepted.index['serial']}, key {accepted.key.key_id}")
        elif args.command == "tag-for":
            print(tag_for(args.commit, root=args.root))
        elif args.command == "tree":
            tree(args.root, keys, published)
    except (IndexError_, trust.Refused) as error:
        print(f"make-index.py: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
