# The install transaction

Two programs change the plugin on a receiver: the companion Home Assistant integration's installer,
which works over SSH, and - planned in [ADR-0015](adr/0015-signed-self-update.md) - the plugin's own
update helper. They must never run at the same time, they must be able to undo each other's
unfinished work, and whichever restarts the receiver's interface must leave the household on the
channel it was watching. This file is the contract between the two: the names on the receiver's
disk, the lock and how it goes stale, the snapshot, the marker, and the restart rule. A change to
any name or rule here is a change to both repositories.

What is built and what is not. For the integration there are two states: **released 0.3.1**, which
is what every installer in the field does, and **integration main (unreleased, 0.4.0)**, which is
merged on the integration's `main` branch, has **not yet been run on a receiver**, is accepted on
hardware before the release that carries it, and is taken out again if that fails.

| Part | Integration's SSH installer | Plugin's self-update |
|---|---|---|
| The lock, its owner record and the stale rule (§2) | **released 0.3.1**; the owner record's `id` (§2.2): integration main (unreleased, 0.4.0) | planned, ADR-0015 |
| The heartbeat (§2.4) | not used | planned |
| The snapshot, schema 2 (§3) | **released 0.3.1** | planned, same layout |
| Recovering an abandoned transaction by its id (§3.3) | integration main (unreleased, 0.4.0) | planned |
| Restoring while the interface runs (§3.4) | integration main (unreleased, 0.4.0) | planned |
| The marker (§4) | not used | planned |
| The restart rule (§5) | integration main (unreleased, 0.4.0); **released 0.3.1 stops and starts the interface with `init 4` / `init 3` on every path** | planned |
| R2 as one detached script (§5.2) | integration main (unreleased, 0.4.0) | planned |
| Proof that the new plugin started (§6) | **released 0.3.1** (live `online`, `info` with the new version, a new enigma2 pid); unchanged on integration main | planned |
| Tests against the other program's released code (§2.5) | - | planned |

Paths are the ones on the receiver. `<id>` is twelve lowercase hexadecimal digits
(`secrets.token_hex(6)`), one per transaction; it names the snapshot, the self-update's transaction
directory, the marker's `id`, the lock owner's `id` and `update.transaction.id` in
[TOPICS.md](TOPICS.md#5-planned-not-implemented-yet).

---

## 1. Names on the receiver

| Path | Written by | What |
|---|---|---|
| `/home/root/mqttbridge-backups/` | both | The directory everything below lives in. Anything else a person puts here is theirs: neither program deletes a name it did not make |
| `/home/root/mqttbridge-backups/.ha-installer.lock/` | both | **The** lock, a directory (0700). The name is historical - the installer came first - and it is kept, because every released installer looks for exactly this name |
| `/home/root/mqttbridge-backups/ha-installer-<id>/` | the installer | Its snapshot, schema 2 (0700) |
| `/home/root/mqttbridge-backups/self-update-<id>/` | the self-update (planned) | Its snapshot, schema 2 (0700) |
| `/home/root/mqttbridge-backups/update-<id>/` | the self-update (planned) | The transaction directory (0700): `request.json` (0600), the copy of the helper that runs, `status.json`. Removed when the transaction commits |
| `/etc/enigma2/mqttbridge-update.json` | the self-update (planned) | The marker (0600), §4 |
| `/etc/enigma2/mqttbridge-index.json` | the plugin (planned) | What the receiver has accepted from the signed release index (0600) - see ADR-0015. **Kept on purpose** by `cmd/uninstall` and by a downgrade to 0.2.0 or 0.3.x, which never read it; a reflash or a factory reset starts it again |
| `/etc/enigma2/mqttbridge-state.json` | the plugin | The state file ([TOPICS.md §4](TOPICS.md#the-state-file)). 🔴 Its key `retained_topics` is read by name by every loader since 0.1.0 and is **never renamed**: a downgrade relies on the older plugin finding it |
| `/home/root/mqttbridge.log`, else `/tmp/mqttbridge.log` | the plugin | The plugin's log. The proof of §6 reads which of them the new enigma2 process holds open |
| opkg's lock file | opkg | The `option lock_file` of opkg's configuration; without one, both `/run/opkg.lock` and `/var/lock/opkg.lock` (§2.6) |
| `/tmp/enigma2-mqtt-r2-<id>/` | the installer: integration main (unreleased, 0.4.0) | The directory (0700) the stop-and-restore script of R2 runs from (§5.2): `helper.py`, the script's own copy of the installer's helper; `r2.sh` (0700), the script; `status` (0600), its status lines; `log` (0600), its output from the first line. Removed only after the installer has read `started` from `status` |
| `/usr/lib/enigma2/python/.mqttbridge-staging-<id>/` | the installer: integration main (unreleased, 0.4.0) | Where a restore builds the plugin tree it puts back, before swapping it in (§3.4). Beside `Plugins/`, never inside it. `<id>` is the id of the snapshot being restored |
| `/usr/lib/enigma2/python/.mqttbridge-aside-<id>/` | the installer: integration main (unreleased, 0.4.0) | Where the live plugin tree waits during that swap; removed once a tree is at the live path again |

A name is recognised by an exact match - `ha-installer-[0-9a-f]{12}`, `self-update-[0-9a-f]{12}`,
`update-[0-9a-f]{12}` - never by a prefix alone. The same holds for the names integration main adds:
a restore removes only the staging and set-aside directories of the snapshot it restores, by their
two exact names, and the installer removes only its own transaction's `/tmp/enigma2-mqtt-r2-<id>/`.
Nothing sweeps the directory beside `Plugins/` by a pattern: it holds enigma2's own `Components`,
`Screens` and `Tools`. A restore of a snapshot that is not named `ha-installer-<id>` stages under a
fresh random twelve-digit id, which matches nothing already there.

---

## 2. The lock

### 2.1 Claim and release (released)

- **Claim**: `mkdir` of `.ha-installer.lock` with mode 0700 and no parents. If it exists, it is
  judged by the stale rule (§2.3). A stale lock is reclaimed by **renaming** it to
  `..ha-installer.lock-stale-<pid>-<8 hex>` - two dots: a dot is prefixed to the lock's own name,
  which already starts with one - judging the renamed directory again, and only then
  deleting it and making a fresh one; a lock that turns out to be alive after all is renamed back
  and the claim fails busy. A rename has one winner, so two claimers can never both believe they
  hold the lock.
- **Owner record**: the claimer writes `owner.json` into the lock directory, atomically - a
  temporary file in the same directory, flushed, renamed over.
- **Release**: remove `owner.json`, then `rmdir` the directory. The `rmdir` fails on anything else
  left inside, so the holder removes its own temporary files first. Only a well-formed lock (one
  with `owner.json`) is released.
- **Integration main (unreleased, 0.4.0)**: the claim writes the transaction's `<id>` into the owner
  record, and when it reclaimed a stale lock whose record carries an `id` and no `origin`, it
  reports that id - which is how §3.3 finds the abandoned transaction's snapshot.

### 2.2 The owner record

| Key | Type | Released | Meaning |
|---|---|---|---|
| `pid` | int | yes | The process that wrote the record. 🔴 **Never a liveness test**: the installer's record names the short-lived process that made the claim, which has exited by the time anybody reads it |
| `started` | int | yes | Epoch seconds when the record was written |
| `boot_id` | string | yes | `/proc/sys/kernel/random/boot_id` at that moment, or empty |
| `uptime` | number or `null` | yes | Seconds since boot at that moment, from `/proc/uptime` |
| `origin` | string | planned | Who started the self-update: `mqtt`, `home_assistant`, `screen`, `page` |
| `id` | string | integration main (unreleased, 0.4.0) for the SSH installer; planned for the self-update | The transaction's `<id>` |
| `target` | string | planned | The version being installed |

A reader ignores keys it does not know. The helper of released 0.3.1 reads only `boot_id`,
`uptime` and `started`, so `origin`, `id` and `target` change nothing for it. The helper on
integration main reads `id` as well, and only from a record without `origin`. 🔴 **The record is ASCII**:
the released helper decodes it as ASCII, and a record it cannot decode falls to rule 1 of §2.3 -
judged by the directory's age, so a live lock with a non-ASCII record looks stale after 30 minutes
however fresh its heartbeat. A record **without** `origin` is the SSH installer's, and is reported
as `started_by: ssh`.

### 2.3 When a lock is stale (released, and unchanged)

`STALE_LOCK_SECONDS` is **30 minutes**. In this order:

1. `owner.json` cannot be read, or is not JSON: stale once the lock **directory's** modification
   time is more than 30 minutes old - an owner record is written atomically, so a missing one is
   either a claim milliseconds old or a claim that died.
2. The record is not a JSON object: stale.
3. Its `boot_id` differs from this boot's: stale at once - the process that held it no longer
   exists.
4. Same boot, and both uptimes are known: stale when `uptime now - uptime recorded` is more than 30
   minutes. Uptime cannot jump, and many receivers have no battery-backed clock: they boot in 1970
   and jump to the real time when NTP answers, so a wall-clock age could read as decades.
5. Otherwise: stale when `started` is missing or malformed, or more than 30 minutes old by the
   clock.

Every implementation uses this rule, and a later one may only lengthen the threshold: a released
installer in the field keeps judging every lock with 30 minutes.

### 2.4 The heartbeat (planned)

The self-update can hold the lock longer than an SSH install does: a download, the package
manager, a question on the television, the restart, the proof, and perhaps a rollback. So its
helper keeps the lock visibly alive:

- **Every 60 seconds, from a thread of its own** - a blocking `opkg` must not starve it - the
  helper rewrites `owner.json` with the same `pid`, `boot_id`, `origin`, `id` and `target` and a
  fresh `started` and `uptime`. Rule 4 of §2.3 then measures from the last beat, so every released
  installer sees the lock as held and refuses busy.
- **It writes only a lock it still holds.** Before each rewrite the helper reads `owner.json`; if
  the `id` there is not its own, the lock was taken from it, it writes nothing, and the transaction
  stops touching the receiver and ends with `result: interrupted`.
- **It is the second line, not the first.** The helper bounds itself: the forward path aborts at
  **15 minutes** and then rolls back, and the rollback aborts at **5 minutes**, so the lock is held
  at most **20 minutes** - under the released 30-minute rule even with no heartbeat at all. The
  expected worst case is shorter: forward 617 s (index and download 70, verify 1, release digest
  check 10, free space 1, snapshot 30, opkg 120, manifest 10, downgrade retraction 15, the
  television's question 60, waiting for the new interface 180, proof 120), rollback 246 s (record 5,
  stop 30, restore 60, settings write 1, start and wait 120, verify and zap 20, standby 10) - **863 s
  (14.4 min)** in all. The companion integration waits 21 minutes for a transaction it follows.

### 2.5 The two implementations meeting

| The lock is held by | and arriving is | Result |
|---|---|---|
| a self-update whose heartbeat is fresh | any released installer, or a later one | busy |
| a self-update whose helper died, no beat for more than 30 minutes of uptime | an installer | reclaimed by §2.3 rule 4, and logged |
| an SSH install (no heartbeat) | a self-update | busy, unless stale by the same rule |
| anybody, before a reboot | anybody | reclaimed at once (§2.3 rule 3) |

**Planned, not yet written:** these are to be tested against the **released** code, not a
description of it. The self-update's pull request adds to the plugin's tests a copy of the
integration's released `installer_helper.py`, with its MIT licence header, and asserts that a lock
with a fresh heartbeat is not stale for it at 31 minutes - also while a fake `opkg` blocks the
helper - and that a lock with no heartbeat is. No such test or copy exists yet.

### 2.6 opkg's own lock (released)

Both programs take **every** lock file opkg could be using, with `lockf` (the lock domain opkg
itself uses; `flock` is a different one), around a snapshot and around a restore, and wait a
bounded time for it. "Is opkg busy?" is answered by taking those locks without waiting and letting
go at once. 🔴 `opkg status` and `opkg list-installed` never take the lock (measured on opkg 0.6.3),
so they cannot tell anybody it is held - only a writer sees it.

---

## 3. The snapshot

### 3.1 Layout, schema 2 (released)

Taken while opkg's lock is held; a snapshot that does not complete is deleted, so a half one can
never be mistaken for a rollback point.

| Name in the snapshot | What |
|---|---|
| `snapshot.json` | `{"schema": 2, "plugin", "package_status", "opkg_info", "provisioning", "settings", "webif_shim", "webif_cache"}` - each a bool saying whether the part below exists. Any other key set, or another schema, is refused |
| `plugin/` | The plugin directory, `/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge` |
| `package-status` | The package's stanza from opkg's status file |
| `opkg-info/` | The package's files from opkg's info directory |
| `plugin-settings` | Every `config.plugins.mqttbridge.*` line of `/etc/enigma2/settings` - so, 🔴 the broker password too |
| `provisioning` | `/etc/enigma2/mqttbridge.json`, when there is one |
| `webif-shim` | The OpenWebif hook, `.../WebInterface/WebChilds/External/MQTTBridge.py` |
| `webif-cache/` | Its compiled bytecode, in either of the two places images put it |

A restore replaces the package's status stanza, its info files, the plugin directory, the hook and
its bytecode; the settings lines and the provisioning file only when asked to.

### 3.2 Pruning (released)

Each program keeps **two** of its own snapshots - the one of the transaction that is committing, by
name, whatever the clock says, and the newest other one - and deletes only directories that match
its own name exactly (§1). The lock, the other program's snapshots, a transaction directory and
anything a person put there are never candidates.

### 3.3 Which snapshot a rollback uses

Released 0.3.1: each program's rollback restores the snapshot its own transaction took, and
nothing recovers another transaction's.

For the recovery of an interrupted transaction (§5.2): **restoring the same snapshot twice is
safe** - a restore replaces the package's status stanza, its info files, the plugin directory and
the hook wholesale - so the recovery restores the interrupted transaction's snapshot again whenever
that transaction had begun to restore, or cannot say:

- **the self-update** (planned) says where it was: its marker and its `status.json` carry the
  phase (§4);
- **the SSH installer** writes no phase record, so an interrupted installer transaction is treated
  as "cannot say", and its snapshot is restored again. **Which** snapshot is not decided by
  modification time - receivers without a battery-backed clock boot in 1970 and jump when NTP
  answers, so "newest" can be wrong. Integration main (unreleased, 0.4.0): the installer writes its
  transaction `<id>` into the lock's `owner.json` (the `id` key of §2.2). An install that reclaims
  such a lock as stale, and finds `ha-installer-<id>` beside it for an id that is not its own,
  restores that snapshot before it takes its own. The interface is running - the install's
  preflight needs it - so this is the restore of §3.4: the files, opkg's metadata and the
  provisioning file, never the settings block.
- If that recovery does not complete - opkg's lock stays held, or the restore fails - the install
  stops before its own snapshot and releases the lock it holds. The abandoned transaction's id is
  then in no lock any more, so no later install recovers it; its snapshot stays as an ordinary one
  and is pruned like one (§3.2).
- A lock the installer released is never a recovery point: an R2 whose end the installer saw -
  including one whose restore failed or was cut off by its bound (§5.2) - releases the lock, and
  its snapshot is then left for a person, named in the installer's sentence.

Either way the recovery first needs the lock, and the two programs' locks age differently: a dead
self-update's lock is stale 30 minutes after its last heartbeat, but the SSH installer's lock has
no heartbeat and stays fresh for **30 minutes from its claim** - the residual named in §5.2.

### 3.4 Restoring while the interface runs (integration main, unreleased 0.4.0; planned for the self-update)

When a restart is withdrawn (§5, R1 with a question), the old enigma2 is still the running process
and the old files go back underneath it. On integration main the same restore also serves the
rollback of an install that failed before its restart, and the recovery of §3.3; and every restore
there, R2's included, swaps the plugin directory in as below. Then:

- **Files and opkg metadata only, never the settings block.** The transaction changed no setting,
  and a block written while enigma2 runs is overwritten from memory by the next clean quit - writing
  it would only race the household's own saves.
- **The plugin directory is never half-written.** The old tree is copied into a staging directory,
  then two renames: the live directory aside, the staged one in. `rename` cannot replace a non-empty
  directory, so it is two steps, and for the microseconds between them the plugin directory is
  absent; a restart that lands there is caught by the pid check below, because the new process then
  fails the proof and is rolled back by R2. The hook, its bytecode and each opkg metadata file are
  single files, each replaced by its own rename. What an earlier, interrupted restore of the same
  snapshot left - its staged or set-aside tree - is removed first, by the two exact names of §1; the
  set-aside tree is removed only once a tree is at the live path again, because until then it is
  the only copy of what was live.
- 🔴 **The staging and set-aside directories are on the same filesystem as the plugin directory**
  (a rename does not cross filesystems) **and outside every directory enigma2 scans for plugins.** A
  complete copy of the plugin where the plugin loader can see it is a second installation of the
  plugin - the same trap as a backup copy inside Home Assistant's `custom_components/`. Integration
  main puts them in `/usr/lib/enigma2/python/`, beside `Plugins/` (§1): the loader lists
  `Plugins/`, then the category directories in it, then the plugin directories in those, so the
  parent of `Plugins/` is never scanned (read from an image's plugin loader, not from every image). A
  test watches every rename of a restore and checks, at each one, that the loader's view holds
  either no plugin directory or one complete tree, and never a second copy. The restore compares the
  two directories' filesystems before it takes opkg's lock or changes anything, and refuses when
  they differ.
- Then the enigma2 pid is read again - on integration main for up to 60 s, and compared with the
  pid read **before the restart was requested**, never with a later reading, so a restart that
  began after the last look is still a restart. If it changed - somebody answered the question - the
  transaction never reports "withdrawn" for a receiver that restarted. When the old files had been
  put back, the new process may have read either version, and a proof could pass on a plugin whose
  files are gone, so the restart is treated as failed: R2 makes the files and the process agree. When they had not been put back (opkg's lock stayed held), the
  new files are what started, and the transaction goes on to the proof, and to R2 if it fails.

---

## 4. The marker (planned)

`/etc/enigma2/mqttbridge-update.json`, 0600, written by the self-update's helper once the new
package is installed and verified:

| Key | Meaning |
|---|---|
| `id` | The transaction's `<id>` |
| `from`, `to` | `{"version", "commit"}` of the plugin before and after |
| `boot_id` | This boot's id |
| `deadline` | Uptime in seconds, 15 minutes after the marker was written |
| `phase` | The transaction's phase, as in the `update` topic |

- A plugin that knows the marker reads it **right after its logging is configured**, before the
  provisioning file is imported and before it checks whether it is switched on - so a new plugin
  that is switched off still confirms that it started.
- It is **ignored** when its boot id differs and no lock with its `id` exists, or when its deadline
  has passed.
- After a power loss, the plugin that starts reports `installed` when it is `to`, `rolled_back`
  when it is `from`, `interrupted` otherwise - and removes the marker.
- 0.2.0 and 0.3.x never read it; the next plugin that knows it discards a stale one by these rules.

---

## 5. The restart rule (planned for 0.4.0, both programs)

**Why.** The image saves its settings - the channel being watched among them, as
`config.tv.lastservice` - only on a **clean** quit: `StartEnigma.py` runs `stopService()`,
`nav.shutdown()` and `configfile.save()` after the main loop returns. A signal stop never reaches
that code (read from the image's code). Measured on a receiver: after enigma2 was stopped by a
signal, it came back on another channel than the one playing, because the saved channel was older.
OpenWebif's restart (power state 3) opens the image's own `TryQuitMainloop`, which is the clean quit
(read from OpenWebif's code). In released 0.3.1 every restart of the installer - install, and both
steps of its rollback - is `init 4` then `init 3`, with nothing recorded and nothing restored.
Integration main (unreleased, 0.4.0) follows this section: an install or update restarts only by R1,
and `init 4` is left to the rollback that is R2. It has not yet been run on a receiver.

**Two hypotheses this rule rests on, not yet measured.** The integration's code that relies on them
is merged on its main branch without that measurement; both are measured on a receiver in the
hardware acceptance before a release carries the code (spike S2 in the plan), and the code is taken
out again if that fails. Until then they are hypotheses, and the rule is built so that a wrong
answer costs a zap, not a lost channel:

- **H1 - `init 4` loses unsaved settings like any signal stop.** Expected from the above: init stops
  the respawn entry by signal, so the image's save never runs. Not measured. On integration main
  only R2 uses `init 4`, and it writes the recorded channel after the stop either way.
- **H2 - the image reads `config.tv.lastservice` at start**, so a service written into the settings
  while enigma2 is stopped is the one it comes back on. Not measured. If it is wrong, R3's zap back
  restores the channel instead.

### 5.1 The three cases

| Case | How | Why the channel survives |
|---|---|---|
| **R1 - restart**: the interface is running and healthy, and only the plugin's files changed | The plugin (planned): `TryQuitMainloop(session, 3, timeout=60, default_yes=False)`. The SSH installer (integration main): the preflight refuses while recording, streaming, in standby, or with a timer due within 10 minutes, and is measured again immediately before the restart; then OpenWebif's power state 3, called on the receiver itself, and **at most 60 s** for a new enigma2 pid, read every 2 s | A clean quit runs `configfile.save()` |
| R1, no new pid within 60 s | The image asked a question on the television (timeshift, a background job). The installer **does not force it**: it restores the plugin's files and opkg metadata as in §3.4, reads the pid again, releases the lock and says so - that the receiver asked whether to restart, that the update was withdrawn and the previous plugin is running, and that the question may still be on the television, where either answer is safe. The outcomes are in the table below | Nothing was stopped |
| **R2 - stop**: the interface must not run - a rollback that puts the settings block back, or (planned) a forced reinstall into an interface that keeps crashing | Record the playing service and the standby state (below); `init 4`; wait for enigma2 to stop; do the work; write the recorded service into `config.tv.lastservice` while enigma2 is stopped; `init 3`; wait for a new pid. The SSH installer's form of it, with its bounds, is §5.2 | Hypothesis H2: the image reads `lastservice` when it starts. R3 covers it being wrong |
| **R3 - verify**, after every restart on every path | Compare the playing service and the standby state with the record. A different service: zap back to the recorded one through OpenWebif, once, and compare again. Standby recorded: enter it through OpenWebif (power state 5) | By effect, not by assumption |

- **A restart is judged by the enigma2 pid changing**, never by the exit status of the call that
  asked for it: the quit can reset the HTTP connection that asked. On integration main a new pid
  means one that was not running before the request; the old pid gone with none new at the 60 s
  bound also counts as a restart - a failed one, which the proof then finds.
- **Once the restart has been requested, nothing the installer cannot see ends in R2** (integration
  main). From the power-state request until a new enigma2 pid is seen, no rollback runs, because the
  image may be keeping the interface up on purpose. A connection that drops while the installer
  waits, or while it withdraws, is connected again and the receiver read again, and a withdraw it
  cut off is repeated - a restore of the same snapshot is safe to repeat (§3.3); the withdraw is
  given up to 120 s for that. A receiver that cannot be read at all, or Home Assistant stopping in
  that window, leaves the transaction as it is: the new files stay, and the lock stays with the
  transaction's id, for the next install to recover by that id once the lock is stale (§3.3).
- **The record** is taken from OpenWebif's `/api/statusinfo` on the receiver - `currservice_serviceref`
  and `inStandby` - and from the plugin's retained `bouquet` topic where one exists. With nothing
  recorded (the interface was not answering), nothing is restored, and the transaction's record says
  so: there was no channel to keep. On integration main the forward record is taken as late as
  possible, the channel last; R2's record is what the receiver plays when the rollback begins, else
  the record taken before the install's restart, whose bouquet is carried over only when it was
  recorded with the same channel.
- **The zap back happens at most once, and only while the playing service is still the one the
  image started on** - the first service read after the start. A channel the household chose in the
  seconds after the start is never overridden.
- **The channel-list bouquet** (`config.tv.lastroot`) is not what `lastservice` restores, and
  `statusinfo` does not report it. When a plugin that has `cmd/bouquet` is running after the start,
  the side that holds the MQTT session - Home Assistant on the SSH paths, the new plugin itself on
  the MQTT path - restores the recorded `bouquet` with it; otherwise the record says `bouquet: not
  restored`, and channel up and down walk the image's saved list until the household changes it.
- **Standby restore** is a rollback corner: the forward paths refuse to start in standby, so a
  standby record exists only when a rollback began in one. OpenWebif's power state 5 opens the
  image's standby only when the receiver is not already in standby, and with HDMI-CEC enabled the
  image then sends a standby to the television as well.
- The transaction's record carries `restart: clean | stopped`,
  `channel: kept | restored | lost | not recorded`, `bouquet: kept | restored | not restored` and
  `standby: kept | restored | lost | not recorded`. `not recorded` means the record held no channel,
  or no standby state. `lost` for the channel covers an interface that reported no channel within
  60 s of the start, a zap back that did not show within 10 s, and a channel somebody changed after
  the start, which is left alone; `lost` for standby is a standby that did not show within 10 s.
  Integration main writes the record as one line to Home Assistant's log; when the check itself
  fails, it logs a warning instead. On the forward path R3 runs **after the commit** - the lock is
  released first - so a lost connection or Home Assistant stopping while the channel is checked
  cannot roll back an install that proved itself. After R2 it runs once the new pid is seen, before
  the lock is released.
- Other settings the household changed since the last save survive R1 and are lost in R2. R2 is
  used only where the settings block has to be put back anyway.
- Nothing uses `killall enigma2`.
- **Nothing reads the order of open screens.** Some images keep a screen open from session start
  (an invisible one below the info bar, from a broadcaster plugin); the question's outcome comes from
  `TryQuitMainloop`'s own callback, standby from the image's standby flag, and "an update is being
  applied" from the plugin's own state.

**The SSH installer's outcomes around the restart** (integration main, unreleased 0.4.0; the names
are the installer's error codes, each shown to the household as a sentence):

| Outcome | When | The receiver afterwards | The lock |
|---|---|---|---|
| `restart_withdrawn` | No new pid within 60 s, OpenWebif confirmed the request, and the old files are back | The previous plugin runs; nothing was restarted; a question may still be on the television, and either answer is safe | released |
| `restart_unconfirmed` | The same, but OpenWebif never confirmed the request (its answer said so, or was lost), so no question is claimed | as above | released |
| `withdraw_failed` | No new pid within 60 s, and the old files could not be put back - opkg's lock stayed held, or the restore failed | The new files are on disk under the plugin still running (after a failed restore, possibly a mix); a question may still be on the television, and "yes" there starts the new version with nothing checking it | released |
| `restart_unobserved` | After the request the receiver could not be read at all - during the wait, or during the withdraw | As it was left: the new files, or what the withdraw had put back | **kept**, with the transaction's id, for the recovery of §3.3 |
| `rollback_unobserved` | R2's script was started and its end - `started` in its status - was not read within the follow bound | The script carries on by itself on the receiver | **kept**, with the id, and the script's directory kept (§5.2) |

Home Assistant stopping in either of the last two windows leaves the receiver and the lock the
same way, with no sentence shown. When the lock cannot be released after a withdrawal, the outcome
is `rollback_lock_failed` instead.

### 5.2 R2 runs as one unit, and an interruption ends with the interface running (integration main, unreleased 0.4.0; planned for the self-update)

Between `init 4` and `init 3` the household has no picture. Nothing that can be interrupted from
outside the receiver may sit between the two. What a shell does when it is interrupted was
measured for this section - busybox 1.36.1 `sh` and `dash` on a development machine, not on a
receiver, with a stand-in script in which "`init 3`" writes a marker and "the restore" is a child
process:

| What happened to the script | `trap ... EXIT HUP INT TERM` | the same plus `PIPE` |
|---|---|---|
| The reader of its output went away (a dropped connection, no signal) | the shell died of `SIGPIPE` on its next write and **the trap did not run** | the trap ran |
| `HUP` or `TERM` to the shell while the restore child ran | the trap ran **at once, while the child was still running**; the child finished after the shell had exited | the same |
| `HUP` or `TERM` to the whole process group | the child was killed too - the restore cut off - and then the trap ran | the same |
| After a signal trap that does not end in `exit` | the script **carried on** with its next step, and the `EXIT` trap ran a second time | the same |

So:

- **The SSH installer starts R2 detached**, as one script in a session of its own, with its output
  going to a file on the receiver from its first line; the installer follows it by reading a status
  file over SSH. A dropped connection or a Home Assistant restart then sends it nothing at all. It
  is **one** script started once: the stop, the restore, the `lastservice` write and the start,
  never several SSH commands. (Released 0.3.1's rollback sends `init 4` and `init 3` as two
  separate commands, which is the case this rule removes; its install restart runs under a trap.)
- **How integration main starts it.** The helper's `r2-start` creates `/tmp/enigma2-mqtt-r2-<id>/`
  (0700), copies itself into it as `helper.py`, writes the script as `r2.sh` (0700), creates
  `status` and `log` (0600 each), and starts `/bin/sh r2.sh` with Python's
  `start_new_session=True` - a new session and process group with no controlling terminal, the
  detachment `setsid` gives and this section once named `start-stop-daemon -S -b` for - with its
  input from `/dev/null` and its output appended to `log`. The script runs on its own copy of the
  helper, so nothing the installer tidies up can take a file from under it.
- **The status lines**, each appended to `status` as its step happens, in this order: `begun <pid>`
  (the script's own pid), `stopping <rc>` (`init 4`'s exit status), `stopped` or `stop_timeout`,
  `restored <rc>` (the restore's exit status), `written <rc>` or `not_written`, `started <rc>`
  (`init 3`'s exit status), `done`. `not_written` means enigma2 was not seen stopped, or no channel
  was recorded. A signal ends the script through its trap after `started`, without `done`; one that
  arrives before the restore began leaves out `restored`.
- **The bounds.** After `init 4` the script waits up to **30 s** for enigma2 to stop. If it is still
  running then, the script says `stop_timeout` and puts back the files and opkg's metadata only -
  no settings block and no channel, because the running interface writes its own settings over
  them on its next clean quit - and still runs `init 3`. The restore is bounded at **90 s** by a
  watchdog that sends it `TERM`, so the picture comes back even from a restore that hangs.
- **How the installer follows it.** It reads `status` every 2 s, connecting again after a dropped
  connection, for at most 180 s (the 30 s stop wait, the 90 s restore bound and a 60 s margin).
  Reading `started` ends the following. Unless the status says `stop_timeout` or `started` is not
  0, the installer then waits up to 120 s for a new enigma2 pid and runs R3; in every case it then
  releases the lock, and only after that removes `/tmp/enigma2-mqtt-r2-<id>/` and its uploaded
  helper. `restored` other than 0 is reported as `rollback_failed` (or, for the helper's two opkg
  exit statuses, `rollback_opkg_busy` and `rollback_opkg_overlap`), `stop_timeout` as
  `rollback_failed` saying that only the files went back, and `started` other than 0 as
  `rollback_restart_failed`.
- **The lock is held until the script's end is seen.** From the moment `r2-start` is sent until
  `started` is read, the installer neither releases the lock nor deletes anything of the script's.
  When the following ends without `started` - its bound passed, or Home Assistant stopped - the lock
  stays with the transaction's id and the directory stays (`rollback_unobserved`).
- **A lost answer to `r2-start`.** When the connection drops under that command, its answer says
  nothing about the receiver: Python may still be starting there, before the directory exists. A
  missing `status` then means only "not yet" for a **30 s** grace period, and after it "never
  started" only when the receiver has neither the directory nor a process whose command line names
  it (read from `/proc/*/cmdline`). When `r2-start` did answer, `status` existed before the script
  started, so a missing one means it never started. A script that never started stopped nothing:
  the lock is released and the uploaded helper removed, but a directory is never removed on the
  strength of its absence.
- **The plugin's helper** (planned) runs R2 inside itself - detached from enigma2 and from any SSH
  session - with handlers for `HUP`, `INT`, `TERM` and `PIPE` that do the same as the trap below.
- **The trap is still there**, set **before** `init 4`: on `HUP INT TERM PIPE` it runs the finishing
  step and exits, and on `EXIT` it runs the finishing step. That step first ignores further `HUP`,
  `INT`, `TERM` and `PIPE`, so a second signal cannot cut the first short. It **waits for a restore
  still running** (the script starts the restore as a child and keeps its pid), stops the watchdog,
  writes the recorded `lastservice` if enigma2 was seen stopped and a channel was recorded, and runs
  `init 3`. It is idempotent, because the `EXIT` trap runs it again.
- **Order inside**: the settings block is restored first and `lastservice` written after it, each
  by an atomic rename, so a restore can never overwrite the channel, and waiting for the restore
  keeps the trap from writing the channel under a restore that then renames the settings file over
  it.
- **Not measured**: which signal, if any, dropbear sends a command without a terminal when its
  connection closes. The detached start makes the answer not matter; the trap list covers each
  possibility that was measured.
- **What the trap cannot cover**: a `SIGKILL`, or power, between `init 4` and `init 3`. Power
  reboots the receiver, and a reboot reclaims every lock at once (§2.3 rule 3). A killed script
  leaves the receiver in runlevel 4 **with no picture until somebody switches it off and on
  again**: nothing on the receiver starts the interface by itself, and the next install cannot help,
  because its preflight needs OpenWebif, which runs only while the interface does. The R2 script
  does not keep the lock alive - the installer holds it, with no heartbeat - so the lock goes stale
  30 minutes after its claim, or at once with that power cycle. Planned, not built: a forced
  reinstall that recognises runlevel 4 with a lock, a marker or a transaction directory of this
  project's as "ours" and recovers it with `init 3` and R3; in runlevel 4 **without** anything of
  ours it refuses, because somebody stopped the interface on purpose.
- An interruption the trap does cover, arriving before the restore has begun, starts the interface
  on the plugin as it was: the picture comes first. The status then has `started` without
  `restored`, which integration main reports as `rollback_failed`, and it releases the lock; the
  snapshot stays, named in the installer's sentence, for a person to restore from - §3.3's recovery
  does not run, because no lock is left behind.
- A restore cut off part-way - by the 90 s watchdog, or by a signal to the whole process group,
  which the detached start is there to prevent - leaves a partial restore. The trap still starts
  the interface, and integration main reports `rollback_failed` and releases the lock, as above.
  Restoring the same snapshot again by hand is safe.
- **Test**: **close the SSH connection** between `init 4` and `init 3` - not a signal delivered by
  the test - and the receiver ends in runlevel 3, with the restore complete and the recorded
  `lastservice` written. Integration main has this test, with stand-ins for `init` and `pidof`,
  under the host's `sh`, busybox `sh` where installed, bash run as `sh`, and bash; it has not been
  run on a receiver.

### 5.3 The SSH path has no doors - a bounded residual (R1)

With the R1 restart (integration main, unreleased 0.4.0), the plugin running during an SSH update -
0.2.0, 0.3.x, or any plugin that is not itself driving the transaction - keeps running with the new
files on disk from `opkg` until the restart: a few seconds normally, **up to 60 seconds** when the
image asks a question, plus the time the withdraw takes to put the old files back, which includes
up to 40 s of waiting for opkg's lock (with `init 4` it was a few seconds). A module that plugin
imports for the first time in that window is new code in an old process. This is accepted as
bounded. When the withdraw fails (`withdraw_failed`, §5.1) the bound does not hold: the new files
stay under the running plugin until the next restart, which then starts the new version unchecked. The self-update path does not have it: from the moment the
package is installed, the plugin answers every command, its setup screen and its OpenWebif page
with "an update is being applied on the receiver" until the restart or the withdrawal.

---

## 6. Proof that the new plugin started

| Path | Proof |
|---|---|
| SSH installer (released 0.3.1, unchanged on integration main) | Within 120 s of the restart: the plugin's `availability` back `online` as a live message (a retained replay does not count) - preceded by a live `offline` only when the plugin was running before the install; then a live `info` with the expected `info.plugin` and `ha_mode: integration`. After that, a new SSH connection must find an enigma2 pid that was not running before the restart. Planned: `info.build.commit` too, when the target publishes one |
| Self-update, a target that knows the marker (planned) | The new plugin writes `started`, with its version and build commit, into the transaction's `status.json`; both must match the request |
| Self-update, a target that does not (0.2.0, 0.3.x - planned) | The **new** enigma2 pid holds the plugin's log file open (`/proc/<pid>/fd`), polled every 2 s through the whole window, because a log rotation closes the file for a moment. Both released plugins configure logging before anything else at start, whether or not they are switched on and whether or not the broker answers. When neither log path was writable before the restart, those plugins hold no file, and the proof is the OpenWebif hook answering anything but 404 after the pid change. The transaction's record says which proof it used. A log line is corroboration only, read from the byte offset recorded at the pid change - never matched by its timestamp, which a broker client can forge into the log |

No proof within the window: R2 (§5.1), and the restored plugin puts the reason on `last_error`.
