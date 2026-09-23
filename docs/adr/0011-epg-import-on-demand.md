# ADR-0011: The EPG import uses the importer enigma2 loaded, and is followed by polling, never hooked

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** [ADR-0003](0003-control-feedback-and-household-features.md) §4, in part — „runs
off the main loop", „refused while recording" and „rebuilt and republished" did not survive a
reading of the importer the feature was written against. The command, the permission, the topic
and the capability stand.

## Context

ADR-0003 §4 planned `cmd/epg_import` before anybody had read the importer it would drive. Reading
the bytecode of EPG-Importer as one image ships it (OpenViX 6.6, `1.0+git286`), and the plugin's
own stall monitor during that image's scheduled imports, changed five things.

**The importer is a singleton in a module enigma2 already loaded.** The plugin loader imports it as
`Plugins.Extensions.EPGImport.plugin`, and that module holds the importer object and the
scheduler's state. Importing it again under any other name builds a second module, a second
importer and a second scheduler, and neither pair would know about the other.

**An import does not run entirely off the main loop, and nothing the plugin does can change that.**
Downloads run on a thread pool and parsing on a worker, but the import ends by saving the guide on
the main loop: the stall monitor caught it at 2.3 and 2.6 seconds on two mornings, with the
importer's own save at the top of the stack.

**The importer has no failure signal.** Its completion callback is called on every run, with a
count of zero when every download failed, and its per-source errors go to a standard output that
is `/dev/null` on that image.

**The completion callback is not the plugin's to take.** The importer's `startImport()` sets it on
every run — the scheduler's runs included — so a replacement would be overwritten by the next
scheduled import, and patching the module's completion function would change what the image does
for everyone else.

**The image's scheduler starts an import without asking whether one is running**, so an import
started close to its time would be restarted underneath itself. And on an image whose guide cannot
take imported events, the importer writes a file and ends by asking for a user-interface restart
with a dialog whose default is yes and whose timeout presses it.

## Decision

- **The importer is looked up, never imported.** The plugin reads `sys.modules` for the module the
  loader left. Every name it needs — the importer and its `isImportRunning()` and `sources`,
  `startImport()`, the source configuration's `loadUserSettings()` and `enumSources()`,
  `CONFIG_PATH`, `lastImportResult` — must be there, and the image's EPG cache must have
  `importEvents` or `importEvent`; otherwise there is no capability, no command and no button, and
  the reason is logged once. A test holds the „never imported" part.
- **The import starts the way the importer's own „Manual" button starts it**, without its dialogs,
  on the main loop where every command runs: the channel cache reset on the scheduler's own test
  (the scheduler's state is read, never written), the selected sources enumerated and handed over
  reversed, `startImport()`. Nothing from the broker reaches it.
- **Completion is observed by polling, never hooked.** Every two seconds while an import runs — the
  test and the period the importer's own screen uses — and once a minute otherwise, the plugin asks
  `isImportRunning()`. When it turns false the result is already in `lastImportResult`. Events mean
  `done` and a grid rebuild that publishes only what changed (ADR-0006); none mean `failed`. A
  30-minute watchdog says `failed` and keeps polling, because the importer cannot be cancelled.
- **The topic follows every import**, whoever started it, so a refusal because one is running is
  never unexplained.
- **The guards are the permission (not needed on the OpenWebif page, ADR-0009), a resolved
  importer, an import already running, the whole recording guard, the importer's own scheduled run
  within ten minutes, and at least one selected source**, in that order. The plugin's own deep
  standby, reboot and user-interface restart are refused while an import runs — **except** once
  this plugin's own start raised, or once the watchdog has fired for the current run. The importer
  marks itself running before its first download, so a start that fails part-way can leave it
  „running" until its next scheduled run; the power block lapses then, while the topic keeps
  reporting what the importer says and a second import is still refused. It lapses only when the
  importer still says it is running at the moment the start failed — an earlier failure leaves
  nothing stuck, and a lapse set then would cover whatever import started next. It comes back once
  the importer has been seen idle, or with the next import this plugin starts. Accepted residual:
  after a stuck start, if the importer's next scheduled run begins without the importer ever being
  seen idle, the lapse covers that run too (about ninety seconds).
- **A press refused as „already running" starts following that import immediately**, so the topic
  and the refusal agree at the same moment.

## Consequences

- **The menus freeze for two to three seconds at the end of every import**, and the documentation
  says so rather than promising otherwise. The recording guard is the whole guard — a timer due
  within ten minutes refuses too — because that freeze lands on the loop that starts recordings.
- The topic can report only three failures — did not run, no events, not finished in 30 minutes —
  and never which source failed. A consumer must not expect more.
- A consumer sees `running` for imports it did not ask for, and an import that starts and ends
  between two idle polls is reported with `started` `null`.
- A settings save that restarts the bridge mid-import loses the original `started`.
- The importer's own `clear_oldepg` and deep-standby behaviour apply to an import started here as to
  a scheduled one; the plugin neither reproduces nor suppresses them. The deep-standby check runs
  after every import and acts only when all four of the importer's conditions hold — „shutdown" on,
  deep standby set to „wake up", „deep standby after import" on, and a timer wake-up — and then only
  on a receiver in standby, with nothing recording and not already shutting down. (The first
  reading of the importer took this for an „either … or"; its code is four nested tests, each of
  which returns when false.)
- Before its first download the importer reads `/proc/mounts` and the free space of the recording
  mount on the main loop, so a network mount that has stopped answering can freeze the picture on
  the press itself. The plugin cannot avoid it without not starting the import.
- While an import the importer wrongly believes is running is stuck after a failed start, the
  receiver can be restarted, but a new import is refused until the importer says it is idle.
- An image whose importer differs in any resolved name offers nothing: the resolution fails closed.
  Supporting another importer means adding its names here and a test for them, not loosening the
  check.
