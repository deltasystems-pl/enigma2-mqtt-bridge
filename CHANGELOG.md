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
  vendored MQTT client. The runtime lands in the next milestone.
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
