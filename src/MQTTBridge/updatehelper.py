"""The update helper: installs one signed release, proves it started, or puts the old one back.

[ADR-0015](../../docs/adr/0015-signed-self-update.md) decides that the plugin updates itself
from a signed release index; [TRANSACTION.md](../../docs/TRANSACTION.md) is the contract this
module keeps with the companion integration's SSH installer - the lock, the snapshot, the marker
and the restart rule. This is the half that runs **outside enigma2**: the plugin checks every
guard it can on its main loop, writes the request into a transaction directory of its own
(`/home/root/mqttbridge-backups/update-<id>/`), copies this file and the three it imports beside
it, and starts it detached with `start-stop-daemon -S -b`, so that a restart of the interface -
which is the point of the exercise - does not take the helper with it.

**Why a copy, and why standard library only.** The package manager is about to replace every
file of the plugin, and the helper has to outlive that and the restart after it. So it runs from
its own copies (`helper.py`, `trust.py`, `ed25519.py`, `trustfile.py`, `netfetch.py`), imports
them from its own directory, and needs nothing but the image's `python3`. It is 3.9-safe for the
same reason the rest of the plugin is.

**What it trusts.** Nothing the plugin or a broker client said about the release itself. The
request names a version and, from Home Assistant, a relay address; the helper reads the signed
index again - from the origin when the receiver has no relay address, else the index it already
holds - re-runs every rule of the index (withdrawn, floor, contract, integration, dependencies,
downgrade), and checks the package's size and sha256 against the signed entry, its `ar` layout,
its control file's package and version, and every path in it, before the package manager sees a
byte. An index it fetches is kept by the trust file's one rule for every writer
(`trustfile.keep`: its own `flock`, a fresh read, a whole write); one the rule accepted but that
could not be kept still decides, because it is the newest the receiver has seen. The relay
address is judged again too - its shape and its expiry - before it is fetched.

**The sequence** (TRANSACTION.md, sections 2 to 6): claim the transaction lock and start the
heartbeat - a thread of its own, so a blocking `opkg` cannot starve it; the index; the package;
the release's asset digest from GitHub, when the receiver fetched it itself; free space; a
schema-2 snapshot `self-update-<id>`; the marker, phase `installing`, so that a power loss or a
`SIGKILL` while the package manager unpacks is known at the next start; `opkg install
--force-reinstall` (with `--force-downgrade` only for a version that is lower, and only when the
television or the page started it); the installed files against the package's manifest; then the
phase `restarting`, which is the plugin's cue - its doors closed since `installing` - to ask the
image for a clean restart (rule R1). The plugin answers through the transaction directory:
`restart.json` when it has asked, `withdraw` when the question on the television was answered
"no" or timed out, or a guard of the household holds again right before the restart, and -
from the new plugin, once it has started - `started.json` with its version and build commit.

**Which process is the restart.** The request names the enigma2 that asked (`enigma2_pid`). A
restart is that process gone **and** an enigma2 running that was never seen beside it - a child
enigma2 forks keeps the name until it runs something else, so a pid seen next to the old one is
never the new interface, and the proof ignores every such pid. The one that asked gone before
the package manager ran (a crash, or a restart from the menu, during the download) ends the
transaction `interrupted` with nothing changed; gone once the package manager had started and
before `restarting`, the process now running may hold either version or a mix, so it goes to R2
and is never proved.

**How it ends**, always releasing what it holds:

- no new enigma2 within 180 s of the plugin's ask, or `withdraw`: the old files and opkg's
  records go back under the interface that is still running (a staged directory swapped in by
  one `rename`), the enigma2 pid is read again, and only a receiver that did not restart is
  reported `withdrawn_before_restart`; one that did goes to R2, because the new process may have
  read either version;
- a new enigma2: the proof, for 120 s - tier 1, `started.json` naming the target and its commit,
  for a target whose index entry says it can (`self_update`); tier 2 for 0.2.0 and 0.3.x, the new
  process holding the plugin's log file open, polled every two seconds, or the OpenWebif hook
  answering when neither log path was writable. Proved: commit. Not proved: R2;
- R2 - stop, restore, start: the playing channel and the standby state are recorded, `init 4`,
  the snapshot goes back (with the plugin's settings block, once enigma2 is seen stopped), the
  recorded channel is written as `config.tv.lastservice`, `init 3`, and R3 compares what the
  image started on with the record, zapping back once and re-entering standby where needed.

**Nothing unproven is left behind.** From the moment the package manager has run, every write
of the marker and of `status.json` is best effort - a full flash changes how the end is
recorded, never which end it is - and an error nothing expected still ends the way a planned
failure would: the files go back under the interface that asked, or by R2 once it has gone -
also when it comes at the start of R2, before anything was stopped. Once `init 4` has been
sent, `init 3` follows in a `finally`. A restore puts the code back first - the plugin tree, the
hook and its bytecode - and opkg's records and the settings block after it, so a full flash or
an I/O error on opkg's status file cannot keep the old code from coming back. A restore that
did not complete ends `failed` with a reason of its own - `restore_failed`, or
`restore_incomplete` when the code is back and only the records are not - whose sentence names
the forced reinstall that repairs both. And the `finally` after `init 4` first puts back
what R2 had not yet put back, before it starts the interface. `rolled_back` also needs an
interface seen starting on the old files: an interface that never stopped (`not_stopped`) or
never came back (`interface_not_started`) ends `failed`, because the old version is on disk but
nothing says it runs.

**Signals.** `HUP`, `INT`, `TERM` and `PIPE` only set a flag, read at the next step. Before the
package manager has run they end the transaction with nothing changed; from then until the
restart they lead to the withdraw; after the restart - the proof, and all of R2 - they are
ignored until the unit ends. **Once enigma2 has been stopped, nothing but the end of the
restore leads to starting it again**: the restore runs inside this process, so a signal cannot
cut it short, and the interface is never started over a tree that is half put back. What no
handler covers is `SIGKILL` or power - while the package manager unpacks, or between `init 4`
and `init 3`; the marker then tells the next plugin that starts, and a reboot frees the lock at
once. A tree left half installed that cannot start the plugin at all is for the companion
integration's forced reinstall over SSH (TRANSACTION.md section 4).

**Bounds.** The forward path gives up at 15 minutes and the rollback's waits add up to less than
five, so the lock is held at most 20 minutes - under the released installer's 30-minute stale
rule even if the heartbeat were never written.
"""

import errno
import fcntl
import glob
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
from urllib.parse import quote

try:
    from . import netfetch, trust, trustfile
except ImportError:  # the flat copy in the transaction directory, run as a script
    import netfetch
    import trust
    import trustfile

PACKAGE = "enigma2-plugin-extensions-mqttbridge"
PLUGIN_DIR = "usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge"
PLUGINS_TREE = "usr/lib/enigma2/python/Plugins"
WEBIF_DIR = "usr/lib/enigma2/python/Plugins/Extensions/WebInterface/WebChilds/External"
WEBIF_SHIM = WEBIF_DIR + "/MQTTBridge.py"
WEBIF_LEGACY_BYTECODE = "MQTTBridge.pyc"
SETTINGS = "etc/enigma2/settings"
SETTINGS_PREFIX = "config.plugins.mqttbridge."
PROVISION = "etc/enigma2/mqttbridge.json"
BACKUPS = "home/root/mqttbridge-backups"
LOCK_NAME = ".ha-installer.lock"
MARKER = "etc/enigma2/mqttbridge-update.json"
# The hardware acceptance drill's hook (TRANSACTION.md section 7): honoured only for a request
# from an acceptance build, used up by the transaction that honours it.
DRILL_R2 = "home/root/mqttbridge-backups/drill-r2"
LAST = "etc/enigma2/mqttbridge-update-last.json"
TRUST_FILE = "etc/enigma2/mqttbridge-index.json"
LOG_PATHS = ("home/root/mqttbridge.log", "tmp/mqttbridge.log")
OPKG = "usr/bin/opkg"
INIT = "sbin/init"
OPKG_CONF_DIR = "etc/opkg"
OPKG_DATABASES = ("var/lib/opkg", "usr/lib/opkg")
OPKG_DEFAULT_LOCKS = ("run/opkg.lock", "var/lock/opkg.lock")
LASTSERVICE_KEY = "config.tv.lastservice"
DIGEST_API = ("https://api.github.com/repos/deltasystems-pl/enigma2-mqtt-bridge/releases/tags/v")

# The files the plugin copies into the transaction directory, and nothing else runs there.
COPIED_MODULES = ("trust.py", "ed25519.py", "trustfile.py", "netfetch.py")
TRANSACTION_ID = re.compile(r"[0-9a-f]{12}")
SNAPSHOT_NAME = re.compile(r"self-update-[0-9a-f]{12}")
DIRECTORY_NAME = re.compile(r"update-[0-9a-f]{12}")
SERVICE_REF = re.compile(r"[^\x00-\x1f\x7f]{1,1024}")
# The only shape of address Home Assistant hands out for a package (TRANSACTION.md section 7):
# its host, a port if any, one fixed path and a `secrets.token_urlsafe(32)` token; nothing else.
# The host is a name or an IPv4 address, never a bracketed IPv6 literal: Home Assistant binds a
# relay address to the receiver's IPv4 address, and a download over IPv6 could not match it.
RELAY_URL = re.compile(r"https?://[A-Za-z0-9.-]{1,253}(?::([0-9]{1,5}))?"
                       r"/api/enigma2_mqtt/relay/[A-Za-z0-9_-]{43}")
KEEP = 2

STALE_LOCK_SECONDS = 30 * 60
HEARTBEAT_SECONDS = 60
FORWARD_LIMIT = 15 * 60
ROLLBACK_LIMIT = 5 * 60
PLUGIN_WAIT = 180
PROOF_WINDOW = 120
POLL = 1
PROOF_POLL = 2
STOP_WAIT = 30
START_WAIT = 120
SERVICE_WAIT = 60
EFFECT_WAIT = 10
INDEX_TIMEOUT = 10
PROBE_TIMEOUT = 5
PACKAGE_TIMEOUT = 60
DIGEST_TIMEOUT = 10
DIGEST_CAP = 512 * 1024
OPKG_LOCK_WAIT_SNAPSHOT = 20.0
OPKG_LOCK_WAIT_RESTORE = 40.0
OPKG_LOCK_POLL = 0.25
OPKG_LOCK_SETTLE = 0.1
TRUST_LOCK_WAIT = 2.0
SPACE_MARGIN = 2 * 1024 * 1024

# `update.transaction` in TOPICS.md: one enum set for both halves.
PHASES = ("downloading", "verifying", "snapshot", "installing", "restarting", "proving",
          "rolling_back", "finished")
RESULTS = ("installed", "withdrawn_before_restart", "rolled_back", "failed", "interrupted")
STARTED_BY = ("mqtt", "home_assistant", "screen", "page", "ssh")
DOWNGRADE_ORIGINS = ("screen", "page")
# What the plugin may name in `withdraw` (TRANSACTION.md section 7); anything else is the question.
WITHDRAW_REASONS = ("question", "retraction", "standby", "recording", "epg_import")

# The helper's sentences, English: they reach `last_error` through the plugin that reports the
# end, and Home Assistant says them in the household's language by their reason code.
SENTENCES = {
    "busy": "an update is already running on the receiver",
    "bad_request": "the update request could not be read",
    "unreachable": "the release index could not be fetched and none is kept on the receiver",
    "bad_index": "the plugin's signed release index was refused: {detail}",
    "unknown_version": "version {version} is not in the plugin's signed release index",
    "withdrawn": "version {version} has been withdrawn: {detail}",
    "below_floor": "version {version} is below the lowest version this plugin can install",
    "incompatible": "version {version} is not compatible with {detail}",
    "depends": "version {version} needs {detail}, which is not installed on this receiver",
    "downgrade": "a downgrade can only be started on the receiver or from Home Assistant's "
                 "options",
    "checksum": "the requested checksum does not match the signed release index",
    "relay": "the download address from Home Assistant is not valid",
    "download": "version {version} could not be downloaded: {detail}",
    "bad_package": "the downloaded package is not the signed release: {detail}",
    "no_space": "there is not enough free space on the receiver",
    "opkg_busy": "the receiver's package manager is busy",
    "snapshot_failed": "the rollback point could not be taken: {detail}",
    "opkg_failed": "the package manager could not install version {version}: {detail}",
    "manifest": "the installed files do not match version {version}: {detail}",
    "not_started": "the new plugin did not start; the previous version {previous} is back",
    "not_stopped": "the receiver's interface did not stop for the rollback: the previous "
                   "version {previous} is back on disk, but the interface still runs the code it "
                   "had; restart the receiver's interface",
    "interface_not_started": "the previous version {previous} is back, but the receiver's "
                             "interface did not start again after the rollback",
    "time_limit": "the update did not finish within its time limit",
    "interrupted": "the update was interrupted: {detail}",
    "question": "the receiver did not restart its interface (the question on the television was "
                "answered no, or nobody answered); the update was withdrawn",
    "retraction": "the downgrade was withdrawn before the restart: the broker did not confirm "
                  "that this receiver's topics were retracted",
    # The plugin asks the household's guards again right before the restart.
    "standby": "the receiver went into standby before its interface was restarted; the update "
               "was withdrawn, so as not to wake it",
    "recording": "a recording was running or due when the interface was to be restarted; the "
                 "update was withdrawn",
    "epg_import": "an EPG import started before the interface was restarted; the update was "
                  "withdrawn",
    "internal_error": "the update helper failed: {detail}",
    "drill": "an acceptance drill sent the update straight into its rollback; the previous "
             "version is back",
    "restore_failed": "the previous version could not be put back ({detail}); reinstall the "
                      "plugin with Force plugin reinstall (SSH) in Home Assistant",
    "restore_incomplete": "the previous version {previous} is back, but not all of its records "
                          "({detail}); reinstall the plugin with Force plugin reinstall (SSH) "
                          "in Home Assistant",
}


class Fail(Exception):
    """End the transaction with a reason code and its sentence."""

    def __init__(self, reason, **details):
        self.reason = reason
        text = SENTENCES.get(reason, reason)
        try:
            text = text.format(**details)
        except (KeyError, IndexError, ValueError):
            pass
        super().__init__(text)


class LockLost(Exception):
    """The transaction lock is not this transaction's any more."""


class OpkgBusy(Exception):
    """opkg's lock stayed held by somebody else for the whole of the wait."""


class PartialRestore(Exception):
    """The old code is back - tree, hook, bytecode - but opkg's records or the settings are not."""


# ------------------------------------------------------------------- the receiver --


class Receiver:
    """Everything the helper asks of the receiver, in one place, so a test can replace it.

    On a receiver `root` and `proc` are `/` and `/proc`; the tests hand a temporary tree, fake
    `init` and `opkg` programs, a fake OpenWebif and a clock they move themselves.
    """

    root = "/"
    proc = "/proc"
    webif_base = "http://127.0.0.1"

    def __init__(self, root=None, proc=None, init=None, opkg=None, webif_base=None):
        if root is not None:
            self.root = root
        if proc is not None:
            self.proc = proc
        self.init = init or self.path(INIT)
        self.opkg = opkg or self.path(OPKG)
        if webif_base is not None:
            self.webif_base = webif_base

    def path(self, relative):
        return os.path.join(self.root, relative)

    # Time. `clock` never jumps; `now` is the wall clock, which on many receivers starts in 1970.
    def clock(self):
        return time.monotonic()

    def now(self):
        return int(time.time())

    def sleep(self, seconds):
        time.sleep(seconds)

    def boot_id(self):
        try:
            with open(os.path.join(self.proc, "sys/kernel/random/boot_id"), encoding="ascii") as f:
                return f.read().strip()
        except OSError:
            return ""

    def uptime(self):
        try:
            with open(os.path.join(self.proc, "uptime"), encoding="ascii") as handle:
                return float(handle.read().split()[0])
        except (OSError, ValueError, IndexError):
            return None

    def run(self, argv, timeout):
        """Run one program, argv only - never a shell. Its exit status, or None on its timeout."""
        try:
            completed = subprocess.run(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.STDOUT, timeout=timeout, check=False)
        except subprocess.TimeoutExpired as expired:
            self.last_output = (expired.output or b"").decode("utf-8", "replace")
            return None
        except OSError as error:
            self.last_output = str(error)
            return 127
        except Exception as error:
            # Anything else - a MemoryError while the child is set up, say - is a program that
            # did not run, the same as one that could not be found. Its callers decide on the
            # status: `init 3` in R2 above all, which must never be skipped because of how
            # the start of a program failed.
            self.last_output = type(error).__name__ + ": " + str(error)
            return 127
        self.last_output = completed.stdout.decode("utf-8", "replace")
        return completed.returncode

    last_output = ""

    def enigma2_pids(self):
        """Every running process named `enigma2`, read from `/proc` - no `pidof` on the image."""
        pids = set()
        try:
            names = os.listdir(self.proc)
        except OSError:
            return pids
        for name in names:
            if not name.isdigit():
                continue
            try:
                with open(os.path.join(self.proc, name, "comm"), encoding="utf-8",
                          errors="replace") as handle:
                    if handle.read().strip() == "enigma2":
                        pids.add(int(name))
            except OSError:
                continue
        return pids

    def open_files(self, pid):
        """The paths process `pid` holds open, from `/proc/<pid>/fd`."""
        directory = os.path.join(self.proc, str(pid), "fd")
        found = set()
        try:
            names = os.listdir(directory)
        except OSError:
            return found
        for name in names:
            try:
                found.add(os.readlink(os.path.join(directory, name)))
            except OSError:
                continue
        return found

    def webif(self, path):
        """`(status, body)` of a GET to OpenWebif on the receiver itself, or None."""
        try:
            return netfetch.relay_get(self.webif_base + path, 1024 * 1024, 5)
        except (netfetch.Unreachable, ValueError):
            return None

    def fetch_origin(self, url, cap, timeout):
        return netfetch.https_get(url, cap, timeout)

    def fetch_relay(self, url, cap, timeout):
        return netfetch.relay_get(url, cap, timeout)

    def free_bytes(self, path):
        stats = os.statvfs(path)
        return stats.f_bavail * stats.f_frsize

    def pause(self, name):
        """A named point of the sequence. Nothing on a receiver; a test stops here."""


# ---------------------------------------------------------------- small file tools --


def atomic_write(path, data, mode=0o600):
    """Write `data` whole beside `path` and rename it into place, fsynced.

    `mode` None keeps the mode of the file being replaced - opkg's status file, enigma2's
    settings - and is 0644 for a new one.
    """
    directory = os.path.dirname(path)
    if mode is None:
        try:
            mode = os.stat(path).st_mode & 0o7777
        except OSError:
            mode = 0o644
    descriptor, temporary = tempfile.mkstemp(prefix=os.path.basename(path) + ".", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data if isinstance(data, bytes) else data.encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def write_json(path, value, mode=0o600):
    # ASCII: a released installer decodes the lock's owner record as ASCII.
    atomic_write(path, json.dumps(value, sort_keys=True, ensure_ascii=True) + "\n", mode)


def read_json(path):
    try:
        with open(path, "rb") as handle:
            value = json.loads(handle.read(1024 * 1024).decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def version_key(value):
    return trust.version_key(value)


def unmet(release, contract, integration, integration_mode, installed):
    """Why a receiver could not install `release`, as `(reason, detail)`, or None.

    The rule of ADR-0015 and TOPICS.md "Contract version", one implementation for the main loop
    and the helper: the same contract major - the integration's, when it has published one, and
    this plugin's own either way; a release that needs a newer integration is unmet where one is
    in use and has not said its version, and met where none is used; every dependency installed
    (not judged when opkg's database cannot be read - `installed` None).
    """
    if release["contract"] != contract:
        return "incompatible", "this plugin's contract"
    if integration is not None:
        if integration.get("contract") != release["contract"]:
            return "incompatible", "the Home Assistant integration on this broker"
    minimum = release["min_integration"]
    if minimum is not None:
        known = integration.get("integration") if integration is not None else None
        if known is not None:
            if not trust.is_version(known) or version_key(known) < version_key(minimum):
                return "incompatible", "the Home Assistant integration on this broker"
        elif integration_mode:
            return "incompatible", "the Home Assistant integration on this broker"
    if installed is not None:
        for name in release["depends"]:
            if name not in installed:
                return "depends", name
    return None


def floor_of(index, integration):
    """The lowest version anybody may install: the index's floor, raised by the integration's."""
    floor = index["floor"]
    wanted = integration.get("plugin_min") if integration is not None else None
    if isinstance(wanted, str) and trust.is_version(wanted) and version_key(wanted) > \
            version_key(floor):
        return wanted
    return floor


def relay_ok(relay, now):
    """Whether `relay` is an address Home Assistant hands out for a package, still ahead of `now`.

    The one rule, asked by the helper before it downloads (`check_relay`) and by the plugin
    before it starts the helper (`cmd/update`) or takes Home Assistant's answer to its own
    `relay_request` (`cmd/relay`): three places that must never disagree, so none of them has a
    copy of it. An object whose `url` is `RELAY_URL` exactly - `http` or `https`, a host name or
    an IPv4 address with no user part, a port from 1 to 65535 if any, the fixed path and a
    43-character token, nothing after it - and whose `expires` is a whole number of epoch
    seconds after `now`. A bool is not a number here, although Python says it is one.
    """
    if not isinstance(relay, dict):
        return False
    url, expires = relay.get("url"), relay.get("expires")
    match = RELAY_URL.fullmatch(url) if isinstance(url, str) else None
    if match is None or (match.group(1) is not None and not 0 < int(match.group(1)) < 65536):
        return False
    return isinstance(expires, int) and not isinstance(expires, bool) and expires > now


# ------------------------------------------------------------------------ opkg --


def _opkg_options(root):
    """`option name value` from the receiver's opkg configuration, the last one winning."""
    directory = os.path.join(root, OPKG_CONF_DIR)
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        names = []
    paths = [os.path.join(directory, "opkg.conf")] + [
        os.path.join(directory, name) for name in names
        if name.endswith(".conf") and name != "opkg.conf"
    ]
    options = {}
    for path in paths:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in lines:
            parts = line.strip().split()
            if len(parts) >= 3 and parts[0] in ("option", "opt"):
                options[parts[1]] = parts[2]
    return options


def _under(root, configured):
    parts = [part for part in str(configured).strip().split("/") if part]
    if not parts or ".." in parts:
        raise ValueError("opkg's configuration names an unusable path: " + str(configured))
    return os.path.join(root, *parts)


def opkg_paths(root):
    """`(status file, info directory)` as the receiver's opkg configuration says."""
    options = _opkg_options(root)
    status = options.get("status_file")
    info = options.get("info_dir")
    base = next((os.path.join(root, candidate) for candidate in OPKG_DATABASES
                 if os.path.isfile(os.path.join(root, candidate, "status"))),
                os.path.join(root, OPKG_DATABASES[0]))
    return (_under(root, status) if status else os.path.join(base, "status"),
            _under(root, info) if info else os.path.join(base, "info"))


def opkg_lock_paths(root):
    configured = _opkg_options(root).get("lock_file")
    if configured is not None:
        return (_under(root, configured),)
    return tuple(os.path.join(root, name) for name in OPKG_DEFAULT_LOCKS)


def installed_packages(root):
    """The names opkg says are installed - packages and what they provide - or None."""
    try:
        status, _info = opkg_paths(root)
        with open(status, encoding="utf-8", errors="replace") as handle:
            text = handle.read()
    except (OSError, ValueError):
        return None
    names = set()
    for stanza in text.split("\n\n"):
        fields = {}
        for line in stanza.splitlines():
            if line[:1] in (" ", "\t") or ":" not in line:
                continue
            key, value = line.split(":", 1)
            fields[key.strip()] = value.strip()
        package = fields.get("Package")
        if not package or fields.get("Status", "").split()[-1:] != ["installed"]:
            continue
        names.add(package)
        for provided in fields.get("Provides", "").split(","):
            provided = provided.split("(", 1)[0].strip()
            if provided:
                names.add(provided)
    return frozenset(names)


def _same_file(first, second):
    return (first.st_dev, first.st_ino) == (second.st_dev, second.st_ino)


def _still_named(path, descriptor):
    try:
        return _same_file(os.fstat(descriptor), os.stat(path))
    except FileNotFoundError:
        return False


class opkg_lock:
    """Hold every lock file opkg could be using, the way opkg holds it (`lockf`), or OpkgBusy.

    opkg deletes its lock file when it lets go, after unlocking; a lock granted in that gap is
    on a name about to go. So a lock counts only once the name still leads to the locked file,
    and still does after a pause longer than opkg's unlock, close and delete take. `wait` bounds
    the whole acquisition; 0 is the probe - "is opkg busy?" - which `opkg status` cannot answer,
    because it never takes the lock.
    """

    def __init__(self, receiver, wait):
        self.receiver = receiver
        self.wait = wait
        self.held = []

    def __enter__(self):
        deadline = self.receiver.clock() + self.wait
        try:
            for path in opkg_lock_paths(self.receiver.root):
                self.held.append((path, self._take(path, deadline)))
        except BaseException:
            self._release()
            raise
        return self

    def _take(self, path, deadline):
        os.makedirs(os.path.dirname(path), exist_ok=True)
        while True:
            descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o640)
            try:
                fcntl.lockf(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as error:
                os.close(descriptor)
                if error.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
            else:
                if _still_named(path, descriptor):
                    time.sleep(OPKG_LOCK_SETTLE)
                    if _still_named(path, descriptor):
                        return descriptor
                os.close(descriptor)
            if self.receiver.clock() >= deadline:
                raise OpkgBusy(path)
            self.receiver.sleep(OPKG_LOCK_POLL)

    def _release(self):
        for path, descriptor in reversed(self.held):
            try:
                if _still_named(path, descriptor):
                    os.remove(path)
            except OSError:
                pass
            finally:
                os.close(descriptor)
        self.held = []

    def __exit__(self, *_exc):
        self._release()
        return False


def opkg_busy(receiver):
    """Whether another opkg run holds its lock right now (taken and let go at once)."""
    try:
        with opkg_lock(receiver, 0.0):
            return False
    except OpkgBusy:
        return True


# ------------------------------------------------------------------- the package --


def ar_members(data):
    """`[(name, bytes)]` of a Unix `ar` archive, or ValueError."""
    if not data.startswith(b"!<arch>\n"):
        raise ValueError("not an ar archive")
    members = []
    position = 8
    while position < len(data):
        header = data[position:position + 60]
        if len(header) < 60 or header[58:60] != b"`\n":
            raise ValueError("a damaged ar header")
        name = header[:16].decode("ascii", "replace").strip()
        if name.endswith("/"):
            name = name[:-1]
        try:
            size = int(header[48:58].decode("ascii").strip())
        except ValueError:
            raise ValueError("a damaged ar member size") from None
        start = position + 60
        if size < 0 or start + size > len(data):
            raise ValueError("an ar member runs past the end")
        members.append((name, data[start:start + size]))
        position = start + size + (size % 2)
    return members


def _member_path(name):
    """A tar member's name as a relative path, or ValueError for anything unsafe."""
    parts = [part for part in name.split("/") if part not in ("", ".")]
    if name.startswith("/") or ".." in parts:
        raise ValueError("an unsafe path in the package: " + name)
    return "/".join(parts)


def _allowed(path, is_directory):
    if path == WEBIF_SHIM or path == PLUGIN_DIR or path.startswith(PLUGIN_DIR + "/"):
        return True
    if not is_directory:
        return False
    # A directory above the two places the package installs into.
    return (PLUGIN_DIR + "/").startswith(path + "/") or (WEBIF_SHIM).startswith(path + "/")


def inspect_package(data, version):
    """The package's `[(path, sha256)]` of regular files, after checking what it is.

    Exactly the three members of an ipk, in order; `debian-binary` 2.0; a control file naming
    this package and `version`; and a payload of plain directories and regular files, every one
    of them inside the plugin's directory or the OpenWebif hook - no link, no device, no path
    that climbs out.
    """
    members = ar_members(data)
    if [name for name, _body in members] != ["debian-binary", "control.tar.gz", "data.tar.gz"]:
        raise ValueError("not the three members of a package")
    if members[0][1] != b"2.0\n":
        raise ValueError("an unknown package format")
    fields = {}
    with tarfile.open(fileobj=io.BytesIO(members[1][1]), mode="r:gz") as control:
        for member in control.getmembers():
            if _member_path(member.name) == "control" and member.isfile():
                text = control.extractfile(member).read(65536).decode("utf-8", "replace")
                for line in text.splitlines():
                    if ":" in line and line[:1] not in (" ", "\t"):
                        key, value = line.split(":", 1)
                        fields[key.strip()] = value.strip()
    if fields.get("Package") != PACKAGE:
        raise ValueError("the package is not " + PACKAGE)
    if fields.get("Version") != version:
        raise ValueError("the package's version is not " + version)
    manifest = []
    with tarfile.open(fileobj=io.BytesIO(members[2][1]), mode="r:gz") as payload:
        for member in payload.getmembers():
            path = _member_path(member.name)
            if not path:
                continue
            if not (member.isdir() or member.isfile()):
                raise ValueError("the package holds something other than files: " + path)
            if not _allowed(path, member.isdir()):
                raise ValueError("the package installs outside the plugin: " + path)
            if member.isfile():
                body = payload.extractfile(member).read()
                manifest.append((path, hashlib.sha256(body).hexdigest()))
    if not any(path == PLUGIN_DIR + "/plugin.py" for path, _digest in manifest):
        raise ValueError("the package has no plugin.py")
    return manifest


# -------------------------------------------------------------------- the snapshot --


def _stanzas(text):
    return [part.strip("\n") for part in text.split("\n\n") if part.strip()]


def _package_name(stanza):
    for line in stanza.splitlines():
        if line.startswith("Package: "):
            return line[len("Package: "):].strip()
    return None


def _bytecode_files(root):
    shim = os.path.join(root, WEBIF_SHIM)
    cache = os.path.join(os.path.dirname(shim), "__pycache__")
    if os.path.islink(cache) or (os.path.exists(cache) and not os.path.isdir(cache)):
        raise ValueError("the OpenWebif bytecode directory is unsafe")
    files = sorted(glob.glob(os.path.join(cache, "MQTTBridge.*.pyc"))) if os.path.isdir(cache) \
        else []
    legacy = os.path.join(os.path.dirname(shim), WEBIF_LEGACY_BYTECODE)
    if os.path.lexists(legacy):
        files.append(legacy)
    if any(os.path.islink(path) or not os.path.isfile(path) for path in files):
        raise ValueError("OpenWebif bytecode must be regular files")
    return files


def take_snapshot(receiver, backup):
    """TRANSACTION.md 3.1, schema 2, under opkg's lock; a snapshot that does not complete goes."""
    root = receiver.root
    created = False
    try:
        with opkg_lock(receiver, OPKG_LOCK_WAIT_SNAPSHOT):
            os.makedirs(os.path.dirname(backup), mode=0o700, exist_ok=True)
            os.mkdir(backup, 0o700)
            created = True
            plugin = os.path.join(root, PLUGIN_DIR)
            status, info = opkg_paths(root)
            with open(status, encoding="utf-8") as handle:
                stanza = next((s for s in _stanzas(handle.read()) if _package_name(s) == PACKAGE),
                              None)
            if stanza is not None:
                atomic_write(os.path.join(backup, "package-status"), stanza + "\n")
            if os.path.islink(plugin):
                raise ValueError("the plugin directory may not be a symlink")
            if os.path.isdir(plugin):
                shutil.copytree(plugin, os.path.join(backup, "plugin"), symlinks=True)
            shim = os.path.join(root, WEBIF_SHIM)
            if os.path.islink(shim) or (os.path.exists(shim) and not os.path.isfile(shim)):
                raise ValueError("the OpenWebif hook must be a regular file")
            if os.path.isfile(shim):
                shutil.copy2(shim, os.path.join(backup, "webif-shim"))
            cache_files = _bytecode_files(root)
            if cache_files:
                os.mkdir(os.path.join(backup, "webif-cache"))
                for source in cache_files:
                    shutil.copy2(source, os.path.join(backup, "webif-cache",
                                                      os.path.basename(source)))
            info_files = glob.glob(os.path.join(info, PACKAGE + ".*"))
            if info_files:
                os.mkdir(os.path.join(backup, "opkg-info"))
                for source in info_files:
                    if os.path.islink(source) or not os.path.isfile(source):
                        raise ValueError("opkg metadata must be regular files")
                    shutil.copy2(source, os.path.join(backup, "opkg-info",
                                                      os.path.basename(source)))
            settings = os.path.join(root, SETTINGS)
            lines = []
            if os.path.isfile(settings):
                with open(settings, encoding="utf-8") as handle:
                    lines = [line for line in handle.read().splitlines()
                             if line.startswith(SETTINGS_PREFIX)]
            atomic_write(os.path.join(backup, "plugin-settings"),
                         "".join(line + "\n" for line in lines))
            provision = os.path.join(root, PROVISION)
            if os.path.islink(provision):
                raise ValueError("the provisioning file may not be a symlink")
            if os.path.isfile(provision):
                shutil.copy2(provision, os.path.join(backup, "provisioning"))
            metadata = {
                "schema": 2, "plugin": os.path.isdir(plugin), "package_status": stanza is not None,
                "opkg_info": bool(info_files), "provisioning": os.path.isfile(provision),
                "settings": os.path.isfile(settings), "webif_shim": os.path.isfile(shim),
                "webif_cache": bool(cache_files),
            }
            write_json(os.path.join(backup, "snapshot.json"), metadata)
            return metadata
    except BaseException:
        if created:
            shutil.rmtree(backup, ignore_errors=True)
        raise


def _replace_file(source, target):
    """A copy of `source` at `target` by one rename, or `target` removed for None."""
    if source is None:
        try:
            os.remove(target)
        except FileNotFoundError:
            pass
        return
    descriptor, temporary = tempfile.mkstemp(prefix=os.path.basename(target) + ".mb-",
                                             dir=os.path.dirname(target))
    try:
        os.close(descriptor)
        shutil.copyfile(source, temporary)
        shutil.copystat(source, temporary)
        os.replace(temporary, target)
    finally:
        try:
            os.remove(temporary)
        except OSError:
            pass


def _remove_own(path):
    if os.path.islink(path) or os.path.isfile(path):
        os.remove(path)
    elif os.path.isdir(path):
        shutil.rmtree(path)


def _replace_tree(root, source, live, token):
    """TRANSACTION.md 3.4: the old tree staged beside `Plugins/`, then two renames."""
    parent = os.path.dirname(os.path.join(root, PLUGINS_TREE))
    staged = os.path.join(parent, ".mqttbridge-staging-" + token)
    aside = os.path.join(parent, ".mqttbridge-aside-" + token)
    _remove_own(staged)
    _remove_own(aside)
    if source is not None:
        os.makedirs(os.path.dirname(live), exist_ok=True)
        shutil.copytree(source, staged, symlinks=True)
    try:
        if os.path.lexists(live):
            os.rename(live, aside)
        if source is not None:
            try:
                os.rename(staged, live)
            except BaseException:
                if os.path.exists(aside) and not os.path.exists(live):
                    os.rename(aside, live)
                raise
    finally:
        if source is None or os.path.exists(live):
            shutil.rmtree(aside, ignore_errors=True)
        shutil.rmtree(staged, ignore_errors=True)


def restore_snapshot(receiver, backup, settings):
    """Put the snapshot back: the plugin tree, the hook - then opkg's records and the settings.

    The settings block only when asked, which R2 does once enigma2 is seen stopped: a block
    written while enigma2 runs is overwritten from memory by its next clean quit. Every file
    goes back by a rename and the plugin tree by two, so an interface that restarts in the
    middle finds the whole old tree or the whole new one.

    **The code first.** What runs at the next start is the tree, the hook and its bytecode, so
    they go back before anything else; opkg's status file and info files and the settings block
    follow, each tried whatever became of the other. A full flash or an I/O error on opkg's
    status file then leaves the old code in place with records that still name the new version
    - `PartialRestore`, which the caller reports as such - instead of the new code with nothing
    put back. An error in the code part raises as it comes, and opkg's records are then left
    untouched, still agreeing with the files on disk. (The companion integration's installer
    writes opkg's records first; both orders restore the same snapshot, and restoring it twice
    is safe either way - TRANSACTION.md 3.3.)
    """
    root = receiver.root
    metadata = read_json(os.path.join(backup, "snapshot.json"))
    flags = ("plugin", "package_status", "opkg_info", "provisioning", "settings", "webif_shim",
             "webif_cache")
    if (metadata is None or set(metadata) != set(flags) | {"schema"} or metadata["schema"] != 2
            or any(not isinstance(metadata[flag], bool) for flag in flags)):
        raise ValueError("invalid snapshot metadata")
    token = os.path.basename(backup).rsplit("-", 1)[1] if SNAPSHOT_NAME.fullmatch(
        os.path.basename(backup)) else secrets.token_hex(6)
    status, info = opkg_paths(root)
    plugin = os.path.join(root, PLUGIN_DIR)
    shim = os.path.join(root, WEBIF_SHIM)
    live_cache = _bytecode_files(root)
    info_sources = sorted(glob.glob(os.path.join(backup, "opkg-info", "*"))) \
        if metadata["opkg_info"] else []
    cache_sources = sorted(glob.glob(os.path.join(backup, "webif-cache", "*"))) \
        if metadata["webif_cache"] else []
    if metadata["plugin"]:
        staging_parent = os.path.dirname(os.path.join(root, PLUGINS_TREE))
        beside = os.path.dirname(plugin)
        while not os.path.exists(beside) and beside != os.path.dirname(beside):
            beside = os.path.dirname(beside)
        if os.stat(staging_parent).st_dev != os.stat(beside).st_dev:
            raise ValueError("the plugin directory cannot be swapped in by a rename")
    with opkg_lock(receiver, OPKG_LOCK_WAIT_RESTORE):
        _replace_tree(root, os.path.join(backup, "plugin") if metadata["plugin"] else None,
                      plugin, token)
        if metadata["webif_shim"] or metadata["webif_cache"]:
            os.makedirs(os.path.dirname(shim), exist_ok=True)
        _replace_file(os.path.join(backup, "webif-shim") if metadata["webif_shim"] else None,
                      shim)
        cache_dir = os.path.join(os.path.dirname(shim), "__pycache__")
        placed = {}
        for source in cache_sources:
            name = os.path.basename(source)
            target = os.path.join(os.path.dirname(shim) if name == WEBIF_LEGACY_BYTECODE
                                  else cache_dir, name)
            placed[target] = source
        for cache_file in live_cache:
            if cache_file not in placed:
                os.remove(cache_file)
        for target, source in placed.items():
            os.makedirs(os.path.dirname(target), exist_ok=True)
            _replace_file(source, target)
        missing = []
        try:
            _restore_records(backup, metadata, status, info, info_sources)
        except Exception as error:  # the code is back; what is not is named, never hidden
            missing.append("opkg's records: " + (str(error) or type(error).__name__))
        if settings:
            try:
                _restore_settings(root, backup, metadata)
            except Exception as error:
                missing.append("the settings block: " + (str(error) or type(error).__name__))
        if missing:
            raise PartialRestore("; ".join(missing))


def _restore_records(backup, metadata, status, info, info_sources):
    """opkg's status stanza and info files of the package, as the snapshot has them."""
    with open(status, encoding="utf-8") as handle:
        current = [s for s in _stanzas(handle.read()) if _package_name(s) != PACKAGE]
    if metadata["package_status"]:
        with open(os.path.join(backup, "package-status"), encoding="utf-8") as handle:
            current.append(handle.read().strip())
    atomic_write(status, "\n\n".join(current) + "\n", mode=None)
    wanted = {os.path.basename(source) for source in info_sources}
    for candidate in glob.glob(os.path.join(info, PACKAGE + ".*")):
        if os.path.basename(candidate) not in wanted:
            _remove_own(candidate)
    for source in info_sources:
        _replace_file(source, os.path.join(info, os.path.basename(source)))


def _restore_settings(root, backup, metadata):
    """The plugin's settings block back, every other line of the settings file kept."""
    path = os.path.join(root, SETTINGS)
    unrelated = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            unrelated = [line for line in handle.read().splitlines()
                         if not line.startswith(SETTINGS_PREFIX)]
    with open(os.path.join(backup, "plugin-settings"), encoding="utf-8") as handle:
        block = handle.read()
    if metadata["settings"] or unrelated:
        atomic_write(path, "".join(line + "\n" for line in unrelated) + block, mode=None)


def write_lastservice(root, reference):
    """Make `reference` the channel enigma2 tunes at its next start; only while it is stopped."""
    if not SERVICE_REF.fullmatch(reference):
        raise ValueError("not a service reference that fits on one settings line")
    path = os.path.join(root, SETTINGS)
    lines = []
    if os.path.isfile(path):
        with open(path, encoding="utf-8") as handle:
            lines = [line for line in handle.read().splitlines()
                     if not line.startswith(LASTSERVICE_KEY + "=")]
    lines.append(LASTSERVICE_KEY + "=" + reference)
    atomic_write(path, "".join(line + "\n" for line in lines), mode=None)


def prune(directory, pattern, keep_name):
    """Keep `keep_name` and the newest other match of `pattern`; remove the rest, by exact name."""
    try:
        names = [name for name in os.listdir(directory) if pattern.fullmatch(name)]
    except OSError:
        return
    others = []
    for name in names:
        if name == keep_name:
            continue
        path = os.path.join(directory, name)
        if os.path.islink(path) or not os.path.isdir(path):
            continue
        try:
            others.append((os.stat(path).st_mtime, name))
        except OSError:
            continue
    others.sort(reverse=True)
    for _mtime, name in others[KEEP - 1:]:
        shutil.rmtree(os.path.join(directory, name), ignore_errors=True)


# --------------------------------------------------------------------- the lock --


def lock_is_stale(receiver, lock_dir):
    """TRANSACTION.md 2.3, unchanged from the released installer: why it may be reclaimed, or ''."""
    try:
        with open(os.path.join(lock_dir, "owner.json"), encoding="ascii") as handle:
            recorded = json.loads(handle.read())
    except (OSError, ValueError):
        try:
            age = time.time() - os.stat(lock_dir).st_mtime
        except OSError:
            return ""
        return "its owner record is unreadable and old" if age > STALE_LOCK_SECONDS else ""
    if not isinstance(recorded, dict):
        return "its owner record is not an object"
    current = receiver.boot_id()
    boot = recorded.get("boot_id")
    if current and isinstance(boot, str) and boot and boot != current:
        return "it was claimed before the receiver last rebooted"
    now = receiver.uptime()
    then = recorded.get("uptime")
    if (current and boot == current and now is not None and isinstance(then, (int, float))
            and not isinstance(then, bool)):
        return "it has been held too long" if now - then > STALE_LOCK_SECONDS else ""
    started = recorded.get("started")
    if not isinstance(started, int) or isinstance(started, bool):
        return "its start time is missing or malformed"
    return "it has been held too long" if int(time.time()) - started > STALE_LOCK_SECONDS else ""


# --------------------------------------------------------------- the transaction --


class Transaction:
    """One run of the helper, from the claim to the end, in the directory `directory`."""

    def __init__(self, directory, receiver=None):
        self.directory = directory
        self.receiver = receiver or Receiver()
        self.signalled = None
        self.lost = threading.Event()
        self.stop_beat = threading.Event()
        self.beat_interval = HEARTBEAT_SECONDS
        self.beater = None
        self.holding = False
        self.claimed = False
        self.files_changed = False
        self.snapshot = None
        self.status = {}
        self.record = {}
        self.request = None
        self.entry = None
        self.old_pid = None
        self.deadline = None
        self.before = None
        self.logs_writable = True
        self.restarted_at = None
        # Every enigma2 pid seen while the process that asked was still running: the old
        # interface and anything it forked. None of them is ever the restarted interface.
        self.restart_pids = set()
        self.lower = False
        self.ended = False
        self.committed = False
        self.rolling_back = False

    # ---------------------------------------------------------------- the files --

    def path(self, name):
        return os.path.join(self.directory, name)

    @property
    def backups(self):
        return self.receiver.path(BACKUPS)

    @property
    def lock_dir(self):
        return os.path.join(self.backups, LOCK_NAME)

    def load_request(self):
        """The plugin's request, checked member by member; Fail("bad_request") otherwise."""
        request = read_json(self.path("request.json"))
        name = os.path.basename(os.path.normpath(self.directory))
        try:
            ok = (request is not None
                  and TRANSACTION_ID.fullmatch(str(request.get("id")))
                  and name == "update-" + request["id"]
                  and trust.is_version(request.get("target"))
                  and request.get("started_by") in STARTED_BY
                  and isinstance(request.get("downgrade"), bool)
                  and isinstance(request.get("enigma2_pid"), int)
                  and isinstance(request.get("from"), dict)
                  and trust.is_version(request["from"].get("version"))
                  and isinstance(request.get("keys"), list)
                  and isinstance(request.get("acceptance"), bool)
                  and trust.is_origin(request.get("origin"))
                  and request.get("contract") in (0, 1, 2, 3)
                  and isinstance(request.get("integration_mode"), bool))
            sha = request.get("sha256") if ok else None
            if ok and sha is not None and not re.fullmatch(r"[0-9a-f]{64}", str(sha)):
                ok = False
            relay = request.get("relay") if ok else None
            if ok and relay is not None and not (isinstance(relay, dict)
                                                 and isinstance(relay.get("url"), str)):
                ok = False
            integration = request.get("integration") if ok else None
            if ok and integration is not None and not isinstance(integration, dict):
                ok = False
            if ok:
                self.keys = trust.keys_from_data(request["keys"])
        except (KeyError, TypeError, trust.Refused):
            ok = False
        if not ok:
            raise Fail("bad_request")
        self.request = request
        self.id = request["id"]
        self.old_pid = request["enigma2_pid"]

    def write_status(self, **changes):
        self.status.update(changes)
        self.status["heartbeat"] = self.receiver.now()
        try:
            write_json(self.path("status.json"), self.status)
        except OSError:
            pass

    def phase(self, name):
        self.write_status(phase=name)
        self.receiver.pause(name)
        self.check()

    def check(self):
        """Between steps: the lock still ours, no signal, inside the forward bound - and, until
        the package manager runs, the interface that asked still the one running."""
        if self.lost.is_set():
            raise LockLost()
        if self.signalled is not None:
            raise Fail("interrupted", detail="signal " + str(self.signalled))
        if self.deadline is not None and self.receiver.clock() > self.deadline:
            raise Fail("time_limit")
        if not self.files_changed and not self.original_running():
            # A crash and respawn, or a restart from the menu: the process that asked is gone,
            # and the one running now loaded the old code and knows nothing of this request.
            raise Fail("interrupted", detail="the receiver's interface restarted before the "
                                             "update was installed")

    def original_running(self):
        """Whether the enigma2 that asked for the update (its pid in the request) still runs."""
        return self.old_pid in self.receiver.enigma2_pids()

    def on_signal(self, number, _frame=None):
        self.signalled = number

    def install_signals(self):
        for name in ("SIGHUP", "SIGINT", "SIGTERM", "SIGPIPE"):
            number = getattr(signal, name, None)
            if number is not None:
                signal.signal(number, self.on_signal)

    # ----------------------------------------------------------------- the lock --

    def owner_record(self):
        return {"pid": os.getpid(), "started": self.receiver.now(),
                "boot_id": self.receiver.boot_id(), "uptime": self.receiver.uptime(),
                "origin": self.request["started_by"], "id": self.id,
                "target": self.request["target"]}

    def claim(self):
        """TRANSACTION.md 2.1: mkdir, or reclaim a stale lock by renaming it; else busy."""
        os.makedirs(self.backups, mode=0o700, exist_ok=True)
        try:
            os.mkdir(self.lock_dir, 0o700)
        except FileExistsError:
            self.reclaim()
        try:
            write_json(os.path.join(self.lock_dir, "owner.json"), self.owner_record())
        except OSError:
            # A lock with no owner record would read as held for thirty minutes.
            try:
                os.rmdir(self.lock_dir)
            except OSError:
                pass
            raise
        self.holding = True
        self.claimed = True

    def reclaim(self):
        """A lock is there: take it over only when it is stale, judged twice; else busy."""
        if not lock_is_stale(self.receiver, self.lock_dir):
            raise Fail("busy")
        retired = os.path.join(self.backups, "." + LOCK_NAME + "-stale-" + str(os.getpid())
                               + "-" + secrets.token_hex(4))
        try:
            os.rename(self.lock_dir, retired)
        except OSError:
            raise Fail("busy") from None
        if not lock_is_stale(self.receiver, retired):
            try:
                os.rename(retired, self.lock_dir)
            except OSError:
                shutil.rmtree(retired, ignore_errors=True)
            raise Fail("busy")
        shutil.rmtree(retired, ignore_errors=True)
        try:
            os.mkdir(self.lock_dir, 0o700)
        except FileExistsError:
            # Another claimer took the free name in the moment between: it holds the lock now.
            raise Fail("busy") from None

    def owned_by_another(self):
        """Whether the lock's owner record names a transaction other than this one.

        Only then may a helper that did not get the lock write its `busy` into its directory:
        a record naming this id belongs to the helper already running this transaction, whose
        `status.json` this directory is; one that cannot be read may be that helper between
        its `mkdir` and its first write.
        """
        recorded = read_json(os.path.join(self.lock_dir, "owner.json"))
        return recorded is not None and recorded.get("id") != self.id

    def beat(self):
        """One heartbeat: rewrite the owner record - only while it is still this transaction's."""
        recorded = read_json(os.path.join(self.lock_dir, "owner.json"))
        if recorded is None or recorded.get("id") != self.id:
            self.lost.set()
            return False
        write_json(os.path.join(self.lock_dir, "owner.json"), self.owner_record())
        return True

    def start_heartbeat(self):
        def loop():
            while not self.stop_beat.wait(self.beat_interval):
                try:
                    if not self.beat():
                        return
                except OSError:
                    continue

        self.beater = threading.Thread(target=loop, name="mqttbridge-heartbeat", daemon=True)
        self.beater.start()

    def release(self):
        """Stop the heartbeat and let the lock go - only a lock this transaction still holds."""
        self.stop_beat.set()
        if self.beater is not None and self.beater is not threading.current_thread():
            self.beater.join(5)
        if not self.holding:
            return
        self.holding = False
        recorded = read_json(os.path.join(self.lock_dir, "owner.json"))
        if recorded is None or recorded.get("id") != self.id:
            return
        try:
            os.remove(os.path.join(self.lock_dir, "owner.json"))
            os.rmdir(self.lock_dir)
        except OSError:
            pass

    # ---------------------------------------------------------------- the index --

    def lineage(self):
        return trustfile.lineage_of(self.request["acceptance"])

    def held_index(self):
        try:
            state = trustfile.read_state(self.receiver.path(TRUST_FILE))
        except trust.BadMemory:
            return None
        found = trustfile.authentic(trustfile.held_part(state, self.lineage()), self.keys)
        return found[0] if found else None

    def fetch_index(self):
        """Ask the origin for the pair; keep it by the trust file's rule. The index, or None."""
        origin = self.request["origin"]
        try:
            status, signature = self.receiver.fetch_origin(
                origin + trust.SIGNATURE_FILE, trust.MAX_SIGNATURE_BYTES, PROBE_TIMEOUT)
            if status != 200:
                return None
            status, index_raw = self.receiver.fetch_origin(
                origin + trust.INDEX_FILE, trust.MAX_INDEX_BYTES, INDEX_TIMEOUT)
            if status != 200:
                return None
        except netfetch.Unreachable:
            # Said in the record for the plugin: the origin's word for the next install.
            self.record["origin"] = "unreachable"
            return None
        except ValueError:
            return None
        self.record["index_fetched"] = True
        verdict, held = trustfile.keep(self.receiver.path(TRUST_FILE), self.keys,
                                       self.request["acceptance"], index_raw, signature,
                                       trustfile.SOURCE_ORIGIN, TRUST_LOCK_WAIT)
        self.record["index_verdict"] = verdict
        if held:
            self.record["index_kept"] = True
            return held[0]
        if verdict in (trustfile.WRITE_FAILED, trustfile.TRUST_BUSY):
            # The rule accepted this pair - signature, key and serial against the trust file as
            # read - and only keeping it failed. It is the newest index the receiver has seen,
            # so it decides: the one kept earlier may still offer a version this one withdrew.
            self.record["index_kept"] = False
            try:
                return trust.authenticate(index_raw, signature, self.keys)[0]
            except trust.Refused as refused:
                raise Fail("bad_index", detail=refused.reason) from None
        return None

    def choose(self):
        """The signed entry of the target, after every rule of the index; else Fail."""
        request = self.request
        index = None
        if request.get("relay") is None:
            index = self.fetch_index()
        if index is None:
            index = self.held_index()
        if index is None:
            raise Fail("unreachable")
        target = request["target"]
        entry = next((e for e in index["releases"] if e["version"] == target), None)
        if entry is None:
            raise Fail("unknown_version", version=target)
        if entry["withdrawn"] is not None:
            raise Fail("withdrawn", version=target, detail=entry["withdrawn"])
        if version_key(target) < version_key(floor_of(index, request.get("integration"))):
            raise Fail("below_floor", version=target)
        problem = unmet(entry, request["contract"], request.get("integration"),
                        request["integration_mode"], installed_packages(self.receiver.root))
        if problem is not None:
            reason, detail = problem
            raise Fail(reason, version=target, detail=detail)
        self.lower = version_key(target) < version_key(request["from"]["version"])
        if self.lower and not (request["downgrade"]
                               and request["started_by"] in DOWNGRADE_ORIGINS):
            raise Fail("downgrade")
        if request.get("sha256") is not None and request["sha256"] != entry["sha256"]:
            raise Fail("checksum")
        return entry

    # -------------------------------------------------------------- the package --

    def check_relay(self):
        """The relay address as the plugin checked it, judged again: Fail("relay") otherwise.

        The request file is the boundary between two programs, so the helper asks what the
        plugin asked: Home Assistant's relay shape - `http` or `https`, a host name or IPv4
        address with no user part, a port if any, the fixed path and a 43-character token, no
        query - and an `expires` that has not passed by this receiver's clock. IPv4 only, like
        the integration: it binds each relay address to the receiver's IPv4 address, offers one
        only to a receiver that reported such an address, and names its own IPv4 address on the
        receiver's subnet, else its internal URL; an internal URL written as an IPv6 literal is
        refused here as `relay`, before a download the binding would refuse anyway. The host
        itself is whatever the message named: a broker client can choose it, and the bytes it
        serves are verified against the signed entry before `opkg` sees them. A receiver whose clock
        still stands in 1970 cannot tell an old address from a new one; Home Assistant's own
        expiry of the token then bounds it.
        """
        relay = self.request.get("relay")
        if relay is not None and not relay_ok(relay, self.receiver.now()):
            raise Fail("relay")

    def download(self, entry):
        relay = self.request.get("relay")
        version = entry["version"]
        try:
            if relay is not None:
                status, body = self.receiver.fetch_relay(relay["url"], trust.MAX_PACKAGE_BYTES,
                                                         PACKAGE_TIMEOUT)
            else:
                try:
                    status, body = self.receiver.fetch_origin(
                        self.request["origin"] + entry["filename"], trust.MAX_PACKAGE_BYTES,
                        PACKAGE_TIMEOUT)
                except netfetch.Unreachable:
                    # No answer at all - not a refused or a wrong one: the plugin keeps it as
                    # the origin's word, so the next install at the television asks Home
                    # Assistant for the package instead (`selfupdate.py`).
                    self.record["origin"] = "unreachable"
                    raise
                self.record["origin"] = "reachable"
        except (netfetch.Unreachable, ValueError) as error:
            raise Fail("download", version=version, detail=str(error)) from None
        if status != 200:
            raise Fail("download", version=version, detail="the answer was " + str(status))
        if len(body) != entry["size"] or hashlib.sha256(body).hexdigest() != entry["sha256"]:
            raise Fail("bad_package", detail="its size or checksum is not the signed one")
        return body

    def cross_check(self, entry):
        """OD 6: the release asset's GitHub digest, once, when the receiver fetched it itself."""
        if self.request.get("relay") is not None or self.request["acceptance"]:
            self.record["digest"] = "not_checked"
            return
        try:
            status, body = self.receiver.fetch_origin(DIGEST_API + entry["version"], DIGEST_CAP,
                                                      DIGEST_TIMEOUT)
            release = json.loads(body.decode("utf-8")) if status == 200 else None
        except (netfetch.Unreachable, ValueError, UnicodeDecodeError):
            release = None
        assets = release.get("assets") if isinstance(release, dict) else None
        asset = next((a for a in assets or () if isinstance(a, dict)
                      and a.get("name") == entry["filename"]), None)
        digest = asset.get("digest") if asset else None
        if not isinstance(digest, str) or not digest.startswith("sha256:"):
            self.record["digest"] = "unavailable"
            return
        if digest[len("sha256:"):].lower() != entry["sha256"]:
            raise Fail("bad_package", detail="the release page's digest differs")
        self.record["digest"] = "matched"

    def estimate_snapshot(self):
        total = 0
        for base, _dirs, files in os.walk(self.receiver.path(PLUGIN_DIR)):
            for name in files:
                try:
                    total += os.lstat(os.path.join(base, name)).st_size
                except OSError:
                    pass
        return total + 256 * 1024

    def enough_space(self, entry):
        needed = 2 * entry["size"] + self.estimate_snapshot() + SPACE_MARGIN
        try:
            free = self.receiver.free_bytes(self.backups)
        except OSError:
            return
        if free < needed:
            raise Fail("no_space")

    def verify_manifest(self, manifest):
        for relative, digest in manifest:
            target = self.receiver.path(relative)
            if os.path.islink(target) or not os.path.isfile(target):
                raise Fail("manifest", version=self.entry["version"], detail=relative + " missing")
            if sha256_file(target) != digest:
                raise Fail("manifest", version=self.entry["version"], detail=relative + " differs")

    # --------------------------------------------------------------- the marker --

    def marker(self, **changes):
        uptime = self.receiver.uptime()
        value = {
            "id": self.id,
            "from": self.request["from"],
            "to": {"version": self.entry["version"], "commit": self.entry["commit"]},
            "boot_id": self.receiver.boot_id(),
            "deadline": None if uptime is None else uptime + FORWARD_LIMIT,
            "phase": self.status.get("phase"),
            "started_by": self.request["started_by"],
            "started": self.status.get("started"),
        }
        value.update(changes)
        try:
            write_json(self.receiver.path(MARKER), value)
        except OSError as error:
            # Once the package manager has run, a marker that cannot be written (a full flash)
            # changes nothing about how this transaction ends; it is only noted. The one that
            # must exist - before the package manager runs - is required by the caller.
            self.record["marker"] = "failed at " + str(value.get("phase")) + ": " + str(error)
            return False
        return True

    # ------------------------------------------------------------------ run it --

    def run(self):
        """The whole transaction. Its result code; raises only what is not an `Exception`."""
        try:
            self.load_request()
        except Fail as failure:
            self.write_status(phase="finished", result="failed", reason=failure.reason,
                              error=str(failure))
            return 2
        self.status = {"id": self.id, "started_by": self.request["started_by"],
                       "target": self.request["target"],
                       "from": self.request["from"]["version"], "phase": "downloading",
                       "started": self.receiver.now(), "finished": None, "result": None,
                       "reason": None, "error": None}
        self.deadline = self.receiver.clock() + FORWARD_LIMIT
        try:
            self.claim()
        except Fail as failure:
            if failure.reason == "busy" and not self.owned_by_another():
                # The lock is this transaction's own - a second copy of its helper - or its
                # owner cannot be read yet: this directory's `status.json` belongs to the
                # helper that holds it, and a `finished` written into it would open the
                # plugin's doors in the middle of an install. So nothing is written.
                return 1
            self.finish("failed", failure)
            return 1
        except OSError as error:
            self.finish("failed", Fail("internal_error", detail=str(error)))
            return 1
        self.start_heartbeat()
        try:
            return self.forward()
        except LockLost:
            if not self.ended:
                self.finish("interrupted", Fail("interrupted", detail="the lock was taken"))
            return 1
        except BaseException as error:  # the last net: never leave unproven code, or the lock
            failure = Fail("internal_error", detail=type(error).__name__)
            self.record["internal_error"] = (type(error).__name__ + ": " + str(error))[:200]
            try:
                self.recover(failure)
            except BaseException as again:  # the end below must still be written
                self.record["recovery"] = "failed: " + type(again).__name__
            if not self.ended:
                self.finish("failed", failure)
            if not isinstance(error, Exception):
                raise
            return 1
        finally:
            self.release()

    def recover(self, failure):
        """After an error nothing expected: leave the receiver as a planned end would.

        Committed, the new version proved itself and stays. Inside R2, its own `finally` has
        started the interface again, and a second R2 would only stop it again. Otherwise, once
        the package manager has run, the files go back - under the interface that asked, or by
        R2 when it restarted (`undo`); before that, only the unused rollback point goes.
        """
        if self.committed or self.rolling_back or self.ended:
            return
        if self.files_changed:
            self.undo(failure, "failed")
        elif self.snapshot is not None:
            shutil.rmtree(self.snapshot, ignore_errors=True)
            self.snapshot = None

    def forward(self):
        request = self.request
        try:
            self.phase("downloading")
            self.check_relay()
            self.entry = entry = self.choose()
            self.check()
            package = self.download(entry)
            self.phase("verifying")
            try:
                manifest = inspect_package(package, entry["version"])
            except (ValueError, tarfile.TarError, OSError, EOFError) as error:
                raise Fail("bad_package", detail=str(error)) from None
            self.cross_check(entry)
            self.enough_space(entry)
            self.phase("snapshot")
            backup = os.path.join(self.backups, "self-update-" + self.id)
            try:
                take_snapshot(self.receiver, backup)
            except OpkgBusy:
                raise Fail("opkg_busy") from None
            except (OSError, ValueError) as error:
                raise Fail("snapshot_failed", detail=str(error)) from None
            self.snapshot = backup
            self.phase("installing")
            ipk = self.path(entry["filename"])
            try:
                atomic_write(ipk, package)
            except OSError as error:
                raise Fail("internal_error",
                           detail="the package could not be written: " + str(error)) from None
            argv = [self.receiver.opkg, "install", "--force-reinstall"]
            # Only for a version that really is lower, and only where `choose` admits one.
            if self.lower and request["downgrade"] and request["started_by"] in DOWNGRADE_ORIGINS:
                argv.append("--force-downgrade")
            argv.append(ipk)
            # The marker goes first: a power loss, an OOM kill or a SIGKILL while the package
            # manager unpacks leaves a tree that is neither version, and the marker is then the
            # only thing that says so at the next start. No marker, no package manager.
            if not self.marker(phase="installing"):
                raise Fail("internal_error", detail="the marker could not be written")
            self.check()
            self.files_changed = True
            code = self.receiver.run(argv, max(1, self.deadline - self.receiver.clock()))
            self.receiver.pause("opkg_done")
            if code != 0:
                detail = (self.receiver.last_output.strip().splitlines() or ["exit " + str(code)])
                raise Fail("opkg_failed", version=entry["version"], detail=detail[-1][:160])
            self.verify_manifest(manifest)
            self.check()
        except Fail as failure:
            return self.before_restart_failed(failure)
        self.logs_writable = self.any_log_writable()
        # R2's record is what plays when a rollback begins; this one, taken before the
        # restart, stands in when the new interface does not answer then.
        self.before = self.statusinfo()
        running = self.receiver.enigma2_pids()
        self.restart_pids = running | {self.old_pid}
        if self.old_pid not in running:
            # The interface restarted by itself once the package manager had started: the
            # process running now may hold the old code, the new or a mix, and nobody asked for
            # it. It is stopped and the old files go back (R2), never proved.
            self.restarted_at = self.receiver.clock()
            return self.rollback(Fail("interrupted", detail="the receiver's interface restarted "
                                                            "while the update was installed"))
        if self.drill_requested():
            # The acceptance drill: from installed straight into R2, with no forward restart, so
            # the stop-restore-write mechanics are proved on a receiver in one restart.
            return self.rollback(Fail("drill"))
        self.write_status(phase="restarting")
        self.marker(phase="restarting")
        self.receiver.pause("restarting")
        word = self.await_restart()
        if word != "restarted":
            return self.withdraw(word)
        return self.prove()

    def drill_requested(self):
        """Whether the hardware acceptance drill asks for R2 now; the hook is used up if so.

        Only a request from an acceptance build honours it - release and development builds
        write `acceptance: false`, so on them the file is ignored and left where it is - and only
        an empty regular file of that exact name, never a link, a directory or a file with
        something in it: the drill's file is made with `touch`, and anything else of that name
        was put there for another reason.
        """
        if not self.request["acceptance"]:
            return False
        path = self.receiver.path(DRILL_R2)
        try:
            found = os.lstat(path)
        except OSError:
            return False
        if not stat.S_ISREG(found.st_mode) or found.st_size != 0:
            return False
        try:
            os.remove(path)
        except OSError:
            return False
        self.record["drill"] = "r2"
        return True

    def before_restart_failed(self, failure):
        """Nothing restarted on request: put the files back if the package manager ran, and end."""
        result = "interrupted" if failure.reason in ("interrupted",) else "failed"
        if self.files_changed:
            return self.undo(failure, result)
        if self.snapshot is not None:
            # A rollback point of a transaction that changed nothing would only push a real one
            # out of the two that are kept.
            shutil.rmtree(self.snapshot, ignore_errors=True)
            self.snapshot = None
        self.finish(result, failure)
        return 1

    def undo(self, failure, result):
        """The package manager ran and the transaction ends early: files and interface agree.

        While the interface that asked still runs, the old files go back under it, as the
        withdraw puts them (no settings block, no restart). Once it has gone - a restart asked
        for, or one nobody asked for - the process running may hold either version, so R2.
        A restart that lands while the files go back goes to R2 as well.
        """
        if self.restarted_at is not None or not self.original_running():
            if self.restarted_at is None:
                self.restarted_at = self.receiver.clock()
            return self.rollback(failure)
        if self.put_back(settings=False) != "done":
            result = "failed"
        if not self.original_running():
            self.restarted_at = self.receiver.clock()
            return self.rollback(failure)
        self.finish(result, self.restore_failure(failure))
        return 1

    def put_back(self, settings):
        """`restore_snapshot`, recorded: `done`, `partial` (the code is back, not all of its
        records) or `failed` (the code is not back - the new files, or a mix, are on disk)."""
        try:
            restore_snapshot(self.receiver, self.snapshot, settings=settings)
        except PartialRestore as error:
            self.record["restore"] = "partial: " + str(error)
            return "partial"
        except Exception as error:  # whatever it was, the end says the files are not back
            self.record["restore"] = "failed: " + (str(error) or type(error).__name__)
            return "failed"
        self.record["restore"] = "done"
        return "done"

    def restore_failure(self, failure):
        """The failure an end reports: its own, or - when the restore did not complete - the
        restore's, whose sentence names the repair; the first cause stays in the record."""
        outcome = self.record.get("restore")
        if outcome is None or outcome == "done":
            return failure
        self.record["cause"] = failure.reason
        detail = outcome.split(": ", 1)[-1]
        if outcome.startswith("partial"):
            return Fail("restore_incomplete", previous=self.request["from"]["version"],
                        detail=detail)
        return Fail("restore_failed", detail=detail)

    def any_log_writable(self):
        for relative in LOG_PATHS:
            path = self.receiver.path(relative)
            target = path if os.path.exists(path) else os.path.dirname(path)
            if os.access(target, os.W_OK):
                return True
        return False

    def await_restart(self):
        """The plugin's word after `restarting`: `restarted`, `withdraw`, `timeout`, `signal`.

        A restart is the process that asked gone **and** an enigma2 running that was not seen
        beside it: a child enigma2 forks keeps the name `enigma2` until it runs something else,
        and one seen on a single poll is not the interface coming back.
        """
        receiver = self.receiver
        limit = receiver.clock() + PLUGIN_WAIT
        asked = False
        while True:
            pids = receiver.enigma2_pids()
            if self.old_pid in pids:
                self.restart_pids |= pids
            elif pids - self.restart_pids:
                self.restarted_at = receiver.clock()
                return "restarted"
            if os.path.exists(self.path("withdraw")):
                return "withdraw"
            if self.lost.is_set():
                raise LockLost()
            if self.signalled is not None:
                return "signal"
            if not asked and os.path.exists(self.path("restart.json")):
                asked = True
                limit = receiver.clock() + PLUGIN_WAIT
                self.write_status(restart="requested")
            if receiver.clock() >= min(limit, self.deadline):
                if self.old_pid not in pids and asked:
                    # The old interface went and none came back: a restart, a failed one.
                    self.restarted_at = receiver.clock()
                    return "restarted"
                return "timeout"
            receiver.sleep(POLL)

    def withdraw(self, word):
        """R1 without a restart: the old files back under the running interface, then look again."""
        self.receiver.pause("withdrawing")
        # The code back, even without all of its records, is what a restarted process may have
        # read, so it counts as restored for the look below.
        restored = self.put_back(settings=False) != "failed"
        self.receiver.pause("withdrawn")
        if not self.original_running():
            # A restart happened while the files went back: the new process may have read
            # either version, so the files and the process are made to agree by R2 (A9).
            self.restarted_at = self.receiver.clock()
            if restored:
                return self.rollback(Fail("not_started", previous=self.request["from"]["version"]))
            return self.prove()
        if word == "signal":
            failure = Fail("interrupted", detail="signal " + str(self.signalled))
            result = "interrupted"
        elif word == "timeout" and not os.path.exists(self.path("restart.json")):
            failure = Fail("time_limit")
            result = "failed"
        else:
            # The plugin may say why it withdrew; anything it does not say is the question.
            said = read_json(self.path("withdraw"))
            reason = said.get("reason") if isinstance(said, dict) else None
            failure = Fail(reason if reason in WITHDRAW_REASONS else "question")
            result = "withdrawn_before_restart"
        if self.record["restore"] != "done":
            result = "failed"
        self.finish(result, self.restore_failure(failure))
        return 0 if result == "withdrawn_before_restart" else 1

    # ------------------------------------------------------------------- proof --

    def prove(self):
        receiver = self.receiver
        self.write_status(phase="proving")
        self.marker(phase="proving")
        receiver.pause("proving")
        tier = 1 if self.entry["self_update"] else 2
        window = receiver.clock() + PROOF_WINDOW
        wanted = {os.path.realpath(receiver.path(p)) for p in LOG_PATHS} | \
            {receiver.path(p) for p in LOG_PATHS}
        # Only a process that was not running beside the one that asked: a child of the old
        # interface inherited its descriptors, the old plugin's log among them.
        before = self.restart_pids | {self.old_pid}
        fd_seen = False
        while True:
            pids = receiver.enigma2_pids() - before
            for pid in pids:
                if receiver.open_files(pid) & wanted:
                    fd_seen = True
            if tier == 1:
                started = read_json(self.path("started.json"))
                if (started is not None and started.get("version") == self.entry["version"]
                        and started.get("commit") == self.entry["commit"]):
                    return self.commit("marker", fd_seen)
            elif fd_seen:
                return self.commit("log_fd", True)
            elif not self.logs_writable and pids:
                answer = receiver.webif("/mqttbridge")
                if answer is not None and answer[0] != 404:
                    return self.commit("webif_hook", False)
            if receiver.clock() >= window:
                break
            receiver.sleep(PROOF_POLL)
        self.record["proof"] = "none"
        return self.rollback(Fail("not_started", previous=self.request["from"]["version"]))

    def commit(self, proof, fd_seen):
        self.committed = True
        self.record.update(proof=proof, fd_seen=fd_seen, restart="clean")
        self.receiver.pause("committing")
        self.release()
        prune(self.backups, SNAPSHOT_NAME, "self-update-" + self.id)
        self.finish("installed", None)
        return 0

    # ----------------------------------------------------------------- rollback --

    def rollback(self, failure):
        """R2 as one unit: record, stop, restore, write the channel, start, verify (R3).

        Once `init 4` has been sent, `init 3` follows whatever happens in between: the handled
        failures are recorded and the unit carries on, and anything else still meets the
        `finally`, which first runs the steps the unit had not reached - the restore, the
        channel - each guarded, records `rollback: cut short`, and then starts the interface.
        That includes an escape from the `init 3` call itself: the `finally` sends it again,
        because starting a runlevel that is already running changes nothing. R3 comes after
        the start and checks a rollback that is already done, so an error in it is noted
        (`channel` / `standby` `unconfirmed`) and never changes the result.

        The result says what runs, not only what is on disk: `rolled_back` only when the old
        files are back and an enigma2 R2 had not seen before started on them. With the files
        back and none, it is `failed`: `not_stopped` when the stop was never seen - `init 3`
        then starts nothing, and the process R2 could not stop runs on with the code it had -
        or `interface_not_started` when nothing came after a stop. The first reason stays as
        `cause`. A restore that did not complete keeps its own reason before either.

        Before `init 4` nothing has been stopped, so this is not yet the unit: the record is
        best effort (an error in it leaves the one taken before the restart), and only once the
        stop is sent does `rolling_back` tell the last net that R2 owns the end. Anything that
        escapes before then meets the net as any other error after the package manager, and
        the net runs R2 again - never a `failed` over the new, unproven code.
        """
        receiver = self.receiver
        limit = receiver.clock() + ROLLBACK_LIMIT
        recorded = self.before
        try:
            self.write_status(phase="rolling_back")
            self.marker(phase="rolling_back")
            recorded = self.statusinfo() or self.before
        except Exception as error:  # a channel not recorded, never a rollback not made
            self.record["internal_error"] = (type(error).__name__ + ": " + str(error))[:200]
        self.record.update(restart="stopped")
        receiver.pause("rollback_recorded")
        service = recorded.get("service") if recorded else None
        starting = stopped = restored = sent = False
        # The enigma2 processes still running at the last look for the stop: none once it was
        # seen. After `init 3` only a process not among them is the interface starting again.
        survivors = set()

        def gone():
            survivors.clear()
            survivors.update(receiver.enigma2_pids())
            return not survivors
        self.rolling_back = True
        try:
            receiver.run([receiver.init, "4"], STOP_WAIT)
            stopped = self.wait(gone, STOP_WAIT)
            receiver.pause("rollback_stopped")
            self.put_back(settings=stopped)  # recorded, whatever it was: init 3 comes next
            restored = True
            receiver.pause("rollback_restored")
            self.put_lastservice(stopped, service)
            starting = True
            receiver.run([receiver.init, "3"], STOP_WAIT)
            # Only once it has returned: an escape from the call itself - the program could not
            # even be started - must still meet the `init 3` below, which is safe to repeat.
            sent = True
        finally:
            if not sent:
                # Something no handler expects cut the unit short, anywhere from `init 4` to the
                # `init 3` call itself. The steps it had not reached still run - each guarded,
                # so none can keep the next from running - before the interface starts: never
                # over a tree that could still have been put back.
                self.record["rollback"] = "cut short"
                if not starting:
                    if not stopped:
                        # "Not seen stopped yet" is not "running": cut short in `init 4` or in
                        # the wait, enigma2 may well be down. Asked once more, from `/proc`,
                        # so that a stopped interface still gets its settings block and its
                        # channel back; only a running one - or no answer - goes without.
                        try:
                            stopped = gone()
                        except BaseException:
                            stopped = False
                    if not restored:
                        try:
                            self.put_back(settings=stopped)
                        except BaseException:
                            self.record["restore"] = "interrupted"
                    if "lastservice" not in self.record:
                        try:
                            self.put_lastservice(stopped, service)
                        except BaseException:
                            self.record["lastservice"] = "failed"
            self.record["stop"] = "seen" if stopped else "not seen"
            if not sent:
                receiver.run([receiver.init, "3"], STOP_WAIT)
        receiver.pause("rollback_started")
        # Not stopped, `init 3` asks for a runlevel that never left and starts nothing: the
        # process R2 could not stop runs on, with whatever code it had, over the old files.
        started = self.wait(lambda: bool(receiver.enigma2_pids() - survivors), START_WAIT)
        if not started:
            self.record["interface"] = "not started" if stopped else "not restarted"
        if started and recorded and receiver.clock() < limit:
            try:
                self.verify(recorded)
            except Exception as error:  # R3 checks a rollback that is done; it cannot undo it
                self.record.setdefault(
                    "internal_error", (type(error).__name__ + ": " + str(error))[:200])
                self.record.setdefault("channel", "unconfirmed")
                self.record.setdefault("standby", "unconfirmed")
        elif not recorded:
            self.record["channel"] = "not recorded"
        else:
            # Not restarted, the channel was never touched; not started, there is none.
            self.record["channel"] = "lost" if stopped else "unconfirmed"
        end = self.restore_failure(failure)
        back = self.record.get("restore") == "done"
        if back and not started:
            # The old files are back, but no interface was seen starting on them, so nothing
            # says the old version runs: never `rolled_back`, which the plugin and Home
            # Assistant read as exactly that. The first reason stays, as `cause`.
            self.record["cause"] = failure.reason
            end = Fail("interface_not_started" if stopped else "not_stopped",
                       previous=self.request["from"]["version"])
        self.finish("rolled_back" if back and started else "failed", end)
        return 1

    def put_lastservice(self, stopped, service):
        """The recorded channel as `config.tv.lastservice`, only while enigma2 is stopped."""
        if stopped and service:
            try:
                write_lastservice(self.receiver.root, service)
                self.record["lastservice"] = "written"
            except Exception:  # a channel not kept, never a stopped GUI
                self.record["lastservice"] = "failed"

    def wait(self, condition, seconds):
        limit = self.receiver.clock() + seconds
        while True:
            if condition():
                return True
            if self.receiver.clock() >= limit:
                return False
            self.receiver.sleep(POLL)

    def statusinfo(self):
        """`{"service", "standby"}` from OpenWebif on the receiver, or None when it does not say."""
        answer = self.receiver.webif("/api/statusinfo")
        if answer is None or answer[0] != 200:
            return None
        try:
            status = json.loads(answer[1].decode("utf-8", "replace"))
        except ValueError:
            return None
        if not isinstance(status, dict):
            return None
        reference = status.get("currservice_serviceref")
        service = reference.strip() if isinstance(reference, str) and SERVICE_REF.fullmatch(
            reference.strip()) else None
        standby = status.get("inStandby")
        if isinstance(standby, str):
            standby = {"true": True, "false": False}.get(standby.strip().lower())
        return {"service": service, "standby": standby if isinstance(standby, bool) else None}

    def verify(self, recorded):
        """R3: the channel and the standby state, by effect; a zap back at most once."""
        first = None
        limit = self.receiver.clock() + SERVICE_WAIT
        while self.receiver.clock() < limit:
            now = self.statusinfo()
            if now is not None and now["service"]:
                first = now
                break
            self.receiver.sleep(POLL)
        if first is None:
            self.record.update(channel="lost" if recorded.get("service") else "not recorded",
                               standby="not recorded")
            return
        wanted = recorded.get("service")
        if not wanted:
            self.record["channel"] = "not recorded"
        elif first["service"] == wanted:
            self.record["channel"] = "kept"
        else:
            self.receiver.webif("/api/zap?sRef=" + quote(wanted, safe=""))
            ok = self.wait(lambda: (self.statusinfo() or {}).get("service") == wanted,
                           EFFECT_WAIT)
            self.record["channel"] = "restored" if ok else "lost"
        if recorded.get("standby") is True:
            if (self.statusinfo() or {}).get("standby") is True:
                self.record["standby"] = "kept"
            else:
                self.receiver.webif("/api/powerstate?newstate=5")
                ok = self.wait(lambda: (self.statusinfo() or {}).get("standby") is True,
                               EFFECT_WAIT)
                self.record["standby"] = "restored" if ok else "lost"
        elif recorded.get("standby") is False:
            self.record["standby"] = "kept"
        else:
            self.record["standby"] = "not recorded"
        self.record.setdefault("bouquet", "not restored")

    # --------------------------------------------------------------------- end --

    def finish(self, result, failure):
        """Write the end: status, the last-transaction record and the marker, then let go."""
        self.ended = True
        now = self.receiver.now()
        reason = failure.reason if failure is not None else None
        error = str(failure) if failure is not None else None
        self.write_status(phase="finished", result=result, reason=reason, error=error,
                          finished=now, record=self.record)
        summary = {key: self.status.get(key) for key in (
            "id", "started_by", "target", "from", "started", "finished", "result", "reason",
            "error")}
        summary["phase"] = "finished"
        # `finished` is the wall clock, which on a receiver without a battery-backed clock
        # starts in 1970 and jumps when NTP answers. The boot and the uptime at the end let the
        # ten-minute limit between transactions measure on a clock that never jumps.
        summary["boot_id"] = self.receiver.boot_id()
        summary["uptime"] = self.receiver.uptime()
        if not self.claimed:
            # Never held the lock (busy, or a request it could not read): the transaction that
            # does hold it owns the last-transaction record and the transaction directories.
            return
        try:
            write_json(self.receiver.path(LAST), summary)
        except OSError:
            pass
        if self.entry is not None and os.path.exists(self.receiver.path(MARKER)):
            try:
                self.marker(phase="finished", result=result, reason=reason, error=error,
                            finished=now)
            except OSError:
                pass
        self.release()
        if result == "installed":
            shutil.rmtree(self.directory, ignore_errors=True)
        prune(self.backups, DIRECTORY_NAME, os.path.basename(os.path.normpath(self.directory)))


# ------------------------------------------------------------------------ main --


def main(argv=None):
    """`helper.py <id> [--test <config.json>]`: run the transaction in this file's directory."""
    argv = list(sys.argv[1:] if argv is None else argv)
    directory = os.path.dirname(os.path.abspath(__file__))
    if not argv or not TRANSACTION_ID.fullmatch(argv[0]):
        return 2
    receiver = Receiver()
    if len(argv) == 3 and argv[1] == "--test":
        receiver = _test_receiver(argv[2])
    transaction = Transaction(directory, receiver)
    transaction.install_signals()
    return transaction.run()


def _test_receiver(path):
    """A receiver of stand-ins, named by the test suite; never used on a receiver."""
    config = read_json(path) or {}

    class Stand(Receiver):
        def pause(self, name):
            fifo = config.get("pauses", {}).get(name)
            if fifo:
                with open(fifo, encoding="utf-8") as handle:
                    handle.read()

    stand = Stand(root=config["root"], proc=config["proc"], init=config["init"],
                  opkg=config["opkg"], webif_base=config.get("webif"))
    for name, value in config.get("constants", {}).items():
        globals()[name] = value
    return stand


if __name__ == "__main__":
    sys.exit(main())
