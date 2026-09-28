"""The trust file: what this receiver has accepted from the signed release index, and its writers.

`/etc/enigma2/mqttbridge-index.json` holds the trust memory in the shape `trust.store` writes,
plus one member, `held`, keeping per lineage the last accepted index, its signature file
(base64) and where it came from. Two programs write it: the update check inside enigma2
(`updatecheck.py`) and the update helper outside it (`updatehelper.py`), which fetches the
index again for the release it installs. So this module is standard library only, 3.9-safe,
and imports nothing of the plugin's but `trust` - the helper runs from copies of these files
in its transaction directory, while the package manager replaces the originals.

**One rule for every writer** (TRANSACTION.md, section 1): take the trust file's own lock -
an exclusive `flock` on `mqttbridge-index.lock` beside it, waited for at most a couple of
seconds - then read the file again, judge the candidate index against that fresh read, write
the result whole (temporary file, fsync, rename) and let go. A judgement made on an earlier
read is never written, so a second writer that accepted a newer index in between is never
rolled back. `flock`, not `lockf`: a `lockf` holder does not exclude a `flock` taker, `flock`
excludes another thread with its own descriptor as well as another process, and it dies with
its holder. Never the transaction lock, and never held across a fetch.

A memory that cannot be read is never rewritten and judges nothing (`trust.BadMemory`); a held
index is verified again whenever it is loaded, so a build whose keys no longer sign it does not
show it - and its memory stays, because that is about keys, not about the file.
"""

import base64
import binascii
import contextlib
import errno
import fcntl
import json
import logging
import os
import time

try:
    from . import trust
except ImportError:  # the helper's flat copy, run as a script
    import trust

LOG = logging.getLogger("MQTTBridge.trustfile")

MAX_STATE_BYTES = 512 * 1024
LOCK_POLL = 0.05

SOURCE_ORIGIN = "origin"
SOURCE_RELAY = "relay"

# Verdicts of `judge` and `keep`, beside the rule's own reason codes (`trust.REASONS`).
ACCEPT = "accept"
TAKEN_BACK = "taken_back"
UNCHANGED = "unchanged"
WRITE_FAILED = "write_failed"
TRUST_BUSY = "trust_busy"
ERROR_BAD_MEMORY = "bad_memory"
ERROR_BAD_KEYS = "bad_keys"


class TrustBusy(Exception):
    """Another writer holds the trust file's lock and did not let go within the wait."""


def read_state(path):
    """The stored state, or None when there is none; `trust.BadMemory` when it cannot be read."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_STATE_BYTES + 1)
    except FileNotFoundError:
        return None
    except OSError as error:
        raise trust.BadMemory(f"the file cannot be read: {error}") from error
    if len(raw) > MAX_STATE_BYTES:
        raise trust.BadMemory(f"the file is over {MAX_STATE_BYTES} bytes")
    try:
        state = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError) as error:
        raise trust.BadMemory(f"the file is not JSON: {error}") from error
    # `trust.check_state` reads None as "never stored anything". A file that says `null` is not
    # a file that is missing: read as nothing remembered, it would put this reader back at first
    # sight, which is the one thing a damaged file must never do.
    if not isinstance(state, dict):
        raise trust.BadMemory("the file does not hold an object")
    return trust.check_state(state)


def write_state(path, state):
    """Write `state` whole, 0600, renamed into place. False, logged, when it could not be.

    The old file stays as it was until the new one is complete on disk: a write that fails -
    a full flash - leaves either the old state or the new one, never half of either.
    """
    temporary = path + ".tmp"
    try:
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(state, handle, sort_keys=True, separators=(",", ":"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    except OSError:
        LOG.exception("could not write %s", path)
        try:
            os.remove(temporary)
        except OSError:
            pass
        return False
    return True


def trust_lock_path(path):
    """The trust file's own lock: `mqttbridge-index.lock` beside `mqttbridge-index.json`."""
    base, _extension = os.path.splitext(path)
    return base + ".lock"


@contextlib.contextmanager
def trust_lock(path, wait):
    """Hold the trust file's lock - a `flock` on its lock file - or raise TrustBusy after `wait`.

    The lock file is 0600 whoever made it: `os.open` sets a mode only on the file it creates,
    so one left by anything else is brought to 0600 here, once it is open.
    """
    descriptor = os.open(trust_lock_path(path), os.O_RDWR | os.O_CREAT, 0o600)
    try:
        try:
            os.fchmod(descriptor, 0o600)
        except OSError:
            LOG.warning("could not make %s 0600", trust_lock_path(path))
        deadline = time.monotonic() + wait
        while True:
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as error:
                if error.errno not in (errno.EAGAIN, errno.EACCES, errno.EWOULDBLOCK):
                    raise
                if time.monotonic() >= deadline:
                    raise TrustBusy(f"the trust file stayed locked for {wait} s") from error
                time.sleep(LOCK_POLL)
        try:
            yield
        finally:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
    finally:
        os.close(descriptor)


def held_part(state, lineage):
    held = state.get("held") if isinstance(state, dict) else None
    part = held.get(lineage) if isinstance(held, dict) else None
    return part if isinstance(part, dict) else {}


def with_held(state, lineage, part):
    updated = dict(state if state is not None else trust.empty_state())
    held = updated.get("held")
    held = dict(held) if isinstance(held, dict) else {}
    held[lineage] = part
    updated["held"] = held
    return updated


def b64(raw):
    return base64.b64encode(raw).decode("ascii")


def unb64(value):
    if not isinstance(value, str):
        return None
    try:
        return base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None


def lineage_of(acceptance):
    return trust.LINEAGES[1] if acceptance else trust.LINEAGES[0]


def authentic(part, keys):
    """`(index, source)` of a held index that `keys` still sign, else None."""
    index_raw, signature_raw = unb64(part.get("index")), unb64(part.get("sig"))
    if index_raw is None or signature_raw is None or keys is None:
        return None
    try:
        index, _key = trust.authenticate(index_raw, signature_raw, keys)
    except trust.Refused as refused:
        LOG.info("the kept release index no longer verifies (%s); not showing it",
                 refused.reason)
        return None
    source = part.get("source")
    return index, source if source in (SOURCE_ORIGIN, SOURCE_RELAY) else SOURCE_ORIGIN


def _take_back(index_raw, signature_raw, part, memory, source, keys):
    """Hold again the very index the memory says was accepted, when none is held.

    The file's index can be lost while its memory - rightly - still names the serial; the
    same genuine index then comes back and the rule calls it a replay. Only with nothing
    authentic held, and at a serial equal to the one remembered for its key. (A silenced key,
    or one below the rank floor, never gets here: the rule refuses it as `rank` before it
    looks at the serial.)
    """
    if authentic(part, keys) is not None:
        return None
    try:
        index, key = trust.authenticate(index_raw, signature_raw, keys)
    except trust.Refused:
        return None
    if memory["serials"].get(key.key_id) != index["serial"]:
        return None
    LOG.info("holding release index serial %d again: accepted before, no longer held",
             index["serial"])
    return (index, source), dict(part, index=b64(index_raw), sig=b64(signature_raw),
                                 source=source)


def judge(path, keys, acceptance, index_raw, signature_raw, source):
    """`(verdict, held or None for no change, state to write or None)` for one pair."""
    if keys is None:
        return ERROR_BAD_KEYS, None, None
    lineage = lineage_of(acceptance)
    try:
        state = read_state(path)
        memory = trust.memory_for(state, keys, acceptance=acceptance)
    except trust.BadMemory as error:
        LOG.warning("%s cannot be read, so no release index is judged until it is removed: "
                    "%s", path, error)
        return ERROR_BAD_MEMORY, None, None
    part = held_part(state, lineage)
    try:
        accepted = trust.accept(index_raw, signature_raw, keys, memory)
    except trust.Refused as refused:
        if refused.reason == "replay":
            if (unb64(part.get("index")), unb64(part.get("sig"))) == (index_raw,
                                                                      signature_raw):
                return UNCHANGED, None, state
            taken = _take_back(index_raw, signature_raw, part, memory, source, keys)
            if taken is not None:
                return TAKEN_BACK, taken[0], with_held(state, lineage, taken[1])
        LOG.warning("a release index from the %s was refused (%s): %s", source,
                    refused.reason, refused.detail)
        return refused.reason, None, state
    stored = trust.store(state, keys, accepted.memory, acceptance=acceptance)
    part = dict(part, index=b64(index_raw), sig=b64(signature_raw), source=source)
    return ACCEPT, (accepted.index, source), with_held(stored, lineage, part)


def keep(path, keys, acceptance, index_raw, signature_raw, source, wait):
    """Judge a pair and keep it when it is accepted: `(verdict, held or None)`.

    The first judgement reads the trust file without the lock; most pairs end there - a
    replay on every reconnect, a refusal. An accepted one is judged again under the trust
    file's lock, against the file as it is now, and written before the lock is let go: a
    second writer that accepted a newer index in between is never rolled back, because the
    write is always of a judgement made on a fresh read.
    """
    verdict, held, _state = judge(path, keys, acceptance, index_raw, signature_raw, source)
    if verdict not in (ACCEPT, TAKEN_BACK):
        return verdict, None
    try:
        with trust_lock(path, wait):
            verdict, held, state = judge(path, keys, acceptance, index_raw, signature_raw,
                                         source)
            if verdict not in (ACCEPT, TAKEN_BACK):
                return verdict, None
            if not write_state(path, state):
                return WRITE_FAILED, None
    except TrustBusy as error:
        LOG.warning("a release index from the %s was not kept: %s", source, error)
        return TRUST_BUSY, None
    except OSError:
        LOG.exception("could not lock %s", trust_lock_path(path))
        return WRITE_FAILED, None
    LOG.info("kept release index serial %d from the %s (%s)", held[0]["serial"], source,
             verdict)
    return verdict, held
