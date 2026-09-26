"""The release index's keys, its format, and the one rule for accepting a signed index.

The plugin will only ever install a release named in a **signed release index**
([ADR-0015](../../docs/adr/0015-signed-self-update.md), decision 2; the format and the whole
publication chain are in [RELEASE-INDEX.md](../../docs/RELEASE-INDEX.md)). This module is the part
of that which every reader shares: the receiver's check and update helper import it, the tools that
build, sign and publish the index import it, and the companion integration implements the same rule
against the same vectors (`tests/vectors/release-index.json`). One rule, written once per language:
two readers that disagree about which index is valid would each install something the other
refuses.

**The keys.** Two Ed25519 public keys are embedded, each with a `key_id` - the first 16 hex digits
of sha256 over the raw 32-byte public key, so an id cannot name another key - a **rank**, and a
**baseline serial**:

- the main key, rank 1, signs every index, in CI, in a job that runs only from `main` and only
  after the maintainer approves it;
- the spare key, rank 2, is sealed offline and signs nothing unless the main key is leaked or lost.
  Once any reader accepts an index signed by it, that reader ignores the main key for good - which
  is how a stolen main key is silenced without a release, and why the spare is never exercised in
  production: using it cannot be undone.

**Accepting an index**, in this order, each refusal a reason code the vectors name:

1. both files within their size caps (`too_large`), and the signature file well formed
   (`malformed_signature`);
2. the signature file's `key_id` names an embedded key (`unknown_key`);
3. the signature verifies over the index's exact bytes (`bad_signature`) - before anything in the
   index is believed;
4. the index is well formed (`malformed_index`), and its own `key_id` is the signature file's
   (`key_mismatch`), so one key's signature cannot stand for another's index;
5. the key has not been silenced, and its rank is not below the highest rank this reader has
   accepted under its embedded keys (`rank`);
6. the serial is above the last one accepted **for that key** (`replay`) and at most 1000 above it
   (`jump`); for a key this reader has never accepted, it lies within 1000 of the key's embedded
   baseline (`first_sight`) - the serial that was published when the release was built, so a
   receiver installed late in a key's life is not locked out.

**The memory** a reader keeps is keyed by `key_id`, which names exactly one public key, so it means
the same thing under every set of embedded keys:

- the last serial accepted from each key;
- the keys it has **silenced**: accepting an index from a key silences, for good, every key of the
  reader's embedded set ranked below it. "Ignored for good" is a fact about those keys, not about a
  set, so it survives a release that changes the set - even one that dropped the higher key and
  kept a silenced one, which the release rules forbid (RELEASE-INDEX.md, "Keys").

The rank floor is the highest rank, **in the reader's own embedded set**, of a key it has accepted.

**Where it is kept** (`memory_for`, `store`): one stored state, in two parts - `release` and
`acceptance` - each mapping a key set's fingerprint to what a reader holding that set learned. A
build reads and writes only its own part, so an acceptance build can never write memory a
production build reads; and an acceptance build's test keys may not include a release key at all
(`configured`, `refuse_release_keys`). Within a part a reader merges what every entry sharing a key
with its own set knows, so a release that adds or drops a key keeps everything the reader knew about
the keys it still carries. A stored state or memory in any other shape is refused (`BadMemory`),
never read as empty.

Standard library only, and 3.9-safe: the update helper runs this outside enigma2 on whatever
Python the image has, and the signing job in CI runs it on the runner's own, with nothing
installed.
"""

import base64
import binascii
import hashlib
import json
import re
from typing import NamedTuple

from . import ed25519

# Where releases come from. A constant, not a setting: fetched with verified TLS and no redirects,
# and only an acceptance build may carry another one (`tools/make-buildinfo.py` refuses it for any
# other flavour).
ORIGIN = "https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed/"
INDEX_FILE = "releases.json"
SIGNATURE_FILE = "releases.json.sig"

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
SCHEMA = 1
ALGORITHM = "ed25519"

MAX_INDEX_BYTES = 64 * 1024
MAX_SIGNATURE_BYTES = 1024
MAX_PACKAGE_BYTES = 8 * 1024 * 1024
MAX_JUMP = 1000

# A release is a plain N.N.N, so opkg, PEP 440 and Home Assistant order every pair the same way -
# `0.4.0rc1` sorts above `0.4.0` in opkg and below it in PEP 440. No leading zeros either: `0.03.0`
# and `0.3.0` are one version to PEP 440 and two strings to everything else. ASCII digits only and
# matched whole, so neither a Unicode digit nor a trailing newline slips through.
_VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", re.ASCII)
_KEY_ID = re.compile(r"[0-9a-f]{16}", re.ASCII)
_SHA256 = re.compile(r"[0-9a-f]{64}", re.ASCII)
_COMMIT = re.compile(r"[0-9a-f]{40}", re.ASCII)
_DEPENDENCY = re.compile(r"[a-z0-9][a-z0-9+.-]*", re.ASCII)
_ORIGIN = re.compile(
    r"https://[A-Za-z0-9.-]+(:[0-9]{1,5})?(/(?!\.{1,2}/)[A-Za-z0-9._~-]+)*/", re.ASCII
)

REASONS = (
    "too_large",
    "malformed_signature",
    "unknown_key",
    "bad_signature",
    "malformed_index",
    "key_mismatch",
    "rank",
    "replay",
    "jump",
    "first_sight",
)


class Key(NamedTuple):
    key_id: str
    rank: int
    public: bytes
    baseline: int


class Refused(Exception):
    """An index, a signature file or a key set that is not accepted, with the reason code."""

    def __init__(self, reason, detail):
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class Accepted(NamedTuple):
    index: dict
    key: Key
    memory: dict


def key_id(public):
    """The id of a raw 32-byte public key: the first 16 hex digits of its sha256."""
    return hashlib.sha256(bytes(public)).hexdigest()[:16]


def _key(key_id_, rank, public_b64, baseline):
    return Key(key_id_, rank, base64.b64decode(public_b64, validate=True), baseline)


# The embedded keys. A test pins every value here, and the key_id derivation with it; the companion
# integration embeds the same set and its tests compare the two. `baseline` is the serial published
# when the release carrying this set was built - raised by the release pull request, never lowered.
MAIN = _key("5de3b24c97e88660", 1, "39Ndn8vAkeWAhYIYWvNubezKuF5F/5aY5E1uo+hSnqQ=", 0)
SPARE = _key("c72fd83e3e514a25", 2, "1F2ajhsDoTuqAGdV2QOHdRl8hV4B0kNE0xwVGrpHdfs=", 0)
EMBEDDED = (MAIN, SPARE)


def is_version(value):
    return isinstance(value, str) and _VERSION.fullmatch(value) is not None


def version_key(value):
    """A release number as a tuple that sorts the way every tool sorts it."""
    return tuple(int(part) for part in value.split("."))


def is_origin(value):
    """Whether `value` could be the origin: https, a host, a path of plain segments, a final `/`."""
    return isinstance(value, str) and _ORIGIN.fullmatch(value) is not None


def _whole(value):
    """An int that is not a bool: JSON's `true` is not a serial."""
    return isinstance(value, int) and not isinstance(value, bool)


# --------------------------------------------------------------------- key sets --


def check_keys(keys):
    """`keys` as a tuple ordered by rank, or Refused("malformed_index") saying what is wrong.

    Every key's id must be derived from its public key and the public key must be a point, ids and
    ranks unique, ranks and baselines whole numbers. Used on the embedded set at import and on an
    acceptance build's override, so a mistake in either stops the reader instead of weakening it.
    """
    keys = tuple(keys)
    if not keys:
        raise Refused("malformed_index", "a key set needs at least one key")
    for key in keys:
        if not isinstance(key, Key) or not isinstance(key.public, bytes):
            raise Refused("malformed_index", f"not a key: {key!r}")
        if len(key.public) != ed25519.PUBLIC_KEY_BYTES or ed25519.decode_point(key.public) is None:
            raise Refused("malformed_index", f"key {key.key_id}: not an Ed25519 public key")
        if key.key_id != key_id(key.public):
            raise Refused("malformed_index", f"key {key.key_id}: its id is not derived from it")
        if not _whole(key.rank) or key.rank < 1:
            raise Refused("malformed_index", f"key {key.key_id}: rank must be a whole number >= 1")
        if not _whole(key.baseline) or key.baseline < 0:
            raise Refused("malformed_index", f"key {key.key_id}: baseline must be >= 0")
    if len({key.key_id for key in keys}) != len(keys):
        raise Refused("malformed_index", "two keys share an id")
    if len({key.rank for key in keys}) != len(keys):
        raise Refused("malformed_index", "two keys share a rank")
    return tuple(sorted(keys, key=lambda key: key.rank))


def keys_from_data(items):
    """A key set from its data form - `[{"key_id", "rank", "public", "baseline"}]`, `public` in
    base64 - as an acceptance build's `buildinfo.py` or a test key set carries it."""
    try:
        keys = [
            Key(item["key_id"], item["rank"],
                base64.b64decode(item["public"], validate=True), item["baseline"])
            for item in items
        ]
    except (KeyError, TypeError, ValueError, binascii.Error) as error:
        raise Refused("malformed_index", f"not a key set: {error}") from error
    return check_keys(keys)


def keys_to_data(keys):
    return [
        {"key_id": key.key_id, "rank": key.rank,
         "public": base64.b64encode(key.public).decode("ascii"), "baseline": key.baseline}
        for key in keys
    ]


def fingerprint(keys):
    """sha256 over the set's `key_id rank public` lines, sorted by `key_id`: a name for the set."""
    lines = "".join(
        f"{key.key_id} {key.rank} {base64.b64encode(key.public).decode('ascii')}\n"
        for key in sorted(keys, key=lambda key: key.key_id)
    )
    return hashlib.sha256(lines.encode("ascii")).hexdigest()


def main_key(keys):
    """The key that signs every ordinary index: the lowest rank."""
    return min(keys, key=lambda key: key.rank)


def by_id(keys, wanted):
    for key in keys:
        if key.key_id == wanted:
            return key
    return None


class OverlappingKeys(Exception):
    """A test key set that contains a release key: one test-signed index would silence a release
    key on the receiver for good, so neither the builder nor the reader accepts such a set."""


def refuse_release_keys(keys):
    """`keys` if none of them is a release key, else OverlappingKeys - by id and by public key."""
    release_ids = {key.key_id for key in EMBEDDED}
    release_publics = {key.public for key in EMBEDDED}
    shared = sorted(key.key_id for key in keys
                    if key.key_id in release_ids or key.public in release_publics)
    if shared:
        raise OverlappingKeys(
            f"the test key set contains the release key {', '.join(shared)}: test keys never "
            "include a release key"
        )
    return keys


def configured(build=None, overrides=None):
    """`(origin, keys, acceptance)` this build uses: the embedded ones, unless it is an acceptance
    build that carries its own.

    `build` is `buildid.LOADED`, `overrides` is `buildid.LOADED_OVERRIDES`. The builder refuses an
    override for any other flavour; this refuses to honour one too, so a release build that somehow
    carried one would still judge by the release keys. An acceptance build's test keys must not
    include a release key: that raises OverlappingKeys - loudly, never by falling back to the
    release keys, because an acceptance build that quietly judged by them could no longer be told
    from the thing it tests. `acceptance` says which part of the stored state it reads and writes.
    """
    if not build or build.get("flavour") != "acceptance":
        return ORIGIN, EMBEDDED, False
    overrides = overrides or {}
    origin = overrides.get("origin") or ORIGIN
    keys = overrides.get("index_keys")
    return origin, refuse_release_keys(keys_from_data(keys)) if keys else EMBEDDED, True


# ----------------------------------------------------------------------- parsing --


def _no_duplicates(pairs):
    # Python keeps the last of two equal keys and other parsers may keep the first; an index that
    # means two things to two readers is refused by both.
    seen = {}
    for name, value in pairs:
        if name in seen:
            raise ValueError(f"duplicate key {name!r}")
        seen[name] = value
    return seen


def _no_constants(name):
    raise ValueError(f"{name} is not JSON")


def _json(raw, reason):
    try:
        return json.loads(
            raw.decode("utf-8"), object_pairs_hook=_no_duplicates, parse_constant=_no_constants
        )
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise Refused(reason, f"not JSON: {error}") from error


def parse_signature(raw):
    """`(key_id, 64 signature bytes)` from a `releases.json.sig`, or Refused."""
    if not isinstance(raw, (bytes, bytearray)):
        raise Refused("malformed_signature", "the signature file is not bytes")
    if len(raw) > MAX_SIGNATURE_BYTES:
        raise Refused("too_large", f"the signature file is over {MAX_SIGNATURE_BYTES} bytes")
    data = _json(bytes(raw), "malformed_signature")
    if not isinstance(data, dict):
        raise Refused("malformed_signature", "the signature file is not an object")
    if data.get("algorithm") != ALGORITHM:
        raise Refused("malformed_signature", f"the algorithm is not {ALGORITHM}")
    wanted = data.get("key_id")
    if not isinstance(wanted, str) or not _KEY_ID.fullmatch(wanted):
        raise Refused("malformed_signature", "the key_id is not 16 lowercase hex digits")
    encoded = data.get("signature")
    try:
        signature = base64.b64decode(encoded, validate=True) if isinstance(encoded, str) else b""
    except (binascii.Error, ValueError):
        signature = b""
    if len(signature) != ed25519.SIGNATURE_BYTES:
        raise Refused("malformed_signature", "the signature is not 64 bytes of base64")
    return wanted, signature


def signature_file(key_id_, signature):
    """The bytes of a `releases.json.sig`: one line, the fields in a fixed order."""
    body = {
        "key_id": key_id_,
        "algorithm": ALGORITHM,
        "signature": base64.b64encode(signature).decode("ascii"),
    }
    return (json.dumps(body) + "\n").encode("ascii")


_TOP = ("schema", "package", "serial", "issued", "key_id", "floor", "releases")
_RELEASE = (
    "version", "filename", "size", "sha256", "commit", "commit_time", "contract",
    "min_integration", "depends", "self_update", "withdrawn",
)


def _bad(detail):
    return Refused("malformed_index", detail)


def _check_release(entry, position):
    where = f"releases[{position}]"
    if not isinstance(entry, dict):
        raise _bad(f"{where} is not an object")
    missing = [name for name in _RELEASE if name not in entry]
    if missing:
        raise _bad(f"{where} lacks {', '.join(missing)}")
    version = entry["version"]
    if not is_version(version):
        raise _bad(f"{where}: version {version!r} is not a plain N.N.N")
    where = f"release {version}"
    if entry["filename"] != f"{PACKAGE}_{version}_all.ipk":
        raise _bad(f"{where}: filename is not the package's own")
    if not _whole(entry["size"]) or not 0 < entry["size"] <= MAX_PACKAGE_BYTES:
        raise _bad(f"{where}: size must be 1 to {MAX_PACKAGE_BYTES} bytes")
    if not isinstance(entry["sha256"], str) or not _SHA256.fullmatch(entry["sha256"]):
        raise _bad(f"{where}: sha256 is not 64 lowercase hex digits")
    if not isinstance(entry["commit"], str) or not _COMMIT.fullmatch(entry["commit"]):
        raise _bad(f"{where}: commit is not 40 lowercase hex digits")
    if not _whole(entry["commit_time"]) or entry["commit_time"] <= 0:
        raise _bad(f"{where}: commit_time must be a positive whole number")
    if not _whole(entry["contract"]) or entry["contract"] < 0:
        raise _bad(f"{where}: contract must be a whole number >= 0")
    if entry["min_integration"] is not None and not is_version(entry["min_integration"]):
        raise _bad(f"{where}: min_integration is neither null nor N.N.N")
    depends = entry["depends"]
    if not isinstance(depends, list) or not all(
        isinstance(name, str) and _DEPENDENCY.fullmatch(name) for name in depends
    ):
        raise _bad(f"{where}: depends is not a list of package names")
    if not isinstance(entry["self_update"], bool):
        raise _bad(f"{where}: self_update is not true or false")
    withdrawn = entry["withdrawn"]
    if withdrawn is not None and (not isinstance(withdrawn, str) or not withdrawn.strip()):
        raise _bad(f"{where}: withdrawn is neither null nor a reason")
    return version


def parse_index(raw, strict=False):
    """The index in `raw` - the exact bytes that were signed - checked field by field, or Refused.

    A reader tolerates members it does not know, at the top and in each release: a later index may
    add one, and a receiver that refused it could never be updated to understand it. `strict` is for
    the tools that build and sign the index, which know every member there is, so that a member
    nobody reads never enters a signed file by accident.
    """
    if not isinstance(raw, (bytes, bytearray)):
        raise _bad("the index is not bytes")
    if len(raw) > MAX_INDEX_BYTES:
        raise Refused("too_large", f"the index is over {MAX_INDEX_BYTES} bytes")
    data = _json(bytes(raw), "malformed_index")
    if not isinstance(data, dict):
        raise _bad("the index is not an object")
    missing = [name for name in _TOP if name not in data]
    if missing:
        raise _bad(f"the index lacks {', '.join(missing)}")
    if strict and set(data) - set(_TOP):
        raise _bad(f"unknown members {sorted(set(data) - set(_TOP))}")
    if not _whole(data["schema"]) or data["schema"] != SCHEMA:
        raise _bad(f"schema is not {SCHEMA}")
    if data["package"] != PACKAGE:
        raise _bad(f"package is not {PACKAGE}")
    if not _whole(data["serial"]) or data["serial"] < 1:
        raise _bad("serial must be a whole number >= 1")
    if not _whole(data["issued"]) or data["issued"] < 0:
        raise _bad("issued must be a whole number >= 0")
    if not isinstance(data["key_id"], str) or not _KEY_ID.fullmatch(data["key_id"]):
        raise _bad("key_id is not 16 lowercase hex digits")
    if not is_version(data["floor"]):
        raise _bad(f"floor {data['floor']!r} is not a plain N.N.N")
    releases = data["releases"]
    if not isinstance(releases, list):
        raise _bad("releases is not a list")
    versions = [_check_release(entry, position) for position, entry in enumerate(releases)]
    if len(set(versions)) != len(versions):
        raise _bad("a version is listed twice")
    if strict:
        for entry in releases:
            extra = set(entry) - set(_RELEASE)
            if extra:
                raise _bad(f"release {entry['version']}: unknown members {sorted(extra)}")
        if versions != sorted(versions, key=version_key, reverse=True):
            raise _bad("releases are not listed newest first")
    return data


# --------------------------------------------------------------------- accepting --


class BadMemory(Exception):
    """A stored memory or state that is not in the one shape every reader writes.

    Never read as "nothing remembered": that would put a reader back at first sight, with no key
    silenced - the one direction a damaged file must not move it. A reader that meets this refuses
    to judge any index and reports it; the recovery is to remove the file, which is exactly a
    factory reset of this memory (the build's baselines apply again), done on purpose.
    """


def empty_memory():
    return {"serials": {}, "silenced": []}


def check_memory(memory):
    """`memory` if it is `{"serials": {key_id: serial >= 1}, "silenced": [key_id]}` - or None,
    which is a reader that has accepted nothing - else BadMemory. Nothing else is guessed at."""
    if memory is None:
        return empty_memory()
    if not isinstance(memory, dict) or set(memory) != {"serials", "silenced"}:
        raise BadMemory(f"a memory is exactly serials and silenced, not {memory!r:.80}")
    serials, silenced = memory["serials"], memory["silenced"]
    if not isinstance(serials, dict) or not all(
        isinstance(key_id_, str) and _KEY_ID.fullmatch(key_id_) and _whole(serial) and serial >= 1
        for key_id_, serial in serials.items()
    ):
        raise BadMemory("serials must map key ids to whole numbers >= 1")
    if not isinstance(silenced, list) or not all(
        isinstance(key_id_, str) and _KEY_ID.fullmatch(key_id_) for key_id_ in silenced
    ):
        raise BadMemory("silenced must be a list of key ids")
    return memory


def _serials(memory):
    return check_memory(memory)["serials"]


def _silenced(memory):
    return set(check_memory(memory)["silenced"])


def rank_floor(keys, memory):
    """The highest rank, among `keys`, of a key this reader has accepted an index from (0: none)."""
    serials = _serials(memory)
    return max((key.rank for key in keys if key.key_id in serials), default=0)


def remember(memory, key, serial, keys):
    """The memory after accepting `serial` from `key`: its serial, and every key of `keys` ranked
    below it silenced for good. Returns a new memory; `memory` is not changed."""
    serials = dict(_serials(memory))
    serials[key.key_id] = serial
    silenced = _silenced(memory) | {other.key_id for other in keys if other.rank < key.rank}
    return {"serials": serials, "silenced": sorted(silenced)}


# ------------------------------------------------------------------ stored state --

STATE_SCHEMA = 1
LINEAGES = ("release", "acceptance")


def empty_state():
    return {"schema": STATE_SCHEMA, "release": {}, "acceptance": {}}


def check_state(state):
    """A reader's stored state - `{"schema": 1, "release": {...}, "acceptance": {...}}`, each
    part mapping a key-set fingerprint to `{"keys": [key_id], "serials": ..., "silenced": ...}` -
    or BadMemory. None is a reader that has never stored anything."""
    if state is None:
        return empty_state()
    if not isinstance(state, dict) or set(state) != {"schema", *LINEAGES}:
        raise BadMemory(f"a state is exactly schema, release and acceptance, not {state!r:.80}")
    if not _whole(state["schema"]) or state["schema"] != STATE_SCHEMA:
        raise BadMemory(f"state schema {state['schema']!r} is not {STATE_SCHEMA}")
    for lineage in LINEAGES:
        entries = state[lineage]
        if not isinstance(entries, dict):
            raise BadMemory(f"{lineage} is not an object")
        for fingerprint_, entry in entries.items():
            if not isinstance(fingerprint_, str) or not _SHA256.fullmatch(fingerprint_):
                raise BadMemory(f"{lineage}: {fingerprint_!r:.20} is not a key-set fingerprint")
            if not isinstance(entry, dict) or set(entry) != {"keys", "serials", "silenced"}:
                raise BadMemory(f"{lineage} {fingerprint_[:12]}: not keys, serials and silenced")
            keys = entry["keys"]
            if not isinstance(keys, list) or not keys or not all(
                isinstance(key_id_, str) and _KEY_ID.fullmatch(key_id_) for key_id_ in keys
            ):
                raise BadMemory(f"{lineage} {fingerprint_[:12]}: keys is not a list of key ids")
            check_memory({"serials": entry["serials"], "silenced": entry["silenced"]})
    return state


def memory_for(state, keys, acceptance=False):
    """The memory a reader holding `keys` judges with, from its stored `state`.

    **Scoped twice.** By lineage: an acceptance build reads and writes only the acceptance part,
    a release build only the release part, so nothing a test index teaches a receiver can ever be
    read by the production build that follows it - whatever keys the test build carried. And by
    key set: an entry is stored under the fingerprint of the set that wrote it, and a reader takes
    memory only from entries whose set shares a key with its own - the largest serial per key it
    holds, and every silenced key - so a release that adds or drops a key keeps what was known
    about the keys it still carries, and a returning older set cannot forget what a newer one
    learned, while a disjoint set of keys is never consulted at all.
    """
    state = check_state(state)
    held = {key.key_id for key in keys}
    serials, silenced = {}, set()
    for entry in state[LINEAGES[1] if acceptance else LINEAGES[0]].values():
        if not held & set(entry["keys"]):
            continue
        for key_id_, serial in entry["serials"].items():
            if key_id_ in held:
                serials[key_id_] = max(serial, serials.get(key_id_, 0))
        silenced |= set(entry["silenced"])
    return {"serials": serials, "silenced": sorted(silenced)}


def store(state, keys, memory, acceptance=False):
    """The state after a reader holding `keys` accepted an index and holds `memory`."""
    state = check_state(state)
    memory = check_memory(memory)
    lineage = LINEAGES[1] if acceptance else LINEAGES[0]
    updated = {name: dict(state[name]) for name in LINEAGES}
    updated["schema"] = STATE_SCHEMA
    updated[lineage][fingerprint(keys)] = {
        "keys": sorted(key.key_id for key in keys),
        "serials": dict(memory["serials"]),
        "silenced": list(memory["silenced"]),
    }
    return updated


def authenticate(index_raw, signature_raw, keys):
    """`(index, key)` when one of `keys` signed exactly these bytes - steps 1 to 4 - else Refused.

    Says nothing about whether this reader should take it: that is `judge`, with its memory.
    """
    if isinstance(index_raw, (bytes, bytearray)) and len(index_raw) > MAX_INDEX_BYTES:
        raise Refused("too_large", f"the index is over {MAX_INDEX_BYTES} bytes")
    signer_id, signature = parse_signature(signature_raw)
    key = by_id(keys, signer_id)
    if key is None:
        raise Refused("unknown_key", f"key {signer_id} is not one of this reader's keys")
    if not isinstance(index_raw, (bytes, bytearray)) or not ed25519.verify(
        key.public, bytes(index_raw), signature
    ):
        raise Refused("bad_signature", f"the signature does not verify with key {key.key_id}")
    index = parse_index(index_raw)
    if index["key_id"] != key.key_id:
        raise Refused(
            "key_mismatch",
            f"the index names key {index['key_id']}, its signature key {key.key_id}",
        )
    return index, key


def accept(index_raw, signature_raw, keys, memory):
    """Accept a signed index, or raise Refused - the rule in the module docstring, in its order.

    `memory` is `{"serials": {key_id: last accepted serial}, "silenced": [key_id, ...]}` (None or
    `{}` for a reader that has accepted nothing) and is not changed; the Accepted result carries the
    memory to store. A reader that already holds these exact bytes has nothing new and need not
    ask: an equal serial is refused as `replay`.
    """
    index, key = authenticate(index_raw, signature_raw, keys)
    return Accepted(index, key, judge(index, key, keys, memory))


def judge(index, key, keys, memory):
    """Steps 5 and 6 for an authentic index: the memory to store after it, or Refused."""
    if key.key_id in _silenced(memory):
        raise Refused(
            "rank", f"key {key.key_id} was silenced by an index signed with a higher-ranked key"
        )
    floor = rank_floor(keys, memory)
    if key.rank < floor:
        raise Refused(
            "rank", f"key {key.key_id} has rank {key.rank}, and rank {floor} is already accepted"
        )
    serial = index["serial"]
    stored = _serials(memory).get(key.key_id)
    if stored is None:
        if not key.baseline <= serial <= key.baseline + MAX_JUMP:
            raise Refused(
                "first_sight",
                f"serial {serial} is not within {MAX_JUMP} of key {key.key_id}'s "
                f"baseline {key.baseline}",
            )
    elif serial <= stored:
        raise Refused("replay", f"serial {serial} is not above {stored}, the last accepted")
    elif serial > stored + MAX_JUMP:
        raise Refused("jump", f"serial {serial} is more than {MAX_JUMP} above {stored}")
    return remember(memory, key, serial, keys)


# The embedded set is checked when the module loads: a typo in a key must stop every reader, not
# make one of them trust nothing - or something else.
check_keys(EMBEDDED)
