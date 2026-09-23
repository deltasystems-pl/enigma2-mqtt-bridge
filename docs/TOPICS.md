# The topic contract

This is the interface between the plugin and everything that consumes it — the companion Home
Assistant integration, an openHAB binding, a Node-RED flow, a shell script with `mosquitto_sub`.
It is versioned with the plugin: a change to a payload is a change to this file, to the
compatibility table in both READMEs, and to the changelog.

Two names appear throughout:

- **`<base>`** — the `base_topic` setting, `enigma2` by default.
- **`<node>`** — the `node_id` setting, `<boxtype>_<last six MAC digits>` by default, lowercase
  ASCII, for example `vuuno4kse_005301`. It is stable across reinstalls and is the unique id the
  integration keys on.

Conventions that hold everywhere:

| | |
|---|---|
| Protocol | **MQTT 3.1.1**. No MQTT 5 feature is used or required — no user properties, no response topics, no subscription identifiers, no shared subscriptions — so the contract holds on every broker a receiver can be pointed at, including the old ones. A broker running MQTT 5 serves it unchanged. |
| Encoding | UTF-8. Every payload is JSON unless the table says otherwise. |
| **State topics** | QoS **0**, **retained** (the exceptions are marked). A fresh subscriber gets the current state immediately, without asking the box for it. |
| **Command topics** | QoS **1**, **never retained**. A retained command would re-fire on every reconnect; the plugin refuses to publish one and you should not either. |
| Timestamps | `begin`, `end`, `generated`, `ts` are **Unix epoch seconds, UTC, integer**. Never a formatted string, never local time. |
| Absent values | `null` for a field that has no value right now (no next event, no recording). A key is not silently dropped. |
| Retraction | An empty payload published retained. That is how `last_error` is cleared and how `cmd/reset` and discovery retraction work. |

Everything the plugin publishes it also publishes again on every `on_connect` — the full state
snapshot, the announcement and, in discovery mode, the discovery payloads. A broker that lost its
retained store, or a box that reconnected after an outage, converges without anybody asking.

---

## 1. State topics

### `<base>/<node>/availability`

Retained, QoS 0. **Not JSON** — the literal string `online` or `offline`.

`offline` is registered as the connection's last will, so the broker publishes it when the box
vanishes without saying goodbye. The plugin publishes `online` in `on_connect` and a clean
`offline` on a graceful shutdown.

### `<base>/<node>/info`

```json
{
  "image": "OpenViX 6.6.007",
  "enigma": "2024-09-11-Release",
  "plugin": "0.2.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "uptime": 384210,
  "ha_mode": "discovery",
  "settings": {"publish_keys": true, "screenshot": "on_zap",
               "screenshot_interval": 60, "screenshot_delay": 4,
               "cam_telemetry": false, "oscam_telemetry": false,
               "softcam_autoheal": false, "softcam_autoheal_seconds": 90,
               "deep_standby_allowed": false, "softcam_restart_allowed": false,
               "epg_import_allowed": false},
  "capabilities": ["power", "service", "epg", "tuner", "softcam", "recording", "timers",
                   "volume", "hdd", "process", "channels", "bouquet_context", "epg_grid", "keys",
                   "screenshot", "toast", "message"]
}
```

| Field | Type | Meaning |
|---|---|---|
| `image` | string | Image name and version as the box reports it |
| `enigma` | string | enigma2's own version string, as `getEnigmaVersionString()` reports it — on OE-Alliance images a build date such as `2024-09-11-Release`, not a number |
| `plugin` | string | This plugin's version — what an `update` entity compares against |
| `boxtype` | string | Machine name, lowercase |
| `mac` | string | Lowercase, colon-separated; the Wake-on-LAN target |
| `ip` | string | Current LAN address |
| `uptime` | int | Seconds since boot |
| `ha_mode` | string | `discovery` \| `integration` \| `off` — the acknowledgement of `cmd/ha_mode` |
| `settings` | object | The complete non-secret settings a consumer may **read**. Writable through `cmd/config`: `publish_keys` (bool), `screenshot` (`off` \| `on_zap` \| `interval`), `screenshot_interval` (integer seconds, 5–3600), `screenshot_delay` (post-zap settling seconds, 1–30), `cam_telemetry` (bool, off by default), `oscam_telemetry` (bool, off by default), `softcam_autoheal` (bool, off by default), `softcam_autoheal_seconds` (integer seconds, 30–600). **Read-only**: `deep_standby_allowed`, `softcam_restart_allowed` and `epg_import_allowed` (bools, off by default). See below. |
| `capabilities` | list of strings | Which hooks this image actually gave the plugin |

**Presence in `settings` is not permission to write it back.** Until 0.2.0 this object was „the
complete remotely writable subset" and the two things were the same; they are not any more. The
**writable** members are the `cmd/config` allowlist and nothing else, and a consumer that writes
back everything it reads is refused — an unknown key fails the whole object, so it loses the
settings it did mean to change as well.

| Read-only member | Since | Meaning | Where it is set |
|---|---|---|---|
| `deep_standby_allowed` | 0.2.0 | Whether `cmd/deep_standby` and `cmd/reboot` are permitted over MQTT. Always present, whichever way it is set | On the receiver: the setup screen, *Menu → Plugins → MQTT Bridge*, the provisioning file at first install, or the plugin's OpenWebif page — never over MQTT |
| `softcam_restart_allowed` | 0.3.0 | Whether `cmd/softcam_restart` is permitted over MQTT, and with it the opt-in auto-heal. Always present, whichever way it is set | The same places, and never over MQTT |
| `epg_import_allowed` | 0.3.0 | Whether `cmd/epg_import` is permitted over MQTT. Always present, whichever way it is set | The same places, and never over MQTT |

The rule behind which side of the line a setting falls on: one that **enables a command** is never
writable over MQTT — it is set on the receiver (the setup screen, the provisioning file, or the
OpenWebif page, which is exactly as open as the receiver's web interface; see
[ADR-0009](adr/0009-the-openwebif-page-trusts-openwebif.md)); one that **tunes a command already
permitted** may be remote. A member of this table exists so a consumer can **hide what the box will
refuse** rather than offering a control that always fails. `info` is republished when it changes,
because saving the setup screen or the OpenWebif page reconnects the bridge and a connect publishes
the snapshot.

A command run from the OpenWebif page does not need these permissions: the page runs the same
handler with the origin `page`, and anybody OpenWebif admits could grant the permission through
OpenWebif's own `saveconfig` anyway. Everything else about the command is unchanged — the
household-safety guards apply, and `last_error` is published or cleared exactly as for a command
over MQTT.

**`capabilities` is the honest part of the contract.** Hook names differ between images, so the
plugin detects what it managed to attach and names it here rather than assuming. A consumer
hides what is missing instead of showing a dead entity. The names are the feature areas, and
nothing else is ever in the list: `power`, `cec_workaround`, `service`, `epg`, `epg_grid`,
`tuner`, `recording`, `timers`, `volume`, `cam`, `oscam`, `softcam`, `keys`, `screenshot`,
`toast`, `message`, `hdd`, `process`, `channels`, `bouquet_context`, `epg_import`. A build that has bound no
feature area publishes `[]` — the connection, `info` and the commands are the plugin itself and
are not capabilities.

A name is in the list because it **worked on this box**, not because this version of the plugin
has the code for it. Three things can take one out: the image did not provide the hooks
(`volume` on a box with no `VolumeControl`), the feature is switched off in the settings
(`keys` with `publish_keys` off, `screenshot` set to `off`, `epg_grid` with `epg_grid_events` at
`0`, `cec_workaround` with `cec_standby_workaround` off, `toast` with `osd_toast` off), or the hook raised while it was being
attached — which is logged once, with the name that could not be bound.

**A capability can also arrive late.** Some hooks can only bind once enigma2 has built the screen
behind them, which on some images happens after the plugin has already connected. `bouquet_context`
is the one that does this today: it is claimed on the first successful read of the receiver's own
service list, not when the plugin starts, so a box that never offers one publishes neither the
capability nor the `bouquet` topic. When a capability appears after the connect, `info` and the
announcement are published again with it — a consumer that acts on `info` therefore has to accept
it more than once per connection, which it has to do anyway because `cmd/config` and `cmd/ha_mode`
both republish it.

### `<base>/<node>/power`

Retained. **Not JSON** — `on` or `standby`.

Deep standby is not a state here: the box is off and the broker shows `availability: offline`.

### `<base>/<node>/cec` — since 0.3.0

```json
{"last_intervention": 1789459200, "kind": "closed_channel_list", "count": 3, "pending": false}
```

Retained, and present only when `cec_workaround` is a capability: the setting
`cec_standby_workaround` (never writable over MQTT) is on (it is off by default), **and** the image has HDMI-CEC switched on
and is set to follow the television into standby (`config.hdmicec.enabled` and
`config.hdmicec.handle_tv_standby`, read when the plugin starts — with either off the image never
queues the television's standby), **and** it has its notification queue and its standby screen.
An image that builds its HDMI-CEC component with CEC switched off does not count as running it.
With the setting off nothing is bound and this
topic is retracted on every connect — so switching the workaround off takes its count off the
broker too. The setting itself is not in `info.settings`: it enables no command, and the
capability already says whether the workaround is at work.

| Field | Type | Meaning |
|---|---|---|
| `last_intervention` | integer or `null` | Unix epoch seconds, UTC, of the last intervention that was counted. For `closed_channel_list` it is when the list was closed; for `dropped_stale_standby`, when the standby was dropped. `null` until there is one |
| `kind` | `closed_channel_list` \| `dropped_stale_standby` \| `null` | What that intervention was |
| `count` | integer | Interventions since the plugin started — **at most one per television standby**. In memory, so not a durable total across a receiver restart |
| `pending` | bool | A standby **the television** asked for is queued and has not been carried out. This is what makes „the box is sitting there with a standby waiting" visible on a dashboard rather than only in a log |

Every key is always present; a value that does not exist yet is `null`, never omitted.

**What it works around** is an upstream enigma2 defect, not this plugin's. When the television
switches itself off it tells the receiver over HDMI-CEC, and the image *queues* a standby that only
the info bar carries out — so with the channel list open it waits until the list is closed, and
then, because the image has already forgotten the standby was the television's, it sends
`<Standby>` back to the television. The workaround:

1. **`closed_channel_list`** — when that standby is queued while the **channel list** is the screen
   in front, closes the list through its own exit (what EXIT does, including going back to the
   channel you were on), so the standby happens now. Only the channel list: the check is an
   allowlist of three class names — `ChannelSelection`, `ChannelSelectionRadio`,
   `PiPZapSelection` — matched against the screen's class and its bases, and **never** the service
   picker other dialogs embed, the channel list's context menu or bouquet selector, an EPG screen, a
   menu, the plugin browser, an input box, a message box or a recording dialog. Anything stacked on
   top of the list counts as „not the list", and the plugin never looks further down. It is
   **counted once the standby has actually happened** (about a second and a half later, when the
   receiver has entered standby), not when the list is closed: a close that released nothing —
   something else was queued ahead, or the screen underneath does not carry out the queue — is not
   counted as `closed_channel_list`.
2. **`dropped_stale_standby`** — when that standby is still queued **30 seconds** after the
   television asked for it, removes it from the queue, so it cannot fire later — when somebody
   closes the menu they were in — and take the television with it. A standby the list was closed
   for and which still did not happen is counted here, once, and not also as a close.

When the receiver has entered standby, any television standby still queued is removed too, so that
waking the receiver does not put it straight back to sleep:

- if one of the television's standbys did run, the rest are repeats — a television that said
  `<Standby>` twice — and removing them completes a request already carried out: **not counted**;
- if none of them ran — the receiver went to standby some other way, say the remote's power button,
  while the television's standby waited behind a popup or a menu — the television's standby was
  thrown away, so it is counted **once, as `dropped_stale_standby`**, exactly as the 30-second
  deadline would have counted it, and never as a close, whether or not the list was closed for it.

🔴 **A standby the household asked for is never touched.** `cmd/power standby` queues exactly the
same notification as the television does. The workaround identifies the television's at the moment
it is queued, by the image's own marker, and keeps that one entry by identity; nothing else in the
queue is ever closed on or removed. (The remote's power button does not queue anything — it opens
the standby screen directly — so it never reaches the workaround.)

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
| `sref` | string | The service reference, trailing colon included. The stable identifier — `name` is for people |
| `name` | string \| null | Channel name as the bouquet spells it |
| `bouquet` | string \| null | The first of the **configured** bouquets (`bouquets_for_select`) that contains the service — `null` when it is in none of them, which is not the same as „no bouquet" |
| `provider` | string \| null | |
| `width`, `height` | int \| null | Video resolution; `null` before the first frame is decoded |

Published on `evStart`, `evTunedIn`, `evUpdatedInfo`, `evNewProgramInfo` and `evEnd`, so it
settles within a second of a zap and is published again when the resolution becomes known — a box
answers `-1` for both until the first frame is decoded, and `-1` is not a resolution.

With nothing playing every field is `null`; the keys are still all there.

Two services are **the same service** when the first eleven colon-separated fields match. That is
what makes `1:0:19:283D:3FB:1:C00000:0:0:0:` and `1:0:19:283D:3FB:1:C00000:0:0:0::TVP 1 HD` one
channel rather than two — and it is why the eleventh field is included rather than the tenth: two
IPTV services differ only in the stream URL that sits in it.

### `<base>/<node>/epg`

```json
{
  "now":  {"title": "Wiadomości", "begin": 1789459200, "end": 1789460700,
           "event_id": 27431, "short": "Serwis informacyjny", "long": "…"},
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

### `<base>/<node>/channels` — since M2

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
names (or of their slugs, which is easier to type on a remote control — „Sport (HD)" and
`sport_hd` both select the same bouquet). Empty means every television bouquet, which is what a
box that has never been configured publishes.

**Only playable services.** A bouquet holds markers, separators and hidden entries alongside its
channels, and they arrive from the same call; they are dropped here, because a marker offered as a
channel is an option in a select box that cannot be tuned. A box with „multiple bouquets" switched
off has no bouquet list at all — its favourites list is published as a single bouquet.

**Picons are not published.** They are tens of kilobytes each and the box's own web interface
serves them on the same address.

Rebuilt when `bouquets.tv` or any `userbouquet.*` file changes — there is no event for that, so
their modification times are compared once a minute — and on `cmd/discovery`. The list on a real
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
capability stays, because the service list is being read successfully — what is missing is a
match, not a hook — and the topic goes back to naming a bouquet as soon as the receiver is in one
again.

### `<base>/<node>/epg_grid/<bouquet_slug>` — since M2

A compact grid, **one retained topic per configured bouquet**: the next few events on every
channel of that bouquet, enough to draw a „what's on" list without a single OpenWebif request.

The grid is per bouquet because a bouquet is what a household actually browses, and because one
combined topic would make every consumer re-read every bouquet whenever any one of them moved on.
A consumer that cares about a single bouquet subscribes to a single topic; one that wants them all
subscribes to `<base>/<node>/epg_grid/+`.

**`<bouquet_slug>`** is derived from the bouquet's name: lower-cased, transliterated to ASCII, and
every run of characters that is not a letter or a digit collapsed to one `_`, with leading and
trailing `_` trimmed. „Ulubione TV" becomes `ulubione_tv`; „Favourites (TV)" becomes
`favourites_tv`. The slug is only an addressable, MQTT-safe name — **the payload carries the
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
| `bouquet` | string | The bouquet's name as enigma2 spells it — not the slug |
| `generated` | int | Epoch seconds, when this bouquet's grid last **changed** — see below; it is not the time of the last build |
| `channels[].sref` | string | |
| `channels[].name` | string | |
| `channels[].events[]` | list | Up to `epg_grid_events` entries per channel, chronological |
| `events[].title` | string | |
| `events[].begin`, `events[].end` | int | Epoch seconds |
| `events[].event_id` | int | |

Refreshed when a bouquet changes, every 15 minutes, and on `cmd/epg_grid` — which regenerates and
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
the television the previous pass found — a bouquet carrying no EPG, an overnight window, an
import that has failed — and that bouquet would rewrite its retained topic four times an hour
for as long as the box stayed on, giving every consumer a state change and every recorder a row
for a payload saying what it already said. The consequence is the one worth knowing: the
retained `generated` moves when the programmes move. A consumer that wants to know the plugin is
still building grids should read `availability`, which is what it is for.

**Slugs that stop being configured are retracted.** The plugin remembers the slugs it has
published in its state file on the box, the same file that carries the discovery component list.
Drop a bouquet from `bouquets_for_select`, or rename one — which changes its slug — and the topic
it used to own gets an empty retained payload on the next start. Without that the broker would go
on serving the grid of a bouquet nobody has configured for as long as the broker lives, which is
the same trap retained discovery payloads set and is answered the same way.

A grid payload can reach tens of kilobytes *per bouquet*. It is **not** meant to become a state
attribute of an entity — a consumer that stores it per update will bloat its recorder database.
The companion integration exposes it through an action that returns a response instead.

Full EPG search, and browsing timers that the plugin did not create, stay on OpenWebif. This is
a grid, not a database.

### `<base>/<node>/epg_import` — since 0.3.0

```json
{"state": "done", "started": 1789459200, "finished": 1789459291, "events": 120074, "error": null}
```

Retained, and present when `epg_import` is a capability. That capability is claimed only where the
image's EPG-Importer is **already loaded** by enigma2's own plugin loader — the plugin looks it up
under `Plugins.Extensions.EPGImport.plugin` and never imports it itself, because a second import
would build a second importer and a second scheduler — where every name the plugin needs from it
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
running" is never an unexplained refusal. While one runs it asks every two seconds — the same test
at the same period the importer's own screen uses — and when the answer turns false it reads the
importer's result: events → `done`, and the EPG grid is rebuilt (each bouquet is **published only
if it changed**, so after most imports nothing on `epg_grid/*` moves); no events → `failed`, „…its
sources may be unreachable". At start-up the topic is `idle`, carrying `finished` and `events` from
the importer's own record of its last run, or `running` if one is under way.

Three things can go wrong, and only three can be told apart: the import did not start (or never
ran), it finished with no events, or it is still running after **30 minutes** — a normal run takes
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
  deep standby. It does so only when **all four** of its own conditions hold — its „shutdown"
  setting is on, its deep-standby setting is „wake up", its „deep standby after import" setting is
  on, and a timer woke the receiver — and then only if the receiver is in standby, nothing is
  recording and it is not already shutting down. The settings are all off by default. That is the
  importer, not this plugin.
- **A network recording mount can hold the press up.** Before its first download the importer reads
  `/proc/mounts` and asks the recording mount for its free space, on the main loop, to choose
  where to put the file. If that mount is a network share that has stopped answering, pressing the
  button can freeze the picture until the mount gives up — as the importer's scheduled run would.

A settings save on the receiver while an import runs restarts the bridge, and the new session
reports `started` as the moment it first saw the import.

**The power commands wait for an import, but not for ever.** Deep standby, reboot and the
interface restart are refused while an import runs, because a restart mid-import loses the run.
That refusal lapses when this plugin's own start failed **and left the importer saying it is
running**, or when the watchdog has fired for the current run: the importer marks itself running
before its first download, so a start that fails part-way can leave it saying „running" until its
next scheduled run — up to a day of refused reboots for an import that is not happening. A start
that failed before that point leaves nothing stuck and lapses nothing. The topic keeps reporting
what the importer says, and a second `cmd/epg_import` is still refused as already running. The
refusal returns once the importer has been seen idle, or with the next import this plugin starts.
One residual is accepted: if a stuck start is followed by the importer's next scheduled run
without the importer ever being seen idle in between, the lapse covers that run too — about a
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
does not come off a tuner — IPTV, a recording being played back — has every field `null`.

### `<base>/<node>/recording`

```json
{
  "active": [{"name": "Wiadomości", "sref": "1:0:19:283D:…:", "begin": 1789459200, "end": 1789460700}],
  "next":   {"name": "Pogoda", "sref": "1:0:19:283D:…:", "begin": 1789460700, "end": 1789461000}
}
```

`active` is a list, empty when nothing is recording. `next` is the soonest future recording timer
or `null`. This topic is what the deep-standby, reboot and GUI-restart guards read.

A timer that only tunes the box — enigma2 calls it `justplay` — is not a recording and appears in
neither field, though it is in `timers`. A disabled timer is in neither either.

### `<base>/<node>/timers`

A JSON **list** (not an object), one entry per timer:

```json
[{"name": "Wiadomości", "sref": "1:0:19:283D:…:", "begin": 1789459200,
  "end": 1789460700, "state": "waiting", "repeated": 0}]
```

| Field | Type | Notes |
|---|---|---|
| `name` | string | |
| `sref` | string | |
| `begin`, `end` | int | Epoch seconds |
| `state` | string | One of `waiting`, `prepared`, `running`, `ended` |
| `repeated` | int | enigma2's day bitmask; `0` for a one-off timer, `127` for daily |

In `begin` order. Published whenever enigma2 writes its timer file — which it does after every
change to the list, including one made with the remote control while somebody is sitting in front
of the television — and after every recording event. A burst of changes produces one payload, not
one per call.

### `<base>/<node>/volume`

```json
{"level": 35, "muted": false}
```

`level` is an integer 0–100, `muted` a boolean. Published on every volume change from any source
— the remote, OpenWebif, the plugin itself — and reconciled on a short timer, because enigma2 has
no single choke point every volume change passes through.

### `<base>/<node>/cam`

```json
{"system": "Nagra", "active": true, "encrypted": true, "ecm_ms": 123}
```

Opt-in with `cam_telemetry`; absent entirely by default. `encrypted` is Enigma's current-service
flag. `active` means only that a fresh, valid ECM result was observed after the latest known
service start; it is **not** a softcam-process or server-health check. `system` is limited to a fixed
list of generic conditional-access systems and `ecm_ms` to 0–600000. Unknown values are `null`.

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
completed within 90 seconds is expired on the next tick — so between 90 and 120 seconds in
practice, because the check rides the same 30-second timer — and counts become unknown with the
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

### `<base>/<node>/softcam` — since 0.3.0

```json
{"selected": "OSCam_00000-r000", "running_instances": 1,
 "last_restart": 1789459200, "last_restart_reason": "manual", "restarts_today": 2,
 "manager_check_on_start": true, "manager_timer_minutes": null}
```

Retained, and present when `softcam` is a capability — which it is only when the image starts the
cam through its own manager rather than through `/etc/init.d/softcam`, the autostart entry resolves
to an executable regular file directly under `/usr/softcams/`, and the plugin knows a start line for
that binary's family.

| Field | Type | Meaning |
|---|---|---|
| `selected` | string or `null` | The **basename of the binary** the image selected for autostart, for example `OSCam_00000-r000`. Not a family name, and never the protocol the cam speaks to its servers — a receiver can run an OSCam that talks `cccam` outward, and those are different things |
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
not optional — two binaries differing only after the fifteenth character truncate to the same
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
| `rss_kb` | integer or `null` | `VmRSS` — resident set, in kB |
| `hwm_kb` | integer or `null` | `VmHWM` — the high-water mark of that, in kB |
| `threads` | integer or `null` | `Threads` |
| `fds` | integer or `null` | Open file descriptors, including the one the count itself opens |
| `started` | integer or `null` | Unix epoch seconds, UTC, when the enigma2 process started |

All five keys are always present. A field that could not be read is `null` rather than missing,
because an absent key renders as an empty string in a Home Assistant template — „ignore this
message", which leaves the previous value on screen for ever — while an explicit `null` renders as
unknown.

Read from `/proc/self/status` (`VmRSS`, `VmHWM`, `Threads`), `/proc/self/fd`, and, for `started`,
field 22 of `/proc/self/stat` (clock ticks since boot, divided by `SC_CLK_TCK`) plus `btime` from
`/proc/stat`. `started` is deliberately not „now minus uptime": the topic is retained and a consumer
keeps the value, so it has to be the same number on every publish rather than one that drifts by a
second each time somebody reads it.

**Cadence.** In the snapshot on every connect, then every 300 seconds. In between, the resident set
is checked every 60 seconds and published early whenever it has moved by 4096 kB or more in either
direction since the last publish — so a jump lands on the curve at the minute it happened rather
than up to five minutes later.

The 300-second publish obeys the publish-on-change rule of §1 like every other state topic, so it is
a **ceiling on the gap, not a heartbeat**: a payload identical to the last one is not sent again, and
a receiver idle enough that none of the five numbers moved can be quiet for longer. The connect
snapshot is the deliberate exception and always goes out. Read `availability` to tell a quiet box
from an absent one.

The capability is `process`, and it is claimed only when `/proc/self/status` can actually be read.
There is no setting: the topic reveals nothing about what anybody is watching, so it is always on.

Unlike every other poll in the plugin this one runs on the main loop, because procfs is memory —
there is no disk behind it, no network and no lock another process holds. `hdd` is the opposite
case and runs on a thread of its own.

### `<base>/<node>/screen`

Retained, QoS 0. **Not JSON** — the raw bytes of a JPEG.

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

In standby nothing is captured unless `cmd/screenshot` asks for it — there is nothing on the
screen to photograph. On a reconnect the **last** picture is republished from memory rather than a
fresh one being taken: a reconnect loop that ran `grab` each time round would be a receiver on its
knees.

It is a picture of what is on the television, retained on the broker. Decide that deliberately.

The plugin's OpenWebif page shows the last payload this process published here, with the time the
capture finished, at `<mount>/screen.jpg`. That is a view of this topic, not a change to it: the
payload, the command and every guard above are unchanged.

### `<base>/<node>/key`

**Not retained** — QoS 0, fire and forget.

```json
{"key": "KEY_RED", "press": "short"}
```

`key` is the enigma2 key name; `press` is `short` or `long`. A key that is held produces one
`long` event, not a stream of repeats — the press is published when the button comes back up, as
`long` if the receiver reported the long-press marker while it was down.

The names are the Linux input event names every image uses (`KEY_OK`, `KEY_RED`, `KEY_CHANNELUP`),
taken from the receiver's own table where it has one, so a remote this plugin has never heard of
still publishes `KEY_PVR` rather than a number. A code with no name anywhere is published as
`KEY_<number>`.

At most **20 presses a second** reach the broker; beyond that they are dropped with one line in
the log.

Not retained on purpose: a retained key press would re-trigger every automation bound to it on
every reconnect. The plugin observes keys and **never consumes them** — the receiver behaves
exactly as it would with the plugin absent — and publishing is capped so that holding a button
cannot flood the broker. `publish_keys` turns the topic off.

### `<base>/<node>/last_error`

Retained.

```json
{"cmd": "zap", "error": "service name 'Sport' is not unique (4 matches)", "ts": 1789459213}
```

Published when a command fails or is refused by a guard, and **cleared** — an empty retained
payload — when the same command later succeeds. A consumer that raises an error to the user reads
this topic, not the absence of a state change.

---

## 2. Commands

`<base>/<node>/cmd/<name>`, QoS 1, never retained. Payloads are UTF-8; several commands accept a
bare string for convenience and a JSON object when there is more to say. Case is not significant
for the keyword payloads (`on`, `ON`, `On` all work).

Every command is **verified by effect**: the plugin does the thing, observes the resulting
enigma2 event, and publishes the new state on the matching state topic. A failure or a refusal
goes to `last_error`. There is no acknowledgement topic and no correlation id in v1 — the state
topic is the answer.

🔴 **A command that arrives with the retain flag set is logged and discarded, never executed.**
This is a guard on every command in the table below, not a property of any one of them. A retained
command is delivered again the instant the plugin subscribes, so the box would obey it on every
reconnect and after every reboot — a retained `deep_standby` is a receiver that will not stay on.
The plugin publishes no command retained and refuses to act on one that is, so a mistake made with
`mosquitto_pub -r` costs a log line rather than the evening. Clear it by publishing an empty
retained payload to that topic; until you do, the broker keeps handing it out.

| Command | Payload | Effect | Guard |
|---|---|---|---|
| `power` | `on` \| `standby` \| `toggle` | Leaves or enters standby | Idempotent: asking for the state the box is already in does nothing |
| `deep_standby` | any (`PRESS` by convention) | Shuts the box down completely | Refused while recording, or with a timer due within 10 minutes; refused while an EPG import runs (since 0.3.0, and see below for when that lapses); also refused unless `deep_standby_allowed` is on |
| `reboot` | any | Reboots the receiver | Same as `deep_standby` |
| `restart_gui` | any | Restarts enigma2 only | Refused while recording or with a timer due within 10 minutes, and while an EPG import runs (since 0.3.0): a restart mid-import loses the run. That refusal **lapses** once this plugin's own start of the import failed, or once the 30-minute watchdog has fired for the current run, and returns with the next import; the `epg_import` topic still says what the importer says |
| `zap` | `<sref>` \| `{"sref": "…"}` \| `{"name": "…"}` | Tunes to a service, waking the box from standby first | By name: refused unless exactly one service in the configured bouquets matches — the error names the count. A zap that does not show up on `service` within 5 s is reported there too |
| `bouquet` | `{"sref": "…"}` | Makes one published TV bouquet the active channel-list context | Exact allowlist match only. The current channel is preserved when it belongs to the bouquet; otherwise the first playable channel is tuned. Empty/marker-only bouquets and unavailable service-list APIs are refused without changing context |
| `volume` | `0`–`100`, or `{"level": 42}` | Sets the volume, with the on-screen bar | Out-of-range values are clamped and noted in the log |
| `mute` | `ON` \| `OFF` | Sets mute | Never a blind toggle: the state is read first, and read back afterwards. A receiver refuses to mute at volume 0, and that refusal is reported |
| `key` | `KEY_OK` \| `{"key": "KEY_OK", "long": true}` | Injects a remote key | Unknown key names are refused with the name in `last_error`; at most 20 a second |
| `message` | `{"text": "…", "type": "info", "timeout": 10}` — or a bare string; `"style": "toast"` since 0.3.0 | Shows an on-screen popup, or a discreet toast | `text` is required; since 0.3.0 **every backslash is removed** and nothing else, as for a toast, and what is left is truncated to 500 characters; `type` is `info`, `warning` or `error` (default `info`); `timeout` is seconds (default 10, `0` until dismissed). A new message replaces the previous one rather than queueing behind it. The toast's rules differ — see below |
| `timer` | see below | Adds or deletes a recording timer | An add that overlaps an existing timer, or refers to an unknown event, is refused — and so is one the receiver quietly dropped as a duplicate of a timer it already had |
| `record` | `start` \| `stop` | Starts or stops an instant recording of the current service | `start` records the current service for two hours; `stop` with nothing recording is a no-op with a note in `last_error` |
| `screenshot` | any | Captures `screen` now, in standby as well | Rate-limited to one per five seconds |
| `softcam_restart` — since 0.3.0 | any (`PRESS` by convention) | Stops **every** running instance of the cam the image selected, then starts exactly one, with the image's own command line | Refused unless `softcam_restart_allowed` is on; refused by the same recording guard as `deep_standby` — while recording, with a timer due within 10 minutes, and when the image will not say; refused for the first 60 seconds after the plugin starts, because the image's own check runs a moment after that and restarting inside that window races a copy already on its way; at most one manual restart a minute; refused while one is already running. 🔴 **The binary is resolved on the box and no part of the command line comes from the payload** |
| `epg_grid` | any | Rebuilds and republishes **every** `epg_grid/<bouquet_slug>` topic | Refused with a note in `last_error` when `epg_grid_events` is `0` |
| `epg_import` — since 0.3.0 | any (`PRESS` by convention) | Starts the image's EPG importer, the way its own „Manual" button does, and reports on `epg_import`; the grid is rebuilt afterwards and each bouquet is published only if it changed | Refused, in this order: unless `epg_import_allowed` is on; when the importer is not available (no capability); while an import is already running, whoever started it; by the same recording guard as `deep_standby`; within 10 minutes of the importer's own scheduled run, which the image starts without checking whether one is running, and when that time cannot be read; when the importer has no sources selected. A start that fails is `failed` on `epg_import` **and** the same sentence on `last_error`. 🔴 **The payload is ignored**: nothing from the broker reaches the importer |
| `config` | `{"publish_keys": false, "screenshot": "interval", "screenshot_interval": 90, "screenshot_delay": 4, "cam_telemetry": false, "oscam_telemetry": false, "softcam_autoheal": false, "softcam_autoheal_seconds": 90}` | Atomically replaces the remotely writable settings subset and publishes fresh `info`/discovery | The original three keys remain required; newer keys are independently optional for older clients and preserve their current values when omitted. Unknown keys and coercion are refused, and a persistence failure applies none of them |
| `discovery` | any | Republishes the announcement, the channel list, and the discovery payloads in discovery mode | — |
| `ha_mode` | `discovery` \| `integration` \| `off` | Switches the Home Assistant mode | See below |
| `reset` | any | Retracts every retained topic this node owns, then republishes | See below |

Every one of them is refused when it arrives retained, as above.

### `cmd/message` styles — `toast` since 0.3.0, capability `toast`

```json
{"text": "…", "style": "toast", "timeout": 5}
```

`style` is optional. **Absent or `null` is the popup, as before 0.3.0** — with one change to its
text, below — and so is a payload that is not a JSON object. The style is decided before any
default is applied.

| Field | Type | Popup | Toast |
|---|---|---|---|
| `text` | string | required; empty refused; **every backslash is removed** and nothing else (since 0.3.0, the toast's rule), then truncated at **500** | required; empty refused; **every backslash is removed** and nothing else, then truncated at **200** |

**One text rule for both styles.** `\cFFFF0000Alarm` shows as `cFFFF0000Alarm`, `C:\config.txt` as
`C:config.txt`, a literal `\n` as `n`; a real newline character is not a backslash and stays a line
break. The cap counts the characters left after the removal, and a text with nothing but backslashes
and blanks is refused as empty („message text is empty"). enigma2's text renderer reads a backslash
and what follows it as a colour change or a line break, and it does so after right-to-left
reordering, where no narrower rule over the string can find it. Until 0.3.0 the popup passed every
backslash through, so a sender that relied on `\n` for a line break in a popup has to send a real
newline instead.
| `style` | string, optional | absent, `null` or `"popup"` | `"toast"` (trimmed, case-insensitive). Any other value is refused: „unknown message style '…'; expected popup or toast" |
| `timeout` | integer seconds, optional | default **10**; `0` or less = until dismissed | default **5**; **`0` or less is refused** („a toast hides itself; timeout must be 1–30 seconds"); more than `30` becomes `30`, with a note in the log |
| `type` | `info` \| `warning` \| `error`, optional | chooses the box's icon | **validated exactly as for a popup** — an unknown value is refused, so a payload is valid or invalid whatever its style — and then **ignored** |

`timeout` is read as the popup reads it, with `int()`: `"5"` is 5, `5.9` is 5 and `0.5` is 0, which a
toast refuses; `null` or a non-number is refused with „'…' is not a number of seconds".

A **toast** is a plugin-owned, non-modal screen in the **top right**, above the channel list, the
info bar and the popup. It has one fixed appearance — a dark, slightly transparent box and light
text — under a header that always reads „MQTT Bridge" in the receiver's language, whatever the
payload says. It auto-hides and does nothing else: it never becomes the current dialog, cannot be
dismissed by the remote, and never enters the image's notification queue, so it cannot wait behind
an open channel list the way a popup does. A newer toast replaces the one on screen and restarts
its timer; there is no queue. A toast and a popup are independent: neither removes the other.

🔴 **It cannot take a key press because of what it is made of, not only because it binds no action
map.** A never-executed screen binds no action map, but some of enigma2's widgets bind keys natively
in their own constructors — every list does — with no action map and no exec. The toast holds two
text labels and nothing else, so nothing in it can bind a key, and a test enforces that.

It is **hidden when the receiver enters standby**, and a toast that arrives while the receiver is in
standby, or while the receiver's own „really shut down / restart?" question is on screen, is
**refused** on `last_error` with „the receiver is in standby" rather than kept for later. That
question appears only when there is a reason to ask — a recording, a running job, timeshift, a
stream — and is gone before the receiver actually quits, so an ordinary shutdown or interface restart
is not covered by this refusal. It is deleted — not merely hidden — when the
plugin stops, so a toast on screen does not survive into the frame a restarting receiver leaves
behind. A toast requested while the capability is absent is refused with „the discreet toast is
switched off on this receiver" (`osd_toast` off) or „the discreet toast could not be created on this
receiver".

The capability `toast` is claimed **only once the screen has actually been created**, and is taken
back — with `info` republished — if a skin reload cannot create it again. The setting `osd_toast`
(default on, never writable over MQTT) is its kill-switch. An image where either fails keeps popups rather than
gaining a style that silently does nothing.

### `cmd/timer` payload forms

```json
{"action": "add", "sref": "1:0:19:…:", "event_id": 27431}
{"action": "add", "sref": "1:0:19:…:", "begin": 1789459200, "end": 1789460700, "name": "Wiadomości"}
{"action": "delete", "sref": "1:0:19:…:", "begin": 1789459200, "end": 1789460700}
```

The first form is the one to prefer: enigma2 resolves the event itself, so the timer inherits the
programme's padding and its name. The second is for a manual window. Deletion matches on the
triple `sref` + `begin` + `end`, which is what enigma2 itself uses as a timer's identity.

🔴 **The `begin` and `end` of a timer are not the `begin` and `end` of the programme.** A receiver
applies its own recording margins — typically a few minutes before and after — so a timer added
from an `event_id` appears in `timers` with wider times than the `epg` topic showed. Delete it
with the times `timers` reports, not the ones the programme had.

Either way, `timers` (and `recording` when it is imminent) is republished afterwards.

### `cmd/config` semantics

This is deliberately not a general settings API. Broker credentials, TLS, identity, topic names,
the configured bouquet filter, logging and destructive-command permission cannot be changed
through `cmd/config`. `deep_standby_allowed` and `softcam_restart_allowed` are **read** from `info.settings` (§1) and are
refused here like any other key outside the allowlist; they are granted on the receiver — its setup
screen, the provisioning file or the OpenWebif page — and never over MQTT, and reading a setting and
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
flow is built on it — which means **any client with publish rights on `<base>/<node>/cmd/config`
can turn on `screenshot`, `publish_keys`, `cam_telemetry` and `oscam_telemetry`**, and with
screenshots on it can ask for a picture of the television at any time with `cmd/screenshot`. There
is no second, box-local confirmation for this, by design: a broker login that can publish there is
a login that can watch the living room. Give the receiver its own broker credential, restrict it
with an ACL, and treat `cmd/#` on this node as sensitive — the ACL recipe is in the project's
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

1. **Retract.** An empty retained payload to every retained topic this node owns — all the state
   topics, every `epg_grid/<bouquet_slug>`, the announcement, and every Home Assistant discovery
   payload named in the state file — and then the plugin forgets what it had announced.
2. **Republish, immediately.** `availability: online`, the full state snapshot, the announcement
   and, in `discovery` mode, the discovery payloads — **the same sequence as `on_connect`**, run
   straight away rather than waited for. The state file is written again with what was just
   published.

That second half is why a reset is **safe to run at any time**: the broker is empty of this node's
topics for the width of one publish burst, not until the box next reconnects, and a subscriber
that was listening throughout ends up exactly where it started. Home Assistant sees the entities
go unavailable and come back, which is the same thing it sees when the box reboots.

It is also the documented step **before uninstalling**, because retained topics outlive the plugin
that created them: remove the package without it and the broker keeps serving a snapshot of a box
that is gone, forever, while Home Assistant keeps showing entities nothing will ever update. Do it
while the plugin is still running and connected — after `opkg remove` there is nothing left to ask.

It is a cleanup, not a factory reset: settings are untouched.

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
  "plugin": "0.2.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "capabilities": ["power", "service", "epg", "tuner", "softcam", "recording", "timers",
                   "volume", "hdd", "process", "channels", "bouquet_context", "epg_grid", "keys",
                   "screenshot", "toast", "message"],
  "ha_mode": "discovery"
}
```

`name` is the `friendly_name` setting — the device name a user sees. `base_topic` is repeated
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
| `homeassistant/device_automation/<node>/<colour>_<press>/config` | Eight payloads — `red`, `green`, `yellow`, `blue` × `short`, `long` — so the colour keys are device triggers in the automation editor |

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
| `volume` | number | `volume` | 0–100, slider |
| `mute` | switch | `volume` | |
| `channel_select` | select | `service` | Options are the channel names of the configured bouquets; selecting one publishes `cmd/zap` |
| `screen` | image | `screen` | `image/jpeg` |
| `screenshot`, `restart_gui`, `refresh_discovery` | button | — | And `deep_standby` and `reboot` **only** when `deep_standby_allowed` is on |
| `snr`, `agc`, `ber` | sensor | `tuner` | Diagnostic, disabled by default |
| `recording_disk` | binary_sensor | `hdd` | Diagnostic |
| `softcam` | sensor | `softcam` | Since 0.3.0. Diagnostic. State is `selected`; the counts and the last restart are JSON attributes |
| `softcam_restart` | button | — | Since 0.3.0, and only when `softcam` is a capability **and** `softcam_restart_allowed` is on |
| `epg_import` | sensor | `epg_import` | Since 0.3.0. Diagnostic. State is `state`; `started`, `finished`, `events` and `error` are JSON attributes |
| `epg_import_start` | button | — | Since 0.3.0, and only when `epg_import` is a capability **and** `epg_import_allowed` is on. Publishes `cmd/epg_import` |
| `process_memory` | sensor | `process` | Diagnostic, MiB, `data_size`, measurement — the one of these five that is **enabled** by default |
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
  Device triggers have no availability — their schema has no room for it.
- **The entity id each component asks for** is `default_entity_id`, not `object_id`. Home
  Assistant 2026.9 has no `object_id` in its MQTT schema; a payload carrying one is accepted and
  the key is silently discarded. It is also read **once**, when the entity is first created:
  changing it later renames nothing.
- **A component is removed by name**, by republishing the device payload with that component cut
  down to nothing but its platform. Leaving it out of the payload does not remove it — Home
  Assistant keeps what it last saw. This is what happens when a capability disappears: switch
  screenshots off and the image entity goes with them.

### What discovery deliberately leaves out

`cam`, `oscam` and `bouquet` have **no discovery components at all**, whatever their capabilities
say. All three are consumed by the companion integration, which subscribes to the topics directly:
the first two would need a whole set of per-reader entities built from a list that changes shape,
and the third is a selector whose options are the channel list, not a state anybody wants as a
sensor. A plugin-only install reads them from the broker as they are documented above.

### The attributes the sensors carry

Two of those components carry JSON attributes rather than only a state, because the state is a
name and the useful identifier is not. They are part of this contract — SETUP.md's `universal`
media_player recipe reads `sensor.<box>_channel|sref`, and that only works because the attribute
is promised here.

| Component | State | JSON attributes |
|---|---|---|
| Channel sensor | the channel name | `sref`, `bouquet`, `provider`, `width`, `height` — the `service` payload minus the name |
| Programme sensor | the title of `now` | `begin`, `end`, `event_id`, `short`, `long`, and `next_title`, `next_begin`, `next_end` for the following programme |

The types are the ones the `service` and `epg` topics define; an attribute whose source field is
`null` is published as `null`, not dropped. `next_*` is flattened rather than nested because Home
Assistant templates read a flat attribute far more comfortably than a nested object.

### The device triggers

Eight retained payloads, one per topic, each with `automation_type: trigger`, the `key` topic, and
a `value_template` of `{{ value_json.key }}_{{ value_json.press }}` matched against a payload of
`KEY_RED_short` and its seven siblings. The `type` is `button_short_press` or `button_long_press`
and the `subtype` is the colour, which is how they are labelled in the automation editor.

They are published only when `keys` is a capability — with `publish_keys` off there is no `key`
topic for them to watch, and a trigger that can never fire is worse than none.

### The state file

The plugin keeps `/etc/enigma2/mqttbridge-state.json` — beside enigma2's own settings, or beside
the plugin itself when `/etc/enigma2` will not take a write — and it records **every topic this
node has published retained**: the state topics, every `epg_grid/<bouquet_slug>`, the
announcement, and every Home Assistant discovery payload. Beside that list it keeps two indexes
that a topic name alone cannot answer: the **slugs** of the EPG grids it has published, so a
bouquet that is renamed or dropped can be retracted, and the **components** it last announced with
their platforms, so one that is no longer announced can be removed by name from a payload that
otherwise contains only the survivors. It is written atomically, through a
temporary file in the same directory and a rename, so an interrupted write leaves the previous
list rather than half of a new one; it survives reboots and plugin upgrades, and `cmd/reset`
empties it and then fills it again with exactly what the reset republished. Nothing secret is in
it — it is a list of topic names.

That file is the whole answer to MQTT's oldest trap: a retained payload outlives the configuration
that created it. Rename a component, drop a feature, drop or rename a bouquet, change the node id,
and without the list the old retained topic stays on the broker and Home Assistant keeps an entity
that nothing will ever update again. So on every connect the plugin compares the list with what it
is about to publish and **retracts the difference first** — a rename made while the box was
switched off is caught the moment it comes back. It is also why `cmd/reset` exists.

Delete the file and the plugin still works, but every topic published before that point becomes a
retained ghost nobody can find: the list is the only record that they exist.

---

## 5. Planned (not implemented yet)

🔴 **Nothing in this section exists on any release, and nothing in it is on `main`.** It is here because the contract
keeps one home: a consumer can be written against these shapes, and they will not move quietly
between now and the release that carries them. Each is decided in
[ADR-0003](adr/0003-control-feedback-and-household-features.md) — or, for the remote uninstall, in
[ADR-0004](adr/0004-remote-uninstall.md); the release that carries it is in the heading. Until a capability below is in `info.capabilities`, the box does not have it — that
rule is unchanged, and it is how a consumer tells a plan from a feature.

### `info.settings` gains further members — 0.3.0

The object already carries read-only members and says so in §1: `deep_standby_allowed`,
`softcam_restart_allowed` and `epg_import_allowed` are on `main` and are documented there, which is
why they are not in this section. These join them on the same terms, rather than inventing a separate
`info.permissions`.

| Member | Release | Writable | Meaning |
|---|---|---|---|
| `uninstall_allowed` | 0.3.0 | **no** | Whether `cmd/uninstall` is permitted ([ADR-0004](adr/0004-remote-uninstall.md)). Default off, and granted on the receiver, never over MQTT |

Which side of the line each one falls on is §1's rule: a setting that **enables a command** is
never writable over MQTT; a setting that **tunes a command already permitted** may be remote.

### `info` gains `wol` — 0.3.0

```json
{"supported": true, "armed": false, "iface": "eth0"}
```

Read back from the system rather than assumed from the fact that a command was issued. A box that
reports Wake-on-LAN supported and **not armed** cannot be woken from deep standby, which is worth
knowing before enabling `deep_standby_allowed`. The `wol_arm` setting (never writable over MQTT, default off) arms
the interface at start and again immediately before deep standby.

### New commands — 0.3.0

| Command | Payload | Effect | Guard |
|---|---|---|---|
| `uninstall` | the node id (confirmation) | Removes the plugin from the receiver: retracts every retained topic it owns, publishes a final `offline`, removes the package, and restarts the interface | Refused unless `uninstall_allowed` is on; refused unless the payload matches this node's id; refused while recording or with a timer due, like every other command that restarts the interface |

The payload is the node id because a household with two boxes has two nearly identical command
topics, and the difference is one word in a path. It is a confirmation, not a secret — the node id
is in every topic name this plugin publishes to. The permission is the security boundary. The
capability `uninstall` is claimed only where the plugin can resolve its own installed package, so
a copy that was unpacked by hand or carried in a firmware image offers no such command.

🔴 **`cmd/uninstall` is a one-way door.** When it has run there is no plugin left to listen, so
nothing over MQTT can undo it — the box comes back only through SSH or the receiver's own package
manager. That is why its permission is off by default, is granted on the receiver and never over MQTT,
and is read-only in `info.settings` like the other two. The decision, and the order the steps run
in, are in [ADR-0004](adr/0004-remote-uninstall.md). `/etc/enigma2/settings` is not touched: a
reinstall finds its configuration where it left it.

---

## 6. Worth knowing

- **A retained state topic is a snapshot, not a heartbeat.** `service` showing a channel means
  that is the last channel the box tuned — check `availability` before believing it is on.
- **An ACL-denied publish looks exactly like a successful one.** Mosquitto drops it silently.
  When the topics are missing and the log shows no error, subscribe as a privileged user and
  publish as the box's user to find out which it is.
- **Nothing here is a request/response protocol.** If a command seems not to have worked, read
  its state topic and then `last_error`; there is no per-command reply to wait for.
