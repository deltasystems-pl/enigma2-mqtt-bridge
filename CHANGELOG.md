# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog 1.1.0](https://keepachangelog.com/en/1.1.0/) and this
project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html). Dates are release
dates. An `Unreleased` section is always present; `tools/build-ipk.sh` refuses to build a
version that has no section here.

## [Unreleased]

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
  kept, so it survives a reinstall.
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

### Changed

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

[Unreleased]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/commits/main
