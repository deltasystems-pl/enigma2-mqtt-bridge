# Roadmap

Where the plugin stands and what comes next. What each release changed is in
[CHANGELOG.md](CHANGELOG.md); why the work was split the way it was is in the decision records in
[docs/adr/](docs/adr/).

## Where it stands

Plugin v0.3.0 is released: the releases page and the opkg feed serve it. The companion
integration [hass-enigma2-mqtt](https://github.com/deltasystems-pl/hass-enigma2-mqtt) v0.3.0 is
released too, through HACS.

0.2.0 fixed what two days of household use turned up, and 0.3.0 added what the household asked
for. The plan for both is in
[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md).

## Milestones

- [x] **M0** - PRD approved and recorded as [ADR-0000](docs/adr/0000-prd.md); the three open
      questions closed in [ADR-0001](docs/adr/0001-m0-decisions.md). The scope added since is
      [ADR-0002](docs/adr/0002-scope-after-m0.md).
- [x] **M1** - repository and skeleton: the plugin loads, connects, publishes `availability`
      and `info`, and has a setup screen. Released as v0.1.0.
- [x] **M2** - power, service, EPG, volume, recording, timers, disk, keys, screenshot, the channel
      list, the EPG grid, bouquet context, the optional CAM and OSCam telemetry, and every `cmd/*`
      with its guards. Released as v0.2.0. The by-effect checklist and a 60-minute active soak
      have passed; see the open items below for what has not.
- [x] **M3** - the integration's entities, released as the companion integration's v0.2.0.
- [x] **M4** - the guided installer and the `update` entity, exercised end to end on one
      receiver. The installer has been run repeatedly on a box that did not have the plugin, and
      the rollback has been exercised for real: a deliberately wrong broker password, the plugin
      refused, the receiver restored to the byte, the lock released, and "the receiver was
      restored" reported as a fact rather than a guess. The correct run then ended on the success
      screen. Four defects those runs found (a pending discovery offer blocked the installer, the
      success screen was lost, the rollback misjudged the restart and left its lock behind, and a
      receiver at default settings was refused as "different") are fixed, and the integration's
      v0.2.0 carries the fixes.
- [ ] **M5** - public beta `v0.x`: releases, opkg feed, HACS custom repository, testers per image.
- [ ] **M6** - `v1.0.0`: submission to the OE-Alliance third-party feed, so the plugin appears in
      the plugin browser of OpenViX, OpenATV and related images with no feed to add, and the HACS
      default pull request.
- [ ] **M7** - OE-Alliance recipe, OpenPLi, broker-login auto-provisioning.

A telnet transport for the guided installer, for images that ship with SSH switched off, is
planned for v1.1.

## Open items

Things M2 promised that have not been done yet:

- **The long passive soak** - not run yet.
- **The watchdog-restart interplay** - not tested yet.
- **The deep-standby drill** - deep standby followed by a Wake-on-LAN magic packet has never been
  tested on any image. A receiver whose `info.wol` reports `supported: false` cannot be woken over
  the network from deep standby at all, only by its remote, its front button or a timer; the
  maintainer's Uno 4K SE is one. On other receivers, treat deep standby as possibly one-way until
  that drill has passed on your image.

## How you can help

Every image other than OpenViX 6.6 needs a tester, and the German translation needs a native
speaker's review. [CONTRIBUTING.md](CONTRIBUTING.md) explains both.
