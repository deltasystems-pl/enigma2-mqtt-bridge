# ADR-0005: The softcam is restarted by collapsing its instances, counted by process roots

**Status:** accepted 2026-09-22
**Date:** 2026-09-22
**Supersedes:** [ADR-0003](0003-control-feedback-and-household-features.md) §3, in part — it
described this feature, and the parts of it corrected below turned out to be wrong when they were
measured. Everything else in ADR-0003 stands.

## Context

ADR-0003 specified `cmd/softcam_restart` from one on-site observation: *several independent
instances running and none of them working*. Implementing it needed four questions answered that
the earlier record did not ask, and the answers changed the design.

**Why an image leaves copies behind.** A softcam manager that starts the bare binary rather than
an init script checks whether the cam is alive by looking it up **by process name**, against
`/proc/<pid>/stat`'s `comm` field. The kernel caps `comm` at **15 characters**. A cam binary whose
basename is longer can therefore never equal its own truncated name: the lookup always comes back
empty, and the manager always takes its „could not find it, start one" branch — which stops
nothing and only adds, once per graphical-interface start. 🔴 **The defect is conditional on the
filename.** A receiver whose cam is named fifteen characters or fewer does not accumulate copies
at all, and a feature written as though every receiver does would be describing somebody else's
box.

**Why a process count is the wrong count.** At its default restart level a cam forks a supervisor
parent that keeps the worker: two processes, identical start times, the child's parent is the
parent, the pidfile names the child, and the parent does no work. That is **one healthy instance
presenting as two processes**. „More than one is a fault", applied to a process count, reports a
fault on a working receiver.

**Why the name alone cannot identify a process.** Since `comm` is truncated, two binaries
differing only after the fifteenth character are indistinguishable by it. And `pgrep -f` is not an
answer: measured, it matched the shell that was running the search, because that shell's own
command line contained the pattern. A matcher that can match the process doing the matching is a
matcher that can kill it.

**Why „not decoding" cannot be „the ECM file is missing".** The cam **removes** `/tmp/ecm.info`
when it stops descrambling — measured by zapping to a free-to-air service and sampling for a
minute, then zapping back and watching it reappear within a second. On a free-to-air channel
absence is therefore the normal, healthy state.

## Decision

**Counting.** `running_instances` counts **instances**, not processes. A process is matched when
its `comm` equals the first `COMM_LENGTH` (15) characters of the binary's basename **and**
`/proc/<pid>/exe` resolves to the binary's exact path; both conditions are required and the second
is not optional. An instance is then a matched process **whose parent is not itself matched**,
which makes a supervisor-and-worker pair one, two independent launches two, and an orphan whose
parent was killed one, without any of the three being a special case. A count that could not be
taken is `null` and never `0`. `pgrep -f`, and name matching of any kind outside these two
conditions, are ruled out.

**Restarting.** The binary is resolved on the receiver from the image's own autostart setting and
is refused unless it is an executable regular file **directly under the softcam directory** after
symlink resolution, with a name that needs no shell quoting. It is then started with a
**family-keyed, fixed command line** — the family being the lowercase prefix of the binary's
basename, never the protocol the cam speaks outward — reproducing the image's own line including
its stack limit and background flag. A family for which no line is known does not get the
capability. 🔴 **No part of the command line comes from the payload**, ever.

**What is never touched.** The cam's runtime directory is not cleaned: the image's own poller does
not clean it either, the cam rewrites its pidfile on start, and the line the image's *manual*
start screen uses would take the live cam log with it. The image's „skip this cam" marker file is
never written: it is shared and unowned, and on these images the manager is the only thing that
starts the cam at all, so a marker left behind by a crash would disable it until the next
interface restart.

**Guards.** One guard, used identically by the manual and the automatic path. The permission
`softcam_restart_allowed` is box-only; the whole recording guard applies — recording, a timer due
within ten minutes, and „the image will not say" — and a restart is refused for the first sixty
seconds after the plugin starts, because the image's own check fires about a second after every
interface start and restarting inside that window races a copy already on its way. Manual restarts
are limited to one a minute and automatic ones to one every ten minutes.

**The decode signal.** Gated on the current service being **encrypted** first, and then on the
**age** of the ECM file's modification time, with absence read as infinitely old. Only that
modification time is read: nothing else in the file reaches the broker, `last_error`, a log record
at any level, a diagnostic or a test fixture.

**Two fields are added to the topic** that ADR-0003 did not name: `manager_check_on_start` and
`manager_timer_minutes`. They are read-only facts about the image, they cost one settings read
each, and each replaces a diagnosis that would otherwise have to be made over SSH on somebody
else's receiver.

## Consequences

- **The capability is narrow by design.** It is claimed only where the image is in the
  manager-poller shape, the binary resolves, and the family has a line. A receiver that starts its
  cam through an init script gets no `softcam` capability and no button, because a process-level
  restart would fight the init script; nobody in this project has such a receiver to measure, so
  refusing is the honest answer rather than guessing at one.
- **The counting rule is now load-bearing elsewhere.** Anything later that counts processes on the
  receiver — a process-cost topic, a watchdog — uses roots, not processes, and `comm` plus `exe`,
  not a name. Changing it means changing what `running_instances` has meant on every published
  payload.
- **A dead cam in the ten minutes before a recording stays dead** until the window passes. The
  asymmetry that suggests itself — healing inside that window, because a recording deserves a
  working cam — was considered and rejected: a restart landing close to a timer risks the opening
  seconds of the recording, and a scrambled recording is recoverable while a truncated one is not.
  This is the accepted cost, not an oversight.
- **Reversing the privacy boundary is a breaking decision.** Publishing anything further from the
  ECM file would put a card-sharing account identifier, a sharing server's address and live
  control words within reach of the broker, so any later feature that wants more of that file
  needs its own record and its own opt-in, not an edit to this one.
- **`restarts_today` is not a durable total.** It lives in memory while the topic is retained, so
  a consumer must not read the retained number as continuity across a receiver reboot. It is
  recomputed from a stored local date at publish time rather than reset by a timer, because a
  receiver's clock can step shortly after boot and a timer armed before the step fires at the
  wrong moment.
