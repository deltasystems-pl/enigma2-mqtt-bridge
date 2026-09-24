# ADR-0002: Scope added and changed after M0

**Status:** accepted 2026-09-21, extended by [ADR-0003](0003-control-feedback-and-household-features.md), §5 partly superseded by [ADR-0009](0009-the-openwebif-page-trusts-openwebif.md)
**Date:** 2026-09-21
**Supersedes:** - (it extends [ADR-0000](0000-prd.md); nothing in that record is reversed)

## Context

[ADR-0000](0000-prd.md) is the product requirements document as it was approved at M0. It is not
rewritten - that is the whole point of keeping it as a record - but the plugin that now runs on a
receiver does several things it does not mention. Some of those came from the one operator who has
a test box asking for them; some came out of reviews and out of what real hardware did. Either way
they are decisions, they constrain later work, and a reader who takes ADR-0000 as the current scope
will be wrong about them.

This record names them, says why each one exists, and - because most of them publish something or
accept something from the broker - says what each one costs in safety and in privacy. The topic
contract itself stays in [TOPICS.md](../TOPICS.md); this is why, not what.

It also says, at the end, what is **not** done, so that the gap between this repository's README and
its behaviour is written down rather than discovered.

## Decision

### 1. Active bouquet context - `bouquet`, `cmd/bouquet`, capability `bouquet_context`

Browsing a bouquet in a consumer was never changing the receiver's own channel list. The box has
one active television bouquet, and it is what its channel-up and channel-down actions walk; picking
a different bouquet in Home Assistant left that untouched, so the next CH+ on the physical remote
carried on through the previous list. A channel list a consumer can read but not enter is half a
feature.

- `bouquet` is a retained topic, `{name, sref}`, **read from the receiver's real service-list
  root** rather than inferred from the current channel. Both fields `null` is ordinary operation -
  the box may be in the radio list, the movie list, or a bouquet the configuration leaves out - and
  it means „channel up and down are not walking a list I know about", not „broken".
- `cmd/bouquet` takes `{"sref": "..."}` and matches it against the published bouquets **exactly**.
  Selecting one **keeps the current channel when that channel belongs to the bouquet**, and
  otherwise tunes the bouquet's first playable channel. Doing nothing would leave the household
  looking at a channel that is not in the list its remote now walks.
- An empty or marker-only bouquet, and a service-list API the image does not offer, are **refused
  without changing context**. A refusal is a sentence on `last_error`.
- A successful command **always republishes `bouquet`**, including when the requested bouquet was
  already active, so a consumer can require a fresh by-effect acknowledgement rather than inferring
  one from silence.
- `bouquet_context` is claimed on the first successful read of the service list, which on some
  images happens after the connect. That is the first capability that can **arrive late**, so
  `info` and the announcement are republished when it does.

Safety: the command changes what the remote walks and, in the worst case, which channel is playing.
It is on an exact allowlist, it is refused rather than approximated, and it never leaves the box in
a bouquet it could not verify.

### 2. Conditional-access telemetry - `cam` and `oscam`, both off by default

A receiver whose card stops decrypting looks exactly like a receiver that is working: the same
channel, the same programme, a black picture. There was no signal for it.

- `cam` publishes the current service's encryption flag, a **generic allowlisted** CA system name
  and a bounded ECM time. It is read from the receiver-local `/tmp/ecm.info`, opened without
  following symlinks, capped at 8 KiB. Reader, server, user, card, CAID, provider and raw lines are
  never published. `active` means a fresh valid ECM result was seen after the latest known service
  start - it is **not** a softcam-process check, and this topic must not drive access or recording
  decisions.
- `oscam` publishes software and API status, bounded aggregate counts, and one neutral entry per
  reader or server. Every source id is an **HMAC under a hidden persisted salt** of the reader's
  label: stable on this receiver, meaningless off it, and not an unsalted hash of a private name.
  Labels, addresses, users, card identifiers, CAIDs and WebIf credentials stay on the box.
- **Both default to off and are absent entirely until switched on**, and switching one off
  **retracts** its retained topic. Telemetry that is off has to mean nothing on the broker, not a
  stale payload the broker keeps serving.
- The OSCam probe runs **off the main loop**, queries only `status` and `readerlist` on
  `127.0.0.1`, follows no redirect, uses no environment proxy, invokes no action API, caps a
  response at 128 KiB and has a bounded deadline. A probe that has not finished is expired and
  **abandoned** - its late answer is discarded - and at most two abandoned workers may be
  outstanding, after which the state stays unknown until one returns. Old health is never presented
  as current.

Privacy: this is the most sensitive thing the plugin can publish, which is why it is off, opaque,
bounded and retractable. The settings are `cam_telemetry`, `oscam_telemetry`, `oscam_port`,
`oscam_username` and `oscam_password`, plus the internal `oscam_identity_salt`, which is never
shown, provisioned or echoed.

### 3. Remote settings - `cmd/config` and the `info.settings` echo

The companion integration needs an options page that actually changes the receiver, and telling a
user to walk to the television and open a setup screen is not an options page.

- `cmd/config` accepts a **strict allowlist**: `publish_keys`, `screenshot`,
  `screenshot_interval`, `screenshot_delay`, `cam_telemetry`, `oscam_telemetry`. The first three
  are required; the rest are independently optional so an older client keeps working and an omitted
  key keeps its current value.
- The whole object is **validated before anything is assigned**, persisted through enigma2's own
  settings store, and only then are the affected publishers rebound. A persistence failure applies
  **none** of it.
- Unknown keys and loose coercion are refused. Broker credentials, TLS, identity, topic names, the
  bouquet filter, logging and `deep_standby_allowed` **cannot be reached from the broker at all**.
  Home Assistant mode keeps its own command, and bouquet context keeps its own; neither widens this
  one.
- `info.settings` carries the complete non-secret subset back. It is the by-effect acknowledgement,
  and it is why a consumer can wait for the box instead of assuming.

🔴 **This makes the broker login the privacy boundary, and that is a deliberate consequence rather
than an oversight.** Anything that can publish on `<base>/<node>/cmd/config` can switch on
`screenshot`, `publish_keys` and both telemetry options, and with screenshots on it can then ask
for a picture of the television whenever it likes. There is no second, box-local confirmation,
because the integration's options flow is built on exactly this path. Give the receiver its own
broker credential and restrict it with an ACL; the recipe is in the README, and
[TOPICS.md](../TOPICS.md) says the same thing at the command. Whether a box-side lock should exist
anyway is an open question, recorded below.

### 4. `screenshot_delay` - post-zap settling

An on-zap capture taken when enigma2 raises its tune event is frequently a picture of the previous
channel, or of nothing. Enigma's events say a service was tuned; they do not say a video frame has
been decoded.

The capture now waits `screenshot_delay` seconds, default **4**, range 1-30. Another zap **resets**
the wait, and a grab still running for an older channel is **discarded and rescheduled**, so a run
of channel changes produces one picture of where the viewer stopped rather than a series of stale
ones. This is deliberately a bounded settling delay and not a claim to detect black video, audio or
image readiness - there is no such signal to read.

### 5. A status page inside OpenWebif at `/mqttbridge`

Somebody whose box will not connect to a broker has to read a log, and the only route to it was a
terminal. A receiver's owner should be able to see what the bridge thinks it is doing from the
machine they already use to administer the box.

- The page is **registered into OpenWebif** and shows what the bridge is connected to, the same
  publisher settings the remote can change, and a bounded tail of the plugin's log with the broker
  password and every other credential scrubbed out of it.
- It is **authenticated by OpenWebif**, and it **fails closed when OpenWebif authentication is
  off** - a box whose web interface is open to its LAN does not get a settings page from this
  plugin.
- A Content-Security-Policy is set, and every write carries a one-shot CSRF token.
- The log viewer is bounded in both bytes and lines.

ADR-0000 §3 decided **"no extra web server on the box"**, and that decision still holds: this opens
no listener, binds no port and starts no server. It is a child resource of the web server the image
already runs, which is the same server the plugin's screenshot fallback already points at. Had it
needed a socket of its own it would not have been built.

### 6. Runtime diagnostics and a main-loop stall monitor

A receiver that freezes for a minute and then carries on is the hardest kind of report to act on,
and the first guess is always the newest thing installed.

The plugin logs privacy-safe reconnect epochs, main-loop dispatch delay, backlog aggregates, and
per-publisher and total snapshot timings; a monitor reports both watcher-observed stalls and the
measured heartbeat gap, because native code can resume before a watcher gets to run. This is what
established that one such freeze was enigma2's own synchronous network-filesystem access in its
timeshift path and not anything on the MQTT session - a conclusion nobody could have reached from
the outside, and one that would otherwise have been guessed the other way.

### 7. `channels`, and one EPG grid topic per bouquet

[ADR-0001](0001-m0-decisions.md) put the EPG grid in v1 and its amendment made it one retained
topic per bouquet. Two things follow that are worth naming here because consumers depend on them:

- **`channels`** publishes the configured bouquets and the **playable** services in each, in the
  user's own order. Markers, separators and hidden entries arrive from the same call and are
  dropped, because a marker offered as a channel is an option that cannot be tuned. It is what a
  channel list is built from, what `cmd/zap` by name resolves against, and what the `select`
  component in discovery mode offers.
- Picons are **not** published. They are tens of kilobytes each and the box's own web interface
  already serves them on the same address.
- A grid builds **four channels per turn of the main loop**, yielding 20 ms between batches, and a
  bouquet is published only once complete. The previous retained grid stays current meanwhile. A
  two-hundred-channel bouquet is two hundred EPG lookups, and doing them in one callback holds the
  thread that draws the television.

### 8. The build is reproducible, and the source travels with the binary

The companion integration ships this plugin's IPK so that an install needs no download. A binary
shipped inside another project is only trustworthy if anyone can rebuild it: its CI therefore
reproduces the IPK **byte for byte from the named plugin commit**, and the package is distributed
together with its corresponding GPL source archive. `tools/build-ipk.sh` is what makes that
possible, and it stays reproducible on purpose - same inputs, same bytes.

## Consequences

- The v1 contract is larger than ADR-0000 describes: three state topics (`bouquet`, `cam`,
  `oscam`), three commands (`bouquet`, `config`, `epg_grid`), the `info.settings` object and three
  capability names (`bouquet_context`, `cam`, `oscam`). All of them are in
  [TOPICS.md](../TOPICS.md), which remains the contract.
- Two of those topics are privacy-sensitive, so „off by default, absent when off, retracted when
  switched off" is now a rule this project follows rather than a property of one feature.
- `cmd/config` is a permanent widening of what the broker can do to the receiver. It is bounded by
  an allowlist that is small on purpose, and the boundary it leaves is stated in the README, in
  TOPICS.md and here, in the same words.
- A capability may now appear **after** the connect, so `info` and the announcement can be
  published more than once per connection. Any consumer must accept that - it already had to,
  because `cmd/config` and `cmd/ha_mode` both republish them.
- The status page ties a plugin feature to OpenWebif's authentication being enabled. That is a
  deliberate dependency: it is the only authentication on the box the plugin can honestly rely on,
  and inventing a second one would be worse.

## Not yet done

Stated so that the README and the behaviour do not drift apart again:

- **There is no 0.2.0 release.** Everything above is on `main` and running on the maintainer's box;
  the opkg feed and the releases page still serve **0.1.0**. A coordinated version bump with the
  companion integration comes first - until then both report `0.1.0` and version equality cannot
  tell two development builds apart.
- **The call-for-testers issues do not exist.** This README claimed there was one per image. There
  is not, and the claim is removed rather than the issues invented. Until they exist: testers are
  wanted, please open an issue.
- **`CONTROL/prerm` and `CONTROL/postinst` do not sweep orphaned `.pyc` files on an upgrade.**
  Bytecode from a removed module has been seen surviving an upgrade on real hardware. The fix is
  obvious and the risk is not: a package script that deletes files under `/usr/lib/enigma2` has to
  be exactly right, and it is the one part of the package that cannot be tested off a receiver.
- **M2's acceptance is not complete.** The by-effect checklist and a 60-minute active soak have
  passed on the one test box; the **long passive soak** and the **watchdog-restart interplay** are
  deferred, not passed, and **deep standby with Wake-on-LAN has never been drilled**.
- **One test box, one image.** Everything here was verified on OpenViX 6.6. Capability detection is
  what keeps that honest, but it is not a substitute for a second image.

## Proposed

Not decided, recorded so the argument is visible:

**Diagnostic sensors for enigma2's resident memory, thread count and process uptime.** Over 50
hours of ordinary household use the receiver's own process grew from roughly 122 MB to 245 MB. That
growth was measured and attributed: a screen grab costs the image about 22 kB and a zap about
50 kB, neither of which comes back, and read-only web-interface polling measured no growth per
request at all - the plugin's own paths sit at the noise floor, and the most plausible remaining
contributor is the image's EPG cache. What nobody can say yet is whether it **plateaus**, because
answering that needs the curve recorded over days rather than sampled by a person.

Publishing RSS, threads and process uptime as three diagnostic values would let any consumer record
it. The cost is three more entities on every install and a `/proc` read on a slow timer, to answer a
question about the image rather than about this plugin. Worth doing if a second box shows the same
curve; not worth doing for one.
