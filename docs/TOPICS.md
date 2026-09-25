# The topic contract

This is the interface between the plugin and everything that consumes it - the companion Home
Assistant integration, an openHAB binding, a Node-RED flow, a shell script with `mosquitto_sub`.
It is versioned with the plugin: a change to a payload is a change to this file, to the
compatibility table in both READMEs, and to the changelog.

Two names appear throughout:

- **`<base>`** - the `base_topic` setting, `enigma2` by default.
- **`<node>`** - the `node_id` setting, `<boxtype>_<last six MAC digits>` by default, lowercase
  ASCII, for example `vuuno4kse_005301`. It is stable across reinstalls and is the unique id the
  integration keys on.

Conventions that hold everywhere:

| | |
|---|---|
| Protocol | **MQTT 3.1.1**. No MQTT 5 feature is used or required - no user properties, no response topics, no subscription identifiers, no shared subscriptions - so the contract holds on every broker a receiver can be pointed at, including the old ones. A broker running MQTT 5 serves it unchanged. |
| Encoding | UTF-8. Every payload is JSON unless the table says otherwise. |
| **State topics** | QoS **0**, **retained** (the exceptions are marked). A fresh subscriber gets the current state immediately, without asking the box for it. The one exception to the QoS: the retractions and the final `offline` of `cmd/uninstall` go out at QoS 1 (§2). |
| **Command topics** | QoS **1**, **never retained**. A retained command would re-fire on every reconnect; the plugin refuses to publish one and you should not either. |
| Timestamps | `begin`, `end`, `generated`, `ts` are **Unix epoch seconds, UTC, integer**. Never a formatted string, never local time. |
| Absent values | `null` for a field that has no value right now (no next event, no recording). A key is not silently dropped. |
| Retraction | An empty payload published retained. That is how `last_error` is cleared and how `cmd/reset` and discovery retraction work. |

Everything the plugin publishes it also publishes again on every `on_connect` - the full state
snapshot, the announcement and, in discovery mode, the discovery payloads. A broker that lost its
retained store, or a box that reconnected after an outage, converges without anybody asking.

---

## 1. State topics

### `<base>/<node>/availability`

Retained, QoS 0. **Not JSON** - the literal string `online` or `offline`.

`offline` is registered as the connection's last will, so the broker publishes it when the box
vanishes without saying goodbye. The plugin publishes `online` in `on_connect` and a clean
`offline` on a graceful shutdown. A settings save restarts the session with a clean disconnect,
which discards the will; when the save changed the connection - broker address or port, login,
TLS - or switched the plugin off, the old session publishes `offline` first, so it stays retained
until the new session connects, and for good if it never does. Any other save reconnects without
an `offline`. A rename of the node id or the base topic publishes none either: every retained topic
under the old name, `availability` included, is retracted, and the new name's `availability` is
published only by the new session. `cmd/uninstall` ends on `offline` too, published at QoS 1 as the
node's last message, after every other retained topic of the node has been emptied - which is what
tells a removed plugin from a switched-off receiver: a switched-off one leaves `info` and the
announcement retained.

### `<base>/<node>/info`

```json
{
  "image": "OpenViX 6.6.007",
  "enigma": "2024-09-11-Release",
  "plugin": "0.3.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "uptime": 384210,
  "wol": {"supported": false, "armed": null, "iface": "eth0", "mechanism": null},
  "ha_mode": "discovery",
  "settings": {"publish_keys": true, "screenshot": "on_zap",
               "screenshot_interval": 60, "screenshot_delay": 4,
               "cam_telemetry": false, "oscam_telemetry": false,
               "softcam_autoheal": false, "softcam_autoheal_seconds": 90,
               "deep_standby_allowed": false, "softcam_restart_allowed": false,
               "epg_import_allowed": false, "uninstall_allowed": false},
  "capabilities": ["power", "service", "epg", "tuner", "softcam", "recording", "timers",
                   "volume", "hdd", "process", "channels", "bouquet_context", "epg_grid", "keys",
                   "screenshot", "toast", "message"]
}
```

| Field | Type | Meaning |
|---|---|---|
| `image` | string | Image name and version as the box reports it |
| `enigma` | string | enigma2's own version string, as `getEnigmaVersionString()` reports it - on OE-Alliance images a build date such as `2024-09-11-Release`, not a number |
| `plugin` | string | This plugin's version - what an `update` entity compares against |
| `boxtype` | string | Machine name, lowercase |
| `mac` | string | Lowercase, colon-separated; the Wake-on-LAN target |
| `ip` | string | Current LAN address |
| `uptime` | int | Seconds since boot |
| `wol` | object | Since 0.3.0. What the **image** says about Wake-on-LAN - whether it has a switch for it and whether that is on. See below |
| `ha_mode` | string | `discovery` \| `integration` \| `off` - the acknowledgement of `cmd/ha_mode` |
| `settings` | object | The complete non-secret settings a consumer may **read**. Writable through `cmd/config`: `publish_keys` (bool), `screenshot` (`off` \| `on_zap` \| `interval`), `screenshot_interval` (integer seconds, 5-3600), `screenshot_delay` (post-zap settling seconds, 1-30), `cam_telemetry` (bool, off by default), `oscam_telemetry` (bool, off by default), `softcam_autoheal` (bool, off by default), `softcam_autoheal_seconds` (integer seconds, 30-600). **Read-only**: `deep_standby_allowed`, `softcam_restart_allowed`, `epg_import_allowed` and `uninstall_allowed` (bools, off by default). See below. |
| `capabilities` | list of strings | Which hooks this image actually gave the plugin |

**Presence in `settings` is not permission to write it back.** Until 0.2.0 this object was „the
complete remotely writable subset" and the two things were the same; they are not any more. The
**writable** members are the `cmd/config` allowlist and nothing else, and a consumer that writes
back everything it reads is refused - an unknown key fails the whole object, so it loses the
settings it did mean to change as well.

| Read-only member | Since | Meaning | Where it is set |
|---|---|---|---|
| `deep_standby_allowed` | 0.2.0 | Whether `cmd/deep_standby` and `cmd/reboot` are permitted over MQTT. Always present, whichever way it is set | On the receiver: the setup screen, *Menu -> Plugins -> MQTT Bridge*, the provisioning file at first install, or the plugin's OpenWebif page - never over MQTT |
| `softcam_restart_allowed` | 0.3.0 | Whether `cmd/softcam_restart` is permitted over MQTT, and with it the opt-in auto-heal. Always present, whichever way it is set | The same places, and never over MQTT |
| `epg_import_allowed` | 0.3.0 | Whether `cmd/epg_import` is permitted over MQTT. Always present, whichever way it is set | The same places, and never over MQTT |
| `uninstall_allowed` | 0.3.0 | Whether `cmd/uninstall` is permitted over MQTT. Always present, whichever way it is set. A consumer offers the removal only on a **stated** `true` together with the `uninstall` capability: for a one-way door, silence means no | The same places, and never over MQTT |

The rule behind which side of the line a setting falls on: one that **enables a command** is never
writable over MQTT - it is set on the receiver (the setup screen, the provisioning file, or the
OpenWebif page, which is exactly as open as the receiver's web interface; see
[ADR-0009](adr/0009-the-openwebif-page-trusts-openwebif.md)); one that **tunes a command already
permitted** may be remote. A member of this table exists so a consumer can **hide what the box will
refuse** rather than offering a control that always fails. `info` is republished when it changes,
because saving the setup screen or the OpenWebif page reconnects the bridge and a connect publishes
the snapshot.

A command run from the OpenWebif page does not need these permissions: the page runs the same
handler with the origin `page`, and anybody OpenWebif admits could grant the permission through
OpenWebif's own `saveconfig` anyway. Everything else about the command is unchanged - the
household-safety guards apply, and `last_error` is published or cleared exactly as for a command
over MQTT.

**`capabilities` is the honest part of the contract.** Hook names differ between images, so the
plugin detects what it managed to attach and names it here rather than assuming. A consumer
hides what is missing instead of showing a dead entity. The names are the feature areas, and
nothing else is ever in the list: `power`, `cec_workaround`, `service`, `epg`, `epg_grid`,
`tuner`, `recording`, `timers`, `volume`, `cam`, `oscam`, `softcam`, `keys`, `screenshot`,
`toast`, `message`, `hdd`, `process`, `channels`, `bouquet_context`, `zap_history`,
`history_clear`, `epg_import`, `uninstall`. A build that has bound no
feature area publishes `[]` - the connection, `info` and the commands are the plugin itself and
are not capabilities. `uninstall` (since 0.3.0) is the one name with no feature area behind it: it
says the package manager installed this very copy of the plugin, so `cmd/uninstall` can work - opkg
is executable, its configured info directory knows the package, and the package's file list names
the running `plugin.py`. A copy unpacked by hand or carried in a firmware image does not claim it.

A name is in the list because it **worked on this box**, not because this version of the plugin
has the code for it. Three things can take one out: the image did not provide the hooks
(`volume` on a box with no `VolumeControl`), the feature is switched off in the settings
(`keys` with `publish_keys` off, `screenshot` set to `off`, `epg_grid` with `epg_grid_events` at
`0`, `cec_workaround` with `cec_standby_workaround` off, `toast` with `osd_toast` off), or the hook raised while it was being
attached - which is logged once, with the name that could not be bound.

**A capability can also arrive late.** Some hooks can only bind once enigma2 has built the screen
behind them, which on some images happens after the plugin has already connected. `bouquet_context`
is the one that does this today: it is claimed on the first successful read of the receiver's own
service list, not when the plugin starts, so a box that never offers one publishes neither the
capability nor the `bouquet` topic. `zap_history` and `history_clear` (since 0.3.0) arrive the same
way, from the same list. When a capability appears after the connect, `info` and the
announcement are published again with it - a consumer that acts on `info` therefore has to accept
it more than once per connection, which it has to do anyway because `cmd/config` and `cmd/ha_mode`
both republish it.

**`wol` says what the image says about Wake-on-LAN, and nothing else.** Since 0.3.0, always present
with every key, `null` for what could not be read, and read from the image at every `info` publish
([ADR-0012](adr/0012-wake-on-lan-is-the-image-s-switch.md)).

| Field | Type | Meaning |
|---|---|---|
| `supported` | bool or `null` | The image found its own Wake-on-LAN switch: a front-processor file, `/proc/stb/fp/wol` (or `/proc/stb/power/wol` on the machines that have that one). 🔴 **`false` means the receiver cannot be woken over the network from deep standby** - only by its remote, its front button or a timer - and it is published only when the image itself answered „no switch". `null` when the image could not be asked or gave no usable answer: unknown, not „no". Never derived from `ethtool`'s `Supports Wake-on`, which describes a Linux suspend that enigma2 images do not use |
| `armed` | bool or `null` | Whether that switch is on: the file read back - `enable` or `on` is `true`, `disable` or `off` is `false`. Where the file cannot be read or says neither, the image's own setting („Wake On LAN"), whose notifier is what writes the file. `null` when not supported, or when neither answers. 🟡 The file's read format is unmeasured: no receiver this project has seen has one |
| `iface` | string or `null` | The interface `mac` is read from - `eth0` when it has an address, otherwise the first other interface that does; `null` with none. Informational: the image's switch takes no interface |
| `mechanism` | string or `null` | `fp` or `power`, by the file the image found; `null` when not supported or unknown |

`armed` is never inferred from `wol_arm`, the setting that asks the plugin to switch the image's
Wake-on-LAN on: that is a request made on the receiver, and the image's switch can be changed in its
own menu as well. A consumer that wants to warn before deep standby reads `supported`, and treats `null`
like an older plugin that publishes no `wol` at all: silence, not `false`. And `supported: true` says the
image has a switch - it is not evidence that a magic packet wakes the receiver, which no drill has
shown on any image yet.

### `<base>/<node>/power`

Retained. **Not JSON** - `on` or `standby`.

Deep standby is not a state here: the box is off and the broker shows `availability: offline`.

### `<base>/<node>/cec` - since 0.3.0

```json
{"last_intervention": 1789459200, "kind": "closed_channel_list", "count": 3, "pending": false}
```

Retained, and present only when `cec_workaround` is a capability: the setting
`cec_standby_workaround` (never writable over MQTT) is on (it is off by default), **and** the image has HDMI-CEC switched on
and is set to follow the television into standby (`config.hdmicec.enabled` and
`config.hdmicec.handle_tv_standby`, read when the plugin starts - with either off the image never
queues the television's standby), **and** it has its notification queue and its standby screen.
An image that builds its HDMI-CEC component with CEC switched off does not count as running it.
With the setting off nothing is bound and this
topic is retracted on every connect - so switching the workaround off takes its count off the
broker too. The setting itself is not in `info.settings`: it enables no command, and the
capability already says whether the workaround is at work.

| Field | Type | Meaning |
|---|---|---|
| `last_intervention` | integer or `null` | Unix epoch seconds, UTC, of the last intervention that was counted. For `closed_channel_list` it is when the list was closed; for `dropped_stale_standby`, when the standby was dropped. `null` until there is one |
| `kind` | `closed_channel_list` \| `dropped_stale_standby` \| `null` | What that intervention was |
| `count` | integer | Interventions since the plugin started - **at most one per television standby**. In memory, so not a durable total across a receiver restart |
| `pending` | bool | A standby **the television** asked for is queued and has not been carried out. This is what makes „the box is sitting there with a standby waiting" visible on a dashboard rather than only in a log |

Every key is always present; a value that does not exist yet is `null`, never omitted.

**What it works around** is an upstream enigma2 defect, not this plugin's. When the television
switches itself off it tells the receiver over HDMI-CEC, and the image *queues* a standby that only
the info bar carries out - so with the channel list open it waits until the list is closed, and
then, because the image has already forgotten the standby was the television's, it sends
`<Standby>` back to the television. The workaround:

1. **`closed_channel_list`** - when that standby is queued while the **channel list** is the screen
   in front, closes the list through its own exit (what EXIT does, including going back to the
   channel you were on), so the standby happens now. Only the channel list: the check is an
   allowlist of three class names - `ChannelSelection`, `ChannelSelectionRadio`,
   `PiPZapSelection` - matched against the screen's class and its bases, and **never** the service
   picker other dialogs embed, the channel list's context menu or bouquet selector, an EPG screen, a
   menu, the plugin browser, an input box, a message box or a recording dialog. Anything stacked on
   top of the list counts as „not the list", and the plugin never looks further down. It is
   **counted once the standby has actually happened** (about a second and a half later, when the
   receiver has entered standby), not when the list is closed: a close that released nothing -
   something else was queued ahead, or the screen underneath does not carry out the queue - is not
   counted as `closed_channel_list`.
2. **`dropped_stale_standby`** - when that standby is still queued **30 seconds** after the
   television asked for it, removes it from the queue, so it cannot fire later - when somebody
   closes the menu they were in - and take the television with it. A standby the list was closed
   for and which still did not happen is counted here, once, and not also as a close.

When the receiver has entered standby, any television standby still queued is removed too, so that
waking the receiver does not put it straight back to sleep:

- if one of the television's standbys did run, the rest are repeats - a television that said
  `<Standby>` twice - and removing them completes a request already carried out: **not counted**;
- if none of them ran - the receiver went to standby some other way, say the remote's power button,
  while the television's standby waited behind a popup or a menu - the television's standby was
  thrown away, so it is counted **once, as `dropped_stale_standby`**, exactly as the 30-second
  deadline would have counted it, and never as a close, whether or not the list was closed for it.

🔴 **A standby the household asked for is never touched.** `cmd/power standby` queues exactly the
same notification as the television does. The workaround identifies the television's at the moment
it is queued, by the image's own marker, and keeps that one entry by identity; nothing else in the
queue is ever closed on or removed. (The remote's power button does not queue anything - it opens
the standby screen directly - so it never reaches the workaround.)

While it closes the list it also keeps the image's „this standby came from the television" marker
set until the standby has happened, or for at most five seconds, so the late standby is not echoed
back to the television. It sets the marker to a value of its own rather than the image's, so that a
standby the household asks for during those seconds is still told apart from the television's.

### `<base>/<node>/service`

```json
{
  "sref": "1:0:19:283D:3FB:1:C00000:0:0:0:",
  "name": "TVP 1 HD",
  "bouquet": "Favourites (TV)",
  "provider": "Cyfrowy Polsat",
  "width": 1920,
  "height": 1080
}
```

| Field | Type | Notes |
|---|---|---|
| `sref` | string | The service reference, trailing colon included. The stable identifier - `name` is for people |
| `name` | string \| null | Channel name as the bouquet spells it |
| `bouquet` | string \| null | The first of the **configured** bouquets (`bouquets_for_select`) that contains the service - `null` when it is in none of them, which is not the same as „no bouquet" |
| `provider` | string \| null | |
| `width`, `height` | int \| null | Video resolution; `null` before the first frame is decoded |

Published on `evStart`, `evTunedIn`, `evUpdatedInfo`, `evNewProgramInfo` and `evEnd`, so it
settles within a second of a zap and is published again when the resolution becomes known - a box
answers `-1` for both until the first frame is decoded, and `-1` is not a resolution.

With nothing playing every field is `null`; the keys are still all there.

Two services are **the same service** when the first eleven colon-separated fields match. That is
what makes `1:0:19:283D:3FB:1:C00000:0:0:0:` and `1:0:19:283D:3FB:1:C00000:0:0:0::TVP 1 HD` one
channel rather than two - and it is why the eleventh field is included rather than the tenth: two
IPTV services differ only in the stream URL that sits in it.

### `<base>/<node>/epg`

```json
{
  "now":  {"title": "Wiadomości", "begin": 1789459200, "end": 1789460700,
           "event_id": 27431, "short": "Serwis informacyjny", "long": "..."},
  "next": {"title": "Pogoda", "begin": 1789460700, "end": 1789461000,
           "event_id": 27432, "short": "", "long": ""}
}
```

`now` and `next` are objects or `null`. Fields: `title` string, `begin`/`end` int epoch seconds,
`event_id` int (what `cmd/timer` wants for `action: add`), `short` and `long` strings, possibly
empty.

Published on `evUpdatedEventInfo`, on a zap, and **when the programme it last published was due
to end**. That last one has no event behind it: at some point the news ends and the weather
begins, and no zap, no tune and no EPG update need happen for „now" to be a different programme.
Without a timer, a box left on one channel would show the morning's programme until somebody
touched the remote.

### `<base>/<node>/channels` - since M2

The bouquets a consumer may offer, and the services in them. This is what a „channel list" is
built from, and it is the list `cmd/zap` by name resolves against.

```json
{
  "generated": 1789459200,
  "bouquets": [
    {"name": "Ulubione TV",
     "sref": "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.ulubione.tv\" ORDER BY bouquet",
     "channels": [{"sref": "1:0:19:283D:3FB:1:C00000:0:0:0:", "name": "TVP 1 HD"}]}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `generated` | int | Epoch seconds, when the bouquets were last walked |
| `bouquets[].name` | string | As enigma2 spells it |
| `bouquets[].sref` | string | The bouquet's own reference |
| `bouquets[].channels[]` | list | In the user's own order, the order the box shows them in |

Which bouquets appear is the **`bouquets_for_select`** setting: a comma-separated list of bouquet
names (or of their slugs, which is easier to type on a remote control - „Sport (HD)" and
`sport_hd` both select the same bouquet). Empty means every television bouquet, which is what a
box that has never been configured publishes.

**Only playable services.** A bouquet holds markers, separators and hidden entries alongside its
channels, and they arrive from the same call; they are dropped here, because a marker offered as a
channel is an option in a select box that cannot be tuned. A box with „multiple bouquets" switched
off has no bouquet list at all - its favourites list is published as a single bouquet.

**Picons are not published.** They are tens of kilobytes each and the box's own web interface
serves them on the same address.

Rebuilt when `bouquets.tv` or any `userbouquet.*` file changes - there is no event for that, so
their modification times are compared once a minute - and on `cmd/discovery`. The list on a real
box went from 36 entries to 11 within half an hour once, so nothing may key on position.

### `<base>/<node>/bouquet`

The active television bouquet used by the receiver's channel-up and channel-down actions:

```json
{"name":"Ulubione TV","sref":"1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.ulubione.tv\" ORDER BY bouquet"}
```

Retained and present when `bouquet_context` is a capability. It is read from the receiver's real
service-list root, not inferred from the current channel. A successful `cmd/bouquet` always
republishes it, including when the requested bouquet was already active, so consumers can require
a fresh by-effect acknowledgement.

**The receiver is not always in one of the configured bouquets.** It may be showing the radio
list, the movie list, or a bouquet that `bouquets_for_select` leaves out. That is ordinary
operation rather than a fault, and it is published as both fields being null:

```json
{"name":null,"sref":null}
```

A consumer should read that as „channel up and down are not walking a list I know about". The
capability stays, because the service list is being read successfully - what is missing is a
match, not a hook - and the topic goes back to naming a bouquet as soon as the receiver is in one
again.

### `<base>/<node>/zap_history` - since 0.3.0

The receiver's own zap history: the list its "History Zap" screen shows when KEY_NEXT or
KEY_PREVIOUS is pressed. Retained, QoS 0, published when it changes, and present when
`zap_history` is a capability ([ADR-0014](adr/0014-the-zap-history-is-the-receivers.md)).

```json
{"entries": [{"sref": "1:0:19:2B66:3F3:1:C00000:0:0:0:", "name": "Das Erste HD",
              "bouquet": "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.favourites.tv\" ORDER BY bouquet",
              "bouquet_name": "Favourites (TV)"}],
 "current": 0, "limit": 20, "panic_button": true}
```

| Field | Type | Meaning |
|---|---|---|
| `entries` | list | The channels in the receiver's list, **newest first**, exactly the ones its own screen shows: an entry the receiver has no information for any more (a channel removed from every list) is left out, as the screen leaves it out. `[]` when the list is empty |
| `entries[].sref` | string | The service reference |
| `entries[].name` | string or `null` | The name the `service` topic uses for the same channel |
| `entries[].bouquet` | string or `null` | The bouquet of the path the channel was zapped in; `null` when the entry has no path |
| `entries[].bouquet_name` | string or `null` | That bouquet's name when it is one of the published bouquets (`channels`); `null` otherwise - a radio bouquet, or one `bouquets_for_select` leaves out |
| `current` | int or `null` | Where the receiver's own position in the list is, as an index into `entries`; `null` when that entry is not shown |
| `limit` | int or `null` | How many entries the image keeps (`HISTORYSIZE`, 20 on the images read); `null` when the image does not say |
| `panic_button` | bool or `null` | The image's `config.usage.panicbutton`: `true` means the 0 key - and `cmd/history_clear` - clears the list and switches to channel 1; `false` means 0 only goes back one channel. `null` when the image has no such setting |

**The receiver's list, not the plugin's.** The plugin keeps no history of its own; it reads the
receiver's every two seconds and publishes when it changed. A user-interface restart, a reboot and
a deep standby empty the receiver's list, and the topic then says so. The image keeps each service
once, with the newest path, and drops the oldest past `limit`. While the image's "e1-like" radio
mode is on - its default - radio services zapped in the same channel list are in the same list.

**What enters it.** Zaps through the receiver's channel list: the remote, and since 0.3.0 every zap
this plugin makes except the direct-play exits of `cmd/zap` (see §2). A zap timer's zap is in it
when the receiver is awake and not in timeshift; an EPG zap is in it once it is confirmed, not
while it is a preview. The full list, with what is measured and what is only read from the
image's code, is in [What enters the receiver's zap history](#what-enters-the-receivers-zap-history).

**Everything is published.** Every entry of every bouquet is on the broker, retained, as every
channel is on `channels`. Hiding a bouquet from a list in a consumer hides nothing here. The
plugin's OpenWebif page shows this payload as it is, to whoever OpenWebif admits.

### `<base>/<node>/epg_grid/<bouquet_slug>` - since M2

A compact grid, **one retained topic per configured bouquet**: the next few events on every
channel of that bouquet, enough to draw a „what's on" list without a single OpenWebif request.

The grid is per bouquet because a bouquet is what a household actually browses, and because one
combined topic would make every consumer re-read every bouquet whenever any one of them moved on.
A consumer that cares about a single bouquet subscribes to a single topic; one that wants them all
subscribes to `<base>/<node>/epg_grid/+`.

**`<bouquet_slug>`** is derived from the bouquet's name: lower-cased, transliterated to ASCII, and
every run of characters that is not a letter or a digit collapsed to one `_`, with leading and
trailing `_` trimmed. „Ulubione TV" becomes `ulubione_tv`; „Favourites (TV)" becomes
`favourites_tv`. The slug is only an addressable, MQTT-safe name - **the payload carries the
original**, and the payload is what a user should be shown.

```json
{
  "bouquet": "Ulubione TV",
  "generated": 1789459200,
  "channels": [
    {"sref": "1:0:19:283D:3FB:1:C00000:0:0:", "name": "TVP 1 HD",
     "events": [{"title": "Wiadomości", "begin": 1789459200, "end": 1789460700, "event_id": 27431}]}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `bouquet` | string | The bouquet's name as enigma2 spells it - not the slug |
| `generated` | int | Epoch seconds, when this bouquet's grid last **changed** - see below; it is not the time of the last build |
| `channels[].sref` | string | |
| `channels[].name` | string | |
| `channels[].events[]` | list | Up to `epg_grid_events` entries per channel, chronological |
| `events[].title` | string | |
| `events[].begin`, `events[].end` | int | Epoch seconds |
| `events[].event_id` | int | |

Refreshed when a bouquet changes, every 15 minutes, and on `cmd/epg_grid` - which regenerates and
republishes **every** configured bouquet, not just one. `epg_grid_events` is a setting, default
**4**; `0` turns the topics off entirely and drops `epg_grid` from `capabilities`.

A channel can carry fewer than `epg_grid_events` events: the plugin asks the EPG cache for a
bounded window of time rather than for a number of events, because that is the question the cache
takes, and a channel showing a three-hour film has one event in it.

**Four channels per main-loop turn.** A bouquet of two hundred channels is two hundred EPG
lookups, and doing one whole bouquet in a single callback can hold the thread that draws the
television for too long. The builder therefore performs at most four channel lookups, yields for
20 ms, then resumes from its cursor. A completed bouquet is published only after all of its
channels have been collected; the previous retained grid stays current while that happens. The
time each batch and completed bouquet took is in the plugin's log. A grid whose content has not
changed is not republished.

**`generated` says when the grid last changed, not when it was last built.** Every build stamps
it from the clock, and it is deliberately left out of the comparison that decides whether to
publish. Were it counted, it would be the only field that differed whenever a pass found exactly
the television the previous pass found - a bouquet carrying no EPG, an overnight window, an
import that has failed - and that bouquet would rewrite its retained topic four times an hour
for as long as the box stayed on, giving every consumer a state change and every recorder a row
for a payload saying what it already said. The consequence is the one worth knowing: the
retained `generated` moves when the programmes move. A consumer that wants to know the plugin is
still building grids should read `availability`, which is what it is for.

**Slugs that stop being configured are retracted.** The plugin remembers the slugs it has
published in its state file on the box, the same file that carries the discovery component list.
Drop a bouquet from `bouquets_for_select`, or rename one - which changes its slug - and the topic
it used to own gets an empty retained payload on the next start. Without that the broker would go
on serving the grid of a bouquet nobody has configured for as long as the broker lives, which is
the same trap retained discovery payloads set and is answered the same way.

A grid payload can reach tens of kilobytes *per bouquet*. It is **not** meant to become a state
attribute of an entity - a consumer that stores it per update will bloat its recorder database.
The companion integration exposes it through an action that returns a response instead.

Full EPG search, and browsing timers that the plugin did not create, stay on OpenWebif. This is
a grid, not a database.

### `<base>/<node>/epg_import` - since 0.3.0

```json
{"state": "done", "started": 1789459200, "finished": 1789459291, "events": 120074, "error": null}
```

Retained, and present when `epg_import` is a capability. That capability is claimed only where the
image's EPG-Importer is **already loaded** by enigma2's own plugin loader - the plugin looks it up
under `Plugins.Extensions.EPGImport.plugin` and never imports it itself, because a second import
would build a second importer and a second scheduler - where every name the plugin needs from it
is there, and where the image's EPG cache can take imported events (`importEvents` or
`importEvent`). Without those the importer writes a file instead and ends by asking for a
graphical-interface restart, with a dialog that answers yes by itself after fifteen seconds, so
the capability is not claimed there. The reason is logged once at start.

| Field | Type | Meaning |
|---|---|---|
| `state` | `idle` \| `running` \| `done` \| `failed` | |
| `started` | integer or `null` | Unix epoch seconds, UTC: when the plugin started the import, or when it first saw one somebody else started. `null` for an import that began and ended between two of the plugin's looks, and after a start-up |
| `finished` | integer or `null` | Unix epoch seconds, UTC: the importer's own finish time |
| `events` | integer or `null` | The importer's event count for the run. It counts events **processed**, not events **new** to the guide |
| `error` | string or `null` | Why it failed, for a person. Never a source name or a URL: the importer does not say which source failed, so neither does this |

**It follows every import**, not only the ones asked for over MQTT or on the page. While nothing
runs the plugin asks the importer once a minute whether an import is running, so an import the
image's own schedule or the importer's own screen started shows up as `running`, and „already
running" is never an unexplained refusal. While one runs it asks every two seconds - the same test
at the same period the importer's own screen uses - and when the answer turns false it reads the
importer's result: events -> `done`, and the EPG grid is rebuilt (each bouquet is **published only
if it changed**, so after most imports nothing on `epg_grid/*` moves); no events -> `failed`, „...its
sources may be unreachable". At start-up the topic is `idle`, carrying `finished` and `events` from
the importer's own record of its last run, or `running` if one is under way.

Three things can go wrong, and only three can be told apart: the import did not start (or never
ran), it finished with no events, or it is still running after **30 minutes** - a normal run takes
one and a half minutes. The plugin cannot cancel the importer: after the watchdog it keeps asking,
keeps refusing a second start, and publishes the real result if one arrives.

Worth knowing before you press the button, all of it the image's behaviour and none of it
something the plugin can change:

- 🔴 **The picture's menus freeze for two to three seconds at the end.** The importer downloads and
  parses off the main loop, but saves the guide on the thread that draws the picture. Measured on
  the receiver this was written against: 2.3 and 2.6 seconds.
- **`clear_oldepg`**: with that importer setting on, every import empties the whole guide first, so
  the grid is empty until it finishes.
- **The importer's own deep-standby settings apply** to an import started from here exactly as to a
  scheduled one: after **every** import, whoever started it, the importer checks whether to put the receiver into
  deep standby. It does so only when **all four** of its own conditions hold - its „shutdown"
  setting is on, its deep-standby setting is „wake up", its „deep standby after import" setting is
  on, and a timer woke the receiver - and then only if the receiver is in standby, nothing is
  recording and it is not already shutting down. The settings are all off by default. That is the
  importer, not this plugin.
- **A network recording mount can hold the press up.** Before its first download the importer reads
  `/proc/mounts` and asks the recording mount for its free space, on the main loop, to choose
  where to put the file. If that mount is a network share that has stopped answering, pressing the
  button can freeze the picture until the mount gives up - as the importer's scheduled run would.

A settings save on the receiver while an import runs restarts the bridge, and the new session
reports `started` as the moment it first saw the import.

**The power commands wait for an import, but not for ever.** Deep standby, reboot and the
interface restart are refused while an import runs, because a restart mid-import loses the run.
That refusal lapses when this plugin's own start failed **and left the importer saying it is
running**, or when the watchdog has fired for the current run: the importer marks itself running
before its first download, so a start that fails part-way can leave it saying „running" until its
next scheduled run - up to a day of refused reboots for an import that is not happening. A start
that failed before that point leaves nothing stuck and lapses nothing. The topic keeps reporting
what the importer says, and a second `cmd/epg_import` is still refused as already running. The
refusal returns once the importer has been seen idle, or with the next import this plugin starts.
One residual is accepted: if a stuck start is followed by the importer's next scheduled run
without the importer ever being seen idle in between, the lapse covers that run too - about a
minute and a half.

A press refused because an import is already running also starts following that import at once:
the topic says `running`, with `started` the moment of the press, rather than waiting for the next
once-a-minute look.

### `<base>/<node>/tuner`

```json
{"snr": 78, "agc": 62, "ber": 0, "tuner": "A"}
```

`snr` and `agc` are integer percentages, `ber` an integer error count, `tuner` the letter of the
frontend in use (`null` when nothing is tuned).

`ber` is deliberately **not** scaled into a percentage the way enigma2's own web interface scales
it. It is a count of errors; dividing it by 65535 produces a number that looks like a percentage
and means nothing.

Published on `evTunedIn` and `evTuneFailed`, and re-read every 60 seconds while something is
playing, because signal quality drifts with the weather rather than with events. A service that
does not come off a tuner - IPTV, a recording being played back - has every field `null`.

### `<base>/<node>/recording`

```json
{
  "active": [{"name": "Wiadomości", "sref": "1:0:19:283D:...:", "begin": 1789459200, "end": 1789460700}],
  "next":   {"name": "Pogoda", "sref": "1:0:19:283D:...:", "begin": 1789460700, "end": 1789461000}
}
```

`active` is a list, empty when nothing is recording. `next` is the soonest future recording timer
or `null`. This topic is what the deep-standby, reboot and GUI-restart guards read.

A timer that only tunes the box - enigma2 calls it `justplay` - is not a recording and appears in
neither field, though it is in `timers`. A disabled, finished or failed timer is in neither
either.

### `<base>/<node>/timers`

A JSON **list** (not an object), one entry per timer the receiver still lists - the pending
ones **and** the ones it has finished with:

```json
[{"name": "Wiadomości", "sref": "1:0:19:283D:...:", "begin": 1789459200,
  "end": 1789460700, "state": "waiting", "repeated": 0}]
```

| Field | Type | Notes |
|---|---|---|
| `name` | string | |
| `sref` | string | |
| `begin`, `end` | int | Epoch seconds |
| `state` | string | One of the words below |
| `repeated` | int | enigma2's day bitmask; `0` for a one-off timer, `127` for daily |

| `state` | Meaning |
|---|---|
| `waiting` | Pending: it will start at `begin` |
| `prepared` | Pending: it is about to start |
| `running` | Recording now |
| `ended` | No longer scheduled. It does **not** say a recording was made: see below. A repeating timer is back to `waiting` for its next day instead |
| `disabled` | Switched off. It will not run until somebody switches it back on. This covers a timer the receiver switched off itself because it conflicted with another when the timer file was loaded. enigma2 files a disabled timer with the finished ones and marks it ended; it is published as `disabled`, never `ended` |
| `failed` | The receiver says this timer's recording could not be written. Best effort - see below |
| `unknown` | The receiver reported a state number this plugin has no word for. Treat it as neither pending nor over |

`disabled` wins over `failed`, and both win over the state number. A consumer that wants only
what is still going to happen keeps `waiting`, `prepared` and `running`.

**`failed` is best effort, and `ended` is not "recorded".** The plugin can only repeat what the
receiver keeps, and on OpenViX 6.6 (read from its timer code) that is little:

- The receiver sets its failure flag in one case only: the disk was too full to start. It gives up
  on a timer for other reasons without setting it - a timer that could not get a tuner (logged as
  "prepare failed") stays waiting until its end passes and is then filed as ended. It is published
  as `ended`.
- The flag is not saved in the timer file. After the receiver's interface restarts - a GUI
  restart, a reboot, a wake from deep standby - the same timer is published as `ended`.
- Images that give a failed timer a state number of its own are published as `failed` too.
- A **repeating** timer keeps the flag when the receiver puts it back in the queue for its next
  day, and on OpenViX a timer with the flag set returns before it starts recording - so the next
  day will not be recorded either, until the interface restarts and the flag is lost. Such a timer
  is pending and is published as `failed`, not `waiting`: `waiting` would promise a recording the
  receiver will not make. The `recording` topic does not read the flag and still lists it as
  `next`.

To learn whether something was actually recorded, look for the recording, not the timer.

A finished, failed or disabled timer is published for as long as the receiver keeps it, which is
the receiver's business, not the plugin's. On OpenViX 6.6 (read from its timer code): a finished
timer is kept only when `config.recording.keep_timers` (days) is above `0`; one whose `end` is
older than that is pruned, but only when the receiver next activates a timer, so a quiet receiver
lists it for longer than the setting says (one was measured still listed 8.8 days after its end
with the setting at 7). A disabled timer is filed the same way whatever that setting is, and a
disabled repeating timer is never pruned. Deleting one - `cmd/timer`, OpenWebif, the remote -
removes it at once. The list therefore grows with that setting and with how many timers the
receiver sets for itself (AutoTimer, for one). Neither the plugin nor the topic caps it: an entry
is about 180 bytes with a 48-character name, so 50 timers are about 9 KB and 1000 about 180 KB.

In `begin` order; timers with the same `begin` keep the pending ones first. Published whenever
enigma2 writes its timer file - which it does after adding, deleting, starting or finishing
a timer - and after every recording event. A burst of changes produces one payload, not
one per call.

Known gap: switching a timer off or back on in the receiver's own timer list does **not** write the
file on OpenViX 6.6, so that change reaches `timers` only with the next write for any other reason.
Until then the topic shows the timer's previous state.

### `<base>/<node>/volume`

```json
{"level": 35, "muted": false}
```

`level` is an integer 0-100, `muted` a boolean. Published on every volume change from any source
- the remote, OpenWebif, the plugin itself - and reconciled on a short timer, because enigma2 has
no single choke point every volume change passes through.

### `<base>/<node>/cam`

```json
{"system": "Nagra", "active": true, "encrypted": true, "ecm_ms": 123}
```

Opt-in with `cam_telemetry`; absent entirely by default. `encrypted` is Enigma's current-service
flag. `active` means only that a fresh, valid ECM result was observed after the latest known
service start; it is **not** a softcam-process or server-health check. `system` is limited to a fixed
list of generic conditional-access systems and `ecm_ms` to 0-600000. Unknown values are `null`.

The source is the receiver-local `/tmp/ecm.info`, opened without following symlinks and read to
an 8 KiB cap. Reader, server, user, card, CAID, provider and raw lines are never published. A zap
invalidates the prior result until the file is updated. Because this file has no universal tuner
identity, attribution on multi-tuner images is best effort; this topic must not drive access or
recording decisions.

### `<base>/<node>/oscam`

```json
{
  "software": "OSCam", "version": "1.20_svn build r11739",
  "software_running": true, "api_reachable": true, "api_access": "granted",
  "readonly": true, "uptime_s": 86400,
  "readers_configured": 2, "readers_enabled": 2, "readers_healthy": 2,
  "cards_ready": 1, "servers_connected": 1, "shared_cards": 18,
  "readers": [
    {"id":"reader_4d3b0e8b32c1","kind":"reader","enabled":true,
     "status":"ready","protocol":"internal","shared_cards":null},
    {"id":"server_b6a8c0344f12","kind":"server","enabled":true,
     "status":"connected","protocol":"cccam","shared_cards":18}
  ]
}
```

Opt-in with `oscam_telemetry`; absent entirely by default. Every 30 seconds a dedicated worker
queries only `status` and `readerlist` from OSCam's JSON API on `127.0.0.1`. It never follows a
redirect, uses an environment proxy, invokes an action API, or blocks enigma's main loop. A
response is capped at 128 KiB and the request has a bounded deadline. A probe that has not
completed within 90 seconds is expired on the next tick - so between 90 and 120 seconds in
practice, because the check rides the same 30-second timer - and counts become unknown with the
reader list cleared, instead of presenting old health as current. That probe is also abandoned:
whatever it answers afterwards is discarded, so a worker stuck on a listener that accepts a
connection and never replies delays the next reading by one interval rather than stopping the
telemetry until the plugin restarts. At most two abandoned workers may be outstanding; while there
are that many, no new probe is started and the state stays unknown until one of them returns.

Reader labels become stable, receiver-local HMAC identifiers using a hidden persisted salt. The
labels themselves, addresses, users, card identifiers, CAIDs, providers and keys never leave the
box. Reordering OSCam's response does not change an id; renaming a reader does. `kind` is
`reader`, `server`, or `unknown`; `status` is one of `ready`, `connected`, `disabled`, `no_card`,
`initializing`, `connecting`, `disconnected`, `sleeping`, `duplicate`, `error`, or `unknown`.

An entry whose kind could be worked out from neither the row type nor the protocol is published as
kind `unknown`, and its id carries the prefix `source_` rather than `reader_` or `server_`.

`cards_ready` counts enabled local readers reporting `CARDOK`. `shared_cards` is OSCam's bounded
CCcam share count and is deliberately separate: it is never added to the physical-reader count.
`api_access: granted` means only that the API returned these read-only views; it does not assert
that OSCam authentication is enabled. `readonly` reports OSCam's own API flag.

### `<base>/<node>/softcam` - since 0.3.0

```json
{"selected": "OSCam_00000-r000", "running_instances": 1,
 "last_restart": 1789459200, "last_restart_reason": "manual", "restarts_today": 2,
 "manager_check_on_start": true, "manager_timer_minutes": null}
```

Retained, and present when `softcam` is a capability - which it is only when the image starts the
cam through its own manager rather than through `/etc/init.d/softcam`, the autostart entry resolves
to an executable regular file directly under `/usr/softcams/`, and the plugin knows a start line for
that binary's family.

| Field | Type | Meaning |
|---|---|---|
| `selected` | string or `null` | The **basename of the binary** the image selected for autostart, for example `OSCam_00000-r000`. Not a family name, and never the protocol the cam speaks to its servers - a receiver can run an OSCam that talks `cccam` outward, and those are different things |
| `running_instances` | integer or `null` | How many **instances** are running: matched processes whose parent is not itself matched. A healthy box reports **1**, because a cam that forks a supervisor is one instance presenting as two processes. More than one is the fault this topic exists for. `null` means the count could not be taken, which is not the same answer as `0` |
| `last_restart` | integer or `null` | Unix epoch seconds, UTC, of the last restart this plugin performed |
| `last_restart_reason` | `manual` \| `autoheal` \| `null` | Which path performed it |
| `restarts_today` | integer | Restarts since **local** midnight, and the number that makes a restart loop visible. 🔴 It lives in memory while this topic is retained, so after a receiver reboot a consumer sees the last published value until the plugin's connect snapshot replaces it: it is **not** a durable total. It is recomputed from a stored local date when the topic is published rather than reset by a timer, so a clock step shortly after boot cannot strand it |
| `manager_check_on_start` | bool | Whether the image's own liveness check will add a copy at every graphical-interface start on this receiver. That check looks the cam up by process name, and the kernel caps that name at 15 characters, so a binary with a longer basename can never be found and the check starts another one instead of leaving the running one alone. This is the difference between a receiver that needs this feature and one that does not |
| `manager_timer_minutes` | integer or `null` | The image's periodic liveness-check interval in minutes when it is switched on, and `null` when it is not. Worth watching: on a receiver where `manager_check_on_start` is true, switching that timer on adds an instance every interval, indefinitely |

`running_instances` is polled once a minute rather than only written after a restart, because it
changes without the plugin: the image adds a copy at every interface start, and on most receivers
the household can start and stop the cam from the extensions menu at any time. The poll reads
`/proc/<pid>/stat` and `/proc/<pid>/exe`, and nothing else.

A process is matched on **two** conditions: its `comm` equals the first 15 characters of the
binary's basename, and `/proc/<pid>/exe` resolves to that binary's exact path. The second one is
not optional - two binaries differing only after the fifteenth character truncate to the same
`comm`.

### `<base>/<node>/hdd`

```json
{"mounted": true, "path": "/media/hdd", "free_mb": 412330}
```

`mounted` boolean, `path` string, `free_mb` integer megabytes or `null` when nothing is mounted.
Checked on a slow timer; a recording disk that silently unmounts is the point of this topic.

### `<base>/<node>/process`

```json
{"rss_kb": 164208, "hwm_kb": 187432, "threads": 22, "fds": 61, "started": 1789042109}
```

What the **enigma2 process** costs. Not the plugin: enigma2 is one process, so the image, every
other plugin and this one share the same resident set and nothing in `/proc` can attribute a
kilobyte to any of them. A rising line here is a question, not a verdict.

| Field | Type | Meaning |
|---|---|---|
| `rss_kb` | integer or `null` | `VmRSS` - resident set, in kB |
| `hwm_kb` | integer or `null` | `VmHWM` - the high-water mark of that, in kB |
| `threads` | integer or `null` | `Threads` |
| `fds` | integer or `null` | Open file descriptors, including the one the count itself opens |
| `started` | integer or `null` | Unix epoch seconds, UTC, when the enigma2 process started |

All five keys are always present. A field that could not be read is `null` rather than missing,
because an absent key renders as an empty string in a Home Assistant template - „ignore this
message", which leaves the previous value on screen for ever - while an explicit `null` renders as
unknown.

Read from `/proc/self/status` (`VmRSS`, `VmHWM`, `Threads`), `/proc/self/fd`, and, for `started`,
field 22 of `/proc/self/stat` (clock ticks since boot, divided by `SC_CLK_TCK`) plus `btime` from
`/proc/stat`. `started` is deliberately not „now minus uptime": the topic is retained and a consumer
keeps the value, so it has to be the same number on every publish rather than one that drifts by a
second each time somebody reads it.

**Cadence.** In the snapshot on every connect, then every 300 seconds. In between, the resident set
is checked every 60 seconds and published early whenever it has moved by 4096 kB or more in either
direction since the last publish - so a jump lands on the curve at the minute it happened rather
than up to five minutes later.

The 300-second publish obeys the publish-on-change rule of §1 like every other state topic, so it is
a **ceiling on the gap, not a heartbeat**: a payload identical to the last one is not sent again, and
a receiver idle enough that none of the five numbers moved can be quiet for longer. The connect
snapshot is the deliberate exception and always goes out. Read `availability` to tell a quiet box
from an absent one.

The capability is `process`, and it is claimed only when `/proc/self/status` can actually be read.
There is no setting: the topic reveals nothing about what anybody is watching, so it is always on.

Unlike every other poll in the plugin this one runs on the main loop, because procfs is memory -
there is no disk behind it, no network and no lock another process holds. `hdd` is the opposite
case and runs on a thread of its own.

### `<base>/<node>/screen`

Retained, QoS 0. **Not JSON** - the raw bytes of a JPEG.

Produced by the image's `grab` utility, downscaled. It is debounced and rate-limited to at most
one capture per five seconds regardless of how many events ask for one, and the `screenshot`
setting chooses between `off`, on every zap, and a fixed interval.
An on-zap capture waits `screenshot_delay` seconds (four by default). Another zap resets that
wait; a grab already running for an earlier channel is discarded and the newest one is scheduled.
Enigma's tune events do not prove that a video frame has been decoded, so this is deliberately a
bounded settling delay rather than a claim to detect black video, audio or image readiness.

Both layers, the video and the menus over it, which is what a person means by a screenshot.
Capped at 400 KB: over that the capture is dropped and the reason goes to `last_error`, because
this topic is retained and an oversized payload would be delivered to every new subscriber for as
long as the broker lives.

In standby nothing is captured unless `cmd/screenshot` asks for it - there is nothing on the
screen to photograph. On a reconnect the **last** picture is republished from memory rather than a
fresh one being taken: a reconnect loop that ran `grab` each time round would be a receiver on its
knees.

It is a picture of what is on the television, retained on the broker. Decide that deliberately.

The plugin's OpenWebif page shows the last payload this process published here, with the time the
capture finished, at `<mount>/screen.jpg`. That is a view of this topic, not a change to it: the
payload, the command and every guard above are unchanged.

### `<base>/<node>/key`

**Not retained** - QoS 0, fire and forget.

```json
{"key": "KEY_RED", "press": "short"}
```

`key` is the enigma2 key name; `press` is `short` or `long`. A key that is held produces one
`long` event, not a stream of repeats - the press is published when the button comes back up, as
`long` if the receiver reported the long-press marker while it was down.

The names are the Linux input event names every image uses (`KEY_OK`, `KEY_RED`, `KEY_CHANNELUP`),
taken from the receiver's own table where it has one, so a remote this plugin has never heard of
still publishes `KEY_PVR` rather than a number. A code with no name anywhere is published as
`KEY_<number>`.

At most **20 presses a second** reach the broker; beyond that they are dropped with one line in
the log.

Not retained on purpose: a retained key press would re-trigger every automation bound to it on
every reconnect. The plugin observes keys and **never consumes them** - the receiver behaves
exactly as it would with the plugin absent - and publishing is capped so that holding a button
cannot flood the broker. `publish_keys` turns the topic off.

### `<base>/<node>/last_error`

Retained.

```json
{"cmd": "zap", "error": "service name 'Sport' is not unique (4 matches)", "ts": 1789459213}
```

Published when a command fails or is refused by a guard, and **cleared** - an empty retained
payload - when the same command later succeeds. A consumer that raises an error to the user reads
this topic, not the absence of a state change.

| Field | Type | Meaning |
|---|---|---|
| `cmd` | string | The command refused |
| `error` | string | Why, in English, for the person reading the topic. This is the contract's human text |
| `ts` | int | When, in epoch seconds |
| `reason` | string, optional | Since 0.3.0. A stable code for the refusal, **only** from a command that defines codes - today `history_clear` (and `zap_history`'s `playback`); see §2. Absent, not `null`, otherwise, so every other refusal is exactly what it was. A consumer that knows the code can say the refusal in the household's language, and one that does not ignores the field |

```json
{"cmd": "history_clear", "error": "the receiver's zap history holds at most one channel, and 0 does nothing then", "reason": "too_short", "ts": 1789459213}
```

---

## 2. Commands

`<base>/<node>/cmd/<name>`, QoS 1, never retained. Payloads are UTF-8; several commands accept a
bare string for convenience and a JSON object when there is more to say. Case is not significant
for the keyword payloads (`on`, `ON`, `On` all work).

Every command is **verified by effect**: the plugin does the thing, observes the resulting
enigma2 event, and publishes the new state on the matching state topic. A failure or a refusal
goes to `last_error`. There is no acknowledgement topic and no correlation id in v1 - the state
topic is the answer.

🔴 **A command that arrives with the retain flag set is logged and discarded, never executed.**
This is a guard on every command in the table below, not a property of any one of them. A retained
command is delivered again the instant the plugin subscribes, so the box would obey it on every
reconnect and after every reboot - a retained `deep_standby` is a receiver that will not stay on.
The plugin publishes no command retained and refuses to act on one that is, so a mistake made with
`mosquitto_pub -r` costs a log line rather than the evening. Clear it by publishing an empty
retained payload to that topic; until you do, the broker keeps handing it out.

| Command | Payload | Effect | Guard |
|---|---|---|---|
| `power` | `on` \| `standby` \| `toggle` | Leaves or enters standby | Idempotent: asking for the state the box is already in does nothing |
| `deep_standby` | any (`PRESS` by convention) | Shuts the box down completely | Refused while recording, or with a timer due within 10 minutes; refused while an EPG import runs (since 0.3.0, and see below for when that lapses); also refused unless `deep_standby_allowed` is on |
| `reboot` | any | Reboots the receiver | Same as `deep_standby` |
| `restart_gui` | any | Restarts enigma2 only | Refused while recording or with a timer due within 10 minutes, and while an EPG import runs (since 0.3.0): a restart mid-import loses the run. That refusal **lapses** once this plugin's own start of the import failed, or once the 30-minute watchdog has fired for the current run, and returns with the next import; the `epg_import` topic still says what the importer says |
| `zap` | `<sref>` \| `{"sref": "..."}` \| `{"name": "..."}` | Tunes to a service, waking the box from standby first. Since 0.3.0 through the receiver's channel list, so the zap is in the receiver's zap history - see below | By name: refused unless exactly one service in the configured bouquets matches - the error names the count. A zap that does not show up on `service` within 5 s is reported there too; from standby, one whose wake has not finished within 5 s is reported as "the receiver did not leave standby" |
| `bouquet` | `{"sref": "..."}` | Makes one published TV bouquet the active channel-list context | Exact allowlist match only. The current channel is preserved when it belongs to the bouquet; otherwise the first playable channel is tuned. Empty/marker-only bouquets and unavailable service-list APIs are refused without changing context. Since 0.3.0 refused during timeshift, before anything changes: "timeshift is active; the receiver would ask on screen whether to leave it" |
| `zap_history` - since 0.3.0, capability `zap_history` | `{"sref": "..."}`, exactly | Zaps to one channel of the receiver's zap history the way its own "History Zap" screen does: the entry moves to the front and plays. From standby the receiver is woken first, as for `zap`. Verified on `service` within 5 s | By reference only - the list reorders on every zap and names repeat - matched by service identity among the entries `zap_history` shows: "that channel is no longer in the receiver's zap history" otherwise - checked first, so a refused channel never wakes the receiver. Refused while a recording is played back ("a recording is being played back", `reason` `playback`). With another screen open over the info bar the channel is played directly and the history is left as it is - refused, as `zap` refuses it and with `zap`'s sentences, where there is nothing to play it with: "there is no session to zap with" without a navigation, "this image's navigation has no playService" without a player. **No permission, no recording guard and no timeshift guard**: the receiver's own screen has none, and leaves timeshift without asking |
| `history_clear` - since 0.3.0, capability `history_clear` | any (`PRESS` by convention) | Clears the receiver's zap history **exactly as its 0 key does**: the image's own `keyNumberGlobal(0)`, which empties the list and **switches to channel 1** - the first channel of the first bouquet - closing picture-in-picture if it is open. Afterwards `zap_history.entries` holds that one channel and `service` names it | Refused, before anything happens, in this order, each with a `reason` on `last_error`: in standby - "the receiver is in standby, where 0 does not clear the history" (`standby`); with the image's panic-button setting off - "the receiver's panic-button setting is off, so 0 goes back one channel instead of clearing the history" (`panic_off`); with fewer than two entries - "the receiver's zap history holds at most one channel, and 0 does nothing then" (`too_short`); in timeshift, or with one waiting to be saved, or when the image's timeshift state cannot be read - "timeshift is active; 0 would ask on screen whether to leave it" (`timeshift`), whatever the image's "check timeshift" setting says; while the image holds zaps after timeshift - "the receiver is holding zaps for a moment after timeshift; try again" (`zap_blocked`); with picture-in-picture shown and the image's "0 key in PiP" setting not "standard" - "picture-in-picture is showing and 0 is set to act on it" (`pip`); while a recording is played back - "a recording is being played back" (`playback`); while any other screen is open over the info bar (the channel list, the EPG, a menu) - "a screen is open on the receiver, and 0 does not reach the zap history there" (`screen_open`). More than one entry left afterwards - "the receiver did not clear its zap history" (`not_cleared`). 🔴 **The payload is ignored**. No permission: `key KEY_0` does the same, unguarded |
| `volume` | `0`-`100`, or `{"level": 42}` | Sets the volume, with the on-screen bar | Out-of-range values are clamped and noted in the log |
| `mute` | `ON` \| `OFF` | Sets mute | Never a blind toggle: the state is read first, and read back afterwards. A receiver refuses to mute at volume 0, and that refusal is reported |
| `key` | `KEY_OK` \| `{"key": "KEY_OK", "long": true}` | Injects a remote key | Unknown key names are refused with the name in `last_error`; at most 20 a second |
| `message` | `{"text": "...", "type": "info", "timeout": 10}` - or a bare string; `"style": "toast"` since 0.3.0 | Shows an on-screen popup, or a discreet toast | `text` is required; since 0.3.0 **every backslash is removed** and nothing else, as for a toast, and what is left is truncated to 500 characters; `type` is `info`, `warning` or `error` (default `info`); `timeout` is seconds (default 10, `0` until dismissed). A new message replaces the previous one rather than queueing behind it. The toast's rules differ - see below |
| `timer` | see below | Adds or deletes a recording timer | An add that overlaps an existing timer, or refers to an unknown event, is refused - and so is one the receiver quietly dropped as a duplicate of a timer it already had |
| `record` | `start` \| `stop` | Starts or stops an instant recording of the current service | `start` records the current service for two hours; `stop` with nothing recording is a no-op with a note in `last_error` |
| `screenshot` | any | Captures `screen` now, in standby as well | Rate-limited to one per five seconds |
| `softcam_restart` - since 0.3.0 | any (`PRESS` by convention) | Stops **every** running instance of the cam the image selected, then starts exactly one, with the image's own command line | Refused unless `softcam_restart_allowed` is on; refused by the same recording guard as `deep_standby` - while recording, with a timer due within 10 minutes, and when the image will not say; refused for the first 60 seconds after the plugin starts, because the image's own check runs a moment after that and restarting inside that window races a copy already on its way; at most one manual restart a minute; refused while one is already running. 🔴 **The binary is resolved on the box and no part of the command line comes from the payload** |
| `epg_grid` | any | Rebuilds and republishes **every** `epg_grid/<bouquet_slug>` topic | Refused with a note in `last_error` when `epg_grid_events` is `0` |
| `epg_import` - since 0.3.0 | any (`PRESS` by convention) | Starts the image's EPG importer, the way its own „Manual" button does, and reports on `epg_import`; the grid is rebuilt afterwards and each bouquet is published only if it changed | Refused, in this order: unless `epg_import_allowed` is on; when the importer is not available (no capability); while an import is already running, whoever started it; by the same recording guard as `deep_standby`; within 10 minutes of the importer's own scheduled run, which the image starts without checking whether one is running, and when that time cannot be read; when the importer has no sources selected. A start that fails is `failed` on `epg_import` **and** the same sentence on `last_error`. 🔴 **The payload is ignored**: nothing from the broker reaches the importer |
| `config` | `{"publish_keys": false, "screenshot": "interval", "screenshot_interval": 90, "screenshot_delay": 4, "cam_telemetry": false, "oscam_telemetry": false, "softcam_autoheal": false, "softcam_autoheal_seconds": 90}` | Atomically replaces the remotely writable settings subset and publishes fresh `info`/discovery | The original three keys remain required; newer keys are independently optional for older clients and preserve their current values when omitted. Unknown keys and coercion are refused, and a persistence failure applies none of them |
| `discovery` | any | Republishes the announcement, the channel list, and the discovery payloads in discovery mode | - |
| `ha_mode` | `discovery` \| `integration` \| `off` | Switches the Home Assistant mode | See below |
| `reset` | any | Retracts every retained topic this node owns, then republishes | See below |
| `uninstall` - since 0.3.0 | this receiver's node id, exactly (surrounding whitespace is stripped; case matters) | Removes the plugin from the receiver: stops publishing, retracts every retained topic this node owns at QoS 1, publishes `offline` last, removes the package and restarts the interface. 🔴 **A one-way door** | Refused, before anything changes, in this order: unless `uninstall_allowed` is on - „uninstall is switched off in the plugin's settings"; unless the payload is this node's id - „the payload must be this receiver's node id"; without the `uninstall` capability - „this plugin was not installed by the package manager, so it cannot remove itself"; while an EPG import runs - „an EPG import is running", exactly as `restart_gui` refuses it and with the same lapse, because the removal ends in the same restart; by the same recording guard as `deep_standby`; where the image has no way to restart the interface; while a removal is already running - „an uninstall is already running". See below |

Every one of them is refused when it arrives retained, as above.

### `cmd/zap` goes through the channel list - since 0.3.0

The receiver keeps a list of the channels zapped to, the one its own "History Zap" screen shows on
KEY_NEXT and KEY_PREVIOUS, and it only records a zap that passes through its channel selection. So
`cmd/zap` does what the remote's number entry does: it calls the image's
`InfoBar.instance.selectAndStartService(service, bouquet)`, and the zap is recorded exactly as a
remote zap is, bouquet included. The payload is unchanged. The bouquet is, in order:

1. the bouquet the channel list is browsing, when it is a published bouquet (`channels`) and the
   service is in it - so a zap to a channel of the bouquet being browsed never moves the channel
   list;
2. otherwise the first published bouquet that holds the service - and the channel list **moves to
   that bouquet**, as it does after a number zap on the remote: channel up and down walk it
   afterwards, and `bouquet` names it.

Ten exits play the service directly with the image's `playService`, as before 0.3.0, and the zap
is **not recorded**. Earlier versions of this page listed six; the code has ten. They
are checked in the order below, and the first that applies wins. None of them is an error: the
channel is tuned, `last_error` stays as it was, and the 5 s verification on `service` runs as for
any zap. Each is logged at `info` as

    zapping to <sref> without the channel list: <reason>

with the reason from the table - except exit 7, which is logged once per service until the
interface restarts, as `zapping to <sref> without the channel list, so it is not in the zap
history: it is in no published bouquet`.

| # | Case | `<reason>` in the log | Why |
|---|---|---|---|
| 1 | the image has no channel-list zap: no `InfoBar.instance`, no `servicelist` or `selectAndStartService` on it, or a channel list without `getRoot` | `this image has no channel-list zap to record it with` | there is nothing to record the zap with. On such an image every `cmd/zap` is a direct play |
| 2 | a screen is open over the info bar - the channel list, the EPG, a menu, a question, a recording being played back - but not an information popup alone (below) | `a screen is open on the receiver` | the zap would work on a list somebody is looking at, or leave the remote on a channel list opened out of sight. The receiver's executing dialog (`session.current_dialog`) must be the info bar itself, or an information popup directly over it; when the session does not say, a screen counts as open |
| 3 | the channel list is in picture-in-picture zap mode | `the channel list is in picture-in-picture zap mode` | the channel list would zap the small picture |
| 4 | timeshift is active, or waiting to be saved - and also when the image's timeshift state cannot be read | `timeshift is active` | the channel list would ask on the television, with no timeout, whether to leave timeshift. The plugin treats any active timeshift as blocking, whatever the image's own "check timeshift" setting says. What happens to the timeshift is [below](#a-zap-from-home-assistant-during-timeshift) |
| 5 | the channel list is in radio mode | `the channel list is not in television mode` | a television bouquet entered under the radio root would be saved as the radio list's root. This covers a television channel too: while the receiver's channel list was last left in radio mode, no zap from Home Assistant is recorded |
| 6 | reading the published bouquets to choose one raised | `the channel list could not be read` | also logged as an exception, `could not choose a bouquet to zap through` |
| 7 | the service is in no published bouquet - a radio station (`channels` holds television bouquets only), a bouquet `bouquets_for_select` leaves out, a reference in no bouquet | `it is in no published bouquet` | there is no bouquet to enter it through. A radio station zapped from Home Assistant is therefore never recorded; one zapped with the remote is |
| 8 | the image's `selectAndStartService` raised | `the channel list's zap raised` | also logged as an exception, `selectAndStartService raised`. Whatever the channel list did before it raised is not undone; what that leaves in the history is not measured |
| 9 | the channel list could not select the service, nothing was tuned, and the service is not protected by parental control | `the channel list could not select it` | preceded by a warning, `the channel list did not select <sref>; playing it directly`. The published bouquets are read once a minute, so a bouquet edited since then, or a list that hides the channel, leaves the selection on what is playing, and the channel list re-zaps that |
| 10 | the channel list tuned a different service | `the channel list tuned another service` | preceded by a warning, `the channel list tuned <tuned> instead of <sref>; playing it directly`. A stale bouquet can leave the selection on a neighbour, and the wrong channel on the television is worse than an unrecorded zap to the right one. The neighbour went through the channel list's zap, so it may be in the history while the requested channel is not (read from the code, not measured) |

**One case counts as recorded whatever happens next.** When nothing was tuned and the service is
protected by parental control - or the image cannot say whether it is - the PIN screen on the
television is what the zap is waiting for. The plugin logs nothing and leaves it to the PIN: the
zap is recorded if the PIN is entered, and the 5 s verification on `service` reports it if not.

A session with no navigation, or a navigation with no `playService`, is not an exit: `cmd/zap` is
refused - "there is no session to zap with", "this image's navigation has no playService".

**An information popup is not a screen open.** When the only thing open is a popup directly over
the info bar - the receiver's own "Zapped to timer service", a recording or zap error, or what
`cmd/message` shows - the zap goes through the channel list and is recorded, and the popup stays
until its own timeout, as it does after the receiver's own zap-timer zap. A protected channel's
PIN is queued behind the popup and appears when it closes, as it would for a direct play. Exactly
this counts: the image's own
`Screens.MessageBox.MessageBox` (not a class built on it), of type information, warning or error,
with no answers to choose from, executing, with the info bar as the one screen under it. A question
(which is what a message box queued without a type is), a type the image does not know, a popup
over any other screen - the movie player, the channel list - and a popup already closing still
count as a screen open. A toast is not a dialog at all and never counts.

**A question, or any other screen, stays open and unanswered.** While a yes/no question or any
other screen is open over the info bar, `cmd/zap`, `cmd/zap_history` and the zap `cmd/bouquet`
makes still change the channel - directly, with `playService`, and unrecorded - and they leave that
screen exactly as it was: the question stays on the television, unanswered, and nothing is closed
or answered for the household. This is intended. The household keeps control with the remote and
answers the question when it wants to, and a zap from Home Assistant is an explicit request, so it
is carried out rather than refused. Timeshift is a separate case with its own rules:
see [below](#a-zap-from-home-assistant-during-timeshift).

**From standby** the receiver is woken first. Its standby screen closes on the next turn of the
main loop and plays the channel it slept on; the zap follows on the turn after that, so it is
recorded and the restored channel is not. The 5 s verification on `service` starts when the zap is
made. When the standby screen has not closed within 5 s, `last_error` says "the receiver did not
leave standby" and no zap follows. **Any later zap replaces one still waiting** - `cmd/zap`,
`cmd/zap_history` or `cmd/bouquet`, whether it arrives before the standby screen has closed or in
the moment between that and the waiting zap running.

A zap made this way asks for a parental-control PIN on the television exactly as one from the
remote does, and it never opens or shows a screen of its own. The same "screen open" rule applies,
information popup included, to `cmd/zap_history` (played directly, the history left as it is) and
to the zap `cmd/bouquet` makes after changing the context. `cmd/history_clear` still refuses under
a popup (`screen_open`): it is the 0 key, and with a popup on the screen the key goes to the popup.

### What enters the receiver's zap history

The receiver records a zap only when it passes through its channel selection, which calls
`addToHistory`; the image's `playService` on its own never records. The plugin publishes the list
as the receiver keeps it (`zap_history`) and adds nothing to it, so a zap missing here is missing
from the receiver's own History Zap screen too.

What follows is read from the bytecode of one image, OpenViX 6.6, and the rows marked
**measured** were also checked on a receiver running it. Other images may route their zaps
differently.

| Zap | In the history? |
|---|---|
| The remote: a number, a channel chosen in the channel list, channel up and down, the History Zap screen | Yes. A radio station too, while the image's "e1-like" radio mode is on (its default); its `bouquet_name` is `null` |
| `cmd/zap` | Yes, filed under the bouquet chosen as [above](#cmdzap-goes-through-the-channel-list---since-030) - except the ten direct-play exits in that section. From standby, the zap is recorded and the channel the receiver slept on is not |
| `cmd/zap_history` | Yes: the entry moves to the front, as the History Zap screen moves it - also under an information popup, which stays open. With any other screen open over the info bar, a question included, it is played directly, the list is left as it is and the screen stays open. During timeshift it leaves timeshift without asking, as the History Zap screen does (read from the code, not measured) |
| `cmd/bouquet`, when it has to tune | Yes, through the channel list's own zap - also under an information popup. With any other screen open over the info bar the channel is played directly and not recorded, and the screen stays open. Refused during timeshift and while the channel list is in radio mode |
| `cmd/history_clear` | The list is emptied and channel 1 is recorded, as the 0 key does. Refused during timeshift, reason `timeshift` (**measured**) |
| A zap timer, receiver awake, no timeshift | Yes (**measured**). It is filed under the **first bouquet in the receiver's own bouquet order** that holds the channel - not the bouquet being browsed, and not only among the published ones, so `bouquet_name` is `null` when that first bouquet is one `bouquets_for_select` leaves out. The channel list moves to that bouquet, so channel up and down walk it afterwards. When the image's "show message when recording starts" setting is on (its default) the receiver shows "Zapped to timer service" for 5 s. A `cmd/zap` in those 5 s goes under the popup and is recorded (the information-popup rule above). Before that rule it was measured played directly and unrecorded; the recorded behaviour is covered by tests and not yet measured on a receiver |
| A zap timer, receiver in standby | No. The receiver wakes and plays the timer's channel the way it restores the channel it slept on, with `playService` - or its start-up channel, when it is set to start on one after standby |
| A zap timer during timeshift | No. The receiver asks on the television, for 20 s: "Zap" and "Save timeshift and zap" play the channel directly; "Don't zap" leaves it alone, and the two remaining answers also disable or remove the timer |
| A zap timer for picture-in-picture | No. It plays in the small picture |
| The EPG, first OK on a channel | No. It is a preview |
| The EPG, a second OK on the same channel | Yes. It confirms the preview through the channel selection |
| Closing the EPG after a preview | No. The receiver returns to the channel it was on, with `playService`, while the image's EPG preview mode is on (its default for every EPG style). With preview mode off, closing confirms the preview instead, and that is recorded |
| After an EPG preview closed that way | A gap. Until the channel list is next used - a channel chosen in it, a confirmed EPG zap, or the image's zap back - a zap back to the channel that was playing before the preview is not recorded. That includes a `cmd/zap` to it, which the plugin counts as recorded because it only checks that the channel plays (read from the code, not run) |
| OpenWebif's own zap | Not read |

Only the two rows marked measured and the timeshift section below were checked on a receiver. The zap-timer rows for standby and timeshift, the EPG rows and the post-preview gap are
read from the bytecode and not run.

### A zap from Home Assistant during timeshift

While the receiver is in timeshift - the service is seekable and timeshift is enabled, or a
timeshift is waiting to be saved - `cmd/zap` plays the channel directly (exit 4 above). That is
every zap from Home Assistant: a channel select, the source list, `play_media`, the zap action.

What happens, measured on OpenViX 6.6:

- The channel is tuned with **no question on the television**, and the receiver shows the new
  channel live.
- The zap is **not in the zap history**. The list is left as it was, so the channel playing is not
  its current entry, and a consumer showing the current entry has none to show (the companion
  integration's history select reads `unknown`) until the next recorded zap.
- **The timeshift buffer stays on the recordings disk.** The buffer file (`pts_livebuffer_<n>` in
  the image's timeshift directory) and its `.sc` index file were left behind, and they had stopped
  growing: same size and time ten seconds later. After a pause of about ten seconds on a
  high-definition channel that was about 28 MB. The plugin does not delete them.
- `last_error` is untouched: a direct play is not a refusal.

The plugin does this because the receiver's own zap would ask a question on the television with
no timeout, and a zap from a phone must not leave a question on somebody's screen. It also plays
directly where the receiver would not have asked - its "check timeshift" setting off, or a
timeshift already stopping. That is conservative and costs only the history entry.

What the receiver does on its own path, read from the bytecode and not something the plugin does:
with "check timeshift" on (the default) it asks. With the timeshift save action "ask user" (the
default) it offers four answers, in this order: two that save the timeshift as a recording (one
stops recording afterwards, one goes on recording), "Yes, but don't save", and "No". With any
other save action it asks Yes or No, and Yes applies that configured action, which may save the
timeshift as a recording. **"Yes, but don't save" erases the timeshift buffer files.** The plugin
answers none of these questions and saves nothing. **Careful when scripting `cmd/key` against that
question:** `KEY_OK` takes the highlighted answer, which is the first one and saves the buffer as a
recording; `KEY_EXIT` answers "No".

The other commands during timeshift:

- `cmd/zap_history` is recorded and leaves timeshift without asking, as the receiver's own History
  Zap screen does (read from the code, not measured - including what it leaves on the disk).
- `cmd/bouquet` is refused: "timeshift is active; the receiver would ask on screen whether to leave
  it".
- `cmd/history_clear` is refused with reason `timeshift` (measured).

**Not measured:** whether the image removes the left-over buffer files later, for instance when
the next timeshift starts or the interface restarts; what a direct play does to a timeshift already
marked for saving; and all of this on any image other than OpenViX 6.6.

### `cmd/history_clear` is the 0 key - since 0.3.0

On the images read, 0 is a single press on key-down - holding it does nothing more - and with the
image's defaults it empties the zap history and switches to **channel 1**. There is no "panic
channel" setting: channel 1 is the first channel of the first bouquet, and it moves when the
bouquet order does. The command runs the key's own handler, never an injected key press, which
would go to whatever screen has focus; `cmd/key KEY_0` does that, unguarded. What the household
sees is what the key does: the television switches to channel 1, picture-in-picture closes if it
was open, and the history holds that one channel.

The image's settings decide what 0 does, and the plugin changes none of them:

| Image setting | Default | Effect |
|---|---|---|
| `config.usage.panicbutton` | on | on: clear and channel 1; off: 0 goes back to the previous channel and clears nothing - so `cmd/history_clear` is refused, and `zap_history.panic_button` says `false` |
| `config.usage.multibouquet` | on | where channel 1 is looked for: the bouquet list, or the favourites when off |
| `config.usage.pip_zero_button` | `standard` | anything else: 0 acts on picture-in-picture while it is shown - refused |
| `config.usage.check_timeshift` | on | whether 0 asks its question in timeshift. The plugin does not read it: it treats any active timeshift as blocking and refuses, whatever this says |

`cmd/history_clear` is also refused while any screen is open over the info bar (`screen_open`):
the key reaches this handler only on the info bar, and with the channel list, the EPG or a menu
open, 0 means something else there.

### `cmd/message` styles - `toast` since 0.3.0, capability `toast`

```json
{"text": "...", "style": "toast", "timeout": 5}
```

`style` is optional. **Absent or `null` is the popup, as before 0.3.0** - with one change to its
text, below - and so is a payload that is not a JSON object. The style is decided before any
default is applied.

| Field | Type | Popup | Toast |
|---|---|---|---|
| `text` | string | required; empty refused; **every backslash is removed** and nothing else (since 0.3.0, the toast's rule), then truncated at **500** | required; empty refused; **every backslash is removed** and nothing else, then truncated at **200** |
| `style` | string, optional | absent, `null` or `"popup"` | `"toast"` (trimmed, case-insensitive). Any other value is refused: „unknown message style '...'; expected popup or toast" |
| `timeout` | integer seconds, optional | default **10**; `0` or less = until dismissed | default **5**; **`0` or less is refused** („a toast hides itself; timeout must be 1&ndash;30 seconds"); more than `30` becomes `30`, with a note in the log |
| `type` | `info` \| `warning` \| `error`, optional | chooses the box's icon | **validated exactly as for a popup** - an unknown value is refused, so a payload is valid or invalid whatever its style - and then **ignored** |

**One text rule for both styles.** `\cFFFF0000Alarm` shows as `cFFFF0000Alarm`, `C:\config.txt` as
`C:config.txt`, a literal `\n` as `n`; a real newline character is not a backslash and stays a line
break. The cap counts the characters left after the removal, and a text with nothing but backslashes
and blanks is refused as empty („message text is empty"). enigma2's text renderer reads a backslash
and what follows it as a colour change or a line break, and it does so after right-to-left
reordering, where no narrower rule over the string can find it. Until 0.3.0 the popup passed every
backslash through, so a sender that relied on `\n` for a line break in a popup has to send a real
newline instead.

`timeout` is read as the popup reads it, with `int()`: `"5"` is 5, `5.9` is 5 and `0.5` is 0, which a
toast refuses; `null` or a non-number is refused with „'...' is not a number of seconds".

A **toast** is a plugin-owned, non-modal screen in the **top right**, above the channel list, the
info bar and the popup. It has one fixed appearance - a dark, slightly transparent box and light
text - under a header that always reads „MQTT Bridge" in the receiver's language, whatever the
payload says. It auto-hides and does nothing else: it never becomes the current dialog, cannot be
dismissed by the remote, and never enters the image's notification queue, so it cannot wait behind
an open channel list the way a popup does. A newer toast replaces the one on screen and restarts
its timer; there is no queue. A toast and a popup are independent: neither removes the other.

🔴 **It cannot take a key press because of what it is made of, not only because it binds no action
map.** A never-executed screen binds no action map, but some of enigma2's widgets bind keys natively
in their own constructors - every list does - with no action map and no exec. The toast holds two
text labels and nothing else, so nothing in it can bind a key, and a test enforces that.

It is **hidden when the receiver enters standby**, and a toast that arrives while the receiver is in
standby, or while the receiver's own „really shut down / restart?" question is on screen, is
**refused** on `last_error` with „the receiver is in standby" rather than kept for later. That
question appears only when there is a reason to ask - a recording, a running job, timeshift, a
stream - and is gone before the receiver actually quits, so an ordinary shutdown or interface restart
is not covered by this refusal. It is deleted - not merely hidden - when the
plugin stops, so a toast on screen does not survive into the frame a restarting receiver leaves
behind. A toast requested while the capability is absent is refused with „the discreet toast is
switched off on this receiver" (`osd_toast` off) or „the discreet toast could not be created on this
receiver".

The capability `toast` is claimed **only once the screen has actually been created**, and is taken
back - with `info` republished - if a skin reload cannot create it again. The setting `osd_toast`
(default on, never writable over MQTT) is its kill-switch. An image where either fails keeps popups rather than
gaining a style that silently does nothing.

### `cmd/timer` payload forms

```json
{"action": "add", "sref": "1:0:19:...:", "event_id": 27431}
{"action": "add", "sref": "1:0:19:...:", "begin": 1789459200, "end": 1789460700, "name": "Wiadomości"}
{"action": "delete", "sref": "1:0:19:...:", "begin": 1789459200, "end": 1789460700}
```

The first form is the one to prefer: enigma2 resolves the event itself, so the timer inherits the
programme's padding and its name. The second is for a manual window. Deletion matches on the
triple `sref` + `begin` + `end`, which is what enigma2 itself uses as a timer's identity, and
takes any timer `timers` lists - pending, `ended`, `failed` or `disabled`. Deleting a finished
timer removes the entry, never the recording it made.

The triple is not always unique. A timer somebody disabled and then set again exists twice - the
receiver checks a new timer only against its pending ones. **A delete removes the pending copy
first**, then, on the next delete, the other one. That is what a delete did before finished
timers could be deleted at all, so a delete of a pending timer - including one that stops a
running recording - behaves exactly as it always has; it is also the order OpenWebif uses.

🔴 **The `begin` and `end` of a timer are not the `begin` and `end` of the programme.** A receiver
applies its own recording margins - typically a few minutes before and after - so a timer added
from an `event_id` appears in `timers` with wider times than the `epg` topic showed. Delete it
with the times `timers` reports, not the ones the programme had.

Either way, `timers` (and `recording` when it is imminent) is republished afterwards.

### `cmd/config` semantics

This is deliberately not a general settings API. Broker credentials, TLS, identity, topic names,
the configured bouquet filter, logging and destructive-command permission cannot be changed
through `cmd/config`. `deep_standby_allowed` and `softcam_restart_allowed` are **read** from `info.settings` (§1) and are
refused here like any other key outside the allowlist; they are granted on the receiver - its setup
screen, the provisioning file or the OpenWebif page - and never over MQTT, and reading a setting and
writing it are two different permissions. Home Assistant mode has its dedicated command, and active TV bouquet
context has `cmd/bouquet`; neither broadens this settings API. The command accepts the three original keys plus independently optional `screenshot_delay`,
`cam_telemetry`, `oscam_telemetry`, `softcam_autoheal` and `softcam_autoheal_seconds`,
with their JSON types unchanged. The two softcam keys only *tune* a restart the receiver has
already permitted; with `softcam_restart_allowed` off they change nothing, because the
permission is the gate. The plugin validates the whole object before assigning anything,
persists the values through enigma2's settings store, then rebinds only the affected publishers
so the new behaviour is immediate. A fresh `info.settings` object is the by-effect acknowledgement.
Switching screenshots off also retracts the retained `screen` image so disabling the private
feature does not leave its last picture readable from broker retention. Disabling either CAM
telemetry option likewise retracts its retained topic. OSCam host, port and credentials are never
accepted here or published in `info`.

🔴 **What this command can switch on is the privacy boundary, and the broker is where that
boundary is.** `cmd/config` is deliberately writable, because the companion integration's options
flow is built on it - which means **any client with publish rights on `<base>/<node>/cmd/config`
can turn on `screenshot`, `publish_keys`, `cam_telemetry` and `oscam_telemetry`**, and with
screenshots on it can ask for a picture of the television at any time with `cmd/screenshot`. There
is no second, box-local confirmation for this, by design: a broker login that can publish there is
a login that can watch the living room. Give the receiver its own broker credential, restrict it
with an ACL, and treat `cmd/#` on this node as sensitive - the ACL recipe is in the project's
README. The settings this command cannot reach are listed above, and that list is the only limit
the plugin itself imposes.

### `cmd/ha_mode` semantics

| Value | Announcement on `enigma2mqtt/discovery/<node>/config` | HA discovery payloads | State topics |
|---|---|---|---|
| `discovery` | published | published | published |
| `integration` | published | **retracted**, then never published | published |
| `off` | **retracted**, then never published | **retracted**, then never published | published |

`off` is for anything that is not Home Assistant: openHAB, Node-RED, a script. The box stays
fully observable and controllable; it simply stops advertising itself.

Switching modes always **retracts before it announces**, so entities are never duplicated. The
new value is echoed on `info.ha_mode`, which is the acknowledgement the companion integration
waits for when it takes a box over.

### `cmd/reset` semantics

Two halves, in this order, on the one session that is already open:

1. **Retract.** An empty retained payload to every retained topic this node owns - all the state
   topics, every `epg_grid/<bouquet_slug>`, the announcement, and every Home Assistant discovery
   payload named in the state file - and then the plugin forgets what it had announced.
2. **Republish, immediately.** `availability: online`, the full state snapshot, the announcement
   and, in `discovery` mode, the discovery payloads - **the same sequence as `on_connect`**, run
   straight away rather than waited for. The state file is written again with what was just
   published.

That second half is why a reset is **safe to run at any time**: the broker is empty of this node's
topics for the width of one publish burst, not until the box next reconnects, and a subscriber
that was listening throughout ends up exactly where it started. Home Assistant sees the entities
go unavailable and come back, which is the same thing it sees when the box reboots.

It is also the documented step **before uninstalling by hand**, because retained topics outlive
the plugin that created them: remove the package without it and the broker keeps serving a snapshot
of a box that is gone, forever, while Home Assistant keeps showing entities nothing will ever update.
Do it while the plugin is still running and connected - after `opkg remove` there is nothing left to
ask. `cmd/uninstall` does it for you, without the republish; never run a reset after one.

It is a cleanup, not a factory reset: settings are untouched.

### `cmd/uninstall` semantics - since 0.3.0

The handler checks the refusals in the table above and returns - so a `last_error` left by an
earlier refusal is cleared - and the removal runs from the next turn of the main loop, in this
order, never blocking it ([ADR-0013](adr/0013-the-uninstall-closes-the-doors-and-waits-for-the-broker.md)):

1. **Stop publishing.** Every publisher lets go of its hooks and no further command is dispatched;
   a command arriving now is dropped with a line in the log, and one from the OpenWebif page is
   answered „an uninstall is already running" without touching `last_error`. From the moment
   the command is accepted, a Save on the receiver's setup screen or on the OpenWebif page keeps
   its settings but neither reconnects nor applies them, and the bridge does not start again.
   `cmd/config` in the same window saves its values, applies nothing and publishes nothing but a
   `last_error` saying so, which the retraction then empties with everything else. From here on nothing re-creates a topic.
2. **Retract** - an empty retained payload - every topic in the state file (§4), which is the set
   `cmd/reset` uses, **and** every command topic of this node on which somebody left a retained
   message during this session (the dispatcher discards those; this is where they are cleared).
   🔴 **At QoS 1**, the one exception to „state at QoS 0": a QoS 0 publish counts as done when it
   reaches the socket, and this is the one sequence that must know the broker has it. A subscriber
   receives at the lower of the two QoS values, so no consumer sees a difference.
3. **`availability: offline`**, retained, QoS 1 - the node's last message. No `online` follows.
4. **Wait for every acknowledgement**, polled every 100 ms for at most **15 s**, with no more than
   half of the client's 200-message queue outstanding at once.
5. **Empty the state file.** It stays in place, saying nothing is published.
6. **Disconnect cleanly.** The last will is not sent; `offline` from step 3 is what stays retained.
7. **`opkg remove enigma2-plugin-extensions-mqttbridge`**, through enigma2's own process runner - no
   `--autoremove`, no `--force` - with `MQTTBRIDGE_UNINSTALL=1` in its environment, which only
   silences `prerm`'s advice to run `cmd/reset`. Its output goes to the plugin's log. 🔴 **An exit
   status of 0 is checked against the disk**: enigma2's process runner reports an opkg killed by a
   signal - or one still running when enigma2 itself went away - as 0, so the package's `.control`
   and the plugin's `plugin.py` must both be gone before step 8.
8. **Restart the user interface**, as `cmd/restart_gui` does. Best effort: with a stream, a
   background job or timeshift running, the receiver asks on screen and waits for an answer. The
   plugin is already disconnected and removed from disk by then.

🔴 **When a step fails, the removal stops there and puts everything back.** No acknowledgement
within 15 s, a dropped connection or a refused publish during steps 2-4, or opkg that cannot start
or exits non-zero - its lock is also taken by the image's own daily update check and its plugin
browser - and nothing further is removed: the plugin opens a fresh session, which republishes
`online`, the full snapshot, the announcement and discovery as every connect does, and then
publishes `last_error` with `"cmd": "uninstall"` and a sentence naming the step - for opkg, its exit
status and its last line of output. It ends where a reset ends. Anything unforeseen that raises
after step 1 is treated the same way, as a failed step, rather than leaving a plugin that neither
publishes nor listens.

🔴 **opkg is not atomic.** It deletes a package's files one at a time, so an opkg that exits 0 while
the package is still installed may have removed some of the plugin already. That case is a failed
step too, and its `last_error` gives the command that puts the plugin back whole:
`opkg install --force-reinstall enigma2-plugin-extensions-mqttbridge`.

What a subscriber sees on success is therefore: every retained topic of the node emptied, then
`offline`, then nothing - while a switched-off receiver leaves `info` and the announcement in place.
There is **no Home Assistant discovery component** for this command: a core MQTT button has no
confirmation, and this is the one command that must not be one press away on a dashboard. The
OpenWebif page offers it behind a confirmation and fills in the node id itself; like every command
from the page, it does not need the permission there.

`/etc/enigma2/settings` is not touched - a reinstall finds its configuration, the broker password
included - and neither are the plugin's log, its backups directory or the package feed's
configuration; `docs/SETUP.md` lists what stays and why.

---

## 3. Announcement

`enigma2mqtt/discovery/<node>/config`, retained, QoS 0. Published in `discovery` and
`integration` modes, retracted in `off`.

```json
{
  "node_id": "vuuno4kse_005301",
  "name": "Living room receiver",
  "base_topic": "enigma2",
  "image": "OpenViX 6.6.007",
  "enigma": "2024-09-11-Release",
  "plugin": "0.3.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "capabilities": ["power", "service", "epg", "tuner", "softcam", "recording", "timers",
                   "volume", "hdd", "process", "channels", "bouquet_context", "epg_grid", "keys",
                   "screenshot", "toast", "message"],
  "ha_mode": "discovery"
}
```

`name` is the `friendly_name` setting - the device name a user sees. `base_topic` is repeated
here so that a consumer can find the state topics without being told where to look; a box behind
an MQTT bridge with a rewritten prefix is the case this serves.

The companion integration subscribes to `enigma2mqtt/discovery/#` and offers a config flow for
every announcement it sees. Anything else that wants to enumerate boxes on a broker can do the
same.

---

## 4. Home Assistant discovery

Published only in `discovery` mode, retained, under the `ha_discovery_prefix` setting
(`homeassistant` by default).

| Topic | What |
|---|---|
| `homeassistant/device/<node>/config` | One device-based discovery payload carrying every component in its `cmps` block: the power and mute switches, the volume number, a channel `select` whose options are the services of the configured bouquets, the channel and programme sensors, tuner sensors, recording and disk binary sensors, the screenshot image, and the buttons |
| `homeassistant/device_automation/<node>/<colour>_<press>/config` | Eight payloads - `red`, `green`, `yellow`, `blue` × `short`, `long` - so the colour keys are device triggers in the automation editor |

Device-based discovery (one payload, many components) is used rather than a payload per entity:
it is a single retained topic to retract, and the device identity cannot drift between
components.

### What is announced, and what it reads

Every component is gated on a **capability**: a box whose image did not provide the volume hooks
gets no volume entity, rather than one that never moves. The unique id of each is
`<node>_<key>`, and the key is the one in this table.

| Key | Platform | State from | Notes |
|---|---|---|---|
| `power` | switch | `power` | `on` / `standby` as the payloads |
| `channel` | sensor | `service` | State is the name; the rest are JSON attributes |
| `program` | sensor | `epg` | State is `now.title`; attributes below |
| `next_program` | sensor | `epg` | `next.title` |
| `recording` | binary_sensor | `recording` | On when `active` is not empty |
| `active_recordings` | sensor | `recording` | How many |
| `next_timer` | sensor | `recording` | `device_class: timestamp` |
| `volume` | number | `volume` | 0-100, slider |
| `mute` | switch | `volume` | |
| `channel_select` | select | `service` | Options are the channel names of the configured bouquets; selecting one publishes `cmd/zap` |
| `history_clear` | button | - | Since 0.3.0, when `history_clear` is a capability. Publishes `cmd/history_clear`: the receiver switches to channel 1 and empties its zap history |
| `screen` | image | `screen` | `image/jpeg` |
| `screenshot`, `restart_gui`, `refresh_discovery` | button | - | And `deep_standby` and `reboot` **only** when `deep_standby_allowed` is on |
| `snr`, `agc`, `ber` | sensor | `tuner` | Diagnostic, disabled by default |
| `recording_disk` | binary_sensor | `hdd` | Diagnostic |
| `softcam` | sensor | `softcam` | Since 0.3.0. Diagnostic. State is `selected`; the counts and the last restart are JSON attributes |
| `softcam_restart` | button | - | Since 0.3.0, and only when `softcam` is a capability **and** `softcam_restart_allowed` is on |
| `epg_import` | sensor | `epg_import` | Since 0.3.0. Diagnostic. State is `state`; `started`, `finished`, `events` and `error` are JSON attributes |
| `epg_import_start` | button | - | Since 0.3.0, and only when `epg_import` is a capability **and** `epg_import_allowed` is on. Publishes `cmd/epg_import` |
| `process_memory` | sensor | `process` | Diagnostic, MiB, `data_size`, measurement - the one of these five that is **enabled** by default |
| `process_memory_peak` | sensor | `process` | Diagnostic, MiB, `data_size`, measurement, disabled by default |
| `process_threads`, `process_open_files` | sensor | `process` | Diagnostic, measurement, disabled by default |
| `process_started` | sensor | `process` | Diagnostic, `timestamp`, disabled by default |
| `uptime` | sensor | `info` | Diagnostic, seconds |

The channel names in `channel_select` are **deduplicated**: `cmd/zap` by name refuses a name that
is not unique, so offering the same „Sport" twice would be offering an option that can only fail.

Four details of the payload are worth knowing before writing a consumer against it, because each
was measured against Home Assistant rather than assumed:

- **Commands go out at QoS 1**, as the contract requires. The `qos` key at the top level of the
  device payload is how a consumer is told so; it reaches every component.
- **Availability is shared.** `avty_t` is set once, at the top level, and applies to every entity.
  Device triggers have no availability - their schema has no room for it.
- **The entity id each component asks for** is `default_entity_id`, not `object_id`. Home
  Assistant 2026.9 has no `object_id` in its MQTT schema; a payload carrying one is accepted and
  the key is silently discarded. It is also read **once**, when the entity is first created:
  changing it later renames nothing.
- **A component is removed by name**, by republishing the device payload with that component cut
  down to nothing but its platform. Leaving it out of the payload does not remove it - Home
  Assistant keeps what it last saw. This is what happens when a capability disappears: switch
  screenshots off and the image entity goes with them.

### What discovery deliberately leaves out

`cam`, `oscam` and `bouquet` have **no discovery components at all**, whatever their capabilities
say - and neither has `uninstall`, which a button with no confirmation must not offer. All three are consumed by the companion integration, which subscribes to the topics directly:
the first two would need a whole set of per-reader entities built from a list that changes shape,
and the third is a selector whose options are the channel list, not a state anybody wants as a
sensor. A plugin-only install reads them from the broker as they are documented above.

`zap_history` has **no select** either, although it is exactly the kind of list a select offers: a
core MQTT select carries its options inside this discovery payload, so a list that changes on every
zap would mean republishing discovery on every zap. The companion integration builds that select
from the topic; discovery mode offers the clear button only.

### The attributes the sensors carry

Two of those components carry JSON attributes rather than only a state, because the state is a
name and the useful identifier is not. They are part of this contract - SETUP.md's `universal`
media_player recipe reads `sensor.<box>_channel|sref`, and that only works because the attribute
is promised here.

| Component | State | JSON attributes |
|---|---|---|
| Channel sensor | the channel name | `sref`, `bouquet`, `provider`, `width`, `height` - the `service` payload minus the name |
| Programme sensor | the title of `now` | `begin`, `end`, `event_id`, `short`, `long`, and `next_title`, `next_begin`, `next_end` for the following programme |

The types are the ones the `service` and `epg` topics define; an attribute whose source field is
`null` is published as `null`, not dropped. `next_*` is flattened rather than nested because Home
Assistant templates read a flat attribute far more comfortably than a nested object.

### The device triggers

Eight retained payloads, one per topic, each with `automation_type: trigger`, the `key` topic, and
a `value_template` of `{{ value_json.key }}_{{ value_json.press }}` matched against a payload of
`KEY_RED_short` and its seven siblings. The `type` is `button_short_press` or `button_long_press`
and the `subtype` is the colour, which is how they are labelled in the automation editor.

They are published only when `keys` is a capability - with `publish_keys` off there is no `key`
topic for them to watch, and a trigger that can never fire is worse than none.

### The state file

The plugin keeps `/etc/enigma2/mqttbridge-state.json` - beside enigma2's own settings, or beside
the plugin itself when `/etc/enigma2` will not take a write - and it records **every topic this
node has published retained**: the state topics, every `epg_grid/<bouquet_slug>`, the
announcement, and every Home Assistant discovery payload. Beside that list it keeps two indexes
that a topic name alone cannot answer: the **slugs** of the EPG grids it has published, so a
bouquet that is renamed or dropped can be retracted, and the **components** it last announced with
their platforms, so one that is no longer announced can be removed by name from a payload that
otherwise contains only the survivors. It is written atomically, through a
temporary file in the same directory and a rename, so an interrupted write leaves the previous
list rather than half of a new one; it survives reboots and plugin upgrades, and `cmd/reset`
empties it and then fills it again with exactly what the reset republished. Nothing secret is in
it - it is a list of topic names.

That file is the whole answer to MQTT's oldest trap: a retained payload outlives the configuration
that created it. Rename a component, drop a feature, drop or rename a bouquet, change the node id,
and without the list the old retained topic stays on the broker and Home Assistant keeps an entity
that nothing will ever update again. So on every connect the plugin compares the list with what it
is about to publish and **retracts the difference first** - a rename made while the box was
switched off is caught the moment it comes back. It is also why `cmd/reset` exists.

Delete the file and the plugin still works, but every topic published before that point becomes a
retained ghost nobody can find: the list is the only record that they exist.

---

## 5. Planned (not implemented yet)

Nothing at the moment. This section is where a planned addition is written down before it is built,
so that a consumer can be written against its shape; the last ones - `uninstall_allowed` and
`cmd/uninstall` ([ADR-0004](adr/0004-remote-uninstall.md)) - are now in §1 and §2. Until a
capability is in `info.capabilities`, the box does not have it - that rule is unchanged, and it is
how a consumer tells a plan from a feature.

---

## 6. Worth knowing

- **A retained state topic is a snapshot, not a heartbeat.** `service` showing a channel means
  that is the last channel the box tuned - check `availability` before believing it is on.
- **An ACL-denied publish looks exactly like a successful one.** Mosquitto drops it silently.
  When the topics are missing and the log shows no error, subscribe as a privileged user and
  publish as the box's user to find out which it is.
- **Nothing here is a request/response protocol.** If a command seems not to have worked, read
  its state topic and then `last_error`; there is no per-command reply to wait for.
