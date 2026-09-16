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

[Unreleased]: https://github.com/deltasystems-pl/enigma2-mqtt-bridge/commits/main
