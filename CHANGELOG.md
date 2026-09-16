# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dates are release
dates. An `Unreleased` section is always present; `tools/build-ipk.sh` refuses to build a
version that has no section here.

## [Unreleased]

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

### Changed

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
- `docs/TOPICS.md` gained the `channels` topic, the capability vocabulary's thirteenth name, the
  discovery entity table, and the four things about Home Assistant 2026.9 that were measured
  rather than assumed — `default_entity_id` in place of `object_id`, removal by platform key,
  QoS 1 for commands, and shared availability.

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

[Unreleased]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/compare/v0.1.0...HEAD
[0.1.0]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/tag/v0.1.0
