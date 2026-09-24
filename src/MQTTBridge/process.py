"""What the enigma2 process costs: memory, threads, open files, and since when.

A receiver that is never restarted is the normal case, so the question somebody
eventually asks about this plugin is „is it leaking?" - and that question cannot
be answered by looking once. It needs a curve, which means a number on the
broker at a steady cadence, recorded by whatever the consumer already records.

The numbers are the *process's*, not the plugin's. enigma2 is one process: the
image, every other plugin and this one share the same resident set, and nothing
in `/proc` can attribute a kilobyte to any of them. A rising line here is a
question, not a verdict - `docs/TROUBLESHOOTING.md` says what is already known
to sit under it, starting with the ~22 kB every screenshot costs whoever takes
it.

**Everything here reads procfs, and procfs is memory.** There is no disk behind
`/proc/self/status`, no network, and no lock that another process holds, which
is what makes this the one poll in the plugin that is allowed to run on the main
loop instead of on a thread of its own - the opposite of `hdd.py`, whose one
`statvfs` can sit in an NFS timeout for thirty seconds. The reads are still each
wrapped: a field that cannot be read is `null` and never an exception, because
the value of this topic is that it is boring.

Two cadences, one timer. A point every 300 seconds, so the curve has points on
it on a box where nothing much is going on; and a check every 60 seconds that
publishes early when the resident set has moved by 4 MiB either way, so the jump
that matters is on the curve at the minute it happened rather than five minutes
later.

The 300-second publish goes through the bridge's publish-on-change rule like
every other state topic, so it is a ceiling on the gap rather than a heartbeat:
a payload identical to the last one is not sent again, and a receiver idle
enough that not one of the five numbers moved simply stays quiet. The connect
snapshot is the exception and always goes out.
"""

import os
import time

from .enigma2 import Ticker
from .log import get_logger
from .publisher import Publisher

LOG = get_logger("process")

PROC_ROOT = "/proc"

# The cheap check. Everything it reads is memory-backed, so this is a handful of
# microseconds on the main loop.
POLL_MILLISECONDS = 60000

# The slow cadence: at most five minutes between points on the curve, and
# longer when nothing has changed - the publish is still subject to the
# bridge's publish-on-change rule.
FULL_INTERVAL_SECONDS = 300

# How far the resident set has to move to earn a publish before that. 4 MiB is
# well above the ordinary breathing of an idle receiver and well below anything
# somebody would want to find out about five minutes late.
RSS_STEP_KB = 4096

# The payload's keys, in the order `docs/TOPICS.md` documents them. Every one is
# always present - a missing key renders as an empty string in a Home Assistant
# template, which means „ignore this message" and leaves the previous value on
# screen for ever, while an explicit `null` renders as unknown.
FIELDS = ("rss_kb", "hwm_kb", "threads", "fds", "started")

# `/proc/self/status` spells them like this, in kB for the two memory ones.
STATUS_FIELDS = (("rss_kb", "VmRSS:"), ("hwm_kb", "VmHWM:"), ("threads", "Threads:"))

# `/proc/<pid>/stat` field 22 (1-based) is the process's start time in clock
# ticks since boot. Fields 1 and 2 are the pid and the comm, and the comm is in
# parentheses and may contain anything at all including spaces and a closing
# parenthesis - so the tail is taken from the *last* `)` and field 22 is index
# 19 of what follows (field 3 being index 0).
STARTTIME_INDEX = 19


def _text(path):
    """A whole procfs file, or None when it cannot be read."""
    try:
        with open(path, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except Exception:
        return None


def status_text(proc_root=PROC_ROOT):
    """`/proc/self/status`, or None - which is what „no `process` capability" means."""
    return _text(os.path.join(proc_root, "self", "status"))


def _whole_number(word):
    try:
        return int(word)
    except (TypeError, ValueError):
        return None


def status_values(text):
    """VmRSS, VmHWM and Threads out of `/proc/self/status`; each None if absent.

    The memory lines are a label, a tab, the number and a unit - `VmRSS:` then
    `12345 kB`. The unit is kB on every architecture Linux runs on, which is why
    the contract says kB and this does not convert.
    """
    found = dict.fromkeys(name for name, _label in STATUS_FIELDS)
    for line in (text or "").splitlines():
        for name, label in STATUS_FIELDS:
            if line.startswith(label):
                parts = line.split()
                if len(parts) > 1:
                    found[name] = _whole_number(parts[1])
    return found


def open_files(proc_root=PROC_ROOT):
    """How many file descriptors the process holds, or None.

    The count includes the descriptor the directory listing itself opens, which
    is what any other reader of this number would see too. It is a trend, not an
    audit.
    """
    try:
        return len(os.listdir(os.path.join(proc_root, "self", "fd")))
    except Exception:
        return None


def boot_time(proc_root=PROC_ROOT):
    """`btime` out of `/proc/stat`: the epoch second the kernel came up."""
    for line in (_text(os.path.join(proc_root, "stat")) or "").splitlines():
        if line.startswith("btime "):
            parts = line.split()
            if len(parts) > 1:
                return _whole_number(parts[1])
    return None


def clock_ticks():
    """`SC_CLK_TCK` - 100 on every receiver, and never assumed to be."""
    try:
        ticks = os.sysconf("SC_CLK_TCK")
    except (AttributeError, ValueError, OSError):
        return None
    return ticks if ticks and ticks > 0 else None


def started_at(proc_root=PROC_ROOT):
    """The epoch second the enigma2 process started, or None.

    Boot time plus the process's own start offset. Deliberately not „now minus
    uptime": this value is published retained and a consumer keeps it, so it has
    to be the same number on every publish rather than one that drifts by a
    second each time somebody reads it.
    """
    text = _text(os.path.join(proc_root, "self", "stat"))
    if not text:
        return None
    _head, _paren, tail = text.rpartition(")")
    fields = tail.split()
    if len(fields) <= STARTTIME_INDEX:
        return None
    ticks_since_boot = _whole_number(fields[STARTTIME_INDEX])
    ticks = clock_ticks()
    booted = boot_time(proc_root)
    if ticks_since_boot is None or ticks is None or booted is None:
        return None
    return int(booted + ticks_since_boot // ticks)


def payload(text, proc_root=PROC_ROOT):
    """The `process` payload, built from an already-read `/proc/self/status`."""
    found = dict.fromkeys(FIELDS)
    try:
        found.update(status_values(text))
        found["fds"] = open_files(proc_root)
        found["started"] = started_at(proc_root)
    except Exception:  # pragma: no cover - the helpers above do not raise
        LOG.exception("reading the process counters raised")
    return {name: found.get(name) for name in FIELDS}


def read(proc_root=PROC_ROOT):
    """The `process` payload, reading everything it needs."""
    return payload(status_text(proc_root), proc_root)


class ProcessPublisher(Publisher):
    """`process` - the enigma2 process's own counters, on a steady cadence."""

    name = "process"

    def __init__(self, bridge=None, proc_root=PROC_ROOT):
        Publisher.__init__(self, bridge)
        self.proc_root = proc_root
        self._ticker = Ticker(self._tick, "process")
        self._readable = False
        self._cached = None
        self._published_at = None
        self._published_rss = None

    # ------------------------------------------------------------- lifecycle --

    def start(self):
        """Read once to find out whether this kernel has the files at all."""
        self._cached = self._read()
        if not self._readable:
            LOG.info("this image has no readable /proc/self/status; not publishing process")
            return False
        self._ticker.start(POLL_MILLISECONDS)
        return True

    def claimed(self):
        """Only while `/proc/self/status` answers. A capability is a fact, not a plan."""
        return self._readable

    def stop(self):
        self._ticker.stop()

    # ---------------------------------------------------------------- reading --

    def _read(self):
        text = status_text(self.proc_root)
        self._readable = text is not None
        return payload(text, self.proc_root)

    def snapshot(self):
        """Read fresh rather than cached: on connect the cheapest thing is the truth.

        `hdd` caches because its read can block for thirty seconds on a network
        mount. Nothing here can block, so a snapshot five seconds old would be
        staleness bought for nothing.
        """
        self._cached = self._read()
        # The bridge publishes this itself, so the cadence is measured from here
        # - „then every 300 s" is 300 seconds after the connect, not 300 seconds
        # after whenever the last tick happened to fall.
        self._remember(self._cached)
        return {"process": self._cached}

    # ----------------------------------------------------------------- cadence --

    def _tick(self):
        found = self._read()
        self._cached = found
        if not self._due(found):
            return False
        self._remember(found)
        self.publish("process", found)
        return True

    def _due(self, found):
        if self._published_at is None:
            return True
        if time.monotonic() - self._published_at >= FULL_INTERVAL_SECONDS:
            return True
        rss = found.get("rss_kb")
        if rss is None or self._published_rss is None:
            # Nothing to compare against; the 300-second cadence still applies.
            return False
        return abs(rss - self._published_rss) >= RSS_STEP_KB

    def _remember(self, found):
        self._published_at = time.monotonic()
        self._published_rss = found.get("rss_kb")
