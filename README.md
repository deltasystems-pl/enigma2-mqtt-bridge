<h1 align="center">Enigma2 MQTT Bridge</h1>

<p align="center">
  <a href="https://github.com/deltasystems-pl/enigma2-mqtt-bridge/actions/workflows/ci.yml"><img src="https://github.com/deltasystems-pl/enigma2-mqtt-bridge/actions/workflows/ci.yml/badge.svg" alt="CI"></a>
  <a href="https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases"><img src="https://img.shields.io/github/v/release/deltasystems-pl/enigma2-mqtt-bridge?include_prereleases&sort=semver" alt="Release"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-GPL--2.0--or--later-blue.svg" alt="License: GPL-2.0-or-later"></a>
  <img src="https://img.shields.io/badge/Python-3.9%2B-blue.svg" alt="Python 3.9+">
</p>

An Enigma2 plugin that keeps one MQTT session to your broker and pushes the receiver's state
the moment enigma2 raises the event — zap, EPG change, standby, recording, volume, remote key —
instead of making Home Assistant poll OpenWebif every fifteen seconds. Commands travel back on
the same session, and the box announces itself with standard Home Assistant MQTT discovery, so a
working set of entities appears without any custom integration at all.

## What it does

- **Push, not poll.** State lands on the broker as the event happens; there is no 15 s ceiling
  on „the TV just changed channel".
- **Availability that tells the truth.** A retained last will means Home Assistant sees the box
  disappear within the keepalive window, not after a ten-minute poll watchdog.
- **Two-way.** Power and standby, zap by service reference or by channel name, volume and mute,
  remote keys (short and long), on-screen messages, recording start/stop, timers, screenshots.
  Every command is verified by effect and answered on its state topic; failures land on
  `last_error`.
- **Home Assistant discovery out of the box**, or a quieter announcement-only mode for the
  companion integration to build richer entities on top.
- **Nothing native, nothing to compile.** Pure Python, `Architecture: all`, one vendored
  dependency (paho-mqtt), no outbound connection other than your broker.
- **It never takes the GUI down with it.** Bad config means one log line and an idle plugin;
  no hook blocks the main thread; deep standby and reboot are refused while a recording runs.

State is published under `enigma2/<node_id>/…`; commands arrive on `enigma2/<node_id>/cmd/…`.
The full contract is in [docs/TOPICS.md](docs/TOPICS.md).

## Supported images

| Image | Python | enigma2 flavour | Status | Test box |
|---|---|---|---|---|
| OpenViX 6.6 | 3.12 | OE-Alliance 5.4 | **supported** | Vu+ Uno 4K SE |
| OpenATV 7.4 / 7.5 | 3.12 | OE-Alliance | **supported** | community tester needed |
| OpenPLi 9.x | 3.9+ | OpenPLi core | best effort | community |
| OpenBH 6.0 | 3.14 | OE-Alliance fork | best effort | community |
| VTi 15 / 16 | 2.7 | OE 2.0 | unsupported | — |

The code uses no syntax above Python 3.9 and is tested on 3.9, 3.12 and 3.14. Every enigma2
import is guarded, and what the running image cannot provide is reported in the `capabilities`
list rather than assumed — so a missing hook loses one feature, not the plugin.

**Testers wanted — please open an issue.** If you run an image that is not in the first two rows,
your report is what moves a row from *best effort* to *supported*. There is no per-image thread to
find yet; opening the first one is genuinely useful.

## Security first

Most Enigma2 boxes ship with a well-known root password and an open telnet or SSH port. This
plugin stores your broker credentials on that box, so before you install it:

1. **Change the box's root password.** Anything on the LAN can otherwise read the credential
   this plugin needs.
2. **Give the box its own broker login** — never the one Home Assistant itself uses.
3. **Restrict that login with an ACL.** For Mosquitto, with `<node_id>` replaced by the node id
   the plugin shows on its setup screen:

   ```
   user enigma2box
   topic readwrite enigma2/<node_id>/#
   topic write enigma2mqtt/discovery/<node_id>/#
   topic write homeassistant/device/<node_id>/#
   topic write homeassistant/device_automation/<node_id>/#
   ```

   Verify the ACL by effect, not by reading it back: subscribe to a topic the box has no business
   writing to and publish there as the box's user. Mosquitto drops an ACL-denied publish
   **silently** — the publisher sees success either way.
4. TLS to the broker is optional (`tls`, `ca_file`); client certificates are not in v1.

🔴 **That ACL is the privacy boundary.** Anything able to publish on `<base>/<node>/cmd/config`
can switch on screenshots, key reporting and the CAM and OSCam telemetry, and then ask for a
picture of the television whenever it likes — the companion integration's options flow is built on
exactly that path, so the plugin does not ask the box for a second confirmation.

The plugin has no telemetry, no cloud component and no update check that phones home.

## Install

**From a GitHub release** (any image, one line on the box). Every release carries the IPK and its
SHA-256 on the [releases page](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases):

```sh
opkg install https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/download/v0.2.0/enigma2-plugin-extensions-mqttbridge_0.2.0_all.ipk
```

**From the opkg feed**, which gets you updates through the normal plugin browser — write

```
src/gz enigma2-mqtt-bridge https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed
```

into `/etc/opkg/enigma2-mqtt-bridge.conf`, then `opkg update && opkg install
enigma2-plugin-extensions-mqttbridge`.

**From the image's plugin browser** — from v1.0, through the OE-Alliance third-party feed that
OpenViX, OpenATV and their siblings already consume.

Restart enigma2 afterwards; the package deliberately never restarts the GUI by itself.
Details, the manual `scp` route and how to uninstall cleanly are in
[docs/INSTALL.md](docs/INSTALL.md).

## Configure

*Menu → Plugins → MQTT Bridge* opens a setup screen with the broker host and port, the
credentials, the node id, the friendly name, the Home Assistant mode and the rest. Every setting
and its default is listed in [docs/SETUP.md](docs/SETUP.md).

For headless installs there is a one-shot provisioning file. Write
`/etc/enigma2/mqttbridge.json` before the first start:

```json
{
  "host": "192.0.2.10",
  "port": 1883,
  "username": "enigma2box",
  "password": "the-broker-password",
  "node_id": "vuuno4kse_005301",
  "friendly_name": "Living room receiver",
  "ha_mode": "discovery"
}
```

The plugin imports those keys into its config at start and then **deletes the file**, because it
holds a password. The setup screen shows the same values afterwards.

## Home Assistant

Two modes, chosen by the `ha_mode` setting:

- **`discovery`** (default) — the plugin publishes standard Home Assistant MQTT discovery
  payloads and HA's own MQTT integration creates the entities. Nothing else to install.
- **`integration`** — the plugin publishes only its announcement and leaves entity creation to
  [hass-enigma2-mqtt](https://github.com/deltasystems-pl/hass-enigma2-mqtt), which adds what
  discovery cannot express: a native `media_player` with channel browsing, a `remote`, a
  `notify` target for on-screen messages, device triggers for the colour keys and an `update`
  entity. The integration switches the box into this mode itself, and the plugin retracts its
  discovery payloads first so entities are never duplicated.
- **`off`** — no discovery, no announcement. State topics still publish, for openHAB, Node-RED
  or anything else that speaks MQTT.

Using another home-automation system, or want a media player without the custom integration?
[docs/SETUP.md](docs/SETUP.md) has a `universal` media_player recipe built entirely from the
discovery entities.

## Topics

Topic strings, retain flags, QoS, payload fields and their types, every command and its guard:
[docs/TOPICS.md](docs/TOPICS.md). That document is the contract — the plugin and the integration
are both written against it, and it is versioned with the plugin.

## Privacy

The `key` and `epg` topics reveal what is being watched and what is being pressed, and they land
in Home Assistant's recorder database by default. If that matters in your household:

- exclude the screenshot image entity from the recorder, and decide deliberately about the
  programme-title sensor. The key `event` entity is worth excluding too — but it only exists
  **with the companion integration**; in plain discovery mode the keys are MQTT device triggers,
  which are not entities and cannot be excluded by name;
- turn `publish_keys` off if you do not automate on remote keys — on a plugin-only install that
  is the control, and it is the stronger one either way, because nothing reaches the broker;
- set `screenshot` to `off` — it is a picture of your screen on the broker, retained;
- adjust `screenshot_delay` (four seconds by default) if the image needs longer to settle after a
  channel change; rapid zaps reset the delay and stale in-flight captures are discarded;
- leave `cam_telemetry` off unless you need conditional-access diagnostics. When enabled it
  publishes only the generic CA system, current-service encryption flag and bounded fresh ECM timing,
  never reader, server, user, card or raw ECM data;
- leave `oscam_telemetry` off unless you need software and reader/server health. It queries only
  receiver-local read-only WebIf views and publishes opaque source ids and bounded aggregate
  counts; raw reader names, addresses, users, card identifiers and WebIf credentials stay on the
  receiver;
- remember that retained topics outlive the plugin: `cmd/reset` retracts everything, and it is
  the documented step before uninstalling.

## Compatibility

| Plugin | Integration |
|---|---|
| 0.1.0 | 0.1.0 |
| 0.2.0 (current) | 0.2.0 (current) |

The integration warns on its `update` entity when the box runs a plugin older than the one it
bundles.

## Roadmap

- [x] **M0** — PRD approved and recorded as [ADR-0000](docs/adr/0000-prd.md); the three open
      questions closed in [ADR-0001](docs/adr/0001-m0-decisions.md). The scope added since is
      [ADR-0002](docs/adr/0002-scope-after-m0.md)
- [x] **M1** — repository and skeleton: the plugin loads, connects, publishes `availability`
      and `info`, and has a setup screen. Released as **v0.1.0**
- [x] **M2** — power, service, EPG, volume, recording, timers, disk, keys, screenshot, the channel
      list, the EPG grid, bouquet context, the optional CAM and OSCam telemetry, and every `cmd/*`
      with its guards. Released as **v0.2.0**, which is what the feed and the releases page serve.
      The by-effect checklist and a 60-minute active soak have passed; the **long passive soak**
      and the **watchdog-restart interplay** have not, and **deep standby with Wake-on-LAN has
      never been drilled**
- [x] **M3** — the integration's entities: *released as the companion integration's **v0.2.0***
- [x] **M4** — the guided installer and the `update` entity, *exercised end to end on one
      receiver.* The installer has been run repeatedly on a box that did not have the plugin, and
      the rollback has been exercised for real: a deliberately wrong broker password, the plugin
      refused, the receiver restored to the byte, the lock released, and „the receiver was
      restored" reported truthfully rather than as a guess. The correct run then ended on the
      success screen. Four defects those runs found — a pending discovery offer blocked the
      installer, the success screen was lost, the rollback misjudged the restart and left its lock
      behind, and a receiver at default settings was refused as „different" — are fixed, and the
      run above is the one after the fixes, and the integration's **v0.2.0** carries them
- [ ] **M5** — public beta `v0.x`: releases, opkg feed, HACS custom repository, testers per image
- [ ] **M6** — `v1.0.0`: third-party feed and HACS default pull requests
- [ ] **M7** — OE-Alliance recipe, OpenPLi, broker-login auto-provisioning

The feed and the releases page serve **0.2.0**, which is what this tree builds, and the companion
integration's **v0.2.0** is released alongside it. Everything after M4 is unreleased, and two
things M2 itself promised are still open: the **long passive soak** and the **deep-standby
drill**.

### What 0.2.0 shipped, and what 0.3.0 will carry

Two days of household use produced a list of problems and a list of wants, and they were split
into two releases. The reasoning is in
[ADR-0003](docs/adr/0003-control-feedback-and-household-features.md); the contract additions that
are still ahead are in [docs/TOPICS.md](docs/TOPICS.md) under **Planned (not implemented yet)**.

**0.2.0 — fixes**, released 2026-09-22 — both halves, this plugin and the companion integration.
Everything M2 covers, and from that list of problems:

- `deep_standby_allowed` is echoed **read-only** in `info.settings`, so a consumer can tell „the
  box refused this" from „the box cannot do this" and hide a control that would always fail.
  🔴 This changes what `info.settings` means: presence no longer implies writability.
- Removing or upgrading the package no longer leaves the plugin behind as compiled bytecode the
  receiver imports on the next graphical-interface restart.

The full list is in [CHANGELOG.md](CHANGELOG.md).

**0.3.0 — features**, in this order:

1. **`cmd/softcam_restart`** — restart the cam *the image selected*, resolved on the box and never
   named over MQTT, behind a permission that is never writable over MQTT; plus an opt-in
   auto-heal for a stuck decode, rate-limited and counted on a new `softcam` topic.
2. **An opt-in CEC standby workaround** for an upstream enigma2 defect that makes a standby
   requested by the television arrive late and echo back at it. It closes the channel list and
   nothing else, never touches a standby the household asked for, and counts every intervention
   on a `cec` topic.
3. **A discreet toast** — a non-modal, auto-hiding, top-right message that never takes focus and
   never waits behind an open channel list. `cmd/message` gains `style: popup | toast`; `popup`
   stays the default.
4. **`cmd/epg_import`** — run the image's EPG importer, behind a permission that is never
   writable over MQTT, with progress on an `epg_import` topic that follows every import, whoever
   started it. The import is the image's: its end freezes the menus for two or three seconds.
   Decided in [ADR-0011](docs/adr/0011-epg-import-on-demand.md).
5. **`wol_arm`** — arm Wake-on-LAN at start and before deep standby, and report in `info.wol` what
   is actually true. 🔴 Until the deep standby → magic packet drill passes, **deep standby may be
   one-way on your box.**
6. **`process`** — what the enigma2 process costs, already in review.
7. **`cmd/uninstall`** — remove the plugin from the receiver on request, behind a permission
   `uninstall_allowed`, never writable over MQTT and echoed **read-only** in `info.settings`: every retained topic is
   retracted, a final `offline` published, the package removed and the interface restarted, in
   that order. The payload is the node id, so a mis-sent message removes nothing.
   🔴 A one-way door — nothing over MQTT can put it back, only SSH or the receiver's own package
   manager. Decided in [ADR-0004](docs/adr/0004-remote-uninstall.md).

## Contributing

Read [CONTRIBUTING.md](CONTRIBUTING.md) for the development loop, the test matrix and the rule
that every change lands through a pull request with CI green.

Two things are worth more than code right now:

- **Testers.** One box, one image, one afternoon. OpenATV, OpenPLi and OpenBH all need somebody
  who can run the by-effect checklist and paste the log. **Testers wanted: open an issue** — there
  is no per-image thread yet, so yours starts it.
- **Translators.** The source strings are English, the Polish ones are reviewed, and the
  **German ones are drafted and marked for review** — a native speaker's pass would be very
  welcome.

Decisions live as ADRs in [docs/adr/](docs/adr/); the security policy is in
[SECURITY.md](SECURITY.md); the version history is in [CHANGELOG.md](CHANGELOG.md).

## License

[GPL-2.0-or-later](LICENSE) — enigma2 is GPL-2.0 and this plugin imports its modules.
The vendored paho-mqtt is used under its EDL-1.0 option; see [NOTICE](NOTICE).
