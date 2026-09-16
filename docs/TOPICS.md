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
  "enigma": "5.4",
  "plugin": "0.1.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "uptime": 384210,
  "ha_mode": "discovery",
  "capabilities": ["power", "service", "epg", "tuner", "recording", "timers",
                   "volume", "keys", "screenshot", "message", "hdd", "epg_grid"]
}
```

| Field | Type | Meaning |
|---|---|---|
| `image` | string | Image name and version as the box reports it |
| `enigma` | string | enigma2 / OE flavour version |
| `plugin` | string | This plugin's version — what an `update` entity compares against |
| `boxtype` | string | Machine name, lowercase |
| `mac` | string | Lowercase, colon-separated; the Wake-on-LAN target |
| `ip` | string | Current LAN address |
| `uptime` | int | Seconds since boot |
| `ha_mode` | string | `discovery` \| `integration` \| `off` — the acknowledgement of `cmd/ha_mode` |
| `capabilities` | list of strings | Which hooks this image actually gave the plugin |

**`capabilities` is the honest part of the contract.** Hook names differ between images, so the
plugin detects what it managed to attach and names it here rather than assuming. A consumer
hides what is missing instead of showing a dead entity. The names are the feature areas:
`power`, `service`, `epg`, `epg_grid`, `tuner`, `recording`, `timers`, `volume`, `keys`,
`screenshot`, `message`, `hdd`.

### `<base>/<node>/power`

Retained. **Not JSON** — `on` or `standby`.

Deep standby is not a state here: the box is off and the broker shows `availability: offline`.

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
| `name` | string | Channel name as the bouquet spells it |
| `bouquet` | string \| null | The bouquet the service was tuned from |
| `provider` | string \| null | |
| `width`, `height` | int \| null | Video resolution; `null` before the first frame is decoded |

Published on `evStart`, `evTunedIn` and `evNewProgramInfo`, so it settles within a second of a
zap, and again when the resolution becomes known.

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
| `generated` | int | Epoch seconds, when this bouquet's grid was built |
| `channels[].sref` | string | |
| `channels[].name` | string | |
| `channels[].events[]` | list | Up to `epg_grid_events` entries per channel, chronological |
| `events[].title` | string | |
| `events[].begin`, `events[].end` | int | Epoch seconds |
| `events[].event_id` | int | |

Refreshed when a bouquet changes, every 15 minutes, and on `cmd/epg_grid` — which regenerates and
republishes **every** configured bouquet, not just one. `epg_grid_events` is a setting, default
**4**; `0` turns the topics off entirely and drops `epg_grid` from `capabilities`.

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

### `<base>/<node>/tuner`

```json
{"snr": 78, "agc": 62, "ber": 0, "tuner": "A"}
```

`snr` and `agc` are integer percentages, `ber` an integer error count, `tuner` the letter of the
frontend in use (`null` when nothing is tuned).

### `<base>/<node>/recording`

```json
{
  "active": [{"name": "Wiadomości", "sref": "1:0:19:283D:…:", "begin": 1789459200, "end": 1789460700}],
  "next":   {"name": "Pogoda", "sref": "1:0:19:283D:…:", "begin": 1789460700, "end": 1789461000}
}
```

`active` is a list, empty when nothing is recording. `next` is the soonest future recording timer
or `null`. This topic is what the deep-standby, reboot and GUI-restart guards read.

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
| `repeated` | int | enigma2's day bitmask; `0` for a one-off timer |

### `<base>/<node>/volume`

```json
{"level": 35, "muted": false}
```

`level` is an integer 0–100, `muted` a boolean. Published on every volume change from any source
— the remote, OpenWebif, the plugin itself — and reconciled on a short timer, because enigma2 has
no single choke point every volume change passes through.

### `<base>/<node>/hdd`

```json
{"mounted": true, "path": "/media/hdd", "free_mb": 412330}
```

`mounted` boolean, `path` string, `free_mb` integer megabytes or `null` when nothing is mounted.
Checked on a slow timer; a recording disk that silently unmounts is the point of this topic.

### `<base>/<node>/screen`

Retained, QoS 0. **Not JSON** — the raw bytes of a JPEG.

Produced by the image's `grab` utility, downscaled. It is debounced and rate-limited to at most
one capture per five seconds regardless of how many events ask for one, and the `screenshot`
setting chooses between `off`, on every zap, and a fixed interval.

It is a picture of what is on the television, retained on the broker. Decide that deliberately.

### `<base>/<node>/key`

**Not retained** — QoS 0, fire and forget.

```json
{"key": "KEY_RED", "press": "short"}
```

`key` is the enigma2 key name; `press` is `short` or `long`. A key that is held produces one
`long` event, not a stream of repeats.

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
| `power` | `on` \| `standby` \| `toggle` | Leaves or enters standby | — |
| `deep_standby` | any (`PRESS` by convention) | Shuts the box down completely | Refused while recording, or with a timer due within 10 minutes; also refused unless `deep_standby_allowed` is on |
| `reboot` | any | Reboots the receiver | Same as `deep_standby` |
| `restart_gui` | any | Restarts enigma2 only | Refused while recording or with a timer due within 10 minutes |
| `zap` | `<sref>` \| `{"sref": "…"}` \| `{"name": "…"}` | Tunes to a service | By name: refused unless exactly one service in the configured bouquets matches — the error names the count |
| `volume` | `0`–`100` | Sets the volume | Out-of-range values are clamped and noted in the log |
| `mute` | `ON` \| `OFF` | Sets mute | — |
| `key` | `KEY_OK` \| `{"key": "KEY_OK", "long": true}` | Injects a remote key | Unknown key names are refused with the name in `last_error` |
| `message` | `{"text": "…", "type": "info", "timeout": 10}` | Shows an on-screen popup | `text` is required and truncated to 500 characters; `type` is `info`, `warning` or `error` (default `info`); `timeout` is seconds (default 10) |
| `timer` | see below | Adds or deletes a recording timer | An add that overlaps an existing timer, or refers to an unknown event, is refused |
| `record` | `start` \| `stop` | Starts or stops an instant recording of the current service | `stop` with nothing recording is a no-op with a note in `last_error` |
| `screenshot` | any | Captures `screen` now | Rate-limited to one per five seconds |
| `epg_grid` | any | Rebuilds and republishes **every** `epg_grid/<bouquet_slug>` topic | Ignored when `epg_grid_events` is `0` |
| `discovery` | any | Republishes the announcement, and the discovery payloads in discovery mode | — |
| `ha_mode` | `discovery` \| `integration` \| `off` | Switches the Home Assistant mode | See below |
| `reset` | any | Retracts every retained topic this node owns, then republishes | See below |

Every one of them is refused when it arrives retained, as above.

### `cmd/timer` payload forms

```json
{"action": "add", "sref": "1:0:19:…:", "event_id": 27431}
{"action": "add", "sref": "1:0:19:…:", "begin": 1789459200, "end": 1789460700, "name": "Wiadomości"}
{"action": "delete", "sref": "1:0:19:…:", "begin": 1789459200, "end": 1789460700}
```

The first form is the one to prefer: enigma2 resolves the event itself, so the timer inherits the
programme's padding and its name. The second is for a manual window. Deletion matches on the
triple `sref` + `begin` + `end`, which is what enigma2 itself uses as a timer's identity.

Either way, `timers` (and `recording` when it is imminent) is republished afterwards.

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
   payload named in `components.json` — and then the plugin forgets what it had announced.
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
  "name": "Dekoder salon",
  "base_topic": "enigma2",
  "image": "OpenViX 6.6.007",
  "enigma": "5.4",
  "plugin": "0.1.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "capabilities": ["power", "service", "epg", "tuner", "recording", "timers",
                   "volume", "keys", "screenshot", "message", "hdd", "epg_grid"],
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
| `homeassistant/device_automation/<node>/<key>_<press>/config` | Eight payloads — `red`, `green`, `yellow`, `blue` × `short`, `long` — so the colour keys are device triggers in the automation editor |

Device-based discovery (one payload, many components) is used rather than a payload per entity:
it is a single retained topic to retract, and the device identity cannot drift between
components.

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

### `components.json`

The plugin keeps `components.json` beside its configuration on the box, listing what it last
announced — the discovery components and the `epg_grid` bouquet slugs it published. On start-up it
compares that list with what it is about to announce and **retracts the difference first**.

That file is the whole answer to MQTT's oldest trap: a retained payload outlives the configuration
that created it. Rename a component, drop a feature, drop or rename a bouquet, change the node id,
and without the list the old retained topic stays on the broker and Home Assistant keeps an entity
that nothing will ever update again. It is also why `cmd/reset` exists.

---

## 5. Worth knowing

- **A retained state topic is a snapshot, not a heartbeat.** `service` showing a channel means
  that is the last channel the box tuned — check `availability` before believing it is on.
- **An ACL-denied publish looks exactly like a successful one.** Mosquitto drops it silently.
  When the topics are missing and the log shows no error, subscribe as a privileged user and
  publish as the box's user to find out which it is.
- **Nothing here is a request/response protocol.** If a command seems not to have worked, read
  its state topic and then `last_error`; there is no per-command reply to wait for.
