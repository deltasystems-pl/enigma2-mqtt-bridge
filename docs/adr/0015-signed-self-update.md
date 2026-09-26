# ADR-0015: The plugin updates itself only to releases from a signed index, in a detached transaction that proves the new plugin started or puts the old one back

**Status:** accepted 2026-09-26, with the first code that implements it - decision 1, the build id.
Decisions 2 to 7 are built by later changes.
**Date:** 2026-09-26
**Supersedes:** in part - [ADR-0000](0000-prd.md) §7 "no outbound connection other than the user's
broker"; the matching sentence of [SECURITY.md](../../SECURITY.md) ("The LAN is the trust boundary.
The plugin makes no outbound connection other than to the broker the user configures"); the README's
"The plugin has no telemetry, no cloud part and no update check of its own" - its last clause; and
the same promise in the package description, `CONTROL/control`. The build id makes no connection, so
all four still describe every released plugin exactly; they are rewritten when the first release
that fetches anything ships.

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

1. **Each build says what it is.** The builder embeds the commit, its time, whether the tracked
   files matched it and the build's flavour - `release`, `development` or `acceptance` - supplied by
   the builder and never guessed, and never read from git on the receiver. `info.build` publishes
   them, with the commit waiting on disk when it differs from the running one. In a git checkout a
   `release` build is refused unless the tree is clean at the tag of its version; a build without
   its checkout must be given the commit and its time, and the builder that gives them vouches for
   the tree and the tag - only the signed index confirms a release. Builds stay reproducible: the same
   commit, time and flavour give the same bytes, with or without `.git`. A development build displays
   as `N.N.N+g<sha7>` - `N.N.N+g<sha7>.dirty` when its tracked files differed from the commit -
   never with a pre-release suffix. `info.contract` publishes the contract
   major, and [TOPICS.md](../TOPICS.md#contract-version) defines what a major allows.
2. **The only list of versions is a signed release index**, signed with an Ed25519 key. It carries,
   per release, size, sha256, commit, commit time, contract major, the lowest integration it needs,
   its package dependencies, whether it can update itself (`self_update`) and whether it has been
   withdrawn, plus a floor and a monotonic serial. The receiver verifies it with an embedded public
   key and a bundled pure-Python verifier, and refuses an older serial. The **main key** is used only
   in this repository's CI, by a signing job that runs in the GitHub Environment `release-signing`,
   which admits the `main` branch and nothing else - never another branch, a tag or a fork - and
   releases the key only to a run the maintainer approves by hand. The signing job cannot write to
   the repository; the job that publishes the index holds no key. Release tags `v*` are protected by
   a tag ruleset. A **spare key of higher rank** stays sealed offline and signs nothing unless the
   main key is leaked or lost; its emergency publication is a separate workflow that holds no
   secret.
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
   irreversible, so it is never exercised in production, and after a leak or theft of the main key
   the next release of both this plugin and the companion integration drops it.

The names on the receiver, the lock and its stale rule, the heartbeat, the snapshot, the marker and
the restart rule are specified in [TRANSACTION.md](../TRANSACTION.md), which the companion
integration's installer implements too. The topics, commands, settings and capability are in
[TOPICS.md §5](../TOPICS.md#5-planned-not-implemented-yet) until they are built; `info.build` and
`info.contract` are built, and are in §1.

**Settled when it was accepted (2026-09-26).** Three points changed between the proposal and its
acceptance with the build id:

- **A third flavour.** The proposal named `release` and `acceptance`. A `release` build now has to
  be clean at its release tag wherever that can be checked, so every other build of the published code - a pull request's CI
  run, a developer's package, a candidate - is `development`, and the build is the default.
- **No `origin` in `info.build`.** A review asked for the build's release source to be published
  with its flavour. It is not: a build with another source will be an `acceptance` build, and a
  `release` build will be refused unless it carries the published source, so the flavour tells a
  consumer what it needs; and "origin" already means where a command came from and, in the planned
  `update` topic, whether the index could be reached. That reasoning rests on two refusals the
  build did not make when this was accepted, because the build id had no source in it at all: the
  builder refusing a source or key override for any flavour but `acceptance`, and the release
  workflow asserting the published source and keys. Both are made since the change that added the
  signed index's tooling ([RELEASE-INDEX.md](../RELEASE-INDEX.md), "Acceptance builds"). If a
  consumer ever needs the source itself, adding it is an addition inside contract 1.

**Settled with the index's tooling (2026-09-26).** Decisions 2 and 7 are built as far as they reach
without a reader: the index format, the two embedded keys and their ranks, the acceptance rule and
its shared vectors, the verifier, and the three publication workflows
([RELEASE-INDEX.md](../RELEASE-INDEX.md)). One point is made precise there: a reader's memory is
the last accepted
serial per `key_id`, and the highest accepted rank is derived from it within the reader's own key
set - which scopes it to the key set, as decision 7 requires, while a release that keeps a key keeps
what was known about it. And a dirty build now displays with `.dirty` after its commit (decision
1), so it no longer looks like the clean build of the same commit.
- **The main key signs in CI** (decision 2), by the maintainer's decision of 2026-09-26. The
  proposal kept it off GitHub; the residual that trades for is stated under Consequences, in the
  same terms as the companion integration's ADR-0008.

## Consequences

- The plugin can connect to one fixed address other than the broker - when a person asks for it,
  when an install needs it, or when `update_check` is on.
- Every index needs the maintainer's approval of the signing job. A release is on the opkg feed
  before that approval, and in the index only after it.
- A deleted environment secret is not a lost key: it is entered again from the main key's encrypted
  offline backup. Only losing both copies moves signing to the spare, and that is irreversible -
  every reader that accepts a spare-signed index ignores the main key from then on - so the spare is
  never exercised in production. A **leak** of the main key - through a malicious workflow, a
  compromised action or the maintainer's account - is treated as a theft: the secret is deleted, the
  spare signs the next index, and the next release of both halves drops the main key, because a
  receiver, or an installation reset later, still accepts the leaked key until a release no longer
  embeds it.
- **What CI signing does not protect against.** Because the main key is used in CI, a compromise of
  the maintainer's GitHub account, a malicious workflow change merged to `main`, or a compromised
  action in the signing job can produce a validly signed index, and a receiver would accept what it
  names. What stands in the way: the approval gate (the key reaches only a run the maintainer
  approves), the environment that admits `main` only, the tag ruleset on `v*`, the `main` ruleset
  (a workflow change needs a pull request and green CI), enforced pinning of every action to a full
  commit SHA, a passkey or two-factor authentication on the account, the companion integration's
  warning in its log and persistent notification for every newly accepted index, and the offline
  spare with a release that drops the main key. Stated plainly: against a compromise of the
  maintainer's account this makes a malicious update **detectable, and recoverable once the
  account - or another channel for a spare-signed index - is under the maintainer's control again;
  it does not prevent it**.
- The lock and the snapshot layout are shared with the integration's installer, and the released
  installer's 30-minute stale rule binds every later implementation. The state file's
  `retained_topics` key can never be renamed.
- Going below the first self-updating release loses self-update.
- There is no index expiry: a withheld index cannot be detected, and a withdrawal reaches a receiver
  only with a newer index.
- A validly signed release is root on the receiver - by design; that is what the signature decides.
- If this is reversed, the plugin goes back to making no connection but the broker, and updates go
  back to the SSH installer, the receiver's own plugin menu and the opkg feed.
