# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dates are release
dates. An `Unreleased` section is always present; `tools/build-ipk.sh` refuses to build a
version that has no section here.

## [Unreleased]

### Added

- **What the enigma2 process costs**, on `process`: resident set, its high-water mark, threads,
  open file descriptors and the epoch second the process started, read from `/proc`. It answers the
  question a box that is never restarted eventually raises — „is it leaking?" — which cannot be
  answered by looking once, only by a curve somebody's recorder already has. Published in the
  snapshot on every connect and then every 300 seconds — a ceiling on the gap rather than a
  heartbeat, because like every state topic an unchanged payload is not republished — plus early
  whenever the resident set moves by 4 MiB either way, so a jump is on the curve at the minute it
  happened. The numbers are the
  process's and not the plugin's: enigma2 is one process and nothing in `/proc` can attribute a
  kilobyte to any of the things sharing it. Five diagnostic sensors in discovery mode, of which the
  resident set is the only one enabled by default. There is no setting — the topic reveals nothing
  about what anybody is watching — and unlike every other poll here it runs on the main loop,
  because procfs is memory and cannot block.
- **`cmd/softcam_restart`, and an opt-in automatic restart behind the same permission.** The
  household symptom is a channel that stops decoding; underneath it, an image whose softcam
  manager starts the bare binary can leave several copies of the cam running and none of them
  working. So the command is not „run the init script" and not „restart the process": it
  **collapses the running copies to exactly one**, signalling every instance of the binary the
  image selected, waiting up to five seconds, killing what is left, and starting one with the
  image's own command line. 🔴 The binary is resolved on the receiver and **no part of the command
  line comes from the payload**.
- **A new retained `softcam` topic and capability**, carrying the selected binary, how many
  instances are running, the last restart and its reason, the count since local midnight, and two
  read-only facts about the image: whether its own liveness check will add a copy at every
  interface start on this receiver, and its periodic check interval when that is switched on.
  Those two are the difference between a receiver that needs this feature and one that does not,
  and they save a diagnosis that would otherwise need an SSH session on somebody else's box.
- **`softcam_restart_allowed`**, a box-only permission echoed read-only in `info.settings` beside
  `deep_standby_allowed`, and **`softcam_autoheal`** with **`softcam_autoheal_seconds`** (default
  90, range 30–600), which are writable through `cmd/config` because they only tune a restart the
  receiver has already permitted.
- **An opt-in workaround for a standby the television asks for and the receiver sits on**,
  behind the box-only setting `cec_standby_workaround` (off by default, and not in `info.settings`
  or `cmd/config`). The defect is upstream enigma2's: a standby requested over HDMI-CEC is queued,
  only the info bar carries the queue out, so with the channel list open the receiver stays on —
  and when the list is finally closed it goes to standby and sends `<Standby>` back to the
  television, because the image forgot the standby was the television's. With the setting on, the
  plugin closes the **channel list and nothing else** through its own exit, holding the image's
  „this came from the television" marker set until the standby has happened (at most five
  seconds) so it is not echoed; and a television standby still waiting after thirty seconds behind
  any other screen is dropped from the queue rather than left to fire later. 🔴 A standby the
  household asked for is never touched: `cmd/power standby` queues the identical notification, so
  the television's is identified at the moment it is queued and kept by identity (the remote's
  power button opens the standby screen directly and never reaches the queue at all). The
  workaround is active only when the receiver has HDMI-CEC switched on and is set to follow the
  television into standby. New capability `cec_workaround` and retained topic `cec`
  (`last_intervention`, `kind`, `count`, `pending`), retracted when the workaround is switched off;
  each television standby counts at most once. The decisions are in
  [ADR-0007](docs/adr/0007-cec-standby-workaround.md).
- **A discreet toast, as a second message style.** `cmd/message` takes an optional
  `"style": "toast"`: a small box in the top-right corner, headed „MQTT Bridge", that hides itself
  after 5 seconds (1–30 on request), never takes focus, never waits behind the channel list the way
  the popup does, and is replaced by the next toast rather than queued. It holds two text labels and
  nothing else, because some of enigma2's widgets bind keys natively without being executed and a
  toast built from one would eat the channel list's arrow keys. It is hidden when the receiver enters
  standby, a toast sent in standby is refused on `last_error`, and it is deleted — never merely
  closed — whenever the plugin stops. Its text is capped at 200 characters after **every backslash is
  removed** (enigma2 applies its colour escapes after right-to-left reordering, so no narrower rule
  holds); `type` is validated as for a popup and then ignored. New capability `toast`, claimed only
  once the screen has been created, and the box-only setting `osd_toast` (on by default, not in
  `info.settings` or `cmd/config`). **Payloads without `style` behave exactly as before.** The
  decisions are in [ADR-0008](docs/adr/0008-discreet-toast.md).

### Changed

- **`epg_grid/<bouquet_slug>.generated` now means when the grid last changed**, not when it was
  last built, which follows from the fix below: an unchanged rebuild publishes nothing, so the
  retained stamp stays where the last real change left it. `docs/TOPICS.md` says so, and the
  reasoning is in [ADR-0006](docs/adr/0006-volatile-fields-and-publish-on-change.md).

### Fixed

- **„Next timer" has been unreadable in discovery mode since 0.2.0.** Its value template appended
  `+00:00` to `timestamp_utc`, which already ends in the offset, so the state arrived as
  `2026-09-10T12:08:29+00:00+00:00`. Home Assistant cannot parse that: it logs „Invalid state
  message" and stores nothing, so the sensor read unknown for ever rather than reading wrong. One
  character in one template. It was found by the review of the process telemetry above, which had
  copied the same shape from it — and it was invisible to both test suites because they compared
  the template as a string and never rendered it. The two timestamp templates are now rendered in
  the tests and the result is parsed as a datetime.
- **An EPG grid that has not changed is no longer republished.** The contract has always said it
  was not, and it was not true: every payload carries `generated`, stamped from the clock at the
  moment the grid was built, and the change comparison is made on the encoded payload — so two
  builds a second apart differed by those bytes alone. The refresh runs every quarter of an hour,
  so a bouquet whose programmes did not move in that window — one carrying no EPG at all, or any
  bouquet overnight — rewrote its retained topic for nothing, delivering a state change to every
  consumer and a row to every recorder. A field a payload stamps from the wall clock now takes no
  part in the change comparison; it is still published, so nothing a consumer reads has moved.

### Notes
- **An instance is not a process.** A cam that forks a supervisor to keep its worker shows two
  processes for one instance, so `running_instances` counts matched processes whose parent is not
  itself matched — a plain process count reports a fault on a healthy receiver. A process is
  matched on its `comm` **and** on `/proc/<pid>/exe`: the kernel keeps only fifteen characters of
  a command name, so two binaries differing after the fifteenth are otherwise indistinguishable.
  `pgrep -f` is not used anywhere, because it matches the shell running the search.
- **The automatic restart uses the whole recording guard**, exactly as a manual one does: refused
  while recording, with a timer due within ten minutes, and when the image will not say. A restart
  landing close to a timer risks the opening seconds of the recording, and a scrambled recording
  is recoverable while a truncated one is not.
- **The decode signal is read even with `cam_telemetry` off** — a repair must not require a
  privacy switch to be turned on — and only the modification time of `/tmp/ecm.info` is ever read.
  Nothing else in that file reaches the broker, `last_error`, a log line or a diagnostic.
  Absence is read as age rather than as a fault, because the cam removes the file when it stops
  descrambling and on a free-to-air channel that is the healthy state.
- **A restart is refused for the first sixty seconds after the plugin starts.** The image's own
  check fires about a second after every interface start, and restarting inside that window races
  a copy already on its way.
- **The autostart setting holds absolute paths**, and the prefix is stripped where the setting is
  read — the same place the image's own manager strips it — so that nothing downstream ever sees a
  path. An entry pointing outside the softcam directory keeps its separators and is refused,
  rather than being rebased onto that directory and started as a different program.
- **An upgraded cam is still counted.** Once `opkg` replaces the binary, every copy already
  running reads `…/<name> (deleted)` from its `exe` link; that marker is removed before the
  comparison, so the copies an upgrade left behind are exactly the ones the button can collapse.
- **The restart is refused rather than half-performed when the image will not give it a timer.**
  The sequence is armed before anything is signalled, so the one failure that would otherwise end
  with the cam stopped, nothing started and every later attempt answering „a restart is already
  running" is now an ordinary refusal that changed nothing.

### Documentation

- **M4 is ticked in the roadmap.** The guided installer has been run end to end on a receiver that
  did not have the plugin, and its rollback exercised for real: a deliberately wrong broker
  password, the plugin refused, the receiver restored to the byte, the lock released, and „the
  receiver was restored" reported because it had been checked rather than assumed. The correct run
  then ended on the success screen. The four defects the earlier runs found — a pending discovery
  offer blocking the installer, a lost success screen, a rollback that misjudged the restart and
  left its lock behind, and a receiver at default settings refused as „different" — are fixed. The
  installer lives in the companion integration, so nothing in this package changed; the roadmap
  here was simply still describing a milestone as unproven.
- **The companion integration's 0.2.0 is released**, so the README no longer says it „follows"
  this one. Both halves of the pair are out: the compatibility table marks 0.2.0 as current on
  both sides, M3 is ticked, and the paragraph under the milestone list says everything after M4 is
  unreleased rather than everything after M2. M2's own open items — the long passive soak and the
  deep-standby drill — are unchanged, and nothing in this package changed.

## [0.2.0] - 2026-09-22

The release that makes the box useful: everything it is doing, on the broker, and everything it
can be asked to do, answered as state rather than as a return code.

0.1.0 was the session and the identity. This one fills in the feature areas it promised —
`power`, `service`, `epg`, `tuner`, `recording`, `timers`, `volume`, `hdd`, `key` and `screen`,
each driven from the enigma2 hook that knows about it rather than from a poll — and adds the
channel list, active bouquet context, a per-bouquet EPG grid, and every command in the contract
with its guard. `info.capabilities` is no longer empty: it names the feature areas that actually
bound on this box, so a consumer hides what is missing instead of offering a control nothing will
ever update.

Commands are verified by effect. The plugin reads the resulting state back rather than trusting a
return value, and a refusal is a sentence on `last_error` written for the person who will read it
— which is also how a consumer tells „the box refused this" from „the box cannot do this", now
that `info.settings` echoes the read-only `deep_standby_allowed` permission.

Three things are optional and off by default, because they are the ones worth thinking about:
screenshots, remote-key reporting, and the conditional-access and OSCam telemetry. They can be
switched on from the broker through `cmd/config`, deliberately — the companion integration's
options flow is built on that path — which makes the broker login and its ACL the privacy
boundary, and the README and `docs/TOPICS.md` both say so in as many words. The broker address
and credentials, the node identity, the topic names and the destructive-command permission are
not in that subset and cannot be changed from the broker at all.

🔴 **`info.settings` no longer means „the remotely writable subset".** It is what a consumer may
read, of which the writable keys are the `cmd/config` allowlist and nothing else. A client that
writes back everything it reads loses the settings it did mean to change.

Removing the package now removes the plugin. `opkg remove` deletes the `.py` files it installed
and leaves the `.pyc` files the receiver compiled afterwards; on an OpenViX 6.6 box forty of them
survived a removal, and because Python 3 imports a legacy-location `.pyc` with no source beside
it, the next graphical-interface restart loaded the plugin that had just been removed and it
reconnected to the broker. The maintainer scripts now sweep compiled bytecode on a removal and on
an upgrade.

Tested on a Vu+ Uno 4K SE running OpenViX 6.6: the by-effect checklist and a 60-minute active
soak have passed. The long passive soak, the watchdog-restart interplay and deep standby with
Wake-on-LAN have not been drilled. The code uses no syntax above Python 3.9 and is tested on 3.9,
3.12 and 3.14.

### Added

- **The receiver's state, on the broker.** `power`, `service`, `epg`, `tuner`, `recording`,
  `timers`, `volume`, `hdd`, `key` and `screen` are published as `docs/TOPICS.md` describes them,
  each from the enigma2 hook that knows about it rather than from a poll: standby from the standby
  counter and the standby screen closing, the service and its programme from `session.nav.event`,
  recordings from the record events and from a wrapper around the call enigma2 makes after every
  change to its timer list, and the volume from wrappers on the receiver's own volume control plus
  a five-second reconciliation that catches whatever changed it from somewhere else.
- **`channels`**, the configured bouquets and the services in them — the list a channel selector is
  built from and the one `cmd/zap` by name resolves against. Rebuilt when a bouquet file changes,
  which is a modification-time comparison once a minute because enigma2 offers no event for it.
- **Active bouquet context** on `bouquet` plus guarded `cmd/bouquet`: selecting a published TV
  bouquet changes the receiver's real channel-up/down list, preserves the current service when it
  belongs there, and otherwise tunes the first playable channel.
- **`epg_grid/<bouquet_slug>`**, one retained topic per configured bouquet, with the next few
  events on every channel in it. Built one bouquet per turn of the main loop, and the time each one
  took is logged.
- **Every command in the contract**: `power`, `deep_standby`, `reboot`, `restart_gui`, `zap`,
  `volume`, `mute`, `key`, `message`, `timer`, `record`, `screenshot`, `epg_grid`, and `discovery`
  now republishes the channel list as well. Each is verified by effect — the plugin reads the
  resulting state back rather than trusting a return value — and each refusal is a sentence on
  `last_error` written for the person who will read it.
- **Home Assistant discovery**: one device payload with nineteen components and eight device
  triggers for the colour keys. A component whose capability is missing is not announced, and one
  that was announced before and is not now is removed by name.
- **Capability detection that is worth reading.** `info.capabilities` lists the feature areas that
  actually bound on this box, so a consumer hides what is missing instead of showing an entity
  nothing will ever update.
- **Settings a consumer may change**, through a fail-closed `cmd/config`: `publish_keys`,
  `screenshot`, `screenshot_interval`, and the backward-compatible optional `screenshot_delay`,
  `cam_telemetry` and `oscam_telemetry`. The complete replacement is validated and persisted in one
  step, the affected hooks and timers are rebound, and the acknowledgement is the same non-secret
  values coming back in `info.settings`. The broker address and credentials, the node identity,
  the topic names, the bouquet filter, logging and destructive-command permission are not in that
  subset and cannot be changed from the broker at all. What *is* in it includes the privacy
  switches — screenshots, key reporting and both telemetry options — so a client allowed to
  publish on `cmd/config` can switch them on, deliberately and by design, because the companion
  integration's options flow is built on this path. The broker login and its ACL are therefore the
  privacy boundary; `docs/TOPICS.md` and the README both say so in as many words.
- **A status page in the box's own web interface**, under OpenWebif: what the bridge is connected
  to, the same publisher settings the remote can change, and the tail of the plugin's log with the
  broker password and every other credential scrubbed out of it. Writes are authenticated by
  OpenWebif and carry a one-shot token, and the log viewer is bounded in both bytes and lines.
- **Optional conditional-access telemetry** on `cam`: the current encryption flag, a generic
  allowlisted CA system name and bounded ECM timing. Off by default, and it excludes reader,
  server, user, card and raw ECM data.
- **Optional OSCam health** on `oscam`, read from the receiver's own loopback interface: whether
  the software and its API are up, bounded aggregate counts, and one neutral entry per reader or
  server keyed by a salted opaque id. WebIf credentials, reader labels, addresses and card
  identifiers never leave the box, and switching the telemetry off retracts the retained topic.
- **`info.settings` echoes `deep_standby_allowed`**, the box-only permission that decides whether
  `cmd/deep_standby` and `cmd/reboot` are obeyed at all. It is always present and always
  read-only: `cmd/config` refuses it like any other key outside its allowlist, and it is granted
  on the receiver's own setup screen or in the provisioning file at first install — never over
  MQTT and never from the OpenWebif page. A consumer can now hide the two buttons the box would
  refuse instead of offering controls that always fail, and the fresh `info` that a save publishes
  is how it learns the permission was granted.

### Changed

- 🔴 **`info.settings` no longer means „the remotely writable subset".** It is the non-secret
  settings a consumer may **read**, of which the writable ones are the `cmd/config` allowlist and
  nothing else. Writability must not be inferred from presence — an unknown key fails the whole
  object, so a client that writes back everything it reads loses the settings it did mean to
  change. `docs/TOPICS.md` §1 names which members are read-only.
- On-zap screenshots now wait a configurable four seconds by default. Rapid channel changes reset
  the wait, and a capture still completing for an older channel is discarded and rescheduled.
- A state topic is published **only when it has changed**. The snapshot on every connect is the
  deliberate exception, because a broker that lost its retained store has to be told everything.
- Every successful `cmd/screenshot` now publishes a fresh `screen` event even when its JPEG is
  byte-identical to the previous capture. Asynchronous capture failures report `last_error` for
  commanded screenshots, while automatic captures only log their failure.
- Volume hooks now retry for a bounded five-second startup window while images such as OpenViX
  create their `VolumeControl` singleton, so remote and OpenWebif button changes publish
  immediately instead of waiting for the five-second reconciliation.
- Remote-key events classify a physical hold as `long` from its repeat duration when an image
  omits the synthetic long marker, while still emitting one event and never swallowing the key.
- Recording-disk probes now run outside the receiver's main loop, so an unavailable network mount
  cannot freeze the user interface; unresolved startup probes no longer report a false disk loss.
- MQTT reconnects now log privacy-safe epochs, main-loop dispatch delay and backlog aggregates,
  plus per-publisher and total snapshot timings for diagnosing receiver stalls.
- The event-loop monitor now reports both watcher-observed stalls and the measured heartbeat gap
  when native code resumes before the watcher could run.
- EPG grids now build in bounded four-channel batches between main-loop turns, retaining the last
  complete grid until its replacement is ready instead of freezing the interface on a bouquet.
- A publisher that is stopped now hands its enigma2 objects back: a `Ticker` takes its callback
  off the timer and drops it, and the screenshot publisher takes its callback off a console
  container once that container's program has ended. Both were append-without-remove, which is
  the shape that keeps an object reachable from enigma2's side for no reason; measurement found
  no growth from either, and this is tidiness rather than a fix for anything observed.
- `docs/SETUP.md` and `docs/TROUBLESHOOTING.md` record what a screenshot actually costs: about
  22 kB of enigma2's memory per capture, permanently, whoever takes it — the image's own `grab`,
  not this plugin — with the measurement behind the number and the advice for a receiver that is
  never restarted.
- `docs/TOPICS.md` gained the `channels`, `cam` and `oscam` topics, the names those areas add to
  the capability vocabulary, the discovery entity table, and the four things about Home Assistant
  2026.9 that were measured rather than assumed — `default_entity_id` in place of `object_id`,
  removal by platform key, QoS 1 for commands, and shared availability.

### Fixed

- Removing the package now removes the plugin. `opkg remove` deletes the files it installed, which
  are the `.py` ones; the `.pyc` files beside them were written by the receiver after the install
  and are in nobody's file list. On an OpenViX 6.6 receiver forty of them survived a removal — the
  whole plugin, still compiled, in the legacy same-directory form that Python 3 imports on its own
  — and because enigma2's plugin loader enumerates by module name, the next GUI restart loaded the
  plugin that had just been removed and it reconnected to the broker while `opkg status` said
  nothing was installed. `prerm` now deletes every `.pyc` and `.pyo` under the plugin directory and
  the compiled OpenWebif hook beside it, then removes every directory it leaves empty, deepest
  first and the plugin directory last. It sweeps on a removal only — the removal half of an upgrade
  is `postinst`'s, after the new tree is unpacked, which is the only moment at which a compiled
  file with no source is an orphan rather than one the new package is about to reuse. It deletes no
  `.py` and nothing else that is not bytecode; a file of your own keeps the plugin directory and is
  counted in a line of output; a plugin directory that is a symlink is refused untouched, because
  unlike the upgrade sweep this one removes directories; `External/` is OpenWebif's and is never
  enumerated; the settings in `/etc/enigma2/settings` are not read or written, so a reinstall still
  finds its configuration; and nothing in it can fail an `opkg remove`.
- An upgrade no longer leaves a removed module behind as importable bytecode. The image
  byte-compiles the plugin after opkg has installed it, so the `.pyc` files are not in opkg's file
  list and opkg — which removes only what it installed — leaves them. Moving `paho/` under
  `_vendor/` took the sources away and left the compiled copies, and in Python 3 a legacy-location
  `.pyc` with no `.py` beside it is still importable, so the old module survived the upgrade meant
  to remove it. `postinst` now deletes every `.pyc` and `.pyo` in the plugin directory whose source
  is gone, in both the same-directory and the `__pycache__` form, and gives back the directories
  that sweep emptied — walking upward from each deleted file and stopping at the first directory
  that still holds something, so one that was already empty before the upgrade is left alone. It
  keeps every compiled file whose source is present; it checks that every path is under the plugin
  directory before touching it, because a directory name may contain a newline and a line-by-line
  read would otherwise hand the second half of one to `rm` as a path relative to a working
  directory `opkg` never set; it follows no symlink and does not walk into a different filesystem
  mounted under the plugin directory; it does nothing at all in
  a tree with no `plugin.py`, which is a build-time packaging rather than an orphaned tree, or
  during an offline rootfs build, where its absolute paths would be the build host's; and nothing
  in it can fail an install.
- A feature that is switched off in the settings no longer says the image could not provide its
  hooks. `cam`, `oscam`, `keys`, `screenshot` and `epg_grid` each have an off switch, and a log
  line blaming the receiver for a choice somebody made is a wrong answer to the question the
  reader is asking.
- `bouquet_context` is claimed only once the receiver's own service list has actually been read.
  A box whose channel list the plugin never gets to see used to announce the capability anyway,
  which promised a consumer a `bouquet` topic and a working `cmd/bouquet` it would never get. The
  list may still appear seconds after the plugin connects; when it does, `info` and the
  announcement are published again with the capability in them, and an image that has not offered
  one after a minute is checked once a minute from then on rather than given up on.
- A receiver that is not in any configured bouquet — on the radio list, in the movie list, or in a
  bouquet the filter leaves out — publishes `bouquet` with both fields null instead of nothing at
  all. That is ordinary operation, and treating it as a missing hook used to retire the whole
  feature a few seconds after somebody opened the radio list.
- The OSCam publisher hands its probe slot back when it stops. Saving the setup screen replaces it
  with a new instance, and a slot still held by the retired one was telemetry that never came
  back. No more than two abandoned workers are left outstanding, so a listener that answers slowly
  for ever cannot accumulate threads.
- The status page in the web interface answers with its own failure page, logged, if building it
  raises — rather than handing OpenWebif a traceback to render — and it derives its icon URL from
  the request instead of assuming where OpenWebif mounted it.
- An omitted optional key in `cmd/config` takes its current value from the settings the result
  will be saved into, rather than from the module-global settings.
- A capture file left in `/tmp` by a plugin older than 0.2.0 is removed at start-up. Nothing else
  would ever have deleted it, including switching screenshots off.
- An OSCam version whose revision carries a suffix — `1.20_svn build r11718-079`, which is what a
  receiver running the current OpenViX build reports — is published instead of being dropped as
  unrecognised. The allowlist still refuses anything that is not a version.
- The status page answers on `/mqttbridge/` as well as `/mqttbridge`. The trailing slash resolves
  to an empty child in Twisted, and without one the receiver answered 404 to an ordinary URL.
- Selecting a bouquet now enters it under the root its channel list was read from. On a box with
  „multiple bouquets" switched off there is no bouquet list at all and everything is read from the
  favourites list, so entering `bouquets.tv` first built — and then persisted — a channel-list path
  the receiver does not use.
- An OSCam probe that stops answering is abandoned instead of holding the plugin's single probe
  slot forever. The deadline only ever governed reading the response body; a connect or an
  authentication exchange that hung froze the telemetry until the plugin was restarted. The
  abandoned worker's answer, whenever it arrives, is discarded.
- OSCam telemetry reports a listener that is not an HTTP server as unavailable, instead of relying
  on a broad `except` further out to make that true.
- The OSCam web-interface password is registered with the log scrubber whether or not the
  telemetry is switched on, so it cannot reach the log through a code path that runs anyway.

### Documentation

- **[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md) records that its first
  decision is implemented on `main`, and corrects one line of it.** `info.settings` carries
  `deep_standby_allowed` as a read-only member; the permission can be set on the setup screen **or
  in the provisioning file at first install**, and never over MQTT or from the status page — the
  record said „only on the setup screen". A consumer needs three states rather than two, because
  the announcement arrives before `info` and carries no settings at all. The record also gains a
  **Found since** section: a deliberate disconnect suppresses the last will, so `Bridge.reload()`
  can leave retained availability saying `online` with nothing connected, and the fix is to publish
  `offline` before any deliberate disconnect.
- **[ADR-0002](docs/adr/0002-scope-after-m0.md) records the scope added and changed after M0** —
  bouquet context, the optional CAM and OSCam telemetry, `cmd/config` and the privacy boundary it
  moves to the broker login, the post-zap screenshot delay, the OpenWebif status page, the runtime
  diagnostics, `channels`, and the reproducible build — each with why it exists and what it costs.
  It also states what was **not** done at the time: no 0.2.0 release, no call-for-testers issues,
  no sweep of `.pyc` files orphaned by an upgrade, and M2's long soak and the deep-standby drill
  open. This release closes the first and the third. The README's roadmap and its claim that a
  call-for-testers thread exists per image are corrected to match — testers are wanted, and
  opening the issue is the way to volunteer.
- **[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md) records the 0.2.0 and
  0.3.0 plan** that came out of two days of household use: a read-only `deep_standby_allowed` echo
  so a consumer can hide a control the box will refuse (0.2.0), and then the discreet toast, the
  softcam restart with its opt-in auto-heal, the EPG import, the opt-in CEC standby workaround,
  Wake-on-LAN arming and the process topic (0.3.0). Every contract addition — three topics, two
  commands, `cmd/message`'s `style`, the new `info` members — is written out in
  [docs/TOPICS.md](docs/TOPICS.md) under **Planned (not implemented yet)**, with the payload fields
  and their types, so the contract keeps one home and a consumer can be written against it before
  it exists. 🔴 One of those additions changes what `info.settings` means: presence there no longer
  implies that a setting is writable. **Only the `deep_standby_allowed` echo is implemented so
  far** — it has moved out of that section and into the contract — and the README's roadmap says
  so.
- **[ADR-0004](docs/adr/0004-remote-uninstall.md) records the remote uninstall**, and the roadmap
  and the contract gain it as a seventh 0.3.0 item. `cmd/uninstall` removes the plugin from the
  receiver on request — retracting every retained topic it owns, publishing a final `offline`,
  removing the package and restarting the interface, in that order, because after the package is
  gone there is nothing left to ask — behind a box-only `uninstall_allowed` permission echoed
  read-only in `info.settings`. Its payload is the node id, which confirms *which* receiver was
  meant rather than who is asking; the permission is the security boundary. 🔴 It is a one-way
  door: once it has run there is no plugin left to listen, so only SSH or the receiver's own
  package manager can put it back. The command, its guard and the permission are written out in
  [docs/TOPICS.md](docs/TOPICS.md) under **Planned (not implemented yet)**.

## [0.1.0] - 2026-09-16

The first release: a receiver that publishes what it is doing to MQTT, and stays out of the way.

Install it, point it at your broker, and the box appears on the broker within seconds of enigma2
starting — `availability` so you can tell a sleeping box from a broken one, `info` with the image,
the box type, its address and what the plugin found it could do, and an announcement a consumer
can discover it by. Commands come back on the same session: switch the Home Assistant mode, ask
it to re-announce itself, or retract every retained topic it owns and publish them again. There
is a setup screen under *Menu → Plugins → MQTT Bridge* for the broker details, and a provisioning
file for installing a box without touching a remote control.

It is deliberately quiet about what it cannot do. Bad configuration produces one line in the log
and an idle plugin, never a dialog and never a retry storm, because the graphical interface has to
come up whatever the broker is doing. The feature areas that publish live state — the channel, the
EPG, the tuner, recordings, volume, keys — arrive in 0.2.0; this release is the session, the
identity and the plumbing they hang off, and `capabilities` says so by being empty.

Tested on a Vu+ Uno 4K SE running OpenViX 6.6. The code uses no syntax above Python 3.9 and is
tested on 3.9, 3.12 and 3.14.

### Added

- Repository scaffold: the licence (GPL-2.0-or-later), `NOTICE` recording the vendored
  paho-mqtt 2.1.0 and its archive hash, the security policy, the contributor guide and the code
  of conduct.
- The topic contract in `docs/TOPICS.md` — every topic, its retain flag and QoS, every payload
  field with its type, every command with its guard — plus install, setup and troubleshooting
  guides.
- Architecture decision records in `docs/adr/`: the product requirements as ADR-0000, and
  ADR-0001 closing the three questions left open at M0 (the EPG grid ships in v1, both project
  documents stay in the operator's `integrations` book, and telnet-only boxes are told to enable
  SSH first).
- The IPK toolchain: `tools/build-ipk.sh` produces a reproducible
  `enigma2-plugin-extensions-mqttbridge_<version>_all.ipk` with its SHA-256 beside it, and
  `tools/make-feed.py` turns a directory of IPKs into an opkg feed index.
- Continuous integration: ruff and pyflakes, the unit tests on Python 3.9, 3.12 and 3.14, and an
  IPK build on every push and pull request; a release workflow that publishes the IPK and
  regenerates the opkg feed on a tag.
- The plugin package skeleton — `src/MQTTBridge/` with the single-sourced version and the
  vendored MQTT client.
- The runtime: the plugin loads under `WHERE_SESSIONSTART`, keeps one MQTT session with a
  retained last will, and on every connect publishes `availability`, `info` and the
  announcement on `enigma2mqtt/discovery/<node>/config` before subscribing to `cmd/#`. A clean
  shutdown publishes `offline` rather than leaving it to the will.
- Settings under `config.plugins.mqttbridge.*` — every key the contract names, including the
  ones later milestones will read — with a `ConfigListScreen` under *Menu → Plugins → MQTT
  Bridge* that shows whether the bridge is connected and which node id it is using.
- Provisioning: `/etc/enigma2/mqttbridge.json` is imported before the first connection and then
  deleted, because it holds a broker password in clear. A file that cannot be parsed is left
  alone and logged. Only the key names are ever logged, never the values.
- The node id is derived once from the box type and the last six digits of the MAC and then
  kept, so it survives a reinstall. The box type is taken from `boxbranding`, then
  `/proc/stb/info/boxtype`, then `/etc/image-version` — deliberately ahead of
  `/proc/stb/info/model`, which on a Vu+ Uno 4K SE running OpenViX 6.6 reads `dm8000`.
- `cmd/ha_mode` and `cmd/reset`, with `cmd/discovery` to republish the announcement. A command
  that arrives retained is discarded and logged; so is one over 4 KB. A refusal goes to
  `last_error`, which is cleared when a command next succeeds.
- Retained hygiene: every retained topic this node publishes is recorded in
  `/etc/enigma2/mqttbridge-state.json`, written atomically, so `cmd/reset` can retract topics
  published by an earlier run of the plugin.
- A capped log at `/home/root/mqttbridge.log` (1 MB, two rotations), falling back to `/tmp`
  when the rootfs will not take it. The broker password is scrubbed from every line at every
  level.
- Polish and German translations of everything the television shows, with the template in
  `src/MQTTBridge/locale/MQTTBridge.pot`. German is marked `# needs-review`.
- `tools/deploy-to-box.sh`: build, refuse while the receiver is recording or about to, copy
  with `scp -O`, back up the installed plugin outside `Extensions/`, install, optionally
  restart the GUI and wait for the receiver to answer again.
- Unit tests against a stub `enigma` module and a fake MQTT client, asserting the contract in
  `docs/TOPICS.md` rather than the implementation: retain flags, QoS, topic spelling, payload
  fields, the guards, and that the password never reaches the log.
- A weekly, non-blocking CI job that compares the vendored paho-mqtt against the latest 2.x on
  PyPI, so a copied-in dependency still gets told when upstream moves.

### Fixed

- Saving the setup screen, and shutting the receiver down, no longer wait for the MQTT session to
  close. A broker whose address answers nothing held the user interface for several seconds.
- Topics published under a previous node id or base topic are now retracted on the next connect,
  not only when the name is changed with a session open. Renaming a box that was switched off
  used to leave its old topics on the broker forever.
- A provisioning file from which nothing could be imported — every key misspelt — is kept and
  reported instead of being deleted with the settings it was meant to carry.
- On an image that offers no way to reach the main loop from a background thread, the plugin now
  says so and stays idle rather than running MQTT callbacks on the network thread.
- Starting the bridge twice leaves the session that is already open alone.
- The vendored MQTT client moved into its own `_vendor` directory, so the plugin's `config`,
  `log`, `setup` and `keys` modules no longer sit ahead of the standard library for every plugin
  in the enigma2 process.
- The outgoing MQTT queues are bounded, and a publish dropped because they are full is logged.
- The setup screen's status line refreshes as the fields are edited, and leaving it with unsaved
  changes now asks first.
- `last_error` caps the command name it echoes, so an absurd command topic cannot be stored
  whole on a retained topic.
- The translation template is no longer packaged into the IPK.
- `tools/deploy-to-box.sh` keeps the three most recent backups instead of every one ever made,
  never lets the provisioning file exist outside a 0700 directory, prints its last help line, and
  accepts a password file written on Windows.

### Changed

- Capabilities are the feature-area names from the topic contract and nothing else; a build with
  no feature area bound publishes an empty list.
- `info.enigma` is documented as enigma2's build-date string rather than a version number, which
  is what `getEnigmaVersionString()` returns on OE-Alliance images.
- The documentation names the retained-topic record correctly — `/etc/enigma2/mqttbridge-state.json`
  — and describes what it holds and what a reset does with it.
- The EPG grid is published as one retained topic per configured bouquet,
  `epg_grid/<bouquet_slug>`, and a bouquet that stops being configured has its topic retracted.
- `cmd/reset` republishes everything immediately after retracting it — availability, the state
  snapshot, the announcement and the discovery payloads — which is what makes it safe to run at
  any time rather than only before uninstalling.
- A command that arrives with the retain flag set is logged and discarded, never executed.
- The topic contract names the attributes the Home Assistant channel and programme sensors carry,
  states that the protocol is MQTT 3.1.1 with no MQTT 5 features, and gives `epg_grid_events` its
  default.
- The recorder-exclusion advice tells a plugin-only install apart from one running the companion
  integration: there is no key `event` entity to exclude without it, only `publish_keys`.
- The IPK is staged on a native filesystem and its file modes are written into the archive
  explicitly, so the package and its SHA-256 are the same whether it was built from a Windows
  checkout or a Linux one.
- A release runs the same lint, pyflakes and test suite as CI before it builds anything, so a tag
  on a commit CI never passed cannot publish; the opkg feed commit is now made by the workflow
  rather than under a maintainer's name and address.
- Release notes stop at the changelog's link-reference block instead of carrying it into the
  release body.
- The development tooling is pinned to exact versions.
- Examples throughout the documentation use the documentation MAC `00:00:5e:00:53:01` and the
  node id derived from it.

[Unreleased]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/compare/v0.1.0...v0.2.0
[0.1.0]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/tag/v0.1.0
