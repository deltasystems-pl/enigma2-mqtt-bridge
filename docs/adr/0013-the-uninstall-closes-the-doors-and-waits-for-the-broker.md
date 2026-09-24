# ADR-0013: The remote uninstall closes the doors first, retracts at QoS 1 and waits for the broker, and puts everything back when a step fails

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** [ADR-0004](0004-remote-uninstall.md) §3 in part - the order of the steps, and what
happens when one fails. Everything else in ADR-0004 stands: the permission, the node id as the
payload, the capability, the one-way door, the settings left alone.

## Context

[ADR-0004](0004-remote-uninstall.md) §3 decided the removal as four steps: retract every retained
topic, publish `offline`, remove the package, restart the interface. Read against the code that was
going to run it, and against the receiver it was going to run on, that order is incomplete in three
places and silent in a fourth.

- **The publishers are still running while the retraction runs.** The plugin publishes on change
  from timers and events - a disk probe every minute, an EPG grid rebuilt on a timer, the process
  sample, the softcam, the screenshot after a zap. Any of them firing between the retraction and the
  end of the session puts a retained topic back on the broker and back into the state file, and
  after the package is gone nothing will ever retract it again. That is the exact failure the
  command exists to prevent.
- **Nothing can know the retraction arrived.** Every retraction, and the shutdown `offline`, went
  out at QoS 0, which the MQTT client counts as done when it reaches the socket. „Retract, then
  remove" has no way to say the first half happened before it starts the second.
- **There is no path for a removal that fails, and removal can fail.** opkg locks its database; the
  image's own update check runs daily and its plugin browser runs opkg from the same process, so a
  lock held at the wrong moment is ordinary. A refused `opkg remove` after the retraction leaves a
  running plugin that has told everybody it is gone.
- **The restart can wait for a person.** The image's restart screen asks „really restart?" with no
  timeout when a stream, a background job or timeshift is running - none of which the recording
  guard looks at.

And one thing the first record did not say: retained messages that other clients left on this
node's command topics. The dispatcher refuses to act on them, and until now nothing cleared them.

## Decision

The handler validates and returns - so the dispatcher's own clearing of `last_error` has happened -
and the removal runs from the next turn of the main loop, never blocking it:

1. **Close the doors.** Stop every publisher and stop dispatching commands. Every publish path
   checks the same flag, so a publisher that was already on its way publishes nothing.
2. **Retract** every topic in the state file and every command topic on which a retained message was
   discarded this session, **at QoS 1**.
3. **`availability: offline`**, retained, **QoS 1**, the last message.
4. **Wait for every acknowledgement** by polling from a timer every 100 ms, at most 15 s, keeping no
   more than half of the client's queue bound outstanding.
5. **Save the state file empty.**
6. **Disconnect cleanly**, so the will is not sent and `offline` stays.
7. **`opkg remove enigma2-plugin-extensions-mqttbridge`** through `eConsoleAppContainer`, with no
   `--autoremove` and no `--force`, and `MQTTBRIDGE_UNINSTALL=1` in its environment so that `prerm`
   does not advise the `cmd/reset` that would undo step 2.
8. **Restart the user interface**, best effort.

**QoS 1 for the retractions is a documented exception** to „state at QoS 0". A subscriber receives
at the lower of the publisher's and its own QoS, so no consumer sees a difference; QoS 1 is simply
the only way the broker says „done". Recorded and not taken: retractions at QoS 0 and only the
`offline` at QoS 1, relying on the broker handling one connection's packets in order - true of
Mosquitto's single-threaded loop, not promised by MQTT 3.1.1 across QoS levels, and not measured.

**A step that fails ends where a reset ends.** No acknowledgement within the bound, a dropped
connection, a refused publish, or opkg that cannot start or exits non-zero: nothing further is
removed, the bridge opens a fresh session - whose connect republishes availability, the snapshot,
the announcement and discovery - and `last_error` names the step, with opkg's exit status and last
line of output where there is one. An exception raised anywhere after the doors close is a
failed step in the same way: every entry point from enigma2 - both timers and opkg's exit - is
wrapped so that it cannot leave the plugin closed, silent and deaf.

**The removal is refused while an EPG import runs**, with the sentence and the lapse `restart_gui`
uses: it ends in the same restart, and a restart mid-import loses the run.

**opkg's exit status is checked against the disk.** `eConsoleAppContainer` reports a child killed
by a signal as exit 0, and an opkg still running when enigma2 itself exits reports the same. After a
0 the package's `.control` and the running `plugin.py` must both be gone before the restart;
otherwise it is a failed step. 🔴 opkg is not atomic - it removes files one at a time - so a failure
at this point may leave an incomplete plugin, and `last_error` says so with the command that repairs
it: `opkg install --force-reinstall enigma2-plugin-extensions-mqttbridge`. opkg's output is read
from `dataAvail` alone: the image sends every chunk there and again on `stdoutAvail` or
`stderrAvail`.

**A state file that cannot be emptied does not stop the removal.** Every topic it still names has
just been acknowledged as retracted, a reinstall republishes them on its first connect anyway, and
stopping would put back everything the broker has just removed. The plugin logs it and carries on.

**Every module the sequence uses is imported when the plugin starts.** After step 7 a first import
cannot succeed, and it would fail inside the one sequence that has nowhere left to report it.

## Consequences

- **The state topics' QoS is no longer uniform.** A consumer that asserts QoS 0 on every retained
  message it sees will see QoS 1 exactly once per node, on its way out.
- **A removal can be refused for a reason the household did not cause** - the image's own update
  check holding opkg's lock. The answer is on `last_error`, the plugin is back as it was, and the
  command can simply be sent again.
- **The restart is not a promise.** The acceptance of this feature on hardware checks that the
  plugin is disconnected and gone from disk, and records whether the restart ran or is waiting on
  screen; it never assumes the restart.
- **The composition has not run on hardware** at the time of writing. Each step is tested offline,
  including a run of the whole sequence with the plugin's files already gone from disk.
