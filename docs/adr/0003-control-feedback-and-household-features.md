# ADR-0003: Control feedback and household features — the 0.2.0 and 0.3.0 plan

**Status:** accepted 2026-09-21, amended 2026-09-22
**Date:** 2026-09-21
**Supersedes:** — (it extends [ADR-0000](0000-prd.md) and [ADR-0002](0002-scope-after-m0.md))

## Context

Two days of ordinary household use, on the one receiver this project has, produced a list of
complaints. Each was diagnosed against the code and against evidence from the running box rather
than guessed at, and the answers below are decisions, not a wishlist.

The complaints divide cleanly, and so does the work:

- **Things that already exist and behave wrongly or invisibly.** A button that publishes a command
  the plugin refuses, and says nothing. A control that cannot be reached because the setting that
  gates it is only on the television. These are **fixes**, and they are **0.2.0**.
- **Things the receiver can do that nothing asks it to.** Restart the softcam. Run the EPG
  importer. Put a message on screen without interrupting what is on it. These are **features**, and
  they are **0.3.0**.

0.2.0 is cut after the guided installer has been run end to end on hardware, which is the gate
[ADR-0002](0002-scope-after-m0.md) left open. Inside 0.3.0 the order is **softcam, then CEC, then
the rest**: those two are what a household notices without opening Home Assistant.

This record covers the receiver plugin's half. The companion integration's half is ADR-0003 in
[its repository](https://github.com/deltasystems-pl/hass-enigma2-mqtt/blob/main/docs/adr/0003-control-feedback-and-household-features.md),
and the two must be read together for anything that spans both.

## Decision

### 1. `deep_standby_allowed` is echoed, read-only — 0.2.0

> **Merged to `main` 2026-09-21, unreleased; noted 2026-09-22, with one line corrected.**
>
> This decision is implemented and on `main`; it is in no release. `info.settings` carries
> `deep_standby_allowed` as a
> boolean, always present, writable through neither `cmd/config` nor the OpenWebif status page.
> `docs/TOPICS.md` lists it among the read-only members, and the rule it establishes — **presence
> in `info.settings` does not imply writability** — is now part of the contract.
>
> **Corrected:** the problem statement says the permission „can only be turned on from the
> receiver's own setup screen". That is imprecise. It can also be set **in the provisioning file
> at first install**, which `docs/SETUP.md` documents and which is how a headless install
> configures it. What was and remains true is the part that matters: it can **never** be set over
> MQTT, and never from the OpenWebif status page, which offers only the writable settings.
>
> **One consequence found on the consumer side**, recorded here because it constrains anything
> reading this member: the announcement arrives before `info` does and carries **no settings at
> all**, so „the box has answered" is not the same question as „the box has stated this". A
> consumer needs three states rather than two — not yet stated, stated true, stated false — and a
> value that is not a boolean belongs in the first of them, because a payload nobody can parse is
> not a decision. The companion integration builds its two power-off buttons exactly that way.


**The problem.** `deep_standby_allowed` is off by default and can only be turned on from the
receiver's own setup screen; it is deliberately outside the `cmd/config` allowlist, because a
setting that *enables a destructive command* must not be settable by anything holding a broker
password. The cost of that correct decision was that a consumer had no way to tell „the receiver
refused this" from „the receiver cannot do this", so a Home Assistant user was offered two buttons
that would always be refused, with no way to find out why.

`info.settings` gains `deep_standby_allowed` as a **read-only** member. It is *not* added to the
`cmd/config` allowlist; an attempt to write it is refused as an unknown key, exactly as now.

🔴 **This changes what `info.settings` means.** It has been documented as „the complete remotely
writable, non-secret subset". It becomes „the non-secret settings a consumer may read, of which the
writable ones are the `cmd/config` allowlist". `docs/TOPICS.md` marks which members are read-only,
and a consumer must not infer writability from presence. 0.3.0 adds two more read-only members on
the same terms, below.

### 2. A discreet toast, as a second message style — 0.3.0

**The problem.** `cmd/message` shows the image's standard centre message box. It takes focus, and
it goes through the image's notification queue — which is drained only while the info bar is
running, so a message sent while the channel list is open **waits until the list is closed**. For a
„the washing machine has finished" notification that is the wrong shape twice over.

The plugin gets its own non-modal screen, instantiated as a dialog it never executes:

- **Top-right, auto-hide only.** Default 5 seconds, 1–30 per message.
- **It never becomes the current dialog and binds no action map**, so it cannot take focus, cannot
  swallow a key press and cannot be dismissed by the remote. It also cannot wait behind anything,
  because it never enters the notification queue.
- **Newest replaces current**: the same screen takes the new text and restarts its timer. A toast
  that queues is a message box with extra steps.
- **Torn down on standby and on shutdown.** Text capped at 200 characters.
- `cmd/message` gains an optional `style`, `"popup"` (**default — exactly today's behaviour**) or
  `"toast"`. Existing payloads are unaffected. For a toast the timeout is clamped to 1–30 and
  defaults to 5; the popup's „0 = until dismissed" has no meaning for something that auto-hides and
  is refused. `type` is accepted and **ignored** for toasts.
- **Capability `toast`**, claimed only once the screen has actually instantiated — an image where
  that fails keeps popups rather than gaining a style that silently does nothing.
- **Setting `osd_toast`**, default on, not remotely writable: it is the kill-switch for a screen
  that lives inside the GUI process, and a kill-switch reachable over the broker is not one.

**Why `type` is ignored.** A toast has one fixed appearance and always carries the plugin's own
label. That is what stops a message being dressed up as a system dialog asking for something. It
cannot be mistaken for one that is waiting for an answer either, because it does not wait.

### 3. Restarting the softcam, manually and optionally by itself — 0.3.0, first

**The problem.** The household symptom is a channel that stops decoding. Underneath it, an image's
softcam manager can start the cam binary directly rather than through an init script, and its
liveness check can fail in a way that starts **another** one on every interface restart — leaving
several independent instances running and none of them working. So „restart the softcam" cannot
mean „run the init script", and it cannot mean „restart the process", because there is not one.

- **`cmd/softcam_restart`**, payload `PRESS` by convention.
- 🔴 **The cam is resolved on the receiver and never named over MQTT.** The plugin reads which cam
  the *image* has selected for autostart — generic across OSCam, NCam and CCcam — and restarts that
  one. **No part of the command line comes from the payload.** A binary name arriving over a broker
  would be remote code execution with extra steps.
- **Sequence**: stop every running instance of that binary, wait up to 5 seconds for them to go,
  then start exactly one. Through `eConsoleAppContainer`, with a fixed command line, off the main
  loop.
- **Permission `softcam_restart_allowed`**, default **off**, **not remotely settable**.
- **Guards**: refused while recording; manual restarts rate-limited to one per minute, with the
  refusal as a sentence on `last_error`.
- **Opt-in auto-heal**: `softcam_autoheal` (default off) and `softcam_autoheal_seconds` (default
  90, range 30–600), **both remotely writable**. When the current service is encrypted and has not
  been decoding for that long, while the receiver is on and not recording, the plugin performs one
  clean restart — **at most one per ten minutes**, whatever the detector says.
- 🔴 **The decode signal is read internally even when CAM telemetry publishing is off.** Reading the
  receiver's own ECM state to decide whether to heal is not the same as putting what is on the
  television onto the broker, and a repair feature must not require a privacy switch to be turned
  on.
- **New retained topic `softcam`**, and **capability `softcam`**, claimed only when the selected cam
  could actually be resolved.

**Why the permission is box-only and the tuning is not.** This is the general rule this project now
follows: **a setting that enables a command is box-only; a setting that tunes a command already
permitted may be remote.** Enabling auto-heal on a receiver whose owner has not permitted softcam
restarts at all does nothing, because the permission is the gate.

### 4. Running the EPG importer on demand — 0.3.0

**The problem.** Images ship an EPG importer that runs on its own schedule. After a channel change
or a fresh install the guide stays thin until the receiver decides otherwise, and nothing could ask
it.

- **`cmd/epg_import`**, payload `PRESS`. **Permission `epg_import_allowed`**, default **off**, not
  remotely settable — an import is minutes of work on a small machine.
- **Guards**: refused while recording, and refused while an import is already running.
- Runs off the main loop. The concrete entry point is **resolved on the receiver**; the plugin
  hard-codes none and takes nothing from MQTT, and **capability `epg_import`** is claimed only when
  one was found. An image without an importer gets no capability and no command.
- On success the EPG grid is rebuilt and republished, as `cmd/epg_grid` does.
- **New retained topic `epg_import`** carrying the state, the timestamps, and — when it fails — a
  sentence written for the person who will read it.

### 5. An opt-in workaround for a CEC standby defect — 0.3.0, second

**The problem.** This one is **upstream enigma2, not this plugin.** A standby requested by the
television over HDMI-CEC is *queued* as a notification, and that queue is drained only while the
info bar is executing. With the channel list open, the standby waits — and fires when the list
closes. Worse, the flag that marks „this standby came from the television" is set and cleared
around the *queueing* call rather than around the execution, so the late standby is treated as
locally originated and is **echoed back to the television**, which then switches itself off.

- **Setting `cec_standby_workaround`**, default **off**, not remotely writable.
- When a standby notification is queued while the current dialog is a **channel-list** screen, close
  that dialog so the queued standby proceeds. And when the television reports power-on while a
  stale standby is still queued, drop it.
- 🔴 **A strict allowlist of one screen class, checked against the dialog's class and its bases.**
  Never an EPG screen, a menu, the plugin browser, an input box or a recording dialog. A screen the
  plugin does not recognise is left alone and the fact is logged at debug. Closing a dialog somebody
  is typing in would be a worse defect than the one being fixed.
- Every hook wrapped, so an exception in this path cannot reach the GUI.
- **Capability `cec_workaround`**; every intervention logged at info and counted on a new retained
  **`cec`** topic, so „did it do anything, and how often" is answerable without reading a log.

**Why this is opt-in.** It is a workaround for somebody else's defect, it closes a screen under the
user's hands, and it is only correct on images that have the defect. Off by default is the only
honest default, and the setting is box-only because it is the kill-switch.

### 6. Arming Wake-on-LAN — 0.3.0

**The problem.** A receiver can report Wake-on-LAN as supported and have it **disabled** on the
interface. Deep standby is then one-way: the box can be sent to sleep and no magic packet will
bring it back. That is exactly why `deep_standby_allowed` defaults to off, and it is a thing the
plugin can see and fix rather than warn about.

- **Setting `wol_arm`**, default **off**, not remotely writable.
- With it on, the plugin arms the interface **at start** and **again immediately before deep
  standby** — the second time because some images reset the flag, and arming it once at boot is not
  evidence it is still armed hours later. Fixed command line, through `eConsoleAppContainer`, with
  the interface resolved on the receiver.
- **`info.wol`** reports `supported`, `armed` and the interface, **read back from the system**
  rather than assumed from the fact that a command was issued.
- 🔴 **Until the deep standby → magic packet drill passes on hardware, the README keeps its warning
  that deep standby may be one-way.** A setting that issues the right command is not a receiver
  that wakes up.

### 7. Process telemetry ships — 0.3.0

[ADR-0002](0002-scope-after-m0.md) proposed publishing what the enigma2 process costs, so that the
memory curve could be recorded rather than sampled by a person. **Decided: yes, in 0.3.0.** It is
already in review, with the `process` topic, the `process` capability and no setting — the topic
reveals nothing about what anybody is watching, so there is nothing to switch off.

## Planned contract changes

Everything above that touches the topic contract, in one table. `docs/TOPICS.md` carries the same
list in a **Planned (not implemented yet)** section, with the payload fields and their types, so
that the contract keeps one home and a consumer can be written against it before it exists.

| Release | Addition | Kind | Capability | Notes |
|---|---|---|---|---|
| 0.2.0 | `info.settings.deep_standby_allowed` | read-only member | — | **On `main` since 2026-09-21; unreleased.** Not writable by `cmd/config` or the status page; changes what `info.settings` means |
| 0.3.0 | `cmd/message` optional `style`: `popup` \| `toast` | command field | `toast` | `popup` is the default and is today's behaviour |
| 0.3.0 | `cmd/softcam_restart` | command | `softcam` | Permission `softcam_restart_allowed`; refused while recording; 1/min |
| 0.3.0 | `softcam` | retained topic | `softcam` | `{selected, running_instances, last_restart, last_restart_reason, restarts_today}` |
| 0.3.0 | `info.settings.softcam_autoheal`, `…_autoheal_seconds` | writable members | `softcam` | The two new members of the `cmd/config` allowlist |
| 0.3.0 | `info.settings.softcam_restart_allowed`, `…epg_import_allowed` | read-only members | — | Permissions, so a consumer can hide what the box will refuse |
| 0.3.0 | `cmd/epg_import` | command | `epg_import` | Permission `epg_import_allowed`; refused while recording or running |
| 0.3.0 | `epg_import` | retained topic | `epg_import` | `{state, started, finished, error}` |
| 0.3.0 | `cec` | retained topic | `cec_workaround` | `{last_intervention, kind, count}` |
| 0.3.0 | `info.wol` | object | — | `{supported, armed, iface}` |
| 0.3.0 | `process` | retained topic | `process` | Already in review |

Nothing is removed, nothing changes shape, and every rule the contract already sets still holds:
state retained at QoS 0, commands at QoS 1 never retained and discarded when they arrive retained,
`null` for absent values, epoch seconds throughout, and every command verified by effect with
refusals on `last_error`.

## Consequences

- **`info.settings` stops being a synonym for „what `cmd/config` accepts".** That is a real
  documentation hazard, and it is answered by marking the read-only members in `docs/TOPICS.md`
  rather than by hoping. A consumer that writes back everything it reads there will be refused.
- **Two of the 0.3.0 features run inside the GUI process**: a screen the plugin owns, and a hook
  that closes a screen the user is looking at. Both are opt-in, both are governed by a box-only
  setting that is their kill-switch, the dialog-closing one acts on an allowlist of exactly one
  class, and every hook is wrapped. This is the risk this project takes most seriously, because the
  worst thing it can do to somebody is make their television unusable.
- **Auto-heal restarts something on a timer**, which is a shape that can loop. It is off by default,
  limited to one restart per ten minutes, and every restart is counted on a topic — so a loop shows
  up on a dashboard rather than only in a log.
- **Three permissions now sit outside `cmd/config`**, and the rule that put them there is written
  down: a setting that enables a command is box-only; a setting that tunes a permitted command may
  be remote. New settings get sorted by that rule rather than case by case.
- **The capability list grows by five** (`toast`, `softcam`, `epg_import`, `cec_workaround`,
  `process`), and every one of them is claimed only when the thing behind it actually worked on
  that receiver — a resolved cam, an instantiated screen, an importer that was found. That is the
  same promise `capabilities` has always made and it is why the new features can be optional
  without a consumer guessing.

## Not yet done

- **None of this is implemented.** This record is the plan; the specification an implementer works
  from is the one in the private blueprint that ADR-0000 was drawn from, and it is reproduced in
  the contract table above for everything that is public.
- **0.2.0 is still gated on the guided installer running end to end on hardware**, which has never
  happened ([ADR-0002](0002-scope-after-m0.md)).
- **There is still no 0.2.0 release**, and both halves still report `0.1.0`.
- **Everything here will be verified on one receiver and one image.** Every hook is wrapped and
  every feature is behind a capability that has to bind, which is what keeps that honest — but a
  second image is still the thing this project most needs.

## Found since, 2026-09-22

Recorded here rather than in a new record, because each is a consequence of something decided
above and none of them reverses a decision.

- 🔴 **A deliberate disconnect suppresses the last will, and `Bridge.reload()` performs one.** A
  clean MQTT disconnect is a goodbye, so the broker does **not** publish the retained last-will
  payload. 🔴 **`Bridge.stop()` already knows this and publishes a retained `offline` before its
  own clean disconnect — `reload()` does not.** That asymmetry is the whole defect and the whole
  fix. `reload()` — which the setup screen calls after every save — stops the client cleanly and
  then starts a new session; if that reconnect fails, retained `availability` stays **`online`
  with nothing connected**, and every consumer believes the receiver is there. Publish `offline`
  **explicitly before** any deliberate disconnect, so that the retained state is true whether or
  not the reconnect succeeds. This is the retained-availability trap approached from the side
  nobody watches: the familiar version is a last will that fires and is never cleared, and this is
  a last will that never fires at all. It is a live defect rather than a decision, it predates
  this record, and it is **to be tracked as an issue** — this bullet is a pointer, not its home.
- **The companion integration's update entity compares version strings**, so a receiver running an
  earlier development build of the same version is never offered a newer one. Comparing the built
  commit instead would need this plugin to publish a build identifier alongside its version — a
  contract addition, and one nobody has asked for yet. Noted so that the option is not rediscovered
  from scratch.
- **The OpenWebif status page shows only the writable settings.** The read-only permission above is
  deliberately not surfaced there; the page is for changing things, and the permission is set on
  the setup screen or in the provisioning file.

## Amendment, 2026-09-22 — removal has to remove the plugin

**Observed.** `opkg remove` left the plugin loadable. It deletes the files it installed, which are
the `.py` ones; the `.pyc` files beside them are written by the image after the install and are in
nobody's file list. On an OpenViX 6.6 receiver forty files survived the removal, in the legacy
same-directory form that Python 3 imports without a source next to it, and enigma2's plugin loader
enumerates by module name — so the next GUI restart loaded the removed plugin and it reconnected to
the broker while `opkg status` said nothing was installed. The compiled OpenWebif hook in
`WebChilds/External/` survived the same way. This is the removal-side twin of the upgrade orphan
that `postinst` already sweeps, and it was missed because the sweep was designed against the
upgrade, which is the case that had actually gone wrong on hardware.

**Decision.** `CONTROL/prerm` sweeps bytecode, under the guards `postinst` already establishes: the
plugin directory's basename is checked before and after resolution, no symlink is followed, no
mount is crossed, and every path is checked to be under the resolved root before anything happens
to it, because a directory name may contain a newline. It deletes `.pyc` and `.pyo` and nothing
else — the `.py` files are opkg's and are all still on disk while it runs — and then removes the
directories it leaves empty, deepest first, the plugin directory last.

Three things are deliberately not shared with `postinst`, and each is a consequence of the
difference between an upgrade and a removal:

- **It sweeps only when opkg says `remove`** (or when it is run by hand with no argument at all).
  The removal half of an upgrade is `postinst`'s, after the new tree is unpacked, which is the only
  moment at which a compiled file without a source is an orphan rather than one the new package is
  about to reuse. An argument this script does not recognise is treated as an upgrade, because that
  is the half of the guess that leaves files alone.
- **A symlinked plugin directory is refused rather than resolved.** `postinst` only ever deletes
  files, so sweeping through a link is safe; this removes directories, and the last of them is the
  plugin directory itself.
- **A directory that was already empty is removed too.** During an upgrade an empty `screenshots/`
  is one the plugin means to keep; during a removal the plugin is going away.

**What it still does not do.** It does not touch `/etc/enigma2/settings`, so a reinstall finds its
configuration, and it does not retract retained topics — that is `cmd/reset`, published while the
plugin is still connected, and a package script has no business making network calls.
