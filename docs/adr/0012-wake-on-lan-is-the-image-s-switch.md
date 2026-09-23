# ADR-0012: Wake-on-LAN is reported from the image, and armed only through the image's own switch

**Status:** accepted 2026-09-23
**Date:** 2026-09-23
**Supersedes:** [ADR-0003](0003-control-feedback-and-household-features.md) §6 — the mechanism
(`ethtool` at start and again before deep standby, through `eConsoleAppContainer`) and the shape of
`info.wol`. The setting `wol_arm`, its default and the rule that it is never writable over MQTT
stand.

## Context

ADR-0003 §6 started from a real measurement: `ethtool eth0` on the maintainer's receiver (a Vu+
Uno 4K SE on OpenViX 6.6, driver `bcmgenet`) reports `Supports Wake-on: gs` and `Wake-on: d`. It
concluded „supported and disabled, so arm it". Reading the image, its shutdown scripts, its kernel
and its vendor modules before writing the code showed the conclusion does not follow.

**`Supports Wake-on` is the Ethernet MAC's ability to wake the system from a Linux suspend.** The
driver programs its magic-packet detector only in its suspend callback, which returns early when
the interface is down; it has no shutdown hook. Its Wake-on-LAN interrupt is registered and, on
that receiver, has never fired.

**An enigma2 image never suspends.** Deep standby is the GUI exiting with code 1, `enigma2.sh`
calling `/sbin/halt`, runlevel 0 taking the network down (`ifdown -a`), the vendor's shutdown tool
handing the box to the front processor, and `halt -p` — a power-off. Nothing on that path reads the
flag `ethtool -s eth0 wol g` sets, and no point a plugin can reach before it is late enough to
matter.

**The image has its own Wake-on-LAN, and it is a front-processor switch.** `Components.SystemInfo`
records `SystemInfo["WakeOnLAN"]` as the path of `/proc/stb/fp/wol` — or `/proc/stb/power/wol`, on
the two machines that have it — when the driver created one, and `False` when it did not. Only then
does the image build `config.usage.wakeOnLAN` („Wake On LAN", expert level), whose notifier writes
`enable`/`disable` (or `on`/`off`) into that file at every start. On the measured receiver neither
file exists, so the image hides its own setting there. Vendor and community answers agree that the
model has no Wake-on-LAN from deep standby; the physical outcome on that box is still to be drilled.

So an implementation of §6 would have run a command that succeeds, read back `Wake-on: g`, and
published `armed: true` for a receiver that cannot be woken — a success message that means nothing,
retained on a topic.

## Decision

- **`info.wol` is `{supported, armed, iface, mechanism}`, read from the image at every `info`
  publish.** `supported` is `SystemInfo.get("WakeOnLAN")` being a path. `armed` is that file read
  back — `enable`/`on` true, `disable`/`off` false, either vocabulary from either file — and, where
  the file cannot be read or says neither, the image's own `config.usage.wakeOnLAN`, since its
  notifier is what writes the file; `null` when not supported or when neither answers. `iface` is
  the interface `info.mac` is read from, by the same rule; `mechanism` is `fp` or `power`, named by
  the directory the file is in (and, for a path under neither, by the image's own rule: `fp`
  anywhere in the path). Every key is always present, `null` for what could not be read. No
  capability name: `supported` is the statement.
- **`armed` is never inferred from what the plugin did.** Not from `wol_arm`, and not from the fact
  that a setting was written.
- **`wol_arm` switches on the image's own setting, and nothing else.** Default off, box-only: the
  setup screen, the provisioning file and the OpenWebif page, never `cmd/config`. At every start of
  the bridge — which is also how the setup screen and the page apply a change — it sets
  `config.usage.wakeOnLAN` to on and saves it, where the image found its switch and built that
  setting. A setting that is already on is left alone. It is not in `info.settings`: it is a
  request, and `info.wol` already says what the receiver is.
- **Switching `wol_arm` off never switches the image's setting off.** Somebody may have switched it
  on in the image's own menu; the plugin arms, it does not disarm.
- **Where the image has no switch, `wol_arm` is inert, and says so.** Its label on the setup screen
  and on the OpenWebif page carries „Wake-on-LAN is not available on this receiver".
- **No `ethtool`, and no process of any kind.** Nothing in this feature spawns a command; a test
  asserts it.

## Consequences

- **On the maintainer's receiver the feature is the report**: `{"supported": false, "armed": null,
  "iface": "eth0", "mechanism": null}`. The README states that a receiver reporting
  `supported: false` cannot be woken over the network from deep standby — only by its remote, its
  front button or a timer — which is what closes the „deep standby may be one-way" question for that
  box: by documenting it, not by fixing it.
- **`supported: true` is not evidence that a packet wakes the box.** It says the image found a
  switch. Nothing may describe this feature as waking a receiver until a deep standby → magic packet
  drill has passed on an image that has the switch — a call-for-testers item, since the
  maintainer's receiver does not.
- **`armed` on a supported box rests on a file whose read format nobody here has measured.** If a
  driver makes it write-only, the fallback to the image's setting answers instead; `docs/TOPICS.md`
  says so.
- **`wol_arm` changes an image setting on the user's behalf.** That is the reason it is off by
  default, box-only, and one-directional.
- **A consumer that wants to warn before deep standby reads `info.wol.supported`.** An older plugin
  publishes no `wol` at all, which is silence, not `false`.
- **If this is ever reversed toward `ethtool`,** the evidence above has to be answered first: on an
  image that powers off rather than suspends, the flag is not on the path.
- Adjacent and not decided here: the image already wakes itself on a **timer**
  (`setFPWakeuptime`). A „wake at a time" feature is possible and is a different feature.
