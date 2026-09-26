# ADR-0015: The plugin updates itself only to releases from a signed index, in a detached transaction that proves the new plugin started or puts the old one back

**Status:** proposed - accepted with the first code that implements it
**Date:** 2026-09-26
**Supersedes:** in part, once accepted - [ADR-0000](0000-prd.md) §7 "no outbound connection other
than the user's broker"; the matching sentence of [SECURITY.md](../../SECURITY.md) ("The LAN is the
trust boundary. The plugin makes no outbound connection other than to the broker the user
configures"); and the same promise in the package description, `CONTROL/control`. Until this record
is accepted, all three describe every released plugin exactly, and they are rewritten when the
first release that implements it ships.

## Context

The plugin could be installed and updated only from outside: by hand, by the receiver's own plugin
menu, or by the Home Assistant integration over SSH. A version string could not tell a development
build from the release with the same number. Some receivers have no internet access at all, and are
reached only through the household's LAN.

Updating from inside enigma2 cannot survive the restart it needs, cannot roll back a new plugin
that does not load, and must not block the main loop. And every restart the project causes today
stops the interface by signal, which the image does not save its settings on: measured on a
receiver, it came back on another channel than the one the household was watching.

The receiver's package manager cannot check a feed's signature ("GPG signature checking not
supported" in opkg 0.6.3), and the receiver's Python may or may not ship a cryptography library - so
anything that decides what runs as root on the receiver has to carry its own verification.

## Decision

1. **Each build says what it is.** The builder embeds the commit, its time, whether the tree was
   clean and the build's flavour - supplied by the builder, never guessed - and `info` publishes
   them, with the commit waiting on disk when it differs. Builds stay reproducible. `info.contract`
   publishes the contract major, and [TOPICS.md](../TOPICS.md#contract-version) defines what a
   major allows.
2. **The only list of versions is a signed release index**, signed with an Ed25519 key kept off
   GitHub. It carries, per release, size, sha256, commit, contract major, the lowest integration it
   needs, its package dependencies and whether it has been withdrawn, plus a floor and a monotonic
   serial. The receiver verifies it with an embedded public key and a bundled pure-Python verifier,
   and refuses an older serial.
3. **One fixed origin.** The index and the packages come from a fixed HTTPS address, fetched with
   verified TLS and no redirects. Without internet, the receiver accepts both from a relay - the Home
   Assistant integration - over plain HTTP, because it verifies both itself: the relay is a courier,
   not an authority.
4. **Who may cause what.** Checking by itself is a receiver-only setting, `update_check`, off by
   default. Installing over MQTT needs the receiver-only permission `update_allowed`, off by default,
   and is **upgrades only**. Downgrades start only at the television or on the OpenWebif page.
5. **A detached helper runs the transaction**, outside enigma2: the lock it shares with the
   integration's installer, kept alive by a heartbeat; the snapshot; `opkg`; the image's own restart,
   with a bounded question that defaults to "no"; a proof that the new plugin started - its own
   confirmation, or the new process holding the plugin's log open, or its web hook answering when no
   log could be written; and a rollback. From `opkg` on, the running plugin takes no commands until
   the restart or the withdrawal. Before a downgrade's restart it retracts its retained topics and
   publishes nothing more.
6. **Every restart keeps the household's channel and power state.** The image's clean quit, which
   saves its settings, wherever the interface only needs to restart. Where it must be stopped, the
   playing service and the power state are recorded first, the service is written back while the
   interface is stopped, and both are verified - and restored if needed - after the start. The stop,
   the work and the start run as one unit whose interruption still starts the interface. Nothing
   reasons about the order of open screens.
7. **Trust memory is bounded and scoped.** An index is accepted only with an increasing serial for
   its key, at most 1000 above the last (a first sight within 1000 of a baseline embedded at build
   time), from a key whose rank is not below the highest rank already accepted. That memory is kept
   per embedded key set, so a build carrying other keys can never raise the rank the release keys are
   judged by. Two keys are embedded - a main key and a sealed spare of higher rank. Using the spare is
   irreversible, so it is never exercised in production, and the release after a key theft drops the
   stolen key.

The names on the receiver, the lock and its stale rule, the heartbeat, the snapshot, the marker and
the restart rule are specified in [TRANSACTION.md](../TRANSACTION.md), which the companion
integration's installer implements too. The topics, commands, settings and capability are in
[TOPICS.md §5](../TOPICS.md#5-planned-not-implemented-yet) until they are built.

## Consequences

- The plugin can connect to one fixed address other than the broker - when a person asks for it,
  when an install needs it, or when `update_check` is on.
- Every release needs an offline signing step. A lost key delays updates; a stolen one needs new
  releases.
- The lock and the snapshot layout are shared with the integration's installer, and the released
  installer's 30-minute stale rule binds every later implementation. The state file's
  `retained_topics` key can never be renamed.
- Going below the first self-updating release loses self-update.
- There is no index expiry: a withheld index cannot be detected, and a withdrawal reaches a receiver
  only with a newer index.
- A validly signed release is root on the receiver - by design; that is what the signature decides.
- If this is reversed, the plugin goes back to making no connection but the broker, and updates go
  back to the SSH installer, the receiver's own plugin menu and the opkg feed.
