# ADR-0004: Remote uninstall behind a box-side permission

**Status:** accepted 2026-09-22, amended by [ADR-0009](0009-the-openwebif-page-trusts-openwebif.md) (§1: „on the receiver" includes the OpenWebif page), §3 partly superseded by [ADR-0013](0013-the-uninstall-closes-the-doors-and-waits-for-the-broker.md) (the order - publishers stopped first, retractions and `offline` at QoS 1 and acknowledged before the removal - and the failure path); implemented in 0.3.0
**Date:** 2026-09-22
**Supersedes:** - (it extends [ADR-0003](0003-control-feedback-and-household-features.md))

## Context

Installing this plugin on a receiver is now a guided operation somebody runs from Home Assistant
without opening a terminal. Removing it is an SSH session, a package name nobody remembers, and -
until the amendment to [ADR-0003](0003-control-feedback-and-household-features.md) - a sweep of
compiled files, because `opkg remove` on its own left the plugin loadable. That asymmetry is the
defect: an install path with no matching removal path leaves a person with something on their
receiver that they cannot get rid of by the means they put it there with.

There is a second half to it, and it is the one that bites quietly. Retained topics outlive the
plugin that published them. `docs/TOPICS.md` already says that the documented step *before*
uninstalling is `cmd/reset`, run while the plugin is still connected - remove the package without
it and the broker keeps serving a snapshot of a receiver that is gone, for ever, while every
consumer keeps showing entities nothing will ever update. A removal procedure whose first step is
easy to forget, and whose consequence is invisible and permanent, is a procedure that will be got
wrong. The ordering is not advice; it belongs inside the operation.

So the question is not whether removal should be reachable remotely. It is what may trigger it.

**Why not when the consumer's configuration entry is deleted.** That is the obvious hook and it is
the wrong one. Deleting an integration from Home Assistant is a local decision about Home
Assistant, it is reached from a page people delete things on by accident, and it is ordinarily
recoverable by adding it back. Attaching an irreversible change to *another machine* to a click
whose known cost is „I will set this up again" would make a recoverable mistake unrecoverable. The
same objection in its general form: **a broker message must not carry that much power by
implication.** Removal has to be asked for, in as many words, by somebody who meant it.

**Why a box-side permission.** [ADR-0003](0003-control-feedback-and-household-features.md) already
settled the rule - a setting that *enables* a command is granted on the receiver only; a setting
that *tunes* a command already permitted may be remote - and applied it to `deep_standby_allowed`,
`softcam_restart_allowed` and `epg_import_allowed`. Uninstall is the strongest case for that rule
rather than an exception to it: anyone holding the broker password can publish a command, the
broker is shared with everything else in a household, and this is the one command that cannot be
undone from the same place it was issued. A permission that can be granted over MQTT is not a
permission; it is a step.

**Alternatives considered.**

- **SSH only, no remote uninstall.** The guided installer already holds an SSH path and it is the
  better one where it exists, because it can *verify* the result instead of trusting it. But SSH
  credentials are deliberately opt-in in the companion integration and most entries will not have
  them, so this would leave the majority of installations with no removal path at all - the
  asymmetry above, unchanged.
- **No remote uninstall in any form.** Rejected for the same reason, with the retained-topic
  problem on top: the people most likely to get the ordering wrong are exactly the people who will
  not be running `cmd/reset` by hand first.

## Decision

Target release **0.3.0**.

### 1. A permission, `uninstall_allowed`

A new setting, default **off**, granted on the receiver's own setup screen. It is never settable
over MQTT: it is outside the `cmd/config` allowlist and an attempt to write it is refused as an
unknown key, exactly as the other permissions are. As with `deep_standby_allowed`, „on the
receiver" includes the provisioning file a headless first install reads - what is excluded is the
broker, in every form.

It is echoed **read-only** in `info.settings`, on precisely the terms
[ADR-0003](0003-control-feedback-and-household-features.md) established for
`deep_standby_allowed`, so that a consumer can hide a control the receiver would refuse instead of
offering one that can only fail. Presence in `info.settings` still does not imply writability.

### 2. A command, `cmd/uninstall`

**The payload is the node id.** Not `PRESS`, and not a secret - the node id appears in every topic
name this plugin publishes to, so it proves nothing about who is publishing. What it proves is
*which receiver was meant*. A household with two boxes has two nearly identical command topics,
and the difference between them is one word in a path. The permission is the security boundary;
the payload is the confirmation, and it is there so that a mis-sent message removes nothing.

The command is refused, with the ordinary `last_error` shape - the command name, a sentence
written for the person who will read it, and a timestamp - when:

- the permission is off;
- the payload does not match this node's id;
- a recording is running or a timer is due, on the same terms as every other command that takes
  the interface down.

### 3. What it does when it is accepted, and in this order

1. **Retract every retained topic this node owns** - the same retraction as `cmd/reset`'s first
   half: state topics, every EPG grid, the announcement and every Home Assistant discovery payload
   named in the state file. Unlike a reset, **nothing is republished afterwards.**
2. **Publish `availability: offline`**, retained, as the last thing the broker hears. A consumer
   then sees the receiver go away, which is true.
3. **Remove the package**, on itself, through the receiver's package manager. This is only a real
   removal because of the amendment to
   [ADR-0003](0003-control-feedback-and-household-features.md): `CONTROL/prerm` sweeps the compiled
   files the image wrote after the install, which are in nobody's file list. Without that sweep
   this sequence would be worse than doing nothing - the retained topics would be gone and the
   plugin would come back at the restart in step 4, connected and announcing, with nothing on the
   broker left to say so.
4. **Restart the interface.**

Steps 1 and 2 are first for the reason the context gives: after the package is removed there is
nothing left to ask. The operation is the documented manual procedure with its ordering made
structural.

🔴 **`/etc/enigma2/settings` is not touched.** A reinstall finds its configuration - the broker,
the base topic, the node id and the permissions - exactly where it left it. This is a removal, not
a factory reset, and it matches what `CONTROL/prerm` already does and does not do.

**Why removing one's own files while running is safe here.** The modules are imported; the running
code is in memory and does not go back to disk. Nothing in steps 3 and 4 calls into a module that
has not already been loaded, and the restart in step 4 is what actually unloads the plugin - by
which time there is nothing on disk to load again. The order is the safety property, not a
convenience.

### 4. A capability, `uninstall`

Claimed only when the plugin can resolve its own installed package on this receiver. An image
where the plugin arrived some other way - unpacked by hand, or carried in a firmware image - names
no capability and takes no command, because a removal it cannot perform is worse than one it does
not offer. That is the same promise `capabilities` has always made.

### 5. It is a one-way door, and it says so

When this has run there is no plugin left to listen. Nothing over MQTT can undo it: the receiver
comes back only through SSH or through the receiver's own package manager. Every interface that
offers the command states that before it is confirmed. See the companion integration's ADR-0004
for how it is offered there, and for the rule that deleting a configuration entry never removes
anything from a receiver.

## Planned contract changes

| Release | Addition | Kind | Capability | Notes |
|---|---|---|---|---|
| 0.3.0 | `info.settings.uninstall_allowed` | read-only member | - | Default off, granted on the box only, never writable by `cmd/config` |
| 0.3.0 | `cmd/uninstall` | command | `uninstall` | Payload is the node id; refused unless permitted, while recording, or with a timer due |

Nothing is removed and nothing changes shape. Every rule the contract already sets still holds:
state retained at QoS 0, commands at QoS 1 never retained and discarded when they arrive retained,
and every refusal on `last_error`.

## Consequences

- **The capability list grows by one**, and `info.settings` gains a third read-only permission. A
  consumer that writes back everything it reads from `info.settings` is refused - which was
  already true, and is now true in one more place.
- **A consumer's control has to depend on the permission echo**, and here the polarity is the
  opposite of the one the power-off buttons use. There, only a *stated* `false` removes anything,
  so an older plugin that says nothing keeps behaving as it did. Here the control appears only on
  a stated `true`: silence means „this receiver has not said it permits this", and for an
  irreversible operation the safe reading of silence is no.
- **A removed plugin leaves a consumer's entities behind.** The topics are retracted and
  `availability` is `offline`, so nothing will update again, but a configuration entry and its
  entities do not delete themselves. That is correct - the consumer's configuration is the
  consumer's - and it is worth saying out loud, because „the device went unavailable" is what a
  person sees, and it looks the same as a receiver that has been switched off.
- **A test asserts the refusal by default**, not only the success path. The permission being off
  is the shipped state of every receiver, so it is the behaviour most installations will ever see,
  and it is the one a regression would make silent.
- **Removal now has a path that cannot forget the retraction**, which is the whole reason it is a
  command rather than a documentation page. The manual path stays documented and stays correct.

## Not yet done

- **None of this is implemented.** This record is the decision; the release is 0.3.0.
- **`docs/TOPICS.md` carries the planned rows for both additions** in its planned section. Its
  payload column predates this record and describes the command's payload as free-form; it takes
  the node id, and the table is brought into line with this record before either is built.
- **The sequence has never been run on hardware.** Its steps are individually proven - the
  retraction is `cmd/reset`'s, the sweep is `prerm`'s, and the restart is the one the installer
  already performs - but the composition of them, on a plugin removing itself, is not.
