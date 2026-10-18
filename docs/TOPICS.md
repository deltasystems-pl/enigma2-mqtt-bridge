# The topic contract

This is the interface between the plugin and everything that consumes it - the companion Home
Assistant integration, an openHAB binding, a Node-RED flow, a shell script with `mosquitto_sub`.
It is versioned with the plugin: a change to a payload is a change to this file, to
[`contract.json`](contract.json) - the same contract as data, which CI holds to this file - to the
compatibility table in both READMEs, and to the changelog. Which changes a release may make
without a new contract major is in [Contract version](#contract-version).

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
| Packet size | No state topic, announcement or discovery payload is published in a packet over **1,000,000 bytes** - the fixed header, the topic and the payload together. (The retractions and the few fixed messages the plugin sends outside that path are small by construction.) That is under Mosquitto 2.1's default `max_packet_size` of 2,000,000 and under the 1 MiB default of other common brokers; a broker closes the connection on a bigger packet, and MQTT 3.1.1 gives a client no way to learn its limit. A payload that would need more is **not published**, and its topic is named in `info.not_published` (§1). A topic under `<base>/<node>/` is also retracted when an earlier, smaller payload was retained there; a topic outside the node's tree - a discovery payload - is left as it was. An event that is not retained and would be too big is dropped, with a line in the log. Three payloads grow with the channel list. Two of them give something up before it comes to that: an `epg_grid/<bouquet_slug>` is cut to fewer events per channel, and in discovery mode the device's discovery payload leaves the channel select out (§1, §4). `channels` has nothing to give up - it is every bouquet with its list - so on a receiver whose bouquets do not fit one packet together it is not published, and the lists are read from `channels/<bouquet_slug>`, one topic for each bouquet, which `bouquets` indexes (§1). Beyond that, what is not published in practice is the list or the grid of a single bouquet that is too big on its own. A folder of the recording library, `recordings/<folder_slug>`, gives something up as well: its oldest recordings are left out until it fits, and the index `recordings` says how many (§1). Added after 0.4.0 (unreleased). |

Everything the plugin publishes it also publishes again on every `on_connect` - the full state
snapshot, the announcement and, in discovery mode, the discovery payloads. A broker that lost its
retained store, or a box that reconnected after an outage, converges without anybody asking.

---

## Contract version

The contract has a major number, and the current contract major is **1**.

| Contract | Plugin releases |
|---|---|
| 0 | 0.1.0 |
| 1 | 0.2.0, 0.3.0, 0.4.0, and every later release until a release declares contract 2 |

A plugin publishes its major as `info.contract` from 0.4.0 on (§1,
[ADR-0015](adr/0015-signed-self-update.md)). A plugin that does not publish it is contract 0 when
`info.plugin` is below 0.2.0 and contract 1 when it is 0.2.0 or a 0.3.x.

**Inside one major a release may**

- add a topic, a payload member, an `info` member, a command, a capability name or a setting -
  writable or read-only;
- add a **value to an existing enumeration** - a `state`, a `kind`, a `reason` code, a setting's
  choice (below);
- add a **refusal** to an existing command - a new guard, answered with a sentence on `last_error`,
  which a consumer already handles for every command;
- tighten the validation of free text - text a person typed, such as a popup's.

**It may not** remove any of those, or retype one - a topic's payload kind or retain flag, a
member's type, a setting's type or which side of `cmd/config` it is on, a choice taken away from an
enumerated setting. That always needs a new major.

**A change of what an existing field or command means** needs a new major too - **unless it is a
named in-major exception** (below), which is the only way such a change stays inside a major.

**A fix is not a change of meaning** when it makes the plugin do what this file already said. It is
one when the plugin comes to do something this file said it did not. So 0.3.0's "a settings change
whose reconnect fails no longer leaves the box `online`" is a fix; 0.3.0's zap through the channel
list, which this file had described as a direct play, is not.

**New values of an enumeration.** Every enumeration in this file is open: a consumer treats a value
it does not know as unknown - neither an error nor any value it does know - and keeps the rest of
the payload. That is what makes adding a value an addition. The release that adds one checks the
companion integration's handling of it before it ships. Where the integration stands (released
0.3.1; the line numbers are its `box.py`):

- **tolerated**: `last_error.reason` - "a code from a newer plugin" falls back to the English
  sentence (`box.py` l.562-583); `epg_import.state` (l.1768), `softcam.last_restart_reason`
  (l.1695) and `oscam.readers[].status` (l.1525) - an unknown value becomes unknown or `null`;
  `timers.state` - the integration does not read it at all (below);
- **not tolerated by 0.3.1**, each needing an integration release that tolerates it before a
  plugin release adds a value:
  - `oscam.readers[].kind` - a value other than `reader`, `server` or `unknown` makes the
    integration discard the whole new `oscam` payload (`_normalize_oscam`, l.1486-1515), so the
    last good sample stays on its panel, going stale;
  - `key.press` - a value other than `short` or `long` is read as `short` (`box.py` l.1985-1987),
    so a new kind of press would fire the automations and device triggers of a short press;

  Both are tolerated since the integration's 0.4.0: a source of a kind it does not know is
  skipped - it gets no new entity, one it already has is kept, and the rest of the payload
  applies - and a press other than `short` or `long` fires nothing. A payload without `press` is
  still a short press, and a `kind` that is not a string is still a malformed payload. A plugin
  release that adds such a value still has to consider installations on 0.3.1 and older, which
  behave as described above;
- **not audited**: the other enumerations, among them the choices of an enumerated setting and
  `info.ha_mode`.

**Named in-major exceptions.** A change of meaning stays inside the major only when all of these
hold:

1. **No known consumer depends on the meaning it changes** - checked against the companion
   integration's code at the time, and against this plugin's own Home Assistant discovery payloads
   (`discovery.py`), which are a consumer too, and cited by file and line in the table below. No
   other consumer is known to this project; one that becomes known is checked the same way. (None
   of the six below touches the discovery payloads: they read neither `timers`, `generated`,
   `channels`, an `epg_grid` topic nor `last_error` -
   "Next timer" reads `recording.next`, `discovery.py` l.332-346 - and carry `bouquet` as reported.)
2. **Every name and type stays.** An exception changes what something means, never whether it
   exists or what type it has - [`contract.json`](contract.json) does not change for it. A removal or
   a retype always needs a new major.
3. **It is listed by name** in the table below and in the changelog of the release that ships it,
   marked as a behaviour change, with what a consumer has to do.
4. **It is decided in the pull request that makes it**, not discovered afterwards, and its date
   says so. It is `unreleased` until the release that ships it, which dates it to itself - the
   version in `version.py`, later than the previous release; a release tag never carries
   `unreleased` - nor does a release pull request, recognised by `version.py` being above the
   previous release tag, so an undated exception fails the pull request rather than the tag; a
   date never changes once it has shipped. The only departure is the first
   release that carries `contract.json`: it records history, dated when it happened.
   `contract.json` carries the same names and dates in `exceptions`, and `tools/check-contract.py`
   fails when the two lists differ, when a later release drops or re-dates one, when a new one is
   dated to anything but the release that ships it, and when a release tag - the one being checked
   included - says `unreleased` or dates an exception to anything but the release it first
   appears in. The release workflow runs the same check before it publishes anything.

| Exception | Release | What changed | Why no known consumer breaks |
|---|---|---|---|
| `zap-moves-channel-list` | 0.3.0 | `cmd/zap` goes through the receiver's channel list, so a zap to a channel outside the bouquet being browsed moves the channel list to that channel's bouquet, and `bouquet` names it | The integration shows `bouquet` as the receiver reports it (`select.py` l.252 and l.278, `sensor.py` l.719 follow the topic), so it follows the move rather than contradicting it. Recorded after the fact: the rule did not exist when 0.3.0 shipped, and this check is made against integration 0.3.1 |
| `epg-grid-generated-means-changed` | 0.3.0 | `epg_grid/<bouquet_slug>`'s `generated` means when the grid last changed, not when it was last built | The integration reads `generated` only into its diagnostics (`diagnostics.py` l.144). Recorded after the fact, as above |
| `timers-lists-finished` | 0.4.0 | `timers` lists the timers the receiver has finished with - ended, failed and disabled - and not only the pending ones; `state` gains `disabled`, `failed` and `unknown`, and a state number with no word is `unknown` where it used to read `waiting`. A consumer that treated every entry as pending filters on `state` (`waiting`, `prepared`, `running`) | The integration keeps the `timers` payload for its diagnostics only (`box.py` l.1299, `diagnostics.py` l.119) and builds no entity from it; its recording guard reads OpenWebif, not this topic (`installer.py` l.516-532). Decided by the maintainer on 2026-09-26 |
| `zap-under-popup-recorded` | 0.4.0 | With only an information popup open directly over the info bar, `cmd/zap`, `cmd/zap_history` and the zap `cmd/bouquet` makes go through the channel list: the zap is recorded in the zap history, and a zap outside the bouquet being browsed moves the channel list. 0.3.0 played them directly, as with any screen open | As for `zap-moves-channel-list`: the integration follows `bouquet` and `zap_history` as the receiver reports them (`box.py` l.1291 for `zap_history`) |
| `oversize-payload-withheld` | unreleased | A retained topic of the node's tree whose packet would be over 1,000,000 bytes is not published, and a smaller payload retained there earlier is retracted, while its capability stays in `info.capabilities`; `info.not_published` names the topic. `channels` and an `epg_grid/<bouquet_slug>` are the topics known to get that big, and the rule is not theirs alone: every retained topic of the tree is held to it the same way, `screen` - a JPEG, not JSON - and `channels/<slug>` included. Until now it was published whatever its size. A consumer treats the empty retained payload as any other retraction, and reads `info.not_published` to tell "too big to send" from "none". Where `channel_topics` is a capability, the lists a withheld `channels` would have carried are on `channels/<slug>`, one topic for each bouquet, with the slugs in `bouquets` | The integration (0.4.0) reads an empty retained `channels` as no channel list: an empty payload parses to nothing (`box.py` l.289, l.293-305), the dispatcher stores that (`box.py` l.1338-1339) and the bouquet readers answer with an empty list (`box.py` l.925-935), so its channel selects lose their options rather than keep stale ones; an empty `epg_grid/<bouquet_slug>` drops that bouquet's grid (`box.py` l.2135-2142). It does not read `info.not_published`, so it shows the empty list without saying why, and it reads neither `bouquets` nor `channels/<slug>`: its dispatcher stores a topic by its exact name and ignores a suffix it does not know (`box.py` l.1302-1311, l.1338-1340). One reader does not go through those: the installer's restart rule reads the retained `channels` itself and takes a payload that is no document, or a bouquet without a list, for a bouquet that does not hold the service (`restart_rule.py` l.242-259, the list test at l.252-254), so after an update on such a receiver it does not restore the bouquet context. The plugin's own discovery payloads read neither topic - the channel select's options are written into the payload (`discovery.py` l.371-382) |
| `last-error-cleared-at-start` | unreleased | A retained `last_error` left by an earlier run of the plugin is cleared - an empty retained payload - at the first connect of a new plugin process: after an interface restart, a reboot, a reinstall or an update. Until now it stayed until the next command that succeeded, however many restarts later, and then described a command of a run that was over. A reconnect within one run clears nothing, nor does the new session a settings save opens; and a refusal the starting plugin has to report itself - how an update ended (section 2) - is published at that connect instead and replaces the older one. Nor is it cleared while the record of the last update ends in a restore that did not complete (`update.transaction.reason` `restore_failed` or `restore_incomplete`): the sentence left there says to install the plugin again, and restarting the interface has not done that; it stays, as before, until a command succeeds or `cmd/clear_error` is sent - also after the reinstall, since that record only changes with the next update. The sentence of an update whose helper stopped part-way is cleared by a start that has no verdict to give ([TRANSACTION.md](TRANSACTION.md), section 7). The plugin knows of the earlier error from its state file (section 4), which is not written at every refusal - a connect and a clean stop write it - so an error published shortly before the interface was killed can be missed and stays as before; `cmd/clear_error` (section 2) clears that one. The empty payload is sent only when the plugin knows of an error, before `availability` says `online` - which includes one empty message to a topic the broker holds nothing on, when the state file knows of an error the broker has lost, or when the only refusal was made before this run had a session. A consumer that wants the last refusal across a restart keeps it itself when it arrives, as it already has to across the next command that succeeds | The integration (0.4.0) keeps its own record: its last-error sensor ignores an empty payload (`sensor.py` l.931-935) and restores what it showed, so the clearing changes nothing it shows. Its update card drops the complaint it kept when the topic is cleared (`update.py` l.664-666), as after any command that succeeds, and says how an update ended from `update.transaction` and from the end's repeat on `last_error`, which is the report that is not cleared. Its command, update and uninstall waits take only a message that is not retained (`box.py` l.2125, `mqtt_update.py` l.598, `uninstall.py` l.722-723), and an empty payload fails or completes no command (the same test, `box.py` l.2125: retained or empty, it only replaces the stored state). The plugin's own discovery payloads announce no entity for `last_error` (`discovery.py`) |

The rule was written down after 0.3.0, so the history below classifies the releases before it by
what they did.

The consumer's half: ignore a topic, a member, a capability name or a setting you do not know;
treat a value of an enumeration you do not know as unknown; treat a member you expect and do not
find as the older plugin it is; and accept `info` more than once per connection. A consumer that
does this and is written for contract N works with every
plugin of contract N, older or newer - which is why there is **no upper bound** inside a major, and
why "contract 1: plugin 0.2.0 and later, integration 0.2.0 and later" is the whole compatibility
statement. The lockstep table in the READMEs lists the pairs that were released together; they are
compatible, but they are not the only compatible pairs, and the table is to be replaced by this
rule.

**The same contract as data.** [`contract.json`](contract.json) lists every state topic with its
payload kind and retain flag, every command, every `info` member with its type, every setting with
its type and whether `cmd/config` may write it, every capability name, the topics outside the
node's tree, the major, the exceptions above and the planned additions of section 5.
`tools/check-contract.py` runs in CI and fails when that file and this one disagree, and - against
`contract.json` at the previous release tag - when a release removes or retypes one of those
without a new major (a new choice of an enumerated setting counts as an addition, a choice taken
away as a retype), or drops, re-dates or back-dates an exception. Once a release carries the file,
a maintenance branch of an older release fails that comparison: its newest tag has no file while a
newer one has - deliberately, as there are no maintenance releases of the lines before it. The
fields inside each payload are **not** in the data,
so a change to one - the commonest way to break a consumer - is caught by review against this file,
not by the checker; their types are the tables below.

| Step | Classification | What changed |
|---|---|---|
| 0.1.0 -> 0.2.0 | a new major: **0.1.0 is contract 0** | 0.1.0 implemented the session only: `availability`, `info` with an empty `capabilities` list and no `settings` member, the announcement, `last_error`, and `cmd/ha_mode`, `cmd/discovery` and `cmd/reset`. Its copy of this file described the other topics before they were built, and 0.2.0 built several of them differently - `service.bouquet` became the first configured bouquet that holds the service rather than the bouquet it was tuned from, and `service.name` may be `null`. A consumer of contract 1 reads the permissions from `info.settings` and the features from `capabilities`, and 0.1.0 publishes neither. This is also why no update path in this project offers anything below 0.2.0 |
| 0.2.0 -> 0.3.0 | contract 1: additions and two named exceptions | **Added**: the topics `cec`, `zap_history`, `epg_import`, `softcam` and `process`; the commands `zap_history`, `history_clear`, `softcam_restart`, `epg_import` and `uninstall`; `info.wol`; `last_error.reason`; `cmd/message`'s `style`; the writable settings `softcam_autoheal` and `softcam_autoheal_seconds`; the read-only `softcam_restart_allowed`, `epg_import_allowed` and `uninstall_allowed`; the capability names `cec_workaround`, `softcam`, `toast`, `process`, `zap_history`, `history_clear`, `epg_import` and `uninstall`. **New refusals**: `deep_standby`, `reboot` and `restart_gui` while an EPG import runs, and `cmd/bouquet` during timeshift. **Free text tightened**: `cmd/message` removes every backslash from a popup's text, as from a toast's; a sender that used a literal `\n` for a line break sends a newline instead. **Exceptions**: `zap-moves-channel-list`, `epg-grid-generated-means-changed` |
| 0.3.0 -> 0.4.0 | contract 1: additions, fixes and two named exceptions | **Added**: `cmd/timer` `delete` accepts a finished, failed or disabled timer; the `info` members `build` and `contract`; the read-only setting `update_check`, the `update` topic, the command `update_check` and the subscription to `enigma2mqtt/release_index` (§3); the read-only setting `update_allowed`, the capability `self_update`, the command `update`, `update.transaction` and the subscription to `enigma2mqtt/integration/<node>` (§3); the event topic `relay_request` and the command `relay`, and `cmd/update`'s refusal `no_relay`. **Exceptions**: `timers-lists-finished` (#42), `zap-under-popup-recorded` (#45, #46). **Fixes** (the plugin now does what this file said): `cmd/zap_history`'s refusal without a navigation or a player (#41), and the refusal of a `cmd/timer` `add` whose window has passed |
| unreleased, on `main` after 0.4.0 | contract 1: additions and two named exceptions | **Added**: the `info` member `not_published`; a discovery payload over the packet bound is not published, and `cmd/reset` leaves the one already retained (§2); the topics `bouquets` and `channels/<bouquet_slug>` and the capability `channel_topics`; `epg_grid/<bouquet_slug>`'s member `events_per_channel`, and a grid over the packet bound is cut to fewer events per channel before it is withheld; a discovery payload over the packet bound is published without the channel select (§4); the command `clear_error` and its discovery button (sections 2 and 4); a command's id - the envelope every command takes on the broker, the `last_error` member `id` and the capability `command_id` (sections 1 and 2); the `service` members `video_codec`, `interlaced`, `frame_rate` and `audio_tracks` and the capability `service_codecs`; the topics `stream` and `stream_clients`, the event topic `stream_playable`, the commands `stream_stop` and `stream_playable` and the capabilities `stream`, `stream_clients`, `stream_stop` and `stream_playable`; the `volume` member `passthrough` and the capability `volume_passthrough`; the receiver menu - the subscriptions to `enigma2mqtt/menu/<node>` and `<ha_discovery_prefix>/status` (§3), the event topic `menu_action` with its member `seen` on a `toggle`, the `info` member `menu`, the read-only setting `menu_allowed`, the writable setting `menu_hotkey` and the capability `menu`; the writable setting `menu_transparency`, and `cmd/config` taking any part of its keys, every key left out keeping its value; the menu creator - the subscriptions to `enigma2mqtt/menu_catalogue/<node>` and `enigma2mqtt/menu_edit_result/<node>` (§3), the event topic `menu_edit` and the capability `menu_editor`; a list of choices - the command `list`, the event topic `list_answer`, the `info` member `list` and the capability `list`, and the `reason` `busy` on the doors' sentence for `cmd/list`; the recording library - the topics `recordings` and `recordings/<folder_slug>`, the commands `play_recording`, `delete_recording` and `recordings_refresh`, the read-only setting `recording_delete_allowed`, the `info` member `recording_trash` and the capabilities `recordings`, `recording_play` and `recording_delete`; the members `service_name`, `description`, `location`, `file`, `id`, `instant` and `bouquet` of each running recording in `recording.active` and the capability `recording_details`; the topic `timeshift` and the capability `timeshift`. **Exceptions**: `oversize-payload-withheld`, `last-error-cleared-at-start` |

---

## 1. State topics

### `<base>/<node>/availability`

Retained, QoS 0. **Not JSON** - the literal string `online` or `offline`.

`offline` is registered as the connection's last will, so the broker publishes it when the box
vanishes without saying goodbye. The plugin publishes `online` in `on_connect` and a clean
`offline` on a graceful shutdown. A settings save on the receiver restarts the session only when
it changed the connection - broker address or port, login, TLS, the CA file, the node id or the
base topic - or switched the plugin off or on; every other save is applied under the open session,
with no reconnect and nothing on this topic. A restart is a clean disconnect, which discards the
will; when the save changed how the box reaches the broker or switched the plugin off, the old
session publishes `offline` first, so it stays retained until the new session connects, and for
good if it never does. A rename of the node id or the base topic publishes none: every retained topic
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
  "plugin": "0.4.0",
  "build": {"commit": "3f6c0a2d9e41b7c85a0f2e6d1c9b8a7f6e5d4c3b", "time": 1790000000,
            "dirty": false, "flavour": "release", "on_disk": null},
  "contract": 1,
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
               "epg_import_allowed": false, "uninstall_allowed": false,
               "update_check": false, "update_allowed": false,
               "menu_allowed": true, "menu_hotkey": "help_long",
               "menu_transparency": 20, "recording_delete_allowed": false},
  "capabilities": ["power", "service", "epg", "tuner", "softcam", "recording", "timers",
                   "volume", "hdd", "process", "channels", "channel_topics", "bouquet_context",
                   "epg_grid", "keys", "screenshot", "toast", "menu", "menu_editor", "list",
                   "message", "command_id"],
  "menu": {"screen": "styled", "hotkey": "help_long", "reason": null},
  "list": {"screen": "styled"},
  "recording_trash": true,
  "not_published": []
}
```

| Field | Type | Meaning |
|---|---|---|
| `image` | string | Image name and version as the box reports it |
| `enigma` | string | enigma2's own version string, as `getEnigmaVersionString()` reports it - on OE-Alliance images a build date such as `2024-09-11-Release`, not a number |
| `plugin` | string | This plugin's version - what an `update` entity compares against. The same for a release and for every development build that carries its number; `build` tells them apart |
| `build` | object | Which build of the plugin is running, and which one is waiting on disk. Since 0.4.0. See below |
| `contract` | int | The [contract major](#contract-version) this plugin implements. Since 0.4.0; a plugin without it is contract 0 below 0.2.0 and contract 1 otherwise |
| `boxtype` | string | Machine name, lowercase |
| `mac` | string | Lowercase, colon-separated; the Wake-on-LAN target |
| `ip` | string | Current LAN address |
| `uptime` | int | Seconds since boot |
| `wol` | object | Since 0.3.0. What the **image** says about Wake-on-LAN - whether it has a switch for it and whether that is on. See below |
| `ha_mode` | string | `discovery` \| `integration` \| `off` - the acknowledgement of `cmd/ha_mode` |
| `settings` | object | The complete non-secret settings a consumer may **read**. Writable through `cmd/config`: `publish_keys` (bool), `screenshot` (`off` \| `on_zap` \| `interval`), `screenshot_interval` (integer seconds, 5-3600), `screenshot_delay` (post-zap settling seconds, 1-30), `cam_telemetry` (bool, off by default), `oscam_telemetry` (bool, off by default), `softcam_autoheal` (bool, off by default), `softcam_autoheal_seconds` (integer seconds, 30-600), `menu_hotkey` (`help_long` \| `red_long` \| `f3` \| `f4` \| `off`; a long HELP by default; added after 0.4.0, unreleased), `menu_transparency` (integer percent, 0-80 in steps of ten, 20 by default: how see-through the receiver menu's background is; added after 0.4.0, unreleased). **Read-only**: `deep_standby_allowed`, `softcam_restart_allowed`, `epg_import_allowed`, `uninstall_allowed`, `update_check`, `update_allowed`, `menu_allowed` and `recording_delete_allowed` (bools, off by default but `menu_allowed`, which is on). See below. |
| `capabilities` | list of strings | Which hooks this image actually gave the plugin |
| `menu` | object or null | Which screen the receiver menu is drawn with on this image and which key opens it; `null` where there is no menu - `menu_allowed` off, or no screen could be built. Added after 0.4.0 (unreleased). See below |
| `list` | object, optional | Which screen a list of choices (`cmd/list`, section 2) is shown with on this image. **Absent**, not `null`, where `list` is not a capability. Added after 0.4.0 (unreleased). See below |
| `recording_trash` | bool or null | Whether the image's trash for recordings is switched on - its own setting, *Use trash can in movie list* (`config.usage.movielist_trashcan`) - which is what `cmd/delete_recording` moves a recording into: `false` means a delete is refused. `null` where the image has no trash function, or the plugin has no session. Read from the image at every `info` publish, since it is changed in the image's own menu. Added after 0.4.0 (unreleased) |
| `not_published` | list of objects | The topics whose payload was too big to send, so they are not on the broker. `[]` when everything fits, which is nearly always. Added after 0.4.0 (unreleased). See below |

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
| `update_check` | 0.4.0 | Whether the receiver asks the plugin's release origin, on the internet, which releases there are: once a day, and when `cmd/update_check` asks over MQTT. Not a permission for anything on the receiver, but the one switch that lets it make a connection other than the broker's, so it sits on this side of the line for the same reason. Always present, whichever way it is set | The same places, and never over MQTT |
| `update_allowed` | 0.4.0 | Whether `cmd/update` is permitted over MQTT - an install of a signed release, which ends in a restart of the interface. Always present, whichever way it is set. A consumer offers an install over MQTT only on a stated `true` together with the `self_update` capability | The same places, and never over MQTT |
| `menu_allowed` |  | Added after 0.4.0 (unreleased). Whether the receiver offers the Home Assistant menu ([§3](#the-receiver-menu---added-after-040-unreleased-capability-menu)): **on by default**. Off, there is no menu - no Extensions entry, no hotkey, no subscription to `enigma2mqtt/menu/<node>`, no `menu_action`, and no `menu` capability. Always present, whichever way it is set | The same places, and never over MQTT |
| `recording_delete_allowed` |  | Added after 0.4.0 (unreleased). Whether `cmd/delete_recording` is permitted over MQTT - a move of one recording into the image's trash, never further. **Off by default.** Always present, whichever way it is set. The `recording_delete` capability needs it on and the image's trash on as well (`recording_trash`) | The same places, and never over MQTT |

The rule behind which side of the line a setting falls on: one that **enables a command** is never
writable over MQTT - it is set on the receiver (the setup screen, the provisioning file, or the
OpenWebif page, which is exactly as open as the receiver's web interface; see
[ADR-0009](adr/0009-the-openwebif-page-trusts-openwebif.md)); one that **tunes a command already
permitted** may be remote. A member of this table exists so a consumer can **hide what the box will
refuse** rather than offering a control that always fails. `info` is republished when it changes:
a save on the setup screen or the OpenWebif page publishes it again under the open session, or -
when the save changed the connection - the new session's connect publishes it with the snapshot.

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
`toast`, `message`, `hdd`, `process`, `channels`, `channel_topics`, `bouquet_context`,
`zap_history`, `history_clear`, `epg_import`, `uninstall`, `self_update`, `command_id`,
`service_codecs`, `stream`, `stream_clients`, `stream_stop`, `stream_playable`,
`volume_passthrough`, `menu`, `menu_editor`, `list`, `recording_details`, `recordings`, `recording_play`, `recording_delete`, `timeshift`. A build that
has bound no feature area publishes that last name alone - `[]` before it existed - because the
connection, `info` and the commands are the plugin itself and are not capabilities. `uninstall`
(since 0.3.0) was the first name with no feature area behind it: it
says the package manager installed this very copy of the plugin, so `cmd/uninstall` can work - opkg
is executable, its configured info directory knows the package, and the package's file list names
the running `plugin.py`. A copy unpacked by hand or carried in a firmware image does not claim it.
`self_update` (since 0.4.0) has no feature area behind it either: it is `uninstall`'s condition, and
the receiver can run the update helper outside enigma2 - `/sbin/start-stop-daemon` and
`/usr/bin/python3` are executable and the helper and the four modules it imports are in the plugin's
directory - so `cmd/update` can work. `channel_topics` (added after 0.4.0, unreleased) is not a
feature area of its own either: it is claimed with `channels`, and says that the index `bouquets`
and each bouquet's list on `channels/<bouquet_slug>` are published - which is where a consumer
finds the lists when `channels` is too big to be. `command_id` (added after 0.4.0, unreleased) is
not a feature area at all, and no hook is behind it: every plugin that opens the envelope a
command's id comes in (section 2) claims it, always. It is how a consumer knows the envelope is
safe to send - a plugin without the name takes an envelope for the command's own payload.

Six names were added after 0.4.0 (unreleased) for what the receiver knows about streaming
and about its audio. `service_codecs` is claimed with `service` on an image that can name
the video type (`iServiceInformation.sVideoType`): the codec members of `service` are then
filled in, where an older plugin has no such members and an image without the name has
them `null`. `stream` says the image has a stream server and the `stream` topic describes
it; `stream_clients` that the server lists who it is serving, on `stream_clients`;
`stream_stop` that it can also end one client's stream, so `cmd/stream_stop` can work; and
`stream_playable` that the image can be asked whether a service could be played now, so
`cmd/stream_playable` can work. Each of the last three is claimed by itself: an image whose
server lists clients and cannot stop one claims the first and not the second.
`volume_passthrough` is claimed with `volume` on an image that has at least one of the
settings `volume.passthrough` is read from; without it the member is still published, and
is `null` for every track the receiver might hand on undecoded.

`menu` (added after 0.4.0, unreleased) says the receiver offers the Home Assistant menu: the
permission `menu_allowed` is on and the image can open a menu screen - the plugin's own, or the
image's list as the fallback. It is what the companion integration waits for before it acts on a
`menu_action` from this node ([§3](#the-receiver-menu---added-after-040-unreleased-capability-menu)).

`menu_editor` (added after 0.4.0, unreleased) says the receiver offers the menu creator: `menu` is
claimed and OpenWebif has mounted the plugin's page, whose Menu subpage the creator is on. It is
what the companion integration waits for before it publishes the catalogue and before it reads a
`menu_edit` from this node ([§3](#the-menu-creator---added-after-040-unreleased-capability-menu_editor)).
It goes with `menu`, and it comes late on a receiver whose OpenWebif mounts its pages after the
plugin has connected: `info` is published again then.

Five names were added after 0.4.0 (unreleased) for the receiver's recordings. `recording_details` is
claimed with `recording`: each running recording in `recording.active` carries its details.
`recordings` says the image has movie locations and the library has been read: the index
`recordings` and the folders `recordings/<folder_slug>` are published, and
`cmd/recordings_refresh` works. It arrives late - with the first read, a moment after the
plugin starts - and `info` is published again then. `recording_play` is claimed with it on a
receiver whose info bar can open the image's movie player, so `cmd/play_recording` can work.
`recording_delete` is claimed only while `recording_delete_allowed` is on **and** the image's
trash is on (`info.recording_trash` `true`) - so a consumer that sees `recordings` without it
reads `info` to say which of the two is missing. `timeshift` says the image's info bar has
timeshift and the plugin found it: the `timeshift` topic is published. It arrives late on an
image that builds its info bar after the plugin has started, as `bouquet_context` does.

`list` (added after 0.4.0, unreleased) says the receiver can put a list of choices on the
television - `cmd/list` (section 2), answered on `list_answer` (below): the plugin's own screen or,
where that cannot be built, the image's own choice list. It is claimed once one of them has been
built on this image, and taken back - with `info` published again - when a skin reload can build
neither. It needs no setting and no permission, as a message needs none
([ADR-0023](adr/0023-a-list-of-choices-is-answered-by-row-id-once.md)).

A name is in the list because it **worked on this box**, not because this version of the plugin
has the code for it. Three things can take one out: the image did not provide the hooks
(`volume` on a box with no `VolumeControl`), the feature is switched off in the settings
(`keys` with `publish_keys` off, `screenshot` set to `off`, `epg_grid` with `epg_grid_events` at
`0`, `cec_workaround` with `cec_standby_workaround` off, `toast` with `osd_toast` off, `menu` and `menu_editor` with `menu_allowed` off), or the hook raised while it was being
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

**`build` says which code runs, which a version number cannot.** Since 0.4.0
([ADR-0015](adr/0015-signed-self-update.md), decision 1). A development build of the commit after a
release carries that release's number until the next release pull request changes it, so `plugin`
alone makes a receiver running it look up to date. The values come from whoever built the package
(`tools/build-ipk.sh`, which records them in the package) and are never worked out on the
receiver. Always present with every key.

| Field | Type | Meaning |
|---|---|---|
| `commit` | string | The commit the package was built from, 40 lowercase hex digits. Empty when the builder did not know it - a package built from a source archive nobody named the commit of - and for a copy of the plugin that was not built into a package at all |
| `time` | int or `null` | The commit's time, epoch seconds: the timestamp every file in the package carries. `null` when unknown, and for a copy that was not built |
| `dirty` | bool or `null` | Whether the tracked files differed from `commit` when it was built. `null` for a copy that was not built |
| `flavour` | string or `null` | `release`, `development` or `acceptance`. `release` is the builder's word that this is a release. Built in a git checkout, the build refuses it unless the tracked files are clean and HEAD carries the release tag. Built from a source archive, the build refuses it unless the builder names the commit and its time, and the builder vouches for the tree and the tag; only the signed release index confirms a release. `development` is every other build of the published code; `acceptance` is a build for a hardware acceptance run. A consumer reads a flavour it does not know as "not a release". `null` for a copy that was not built |
| `on_disk` | string or `null` | Whether another build is staged: the files were replaced and the interface has not restarted yet. `null` means no other build is staged - the file on disk is the running build - or the file on disk cannot be read. A string means another build is staged, and it is that build's `commit`: `""` when that build's commit is unknown, so test for `null`, never for truth; and equal to the running `commit` when the staged build is the same commit built another way - another flavour, or dirty. Checked whenever `info` is built and every ten minutes; a change republishes `info` |

**How to show the version.** `plugin` as it is for a release build - `flavour` `release` and
`dirty` `false` - and for a build whose `commit` is empty, which cannot be named any other way;
`plugin` + `+g` + the first seven digits of `commit` for everything else: `0.3.0+g3f6c0a2`, and
`0.3.0+g3f6c0a2.dirty` when `dirty` is `true`. The part after `+` is a local label, not a
pre-release: a consumer never orders versions by it. Two builds of **different commits** with the
same `plugin` are ordered by `time`, the later one being the newer. Builds of the same commit share
`time` - clean and dirty, development, acceptance and release - so the order cannot tell them
apart; the display tells a dirty build from a clean one, and only `flavour` tells the flavours
apart. A consumer that knows a release's commit from somewhere the plugin cannot see - the signed
release index ([RELEASE-INDEX.md](RELEASE-INDEX.md)) - holds a `release` build to that commit as
well.

There is deliberately no `origin` member. Where a build fetches its releases from is not something
this object has to say to tell builds apart - only an `acceptance` build may carry another source
or other index keys, and the build refuses both for every other flavour - and "origin" already
means two other things in this contract: where a command came from, and whether the release index
could be reached (`update.origin`). The release workflow reads every release package
back and refuses one that carries either.

**`not_published` names the topics that are missing on purpose.** Added after 0.4.0 (unreleased).
A payload whose packet would be over the plugin's bound of 1,000,000 bytes (the conventions at the
top of this file) is not sent, because a broker answers an oversize packet by closing the
connection, and the plugin would send it again on every reconnect. Always present; `[]` when
everything fits, otherwise one entry for each such topic, sorted by `topic`:

```json
"not_published": [{"topic": "channels", "bytes": 2315478, "limit": 1000000},
                  {"topic": "epg_grid/astra", "bytes": 1204611, "limit": 1000000}]
```

| Field | Type | Meaning |
|---|---|---|
| `topic` | string | The topic, relative to `<base>/<node>/` - `channels`, `epg_grid/astra` - or in full for a topic outside the node's tree, which is how a discovery payload appears: `homeassistant/device/<node>/config` |
| `bytes` | int | The size of the packet the payload would have needed - the fixed header counted at its longest, the topic and the payload, in bytes - **measured when the topic was first withheld**, in this run of the plugin. It is not updated while the topic stays in the list, so it says how far over the bound the payload was then, not what it is now. For an `epg_grid/<bouquet_slug>` it is the size of the grid with **one event on each channel** - the smallest there was to send, since a grid is cut before it is withheld - and not of the grid with `epg_grid_events` |
| `limit` | int | The bound it was held to, `1000000` |

A topic of the node's own tree that is in the list is **not on the broker**: when this node had
published a smaller payload there earlier, that copy is retracted - at once on an open session,
otherwise on the connect that follows - so a consumer never reads an old channel list as the
current one. A topic outside the tree is never retracted for its size: an empty
retained device payload would delete the device and all its entities in Home Assistant, so the
discovery payload published earlier stays as it was, and a first install has none until the
payload fits. It takes a payload that is too big without its channel select to get there, because
the select is left out first (§4).

The entry goes when the topic is published again - the payload fits - or is retracted for another
reason, such as the EPG grid being switched off. `info` is published again when a topic joins or
leaves the list, which for a grid built after the connect is some time after the first `info` of the
connection. It follows the change by two seconds rather than at once, and a further change within
them starts the two seconds again - for at most ten from the first - so the topics that become too
big one after another at a start cost one `info` and not one each; a topic that joined and left
within that time is not announced at all. The first `info` of a connection does not wait, and a
change still waiting when the plugin stops or its settings are saved is published before the
session ends. `info` is not published again while a topic stays in the list: a grid that stays too
big has another size at every pass, and neither `bytes` nor `info` follows. A topic that fitted,
was published and is too big again is a new entry with a new `bytes`. The receiver's log has one
line each time a topic joins the list.

What gets into the list is what could not be made smaller. A grid is cut to fewer events per
channel and the discovery payload leaves the channel select out before either is withheld (below,
and §4). `channels` cannot be made smaller, so it is in the list on a receiver whose bouquets do not
fit one packet together, and its lists are then on `channels/<bouquet_slug>`, with `bouquets` as
their index (below). Any other entry is nearly always `channels/<bouquet_slug>` or
`epg_grid/<bouquet_slug>` for one bouquet that is too big by itself - a list of many thousands of
services, or a grid that does not fit with one event on each channel. The remedy is on the
receiver: `bouquets_for_select` limited to the bouquets in use brings `channels` back and takes
such a bouquet out, or the bouquet is split into smaller ones. A lower `epg_grid_events` changes
nothing for a grid that is in the list; `0` switches the grids off
([TROUBLESHOOTING.md](TROUBLESHOOTING.md#entities-keep-going-unavailable-and-coming-back)).

**`menu` says how the receiver menu is shown here.** Added after 0.4.0 (unreleased),
[ADR-0020](adr/0020-the-receiver-menu-is-drawn-on-the-receiver-and-decided-in-home-assistant.md)
and [ADR-0021](adr/0021-the-menu-hotkey-is-offered-the-key-after-the-image.md). `null` when the
receiver has no menu: `menu_allowed` off, no session, or neither screen could be built. Otherwise:

```json
"menu": {"screen": "styled", "hotkey": "help_long", "reason": null}
```

| Field | Type | Meaning |
|---|---|---|
| `screen` | string | `styled` - the plugin's own screen, with icons, states and colours - or `fallback` - the image's own list, with the same rows and no icon, state or number keys. Decided when the menu is opened: a styled screen that cannot be built then is replaced by the fallback for that opening and every later one, and `info` is published again |
| `hotkey` | string or null | The `menu_hotkey` choice that is bound and opens the menu in live TV and in the image's movie player, or `null` when none does: switched `off`, an image that does not let a plugin bind a key, or a key the image's own user-assignable button setup gives a function of its own - which then runs first, by design; that setup is the same in live TV and in the movie player. The key opens the menu in the image's media player too, unless that player uses the key itself, which it then keeps: OpenATV's media player seeks back on a long RED. Over any other screen the key does nothing new |
| `reason` | string or null | Why `hotkey` is `null`, in English for the person reading the topic; `null` when a key is bound. The Extensions entry "Home Assistant" opens the menu whatever this says |

**`list` says how a list of choices is shown here.** Added after 0.4.0 (unreleased),
[ADR-0023](adr/0023-a-list-of-choices-is-answered-by-row-id-once.md). **Absent** - not `null` -
where `list` is not a capability: no session, or neither screen could be built. Otherwise:

```json
"list": {"screen": "styled"}
```

| Field | Type | Meaning |
|---|---|---|
| `screen` | string | `styled` - the plugin's own screen, with the icons, the countdown and the keys drawn as buttons - or `choicebox` - the image's own choice list, with the same rows, ids and keys, no icon, and the header line and the countdown in its window title where the image lets the title be set. Decided when the plugin starts and after every skin reload, and again for every list: a styled screen that cannot be built for a list is replaced by the image's list for that list and every later one, and `info` is published again |

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
  "height": 1080,
  "video_codec": "h264",
  "interlaced": true,
  "frame_rate": 25.0,
  "audio_tracks": [
    {"index": 0, "description": "MPEG", "language": "pol", "pid": 5001,
     "codec": "mp2", "active": false},
    {"index": 1, "description": "Dolby Digital", "language": "pol", "pid": 5002,
     "codec": "ac3", "active": true}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `sref` | string | The service reference, trailing colon included. The stable identifier - `name` is for people |
| `name` | string \| null | Channel name as the bouquet spells it |
| `bouquet` | string \| null | The first of the **configured** bouquets (`bouquets_for_select`) that contains the service - `null` when it is in none of them, which is not the same as „no bouquet" |
| `provider` | string \| null | |
| `width`, `height` | int \| null | Video resolution; `null` before the first frame is decoded. From after 0.4.0 (unreleased) read from the video decoder (`/proc/stb/vmpeg/0`) where the image has it, else from the image's service information - see below |
| `video_codec` | string \| null | Added after 0.4.0 (unreleased). The video codec on the decoder, as a stable lower-case name: `mpeg2`, `h264`, `hevc`, `mpeg1`, `mpeg4` (MPEG-4 Part 2), `vc1`, `vc1_sm`, `avs`, `avs2`. `null` when the image gives a value that is none of these, or none at all. An open enumeration |
| `interlaced` | bool \| null | Added after 0.4.0 (unreleased). `true` for an interlaced picture, `false` for a progressive one; `null` before the decoder knows. From the video decoder where the image has it, as `width` |
| `frame_rate` | number \| null | Added after 0.4.0 (unreleased). Frames a second - `25.0`, `50.0`, `23.976` - to three decimal places; `null` before the decoder knows. For an interlaced picture it is the frame rate, not the field rate. It is the decoder's number (or the image's) divided by a thousand and is held to no upper bound: a value no broadcast has is published as it was given. From the video decoder where the image has it, as `width` |
| `audio_tracks` | list \| null | Added after 0.4.0 (unreleased). The audio tracks of the service, in the image's order, at most 32. `[]` for a service without audio; `null` when nothing is playing or the image will not say. See below |

Published on `evStart`, `evTunedIn`, `evUpdatedInfo`, `evNewProgramInfo` and `evEnd`, so it
settles within a second of a zap and is published again when the resolution becomes known - a box
answers `-1` for both until the first frame is decoded, and `-1` is not a resolution. Since the
codec members were added it is also published on the decoder's own events - `evVideoSizeChanged`,
`evVideoFramerateChanged`, `evVideoProgressiveChanged` - where the image has them, which is when
the frame rate and the scan become known; and when the audio track being played is not the one
last published. Nothing announces a change of track - the image raises no event when somebody
picks another one on the remote - so the plugin asks which track is playing every five seconds -
two cheap calls that read no track, and none at all with nothing playing, which is what standby
is - and publishes only when the answer moved. As always, a payload equal to the last one is not
sent again.

With nothing playing every field is `null`; the keys are still all there.

**The picture is the decoder's word** (after 0.4.0, unreleased). `width`, `height`, `interlaced`
and `frame_rate` are read from the first video decoder's own files, `/proc/stb/vmpeg/0/xres`,
`yres`, `progressive` and `framerate`, where the image has them - Broadcom receivers do, most
others too - and only while the image's service information itself reports a picture size: a
radio service, an audio file, or a tune or a key that has not come yet report none, and the
files then still hold the last channel's picture, so the members are what the image says, `null`.
A receiver without the directory publishes what it always did. The size is a pair: both values
are the decoder's, or - when it cannot give both - all four are the image's. When the decoder's
size and the image's agree, a scan or frame rate the decoder cannot give (its file is missing,
unreadable, or not a value it can have) is the image's; when they differ the image is describing
another picture, and it is `null`. A size of `0` is the decoder with no picture yet, and then all
four are `null`. The reason is an enigma2 restart: the decoder goes on showing the same
picture, no size event comes, and the image's service information can report a picture that is
not the one on the screen - 1920 x 1080 for a 720p channel - until the next zap. For the same
reason `service` is read again three and eight seconds after the plugin starts and after every
service start, and published if anything changed; then not again until the next start.
**Right after a zap** the picture members are `null` or still the previous channel's for up to a
few seconds, as the decoder itself is: from `evStart` until the decoder raises one of its own
events or the first re-read comes, its files - which still hold the last channel's picture for
about a second - are not read, and the image's service information answers alone.

**The codec members say what is on the decoder, not what the channel list says.** The service
type in the reference says nothing about the codec; `video_codec` is the stream type the image
read from the service's own tables (`iServiceInformation.sVideoType`). Only the values the three
images define for a DVB service are named, and they are the same numbers in OpenViX, OpenATV and
OpenPLi. A stream played through the media player can report others; those are `null`, never a
guess and never the bare number. The members are present on every plugin that has them; the
capability `service_codecs` says the image can fill them in.

| `audio_tracks[]` | Type | Meaning |
|---|---|---|
| `index` | int | The track's position in the image's list, from 0 |
| `description` | string \| null | The image's own word for the track, as it shows it in its audio menu: `MPEG`, `AC3` or `Dolby Digital`, `AC3+` or `Dolby Digital +`. It differs between images, which is why `codec` exists. At most 64 characters |
| `language` | string \| null | As the image gives it: usually an ISO 639 code (`pol`), sometimes the broadcaster's text. At most 64 characters |
| `pid` | int \| null | The track's PID |
| `codec` | string \| null | A stable name derived from `description`: `mp2`, `ac3`, `eac3`, `aac`, `aac_he`, `dts`, `dts_hd`, `lpcm`, `dra`, `ac4`. `null` for a description that is none of the words the three images write for a DVB track - `???` included. An open enumeration |
| `active` | bool | Whether this is the track being played. At most one is; none is when the image does not say, or the active track is past the 32 listed |

`codec` is read off `description` because that is all the image offers: on the measured image a
track has a description, a language and a PID, and nothing that names its type. What each image
writes: `MPEG`, `DTS`, `DTS-HD`, `LPCM` and `DRA` on all three; `AC3`, `AC3+`, `AAC`, `AAC-HE` and
`AC4` on OpenATV and OpenPLi; `Dolby Digital`, `Dolby Digital +`, `AAC-LC`, `HE-AAC` and
`Dolby AC-4` on OpenViX, which also writes `Dolby Atmos` for an E-AC-3 track that carries it
(`eac3` here). The comparison ignores case and surrounding spaces and is otherwise exact.

Whether the service is encrypted is not in this payload: `cam.encrypted` (below) already says it,
on the opt-in `cam` topic, with whether it is being descrambled.

**A service of a bouquet that `bouquets_for_select` leaves out** is published like any other, with
its reference, its name and its codec members; only `bouquet` is `null` for it, as it always was.

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

**The same lists are published a second way, one bouquet a topic.** Added after 0.4.0
(unreleased). This payload is every bouquet with its list, so it grows with the sum of all of
them, and on a receiver with a very large channel list its packet would be over the bound of
1,000,000 bytes (the conventions at the top of this file). It is then **not published** - withheld,
named in `info.not_published`, and a smaller copy retained earlier retracted - and its shape never
changes to make it fit: it is all the lists or it is absent. What a consumer reads instead is
`bouquets`, which names every bouquet, and `channels/<bouquet_slug>`, which carries one bouquet's
list; both are published on every receiver that claims `channel_topics`, whether or not `channels`
fits, so a consumer that reads them never has to care. `cmd/zap` by name resolves against the
complete list either way - what is too big is the packet, not what the receiver knows.

### `<base>/<node>/bouquets` - added after 0.4.0 (unreleased)

The index of the per-bouquet channel topics: every configured bouquet, in the receiver's order,
with the slug of its `channels/<bouquet_slug>` topic and how many services it holds. Retained, and
present when `channel_topics` is a capability. It is small on any receiver - a line a bouquet - so
it is there when `channels` is not.

**Not `bouquet`.** `bouquet`, in the singular (below), is the one bouquet the receiver's
channel-up and channel-down keys are walking right now, and it changes with the remote control.
`bouquets` is the list of all of them and changes when the bouquet files do.

```json
{
  "generated": 1789459200,
  "bouquets": [
    {"name": "Ulubione TV",
     "sref": "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.ulubione.tv\" ORDER BY bouquet",
     "slug": "ulubione_tv",
     "count": 1}
  ]
}
```

| Field | Type | Notes |
|---|---|---|
| `generated` | int | Epoch seconds, when the index last **changed** - a bouquet added, removed, renamed or reordered, or a count that moved - by the rule `channels/<bouquet_slug>` states for its own stamp (below). It is left out of the comparison that decides whether to publish |
| `bouquets[].name` | string | As enigma2 spells it |
| `bouquets[].sref` | string | The bouquet's own reference |
| `bouquets[].slug` | string | The `<bouquet_slug>` of **this bouquet's `channels/<bouquet_slug>` topic**, as it was actually used. `""` for a bouquet that has no such topic, because its name has no letter or digit in it; it is still listed here and still in `channels` |
| `bouquets[].count` | int | How many playable services the bouquet's list has |

The bouquets are the ones `channels` carries, in the same order: `bouquets_for_select` applies to
both. Published when the bouquets are walked and the index differs from the last one, and on every
connect.

**Take the slug from here, never from the name.** For most bouquets it is what
`epg_grid/<bouquet_slug>` uses, but not for all - see the next section - and only this topic says
which it is.

### `<base>/<node>/channels/<bouquet_slug>` - added after 0.4.0 (unreleased)

The services of one bouquet, **one retained topic per configured bouquet**, and present when
`channel_topics` is a capability - which it is whenever `channels` is. The same lists `channels`
carries, published a second way: the size of this payload follows one bouquet, where the size of
`channels` follows the sum of all of them, so these are still there on a receiver whose `channels`
is too big to send. A consumer that wants them all subscribes to `<base>/<node>/channels/+`.

**`<bouquet_slug>`** is `bouquets[].slug` in `bouquets`, and that is where a consumer takes it
from. It is derived from the bouquet's name the way the grid's slug is - lower-cased,
transliterated, every run of other characters one `_` - with one difference. Two bouquets whose
names differ only in punctuation slug the same, and here each must have a topic of its own, or one
list would be nowhere when `channels` is withheld. So **the first such bouquet in the receiver's
order keeps the slug and each later one gets `_2`, `_3` and so on behind it** - the first number
from 2 that neither another bouquet's own slug nor a number already given out has taken, so a
bouquet really called "Sport HD 2" keeps `sport_hd_2` wherever it stands, and a second "Sport
(HD)" beside "Sport HD" then gets `sport_hd_3`. The numbering follows the order: swap the two
bouquets in the receiver's list and their topics swap contents, each payload still saying in
`bouquet` and `sref` whose list it is. The receiver's log has one line for each bouquet that was
numbered, when that changes. A bouquet whose name leaves no slug at all has no topic here, as it
has no grid, and `""` in the index.

The grid does not number: `epg_grid/<bouquet_slug>` slugs each name by itself, and two such
bouquets share one grid topic, as they always have. So for a numbered bouquet the two slugs
differ, and a channel topic's slug must come from `bouquets`, never from the grid's or the name.

```json
{
  "bouquet": "Ulubione TV",
  "sref": "1:7:1:0:0:0:0:0:0:0:FROM BOUQUET \"userbouquet.ulubione.tv\" ORDER BY bouquet",
  "generated": 1789459200,
  "channels": [{"sref": "1:0:19:283D:3FB:1:C00000:0:0:0:", "name": "TVP 1 HD"}]
}
```

| Field | Type | Notes |
|---|---|---|
| `bouquet` | string | The bouquet's name as enigma2 spells it - not the slug |
| `sref` | string | The bouquet's own reference, `bouquets[].sref` in `channels` and in `bouquets` |
| `generated` | int | Epoch seconds, when this bouquet's list last **changed** - see below. It is left out of the comparison that decides whether to publish |
| `channels[]` | list | What `bouquets[].channels[]` holds in `channels`: `sref` and `name`, playable services only, in the user's own order |

Rebuilt when `channels` is, and published only when the bouquet's list changed, so a bouquet
nobody edited keeps its retained payload while another one moves.

**`generated` says when this bouquet last differed, as far as this run of the plugin knows.** The
plugin keeps one stamp for each bouquet: the time of the walk at which its name, its reference or
its list was first found different from the walk before. A walk that finds the bouquet unchanged
keeps the stamp, and so does a reconnect, which sends every topic again with the stamp it had -
unlike `channels`, whose `generated` is the time of the last walk. The stamps are kept in memory
only. **A start of the plugin - a restart of the interface, a reboot - or a save of its settings
that reconnects begins them anew** (a save that changes no connection setting keeps them): every
bouquet is then stamped with the first walk of that run, and the connect
that follows publishes that, whether or not anything was edited. So a `generated` that moves is
not proof of an edit; one that stays is proof of none since it was set.

**Slugs that stop being configured are retracted**, as the grid's are, from the same state file:
drop a bouquet from `bouquets_for_select`, remove it, or rename it - which changes its slug - and
the topic it used to own gets an empty retained payload, at once on an open session and otherwise
on the next connect, also when the plugin was restarted in between.

A single bouquet whose list does not fit one packet is not published and is named in
`info.not_published` as `channels/<bouquet_slug>`; the other bouquets are unaffected, and the
index still lists it with its `count`.

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
  "events_per_channel": 4,
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
| `events_per_channel` | int | The most events a channel carries in this payload: `epg_grid_events`, or fewer when the grid was cut to fit one packet - see below. Added after 0.4.0 (unreleased) |
| `channels[].sref` | string | |
| `channels[].name` | string | |
| `channels[].events[]` | list | Up to `events_per_channel` entries per channel, chronological |
| `events[].title` | string | |
| `events[].begin`, `events[].end` | int | Epoch seconds |
| `events[].event_id` | int | |

Refreshed when a bouquet changes, every 15 minutes, and on `cmd/epg_grid` - which regenerates and
republishes **every** configured bouquet, not just one. `epg_grid_events` is a setting, default
**4**; `0` turns the topics off entirely and drops `epg_grid` from `capabilities`.

A channel can carry fewer than `epg_grid_events` events: the plugin asks the EPG cache for a
bounded window of time rather than for a number of events, because that is the question the cache
takes, and a channel showing a three-hour film has one event in it.

**A grid that does not fit one packet is cut, not dropped.** Added after 0.4.0 (unreleased). When
the packet of a bouquet's grid would be over the bound of 1,000,000 bytes (the conventions at the
top of this file), the plugin publishes that bouquet's grid with **the largest number of events
per channel that fits**, from `epg_grid_events` down to 1. Each channel
keeps its earliest events; the EPG cache is not asked again. `events_per_channel` is the number
the payload was built with: equal to `epg_grid_events` on a grid that was not cut, and lower on
one that was, which is how a consumer tells "this channel has two programmes left today" from "this
grid was cut to two". The other bouquets keep their own count. The receiver's log has one line
when a bouquet's grid is first cut or the cut moves - `epg grid: Astra is cut from 4 to 2 event(s)
per channel to fit one packet`. Only a grid that does not fit with one event on each channel is
not published, and is named in `info.not_published`.

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

A settings save on the receiver that changes the connection while an import runs restarts the
bridge, and the new session reports `started` as the moment it first saw the import; any other
save leaves the import's `started` as it was.

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

### `<base>/<node>/stream` - added after 0.4.0 (unreleased)

Where the image's stream server is and what it wants. Every enigma2 image serves a service's
transport stream to a client that asks for it over HTTP; this topic carries the facts a consumer
needs to ask. The plugin starts no stream and carries no video. Retained, and present when
`stream` is a capability - the image has a stream server (`eStreamServer`).

```json
{"port": 8001, "path": "/{sref}", "authentication": false, "encoder": true,
 "encoder_port": 8002}
```

| Field | Type | Meaning |
|---|---|---|
| `port` | int | The port the stream server listens on: `8001`. It is not a setting - OpenViX, OpenATV and OpenPLi each hard-code it where the server is built - so it is the same on every receiver, and is published so that a consumer does not have to know that. OpenWebif's own "streaming port" setting is not this and is not published: it is what OpenWebif writes into the links it hands out |
| `path` | string | The form of the request path, with `{sref}` where the service reference goes: `/{sref}`. The reference goes in as it is published - colons and the trailing colon included |
| `authentication` | bool or `null` | Whether the stream server asks for a login (`config.streaming.authentication`). `null` where the image has no such setting. **Never the login itself** - see below |
| `encoder` | bool | Whether the receiver has a hardware encoder: one of `/dev/bcm_enc0`, `/proc/stb/encoder/0`, `/dev/encoder0` exists. Looked for once, when the plugin starts |
| `encoder_port` | int or `null` | The port of the image's transcoding plugin (`config.plugins.transcodingsetup.port`), where the receiver has an encoder and the image has that setting as a plain number. `null` otherwise - never a guessed 8002 |

**There is no host and no URL in it.** The receiver's address is `info.ip`; a stream's address is
`http://` + `info.ip` + `:` + `port` + `path` with the reference filled in -
`http://192.0.2.12:8001/1:0:19:283D:3FB:1:C00000:0:0:0:`. A receiver reached by another name
or through a forwarded port is the consumer's business - the Home Assistant side knows how it
reaches the receiver, and the receiver does not.

**No credential is ever published.** `authentication` is a boolean. With it on, the stream server
wants HTTP basic authentication with an account of the receiver; which account, and its password,
stay on the receiver, and whoever configures the consumer types them there.

Published on every connect, and again when a value changed: the two settings behind
`authentication` and `encoder_port` are read from memory every five seconds, with the look
`stream_clients` takes, and the topic is sent only when it differs. The plugin does not test
that anything answers on the port.

### `<base>/<node>/stream_clients` - added after 0.4.0 (unreleased)

Who the stream server is serving right now, from the image's own list. Retained, and present
when `stream_clients` is a capability - the image's stream server lists its clients
(`eStreamServer.getConnectedClients`).

```json
{"count": 2,
 "clients": [{"host": "192.0.2.20", "sref": "1:0:19:283D:3FB:1:C00000:0:0:0:",
              "name": "TVP 1 HD", "bouquet": "Favourites (TV)", "transcoded": false},
             {"host": "2001:db8::17", "sref": "1:0:19:283E:3FB:1:C00000:0:0:0:",
              "name": "TVN HD", "bouquet": null, "transcoded": true}]}
```

| Field | Type | Meaning |
|---|---|---|
| `count` | int | How many clients the image lists |
| `clients` | list | One row for each, in the image's order, **at most 32**. With more, the list is cut there and `count` still says how many there are. `[]` with none |
| `clients[].host` | string | The client's address. An IPv4 client, which the image may hold as `::ffff:192.0.2.20`, is shown as `192.0.2.20` - only a well-formed dotted quad is unwrapped; any other address is shown as the image holds it. At most 64 characters |
| `clients[].sref` | string or `null` | The service reference being streamed, as the image holds it. `null` when it holds none, and when what it holds is longer than 512 characters, which no reference of a receiver is. A row without a reference is still listed and counted, has no `name` and no `bouquet`, and **cannot be stopped**: `cmd/stream_stop` names a client by its reference |
| `clients[].name` | string or `null` | The name the `service` topic uses for the same channel |
| `clients[].bouquet` | string or `null` | As `service.bouquet`: the first of the configured bouquets (`bouquets_for_select`) that holds the service, `null` when none does. It and `name` follow the channel list: when the bouquets are walked again - at a start, after a save of the settings, after an edit - the rows are resolved again at the next look, so within five seconds |
| `clients[].transcoded` | bool | Whether the client is served through the receiver's encoder |

**Polled, not hooked.** A client that connects or leaves raises no event the three images share,
so the image's list is read every five seconds - one call that returns a short list - and the
topic is published only when the list changed; names and bouquets are resolved only then, and
when the channel list was walked. A client that came and went between two looks was never on
the topic. After a `cmd/stream_stop` the list is read
again two seconds later, without waiting for the next look.

**Everything is published.** Every client of every bouquet is on the broker, retained, as every
entry is on `zap_history` and every channel is on `channels`. Hiding a bouquet from a list in a
consumer hides nothing here: a client watching a channel of a bouquet that `bouquets_for_select`
leaves out is listed with its reference and its name, and a `null` `bouquet`. The plugin's
OpenWebif page shows this payload as it is, to whoever OpenWebif admits. There is no setting for
this topic: it is published wherever the image's stream server lists its clients.

**What is retained is the list the image holds now, or nothing.** A consumer offers to stop what
this topic lists, so a list that names a client that has left is worse than none. The first
publish after every connect - a new session after a broker outage, and the first one of a plugin
that has just started - is a fresh read of the image's list, never the one the plugin held
before. When the list cannot be read, the topic is emptied - an empty retained payload - rather
than left saying what it last said, and it is published again with the next read that works.
"Cannot be read" is a call that raises and an answer that is not a list of rows - nothing, a
string, a row that is no host, reference and flag; only an empty list is "no clients". And a
plugin that starts on an image where this area does not bind - no stream server, or one that
does not list its clients - empties what an earlier run left here when it connects, and
`stream` with it when there is no stream server at all.

**It holds only while `availability` is `online`.** Like every state topic here, it stays
retained when the plugin stops or the receiver is switched off - nothing is retracted at a stop,
`offline` says it - so a consumer offers nothing from this list for a receiver that is not
`online`.

On OpenATV the list also holds the clients of its RTSP server (read from its source, not
measured), which `cmd/stream_stop` cannot end: the image does not look for them there, and the
command is refused with `not_stopped`.

### `<base>/<node>/stream_playable` - added after 0.4.0 (unreleased)

**Not retained** - QoS 0. The answer to one `cmd/stream_playable` (section 2): whether the service
asked about could be played now, beside everything the receiver is already doing. Present when
`stream_playable` is a capability
([ADR-0017](adr/0017-whether-a-service-can-be-played-is-asked-with-an-empty-second-reference.md)).

```json
{"sref": "1:0:19:283F:3FB:1:C00000:0:0:0:", "playable": false, "ts": 1789459213,
 "id": "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90"}
```

| Field | Type | Meaning |
|---|---|---|
| `sref` | string | The service reference that was asked about, as it was sent without the spaces around it |
| `playable` | bool | Whether the image says the service could be played now without taking a tuner from anything that is running - the television, a recording, another stream: `true` for a positive number from the image, `false` for zero. The image gives no reason with a `false` |
| `ts` | int | When it was asked, in epoch seconds |
| `id` | string, optional | The id the command carried in its envelope (section 2), exactly as it was sent. Absent, not `null`, when it carried none. **A question without an id is answered all the same**, and its sender tells the answer by `sref` |

One message for each question, including the same question asked twice. **It is true for the
moment it was asked**: a recording that starts a second later takes the tuner the answer counted
on. That is why the topic is not retained and goes out at QoS 0 - an answer replayed by a
reconnect, or delivered late from a queue, would be an answer about tuners that have moved on. A
consumer asks just before it opens a stream, and treats no answer within a few seconds as
"unknown", not as "no".

A question that could not be asked - no reference, text that is none, a reference that is not
one playable service, an image that will not say or answers with something that is no number or a negative
one - is a refusal on `last_error`, with the command's id, and no message here.

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

**Each entry of `active` carries its details** - added after 0.4.0 (unreleased), capability
`recording_details`, claimed with `recording`. `next` keeps the four members above.

```json
{"name": "Title", "sref": "1:0:19:283D:3FB:1:C00000:0:0:0:", "begin": 1789459200,
 "end": 1789460700, "service_name": "Channel", "description": "Short text",
 "location": "/media/hdd/movie", "file": "Series/20261017 2015 - Channel - Title.ts",
 "id": "3f9c2e4a1b7d5c60", "instant": false, "bouquet": "Favourites"}
```

| Member | Type | Meaning |
|---|---|---|
| `service_name` | string or `null` | The channel's name, as the receiver resolves the reference |
| `description` | string or `null` | The timer's short description, its whitespace collapsed and cut at 200 characters |
| `location` | string or `null` | The movie location the recording is written into - one of `recordings.locations[].path` (below), the outermost that holds it. `null` before the receiver has named the file, and for a file outside every movie location |
| `file` | string or `null` | The file's path relative to that location, the subfolder included; `null` with no location |
| `id` | string or `null` | The id the recording library gives the file (`recordings/<folder_slug>`, below) - the same id once the library lists it. Also for a file outside every location; `null` only before the receiver has named the file |
| `instant` | bool | `true` for a recording started by hand - the remote's record key, or `cmd/record start` - and `false` for one a timer started. The record key's recording is marked by the image itself. `cmd/record start` sets an ordinary timer, which the receiver keeps in its timer file, and the plugin marks it - a mark the image does not keep, so after an interface restart that recording reads `false` |
| `bouquet` | string or `null` | The first published bouquet that holds the channel - the value `service.bouquet` carries - or `null`. What a consumer that hides bouquets hides a recording by |

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

### `<base>/<node>/timeshift` - added after 0.4.0 (unreleased)

```json
{"active": true, "paused": false, "behind": true, "since": 1792050000}
```

Retained, capability `timeshift`: the timeshift buffer of the service being watched.

| Field | Type | Meaning |
|---|---|---|
| `active` | bool | A timeshift buffer runs |
| `paused` | bool or `null` | The picture is paused - the info bar's play state. `null` when no buffer runs, or when the image does not say |
| `behind` | bool or `null` | Playback is from the buffer, behind live, rather than live. `null` when no buffer runs, or when the image does not say |
| `since` | int or `null` | When the buffer started: the image's own start where it keeps one (OpenViX and OpenATV), otherwise when the plugin first saw the buffer running - after a restart of the plugin, when it next saw it. `null` when no buffer runs |

With no buffer, `active` is `false` and the rest is `null`: `{"active": false, "paused": null,
"behind": null, "since": null}`. A recording being played back has no buffer.

**Published when it changes, never read on a timer.** The image sends no event when a buffer starts
or stops: its info bar starts one from a key, from the permanent-timeshift setting or after a zap,
and stops it the same way. So the state is read again on the image's own events - a service
starting or ending, playback moving into or out of the buffer, the buffer moving on to its next
file (OpenViX and OpenATV) and the info bar's pause and play - and on the info bar's own calls that
start and stop a buffer, which the plugin wraps on the one info bar there is. A buffer the service
starts in some other way is seen at the next of those events. The only timer looks for the info
bar, on an image that builds it after the plugin has started, and stops when it has found it.

### `<base>/<node>/recordings` - added after 0.4.0 (unreleased)

```json
{"read": 1792050000, "reason": "start",
 "locations": [{"path": "/media/hdd/movie", "mounted": true, "free_mb": 412330,
                "folders": [{"slug": "movie", "path": "", "count": 41, "size_mb": 98211, "cut": 0},
                            {"slug": "movie_series", "path": "Series", "count": 12,
                             "size_mb": 30110, "cut": 0}]}]}
```

Retained, capability `recordings`: the index of the receiver's recording library - every recording
in every movie location, with its subfolders. The recordings themselves are on one topic for each
folder, `recordings/<folder_slug>` (below).

| Field | Type | Meaning |
|---|---|---|
| `read` | int | When the library was last read |
| `reason` | string | Why it was read: `start` - the plugin started; `recording` - a recording started or ended; `delete` - `cmd/delete_recording` moved one into the trash; `mount` - a movie location was mounted; `refresh` - `cmd/recordings_refresh`. An open enumeration |
| `locations` | list of objects | Each movie location: first the image's default movie location (`config.usage.default_path`), then the movie list's locations in the image's order |
| `locations[].path` | string | The location as the receiver's settings name it, without a trailing slash |
| `locations[].mounted` | bool | Whether it is on a mounted file system of its own, read from `/proc/mounts`: not the root file system, and not an automounter's entry waiting to mount a share |
| `locations[].free_mb` | int or `null` | Free space, in MiB; `null` when it is not mounted |
| `locations[].folders` | list of objects | The location's folders that hold at least one recording - the location itself and every subfolder, flattened |
| `folders[].slug` | string | The folder's topic, `recordings/<slug>`: the location's last path element, then the subfolder's path - `movie`, `movie_series` - made as a bouquet's slug is, and unique in the node: a second folder with the same slug gets `_2` |
| `folders[].path` | string | The folder relative to its location; `""` for the location itself |
| `folders[].count` | int | How many recordings `recordings/<slug>` lists |
| `folders[].size_mb` | int | Their total size, in MiB |
| `folders[].cut` | int | How many recordings were left out of the folder's topic for the packet bound - its oldest. `0` nearly always |

**What a recording is.** A `.ts` or a `.stream` file in a movie location or under it. Other media
files the image's movie list shows are not recordings. The movie locations are the image's default
movie location, then its movie list locations in the image's order. The image's trash (`.Trash`) and every other name starting
with a dot are left out; a folder that is a link is not followed; a location inside another is
listed once, under the outer one; and a location reached through a link on the receiver's flash -
`/hdd/movie` for `/media/hdd/movie` - is read at the path the link leads to, which is the path the
ids are made from.

**When it is read - never on a timer.** A recording disk spins down when nobody uses it, and
reading the library is the one thing that reads its folders and the files beside each recording.
So the library is read once when the plugin starts, and then only when something changed it: a
recording started or ended (read once three seconds have passed without another such event), a
delete this plugin made, and a movie location that was mounted - and when `cmd/recordings_refresh`
asks, which is the only read that may wake a disk on purpose. Whether each location is mounted is
read from `/proc/mounts`, which touches no disk, and its free space with `statvfs`, which a disk
answers without spinning up and which is asked only of a location that is mounted - both on the
minute timer of the `hdd` topic, in its thread. A change of free space alone republishes the index
and nothing else. One read runs at a time: an event during a read is read again after it, and a
refresh during a read is answered by that read.

A location that is **not mounted** is not read: it keeps the recordings it had when it was last
read, with `mounted` `false` - an automounted network share is unmounted when it has been idle,
and its recordings are not gone. A refresh reads it anyway, which mounts such a share. **Changes
made on the receiver itself** - a recording deleted, moved or renamed in its movie list, through
OpenWebif or over FTP - raise no event, and show after the next of the events above or a refresh.

**The receiver lists everything.** Every recording is listed and counted - also one of a channel
in no published bouquet. Which recordings a consumer shows is the consumer's to decide: each entry
carries `sref` and `bouquet` for that, as the zap history's entries do. The receiver publishes the
whole library, titles and descriptions included, to anybody on the broker, as it publishes the
channel lists and the zap history.

### `<base>/<node>/recordings/<folder_slug>` - added after 0.4.0 (unreleased)

```json
{"recordings": [{"id": "3f9c2e4a1b7d5c60", "file": "20261017 2015 - Channel - Title.ts",
                 "name": "Title", "description": "Short text", "service_name": "Channel",
                 "sref": "1:0:19:283D:3FB:1:C00000:0:0:0:", "bouquet": "Favourites",
                 "begin": 1792050000, "duration": 3600, "size_mb": 2210}]}
```

Retained, capability `recordings`: one topic for each folder of the index, and the set of these
topics follows the index exactly. A folder that goes - its last recording deleted, or the folder
itself - has its topic retracted, the empty retained payload; so has one that went while the
plugin was not running, which the plugin finds in its state file (§4).

| Field | Type | Meaning |
|---|---|---|
| `recordings` | list of objects | The folder's recordings, newest first |
| `recordings[].id` | string | The recording's id: the first 16 hex digits, lowercase, of the sha256 of its full path - the path's bytes on the receiver's file system. What every command takes; a command never names a file |
| `recordings[].file` | string | The path relative to the movie location - not to the folder - the subfolder included: `Series/20261017 2015 - Channel - Title.ts` in the folder `Series`, the bare file name at the location's top level, as in the example above. The same as `recording.active[].file` for the same recording. A name that is not UTF-8 is shown with replacement characters |
| `recordings[].name` | string | The title: the one in the recording's `.meta` file, otherwise the title in the image's own file name (`YYYYMMDD HHMM - Channel - Title`), otherwise the file name. Cut at 200 characters |
| `recordings[].description` | string or `null` | The short description in the `.meta` file, cut at 200 characters |
| `recordings[].service_name` | string or `null` | The channel's name as the receiver resolves `sref`, otherwise the channel in the image's file name |
| `recordings[].sref` | string or `null` | The recorded service, from the `.meta` file |
| `recordings[].bouquet` | string or `null` | The first published bouquet that holds the service - the value `service.bouquet` carries - or `null`: what a consumer that hides bouquets hides a recording by |
| `recordings[].begin` | int or `null` | When the recording was made: the `.meta` file's time, otherwise the file's change time |
| `recordings[].duration` | int or `null` | Seconds: the `.meta` file's length, otherwise from the first and the last entry of the `.ap` file; `null` when neither says |
| `recordings[].size_mb` | int | The file's size, in MiB |

**A folder over the packet bound is cut.** Its newest recordings are kept and its oldest left out
until the packet fits under 1,000,000 bytes; `folders[].cut` in the index says how many, and the
recordings left out are not in the library that the commands act on. At about 300 bytes a
recording, a folder holds about three thousand before that happens.

### `<base>/<node>/volume`

```json
{"level": 35, "muted": false, "passthrough": false}
```

`level` is an integer 0-100, `muted` a boolean. Published on every volume change from any source
- the remote, OpenWebif, the plugin itself - and reconciled on a short timer, because enigma2 has
no single choke point every volume change passes through.

**`passthrough` says whether the volume changes anything.** Added after 0.4.0 (unreleased),
[ADR-0019](adr/0019-passthrough-is-true-false-or-unknown.md). A receiver can hand AC-3, AAC and
other encoded audio to the television undecoded - "passthrough" in its audio settings - and then
its own volume moves a bar on the screen and nothing anybody can hear. The member is about the
audio track **being played now**:

| `passthrough` | Meaning |
|---|---|
| `true` | The image is set to pass that track's codec to the television undecoded: the receiver's volume and mute do nothing audible, and the television's or the amplifier's are the ones to use |
| `false` | The receiver decodes the track, so its volume acts on it: the image is set to "downmix" for that codec, or the codec is one the receiver always decodes (`mp2`, `lpcm`) |
| `null` | It cannot be told: nothing is playing, the track's codec is unknown (`service.audio_tracks[].codec` is `null`), the image has no setting for that codec, or the setting is a choice other than those two |

It is the image's setting for the active track's codec, read from memory - `config.av.downmix_ac3`
for `ac3`; `downmix_ac3plus` (OpenViX) or `transcodeac3plus` (OpenATV, OpenPLi) for `eac3`;
`downmix_aac` for `aac`; `downmix_aacplus` for `aac_he`; `downmix_dts` for `dts`; `dtshd` for
`dts_hd`. The value is a word on some images and a boolean on others, and only the two certain
answers are taken: `passthrough`, and `downmix` or the boolean that stands for it. Every other
choice an image offers - "use the best the television supports", "convert to multi-channel PCM",
a transcode to another codec - is `null`, because what reaches the television then depends on the
television. So is `ac4` and `dra`, for which no image has such a setting, and `aac_he` on a
receiver without a setting of its own for it. AAC has a second switch beside its downmix setting,
`config.av.transcodeaac` (`off`, `ac3` or `dts` on all three images): with it on anything but
`off`, `aac` and `aac_he` are `null` whatever the downmix setting says, because a transcode is
neither of the two answers. And on the two OpenATV models whose `downmix_ac3` is `0`, `1` or `2`
(dreamone, dreamtwo) `ac3` is `null`: those are not the two words.

**It says what the receiver is set to do, not what the television does with it.** A television
that cannot take the bitstream is the television's business; the receiver still sends it.

A consumer greys out or relabels its volume control on a stated `true`, and treats `null` as an
older plugin that publishes no such member: unknown, not "no". The capability
`volume_passthrough` says the image has at least one of those settings.

The look is cheap: it asks which track is playing, and reads that one track's description only
when the track or the service is not the one it last looked at - never every track, and nothing
of a track on a volume key press. So that an answer cannot outlive its track, the description is
also read again every sixth reconciliation, and at the next one after a read that failed.

It follows the track: read again at once on `evStart`, `evUpdatedInfo` and `evEnd`, and - because
neither a track picked on the remote nor a setting changed in the image's menu announces itself -
with every reconciliation of the volume, so within five seconds of either. **The bounds**: a
track picked on the remote, a changed setting and a track that could not be read at first -
five seconds; a track that became another codec under the same index with no service event,
which only a broadcaster changing its audio on the fly produces - thirty seconds; and on a
receiver whose session announces no service events at all, five seconds for everything.

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
recording decisions. The file holds the last ECM of any descrambled service, so while the receiver
also descrambles another encrypted channel - a stream to a client, for example - `system` and
`ecm_ms` can describe that channel rather than the one on the television.

The softcam removes the file for a few seconds now and then while it is still decoding, for example
when a descrambled stream to a client ends. While the file is absent, the last fresh result - its
`system` and `ecm_ms`, with `active: true` - is published until 30 seconds have passed since that
ECM was written, and `null` after that, as before. A file that is there counts for 15 seconds
after its ECM, as before. A zap ends it at once, and so does a poll that
finds the service free-to-air or cannot tell whether it is encrypted, or finds the file there but
stale, unreadable or replaced by a link. Changed after 0.4.0
(unreleased): until then an absent file was `null` at once.

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

### `<base>/<node>/process` - since 0.3.0

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

### `<base>/<node>/menu_action` - added after 0.4.0 (unreleased)

**Not retained** - QoS 1. One message for each OK on a row of the receiver menu that can be acted
on ([§3](#the-receiver-menu---added-after-040-unreleased-capability-menu)), published while
`menu` is a capability. It says which row and what was done to it, and nothing about what that
means: the companion integration looks the row up in its own configuration and performs the
action ([ADR-0020](adr/0020-the-receiver-menu-is-drawn-on-the-receiver-and-decided-in-home-assistant.md)).

```json
{"id": "3f9c2e4a1b7d", "rev": 42, "row": "r_blind", "action": "set", "value": 60, "ts": 1792050000}
```

```json
{"id": "3f9c2e4a1b7d", "rev": 42, "row": "r_lamp", "action": "toggle", "seen": false, "ts": 1792050000}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Twelve lowercase hexadecimal digits, new for every action - for logs and to tell a repeat |
| `rev` | int | The `rev` of the menu the viewer acted on |
| `row` | string | The row's `id` |
| `action` | string | `press` for a `button`, `toggle` for a `toggle`, `set` for a `selector`, a `slider` and an `input` |
| `value` | string or number | `set` only, absent otherwise: the option chosen, exactly as the menu listed it; the slider's value, `min` plus a whole number of `step`s, an integer when `min` and `step` are; the text typed, cut at the row's `max_length` |
| `seen` | `true`, `false` or `null` | `toggle` only, absent otherwise: the `value` the row showed when the viewer pressed OK - the value drawn on the screen at that moment, which is the `value` of the last menu payload the receiver applied for that row. `null` when the row showed no state. Never sent with another action |
| `ts` | int | When, in epoch seconds by the receiver's clock. The companion integration refuses an action older than 120 s |

**What Home Assistant does with a `toggle`.** It finds and checks the row exactly as for any action
- its own configuration, the row's type, availability, the 120 s age - and then acts on the state
the viewer saw rather than on the state the entity has when the message arrives, so an OK that
arrives late, or twice, repeats what the viewer meant and never inverts it:

| `seen` | light, switch, input_boolean, fan | cover | lock |
|---|---|---|---|
| `false` | `turn_on` - unless the entity is already on | `open_cover` - unless already open | `lock` - unless already locked |
| `true` | `turn_off` - unless already off | `close_cover` - unless already closed | `unlock` - unless already unlocked |
| `null` or absent | `toggle`, unchanged | unchanged: by the state at arrival, refused `state_unknown` when it has none | unchanged |

"Already" is judged by the same reading of the state that builds the row's `value` - a cover is
"open" exactly when its row would show `true`. An action whose target is already met calls nothing,
logs one debug line, and publishes the menu again, so the receiver redraws the row from the current
state. A `seen` of another type, or a `seen` on another action, is refused as malformed: one line
in the integration's log, and no action. `seen` is optional on the wire, so a development build that
does not send it gets the behaviour of the `null` row. The receiver still compares `value`s, never
`rev`.

**Never sent** for an `indicator`, for a row with `available: false`, while the menu is greyed
(no broker connection, or Home Assistant's status `offline`), with `menu_allowed` off, for a row
the current menu does not hold with that type, for a row of a type the plugin does not know, or
for a `set` of the value the row already has. Nothing on the receiver waits for an answer: the
answer is the menu published again with the row's new `value`. Not retained, so a reconnect
replays no action, and QoS 1, so the broker takes the one that was sent. **Never queued across a
disconnect**: an OK without a broker connection is the greyed menu, and sends nothing; an action
the client had handed over and the broker had not acknowledged when the connection went is taken
out of the client's queue rather than sent after the reconnect, minutes late - by the MQTT client
itself as the broker accepts the new connection, before it sends anything again.

The broker's ACL does not protect this topic in every set-up, so the integration treats it as
untrusted input and acts only on a row its own current configuration holds, with the type that row
has and a value it validates. Whoever can publish here can do exactly what the menu offers, and
nothing else.

### `<base>/<node>/menu_edit` - added after 0.4.0 (unreleased)

**Not retained** - QoS 1. One message for each save on the menu creator, the OpenWebif page that
builds the receiver menu ([§3](#the-menu-creator---added-after-040-unreleased-capability-menu_editor)),
published while `menu_editor` is a capability: the whole menu as the page left it, and the catalogue
revision it started from. The companion integration validates it with the rules of its own menu
options, stores it or refuses it, and answers on `enigma2mqtt/menu_edit_result/<node>`.

```json
{"id": "8c1d2e3f4a5b", "base": "3f9c2e4a1b7d5c60", "ts": 1792050000,
 "menu": [
   {"type": "group", "id": "g_0a1b2c3d4e", "label": "Living room", "icon": "sofa",
    "rows": [
      {"type": "row", "id": "r_5f6a7b8c9d", "entity": "light.living_room", "control": "toggle",
       "label": null, "icon": null},
      {"type": "row", "id": null, "entity": "cover.living_room_blind", "control": "slider",
       "label": "Blind", "icon": "blinds"}]},
   {"type": "group", "id": null, "label": "Garden", "icon": "garden", "rows": []}]}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Twelve lowercase hexadecimal digits, new for every save; the answer says it back |
| `base` | string | The catalogue's `revision` the edit started from. Home Assistant refuses an edit whose `base` is not the menu's current revision |
| `ts` | int | When, in epoch seconds by the receiver's clock. For the log only: a save has no time limit |
| `menu` | list | The whole menu, in the catalogue's `menu` form: groups and rows. A group or row that exists carries its `id`; a new one carries `null`. `missing` is never sent |

The receiver checks only the envelope - a revision, a list, at most 65,536 bytes encoded - and that
the creator is offered; whether an entity may be bound, which control it takes and what a label may
say is Home Assistant's to decide. **Never queued across a disconnect**, like `menu_action`:
published on the open session only, and one the broker had not acknowledged when the connection
went is taken out of the client's queue. The page's save then says so at once.

### `<base>/<node>/list_answer` - added after 0.4.0 (unreleased)

**Not retained** - QoS 1. The answer to one `cmd/list` (section 2): how the list of choices on the
television ended, and which row was picked. Present when `list` is a capability
([ADR-0023](adr/0023-a-list-of-choices-is-answered-by-row-id-once.md)). **Every list the plugin
accepted - shown, or waiting for the screen - has exactly one answer here, or, when it could not be
put on the television after all, one late refusal on `last_error` instead** (`not_shown`, section
2). A list refused when it arrived has neither: its refusal is on `last_error` at once.

```json
{"id": "a1b2c3d4e5f60718", "ending": "picked", "row": "open", "ts": 1792050000}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string, optional | The id the command carried in its envelope (section 2), exactly as it was sent. **Absent**, not `null`, when it carried none or one the plugin could not use: such a list is shown and answered all the same |
| `ending` | string | `picked` - OK on a row, or the row's own key; `timeout` - the list's `timeout` ran out; `dismissed` - EXIT; `replaced` - a newer `cmd/list` arrived while this one was on the screen or waiting for it; `closed` - the receiver went to standby, the plugin stopped (an interface restart, a shutdown, a settings save that reconnects, an update, the plugin being removed) or something else closed the screen. An open enumeration |
| `row` | string or null | `picked`: the `id` of the row picked. `timeout`: the `id` of the list's `default` row, or `null` when it has none - so a timeout is never mistaken for a press. Every other ending: `null` |
| `ts` | int | When the list ended, in epoch seconds by the receiver's clock |

The receiver says which row id was picked and nothing else: it never learns what a row does and
never calls a service. **Never queued across a disconnect**, like `menu_action`: published on the
open session only, and one the broker had not acknowledged when the connection went is taken out
of the client's queue - by the MQTT client itself as the broker accepts the new connection, before
it sends anything again. A pick made while the broker cannot be reached is lost, and its sender's
wait ends without an answer: a late "open the gate" is worse than none. `closed` is published only
while the session is up at that moment; nothing is sent later. Anyone who can publish on the broker
can publish here too, so a consumer never takes an answer as proof of who pressed what
([SECURITY.md](../SECURITY.md)).

### `<base>/<node>/update` - since 0.4.0

Retained. Which releases of the plugin this receiver could install, read from the signed release
index ([RELEASE-INDEX.md](RELEASE-INDEX.md), [ADR-0015](adr/0015-signed-self-update.md)) and from
nowhere else: a release the index does not name is not here. Published on every connect by every
receiver, whatever its settings, and again when an index is accepted, when a check ends and when
`ha_mode` changes.

```json
{"origin": "reachable", "checked": 1790410000, "check_error": null,
 "index": {"serial": 7, "issued": 1790500000, "source": "origin"},
 "latest_compatible": "0.4.1",
 "available": [{"version": "0.4.1", "compatible": true, "reason": null},
               {"version": "0.4.0", "compatible": true, "reason": null}],
 "transaction": null}
```

| Field | Type | Meaning |
|---|---|---|
| `origin` | string | `reachable`, `unreachable` or `unknown`: whether the receiver's last probe of the release origin - a verified HTTPS request for the index's signature file, five seconds - got an answer. `unknown` until the receiver has probed, which it does only when it checks (below) or when an install is started at the television or on the OpenWebif page (§2, "A receiver without internet"); an update helper whose download got no answer from the origin makes it `unreachable` too |
| `checked` | int or `null` | When the receiver last checked, epoch seconds. Stamped when the check starts, so a check that failed has a time too. Volatile in the sense of [ADR-0006](adr/0006-volatile-fields-and-publish-on-change.md): a check that changed nothing else does not publish the topic again |
| `check_error` | string or `null` | Why the last check brought no index, `null` when it did or found the one already held: `unreachable`, `redirect` (the origin answered with a redirect, which is never followed), `http_error`, `too_large`, `trust_busy` (the index verified, but another writer held the trust file's lock for longer than two seconds, so it was not kept), `write_failed` (the index verified but could not be kept - a full flash - so it is not held, and the next check will say the same until there is room), `bad_memory` (what the receiver keeps about the index cannot be read, and nothing is judged until it is removed - [RELEASE-INDEX.md](RELEASE-INDEX.md), "Loading it"), `internal_error`, or a reason code of the rule for accepting an index (`bad_signature`, `replay`, `rank`, `jump` and the others RELEASE-INDEX.md lists). An open enumeration |
| `index` | object or `null` | The index this receiver holds: `serial`, `issued` (epoch seconds; when it was built - its age, never an expiry) and `source`, `origin` (fetched by the receiver) or `relay` (relayed on `enigma2mqtt/release_index`, §3). `null` while it holds none |
| `latest_compatible` | string or `null` | The newest compatible release in `available`; `null` when none is |
| `available` | list | The twenty newest releases at or above the floor - the index's, raised by the integration's `plugin_min` (§3) - that are not withdrawn, newest first, as `{"version", "compatible", "reason"}`. `reason` is `null` when compatible; otherwise `incompatible` - another contract major than this plugin's or the integration's, or a release that needs a newer integration than the one on `enigma2mqtt/integration/<node>` (§3), or, while that topic says nothing, one that needs any integration while `ha_mode` is `integration` - or `depends`, a package the release needs is not installed (when opkg's database cannot be read the dependencies are not judged and are taken as met here; the install checks them again). An open enumeration. The running release and older ones are listed too: whether one may be installed, and from where, is the install's business |
| `transaction` | object or `null` | An update in progress, or the last one that ended - `null` when this receiver never ran one. Below |

**When the receiver checks.** Only when asked. With `update_check` on (the read-only setting
above), once a day - after its clock has been set - and on `cmd/update_check`; from the plugin's
OpenWebif page ("Check for plugin updates now") and from the receiver's own "Plugin updates"
screen whatever the setting says, because whoever the page admits, or holds the remote control,
could switch it on anyway. Manual checks share one ten-minute limit, inside which the last
result is the answer and nothing is fetched. With `update_check` off and nobody at the page or
the television, the receiver makes no connection but the broker's. A check fetches the signature
file first (the probe, 1 KiB), then the index (ten seconds, 64 KiB), from the origin built into the plugin, over
TLS that verifies the certificate and the host name, and never follows a redirect. An index relayed
on `enigma2mqtt/release_index` (§3) is judged by the same rule, needs no setting and causes no
connection.

**`transaction`** - the update `cmd/update` started (§2), in progress or the last one that ended:

```json
{"id": "a1b2c3d4e5f6", "started_by": "home_assistant", "target": "0.4.1",
 "from": "0.4.0", "phase": "installing", "started": 1790410100,
 "finished": null, "result": null, "reason": null, "error": null}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Twelve lowercase hexadecimal digits, one per transaction ([TRANSACTION.md](TRANSACTION.md)) |
| `started_by` | string | `mqtt`, `home_assistant` (a `cmd/update` carrying a relay address), `screen` or `page` - also when Home Assistant relayed the package for it (the relay handshake, section 2). An install over SSH is not reported here - it has no phases to report - and while it holds the lock `cmd/update` is refused `busy` |
| `target`, `from` | string | The release being installed, and the version that ran when it began |
| `phase` | string | `downloading`, `verifying`, `snapshot`, `installing`, `restarting`, `proving`, `rolling_back` or `finished` |
| `started`, `finished` | int or `null` | Epoch seconds; `finished` only once the phase is `finished` |
| `result` | string or `null` | Only with `finished`: `installed`, `withdrawn_before_restart`, `rolled_back`, `failed` or `interrupted` |
| `reason` | string or `null` | Only with a `result` other than `installed`: the end's reason code - the helper's ([TRANSACTION.md](TRANSACTION.md) §7), or `interrupted` / `internal_error` when the plugin judged the end itself. The same code goes on `last_error`, which the next successful command clears; this one stays with the transaction. A consumer says a `result` by its `reason`: `rolled_back` with `not_started` means the new release did not start, with `time_limit` that the update ran out of time and was undone - both put the previous version back. An open enumeration |
| `error` | string or `null` | Why it did not end `installed`, in English; the same sentence and its `reason` go on `last_error` for `cmd/update` |

The phases come from the update helper, which runs outside enigma2, polled once a second. From
`installing` on the package manager is replacing the files under the running plugin, whose doors are
closed (§2, `cmd/update`); the plugin that starts after the restart reports the rest. After a
downgrade chosen on the receiver the old plugin publishes nothing from the retraction on, so this
topic stands still until the older release connects.

### `<base>/<node>/relay_request` - since 0.4.0

**Not retained** - QoS 1. A receiver without internet asks the companion integration for a
package: an install started at the television or on the OpenWebif page, when the release origin is
`unreachable` (`update.origin` above - a probe of the last ten minutes, or the one the install
asks for first, §2), once every refusal of `cmd/update` (§2) has been passed, and only while the
integration's word is on `enigma2mqtt/integration/<node>` (§3).

```json
{"id": "a1b2c3d4e5f6", "version": "0.4.1", "serial": 7}
```

| Field | Type | Meaning |
|---|---|---|
| `id` | string | Twelve lowercase hexadecimal digits, new for every request; the answer must name it |
| `version` | string | The release to install, `N.N.N` - `latest` is resolved before asking |
| `serial` | int | The serial of the signed index this receiver holds |

The answer is `cmd/relay` (§2), within 120 s. Never retained, so nothing answers a leftover after a
reconnect, and never in `retained_topics` (§4): there is nothing to retract.

### `<base>/<node>/last_error`

Retained.

```json
{"cmd": "zap", "error": "service name 'Sport' is not unique (4 matches)", "ts": 1789459213}
```

Published when a command fails or is refused by a guard, and **cleared** - an empty retained
payload - when any command later succeeds, not only the same one: a successful zap clears a
refusal `history_clear` left there. A check that runs after a command has returned, such as the
5 s verification of a zap on `service`, can publish a new refusal. A consumer that raises an error
to the user reads this topic, not the absence of a state change. A refusal names its command in
`cmd`, and - added after 0.4.0 (unreleased) - says back the id that command carried, in `id`, when
it carried one (section 2).

**It is the last refusal of this run of the plugin.** Added after 0.4.0 (unreleased), the named
exception `last-error-cleared-at-start` (Contract version, above). The topic is retained, so it
outlives the process that published it; the first connect of a new plugin process - after an
interface restart, a reboot, a reinstall or an update - clears what an earlier run left there. It
does that once: a reconnect after a broker outage leaves this run's refusal where it is, and so
does the new session a settings save opens. When the starting plugin has a refusal of its own to
report at that connect - how an update ended (section 2) - it publishes that one instead, and it
replaces the older payload without an empty one in between. It is not cleared either while
`update.transaction.reason` is `restore_failed` or `restore_incomplete`: the sentence an earlier
run left then says to install the plugin again, a restart has not done that, and it stays until a
command succeeds or `cmd/clear_error` is sent - the record does not change when the plugin is
installed again, only with the next update.

The empty payload is sent when the plugin knows of an error at that connect, and only then; it
goes out before `availability` says `online`. What the plugin knows is not always what the broker
holds, in both directions. It knows of an earlier error from its state file (section 4), which is
not written at every refusal - a connect and a clean stop write it: an error published shortly
before the interface was killed can be missed, and then stays until a command succeeds or
`cmd/clear_error` (section 2) is sent. And it sends one empty retained message to a topic the
broker holds nothing on when the state file knows of an error the broker no longer has - its
retained store was lost, or the topic was emptied by hand - and when the only refusal is one this
run made before it had a session, which the broker never got. The clearing counts only when the
client took the message: at a connect whose socket was gone again nothing is forgotten, and the
next connect clears instead. The same holds for `cmd/clear_error` and for the clearing after a
command that succeeds.

**`cmd/clear_error` clears it on request** - also added after 0.4.0 (unreleased), section 2.

**One refusal is not retained.** While an update keeps the plugin silent - from a downgrade's
retraction of the node's topics until the update ends - nothing is published, retained or not, with
one exception: the doors' refusal of a `cmd/list` (`reason` `busy`, with the list's `id`), because
its sender is waiting for an answer. It goes out once on this topic **not retained**, at QoS 1 and
never queued across a disconnect, so it does not outlive the retraction: a consumer subscribed to
`last_error` receives it, and one that subscribes later does not find it retained. Added after 0.4.0
(unreleased).

| Field | Type | Meaning |
|---|---|---|
| `cmd` | string | The command refused |
| `error` | string | Why, in English, for the person reading the topic. This is the contract's human text |
| `ts` | int | When, in epoch seconds |
| `reason` | string, optional | Since 0.3.0. A stable code for the refusal, **only** from a command that defines codes - today `history_clear`, `update_check`'s `not_permitted`, `update`, `uninstall`'s `busy`, `stream_stop`'s `not_connected` and `not_stopped` (added after 0.4.0), `list`'s `invalid`, `standby`, `busy`, `no_capability` and `not_shown` (added after 0.4.0) (and `zap_history`'s `playback`); see §2. Absent, not `null`, otherwise, so every other refusal is exactly what it was. A consumer that knows the code can say the refusal in the household's language, and one that does not ignores the field |
| `id` | string, optional | Added after 0.4.0 (unreleased), capability `command_id`. The id the refused command carried in its envelope (section 2), exactly as it was sent. It is the id of the command this refusal is about - also when the refusal is published after that command returned, or by the plugin process that starts after an update - and never the id of another command. Absent, not `null`, when the command carried none or one the plugin could not use, so a refusal without an id is exactly what it was |

```json
{"cmd": "history_clear", "error": "the receiver's zap history holds at most one channel, and 0 does nothing then", "reason": "too_short", "ts": 1789459213}
```

```json
{"cmd": "zap", "error": "the box did not tune to 1:0:19:283D:3FB:1:C00000:0:0:0: within 5 s (it is on 1:0:19:283E:3FB:1:C00000:0:0:0:)", "id": "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90", "ts": 1789459213}
```

---

## 2. Commands

`<base>/<node>/cmd/<name>`, QoS 1, never retained. Payloads are UTF-8; several commands accept a
bare string for convenience and a JSON object when there is more to say. Case is not significant
for the keyword payloads (`on`, `ON`, `On` all work).

Every command is **verified by effect**: the plugin does the thing, observes the resulting
enigma2 event, and publishes the new state on the matching state topic. A failure or a refusal
goes to `last_error`. There is no acknowledgement topic - the state topic is the answer. (Two
commands have no state to change and are answered on a topic of their own: `stream_playable`, a
question, and `list`, a question put to the viewer - below.) A sender
that has to tell which of its commands a refusal belongs to gives the command an id - added after
0.4.0, [below](#a-commands-id---added-after-040-unreleased-capability-command_id); a command that
worked is still answered by its state topic and by nothing else.

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
| `stream_stop` - added after 0.4.0 (unreleased), capability `stream_stop` | `{"host": "192.0.2.20", "sref": "1:0:19:283D:3FB:1:C00000:0:0:0:"}`, exactly | Ends one stream client's stream: the one `stream_clients` lists with that `host` and that service. The answer is `stream_clients` without it, read again two seconds later. See below | **No permission**, as `zap` and `power` have none. Both members and nothing else, each a non-empty string - an empty payload, one member alone or a third one is refused before anything happens, so nothing here ever stops "all of them". Refused when no such client is connected - "that stream client is not connected" (`reason` `not_connected`) - and when the image did not stop it - "the receiver did not stop that stream client" (`not_stopped`), or raised while trying - "the receiver could not stop that stream client: " and the kind of error, never its text, which can hold an address (`not_stopped`); without the capability, "stopping a stream client is not available on this image" |
| `stream_playable` - added after 0.4.0 (unreleased), capability `stream_playable` | `<sref>` \| `{"sref": "..."}` | Asks the image whether that service could be played now, beside everything the receiver is doing, and publishes the answer on `stream_playable` (section 1) - **one of the two commands answered on a topic of their own**, with `list`. See below | Refused, with nothing published on `stream_playable`: without a reference - "no service reference given"; for text that is none; for a reference that is not one playable service - "cmd/stream_playable takes the reference of one playable service"; when the receiver has no information for the service; when the image raises, or gives no number or a negative one; without the capability, "this image cannot say whether a service can be played". **No permission**: it changes nothing on the receiver |
| `volume` | `0`-`100`, or `{"level": 42}` | Sets the volume, with the on-screen bar | Out-of-range values are clamped and noted in the log |
| `mute` | `ON` \| `OFF` | Sets mute | Never a blind toggle: the state is read first, and read back afterwards. A receiver refuses to mute at volume 0, and that refusal is reported |
| `key` | `KEY_OK` \| `{"key": "KEY_OK", "long": true}` | Injects a remote key | Unknown key names are refused with the name in `last_error`; at most 20 a second |
| `message` | `{"text": "...", "type": "info", "timeout": 10}` - or a bare string; `"style": "toast"` since 0.3.0 | Shows an on-screen popup, or a discreet toast | `text` is required; since 0.3.0 **every backslash is removed** and nothing else, as for a toast, and what is left is truncated to 500 characters; `type` is `info`, `warning` or `error` (default `info`); `timeout` is seconds (default 10, `0` until dismissed). A new message replaces the previous one rather than queueing behind it. The toast's rules differ - see below |
| `list` - added after 0.4.0 (unreleased), capability `list` | `{"title": "...", "text": "...", "rows": [{"id": "...", "label": "...", "key": "green", "icon": "gate"}], "timeout": 60, "default": "..."}`, in the envelope with an id (below) | Shows a list of choices on the television, over whatever is on screen, and answers once on `list_answer` (section 1) with the row the viewer picked or how the list ended - the other command answered on a topic of its own. A newer list replaces the one on screen. See below | Refused before anything is shown, each with a `reason` on `last_error`, in this order: while an update is being applied - the doors' sentence, here with `reason` `busy`, asked by the dispatcher before the command's own checks, and published even while the update keeps the plugin silent, then **not retained**; a payload that breaks a rule below - a sentence that names the member and the row's index, never the sender's text (`invalid`); without the capability - "lists cannot be shown on this receiver" (`no_capability`); in standby, or while the receiver's own "really shut down / restart?" question is on screen - the toast's "the receiver is in standby" (`standby`). A list that neither screen could put on the television - "the receiver could not show the list" (`not_shown`), also later, with the command's id, and then no answer. **No permission and no setting**, as a message has none |
| `timer` | see below | Adds or deletes a recording timer | An add that overlaps an existing timer, or refers to an unknown event, is refused - and so is one the receiver quietly dropped as a duplicate of a timer it already had |
| `record` | `start` \| `stop` | Starts or stops an instant recording of the current service | `start` records the current service for two hours; `stop` with nothing recording is a no-op with a note in `last_error` |
| `play_recording` - added after 0.4.0 (unreleased), capability `recording_play` | `{"id": "<16 hex digits>"}`, exactly | Plays one recording of the library in the image's own movie player, as if it had been chosen in the image's movie list - with the image's own rule for resuming, so the television may ask whether to resume. Wakes the receiver from standby first, as `zap` does. A recording playing already is replaced in the same player. Verified on `service` within 5 s | Refused, before anything happens: without the capability; for a payload that is not exactly one id - "the command takes the id of one recording: ..."; for an id the current library does not list - "that recording is not in the receiver's library" (`reason` `not_in_library`), checked first, so a refused id never wakes the receiver; while an update is being applied (the doors' sentence, `busy`); when the file is gone - "that recording's file is gone from the receiver" (`file_gone`); without an info bar to open the player from, with no code. "the receiver did not start playing the recording within 5 s" when `service` does not show it. A recording that parental control protects asks for the PIN on the television, and is not verified. **No permission**, as `zap` has none. The codes are `not_in_library`, `file_gone` and `busy`; every other refusal is a sentence without one |
| `delete_recording` - added after 0.4.0 (unreleased), capability `recording_delete` | `{"id": "<16 hex digits>"}`, exactly | Moves one recording of the library, and the files the image moves with it (`.eit`, `.ap`, `.cuts`, `.meta`, `.sc`), into the image's trash, with the image's own move; the image's trash settings decide when it is emptied. The answer is `recordings` read again (`reason` `delete`) once the file is gone. 🔴 **Never a delete for good** | Refused, before anything happens, in this order, each with a `reason`: unless `recording_delete_allowed` is on - "deleting recordings is switched off in the plugin's settings" (`not_permitted`); without the library; for a payload that is not exactly one id; on an image with no trash for recordings, with no code; with the image's trash switched off - "the receiver's trash is switched off; nothing is deleted for good" (`trash_off`); for an id the library does not list (`not_in_library`); while the recording is being recorded or played (`in_use`); while another delete still waits for its move (`busy`); when the file is gone (`file_gone`); when the image cannot make its trash folder or refuses the move - "the receiver could not move that recording to its trash", with no code, the image's own offer to delete it for good instead never being taken; while an update is being applied (the doors' sentence, `busy`). A move that has not taken the file away within 60 s is reported - "the receiver did not move that recording to its trash", with no code. The codes are `not_permitted`, `trash_off`, `not_in_library`, `in_use`, `file_gone` and `busy` |
| `recordings_refresh` - added after 0.4.0 (unreleased), capability `recordings` | any (`PRESS` by convention) | Reads the library again now (`reason` `refresh`) - the one read that may wake a disk, and that reads a movie location which is not mounted, which mounts an automounted share | At most one read at a time: a refresh during a read is answered by that read. **The payload is ignored**. No permission. Refused only while an update is being applied (the doors' sentence, `busy`) |
| `screenshot` | any | Captures `screen` now, in standby as well | Rate-limited to one per five seconds |
| `softcam_restart` - since 0.3.0 | any (`PRESS` by convention) | Stops **every** running instance of the cam the image selected, then starts exactly one, with the image's own command line | Refused unless `softcam_restart_allowed` is on; refused by the same recording guard as `deep_standby` - while recording, with a timer due within 10 minutes, and when the image will not say; refused for the first 60 seconds after the plugin starts, because the image's own check runs a moment after that and restarting inside that window races a copy already on its way; at most one manual restart a minute; refused while one is already running. 🔴 **The binary is resolved on the box and no part of the command line comes from the payload** |
| `epg_grid` | any | Rebuilds and republishes **every** `epg_grid/<bouquet_slug>` topic | Refused with a note in `last_error` when `epg_grid_events` is `0` |
| `epg_import` - since 0.3.0 | any (`PRESS` by convention) | Starts the image's EPG importer, the way its own „Manual" button does, and reports on `epg_import`; the grid is rebuilt afterwards and each bouquet is published only if it changed | Refused, in this order: unless `epg_import_allowed` is on; when the importer is not available (no capability); while an import is already running, whoever started it; by the same recording guard as `deep_standby`; within 10 minutes of the importer's own scheduled run, which the image starts without checking whether one is running, and when that time cannot be read; when the importer has no sources selected. A start that fails is `failed` on `epg_import` **and** the same sentence on `last_error`. 🔴 **The payload is ignored**: nothing from the broker reaches the importer |
| `config` | `{"publish_keys": false, "screenshot": "interval", "screenshot_interval": 90, "screenshot_delay": 4, "cam_telemetry": false, "oscam_telemetry": false, "softcam_autoheal": false, "softcam_autoheal_seconds": 90, "menu_hotkey": "help_long", "menu_transparency": 20}` | Atomically replaces the remotely writable settings it names and publishes fresh `info`/discovery | Every key is optional and one left out keeps its current value - also the original three, which were required up to 0.4.0; an empty object is valid and changes nothing. Unknown keys and coercion are refused, and a persistence failure applies none of them |
| `discovery` | any | Republishes the announcement, the channel list, and the discovery payloads in discovery mode | - |
| `ha_mode` | `discovery` \| `integration` \| `off` | Switches the Home Assistant mode | See below |
| `reset` | any | Retracts every retained topic this node owns, then republishes | See below |
| `clear_error` - added after 0.4.0 (unreleased) | any (`PRESS` by convention) | Clears `last_error` (section 1): publishes the empty retained payload to it. It succeeds whether or not an error is retained, and sends the payload either way - also for an error this run of the plugin does not know of - so one press costs one empty message and nothing else changes. Sent while the socket to the broker is gone, it clears nothing and forgets nothing, and still answers nothing on `last_error` | No permission and no capability: it changes nothing on the receiver. **The payload is ignored.** It is refused only as every command is - discarded when retained or oversized, and answered with the doors' sentence on `last_error` while an update is being applied |
| `uninstall` - since 0.3.0 | this receiver's node id, exactly (surrounding whitespace is stripped; case matters) | Removes the plugin from the receiver: stops publishing, retracts every retained topic this node owns at QoS 1, publishes `offline` last, removes the package and restarts the interface. 🔴 **A one-way door** | Refused, before anything changes, in this order: unless `uninstall_allowed` is on - „uninstall is switched off in the plugin's settings"; unless the payload is this node's id - „the payload must be this receiver's node id"; while an update runs, or an update or an SSH install holds the transaction lock - `cmd/update`'s `busy` sentence, with `reason` `busy`, because both end in the package manager and a restart; without the `uninstall` capability - „this plugin was not installed by the package manager, so it cannot remove itself"; while an EPG import runs - „an EPG import is running", exactly as `restart_gui` refuses it and with the same lapse, because the removal ends in the same restart; by the same recording guard as `deep_standby`; where the image has no way to restart the interface; while a removal is already running - „an uninstall is already running". See below |
| `update` - since 0.4.0, capability `self_update` | `{"version": "0.4.1" \| "latest", "sha256": "<64 hex>", "relay": {"url": "...", "expires": <epoch>}}`; `sha256` and `relay` optional | Installs one signed release of the plugin and restarts the interface to run it, keeping the channel being watched; rolls back by itself when the new release does not start. **Upgrades and repairs only** - never a downgrade over MQTT. The answer is `update.transaction` moving (§1). See below | Refused before anything changes, in the order below, each with a `reason` on `last_error` |
| `update_check` - since 0.4.0 | any (`PRESS` by convention) | Asks the plugin's release origin for the signed release index, judges what comes back, and says what it found on `update` (§1) | Refused unless `update_check` is on: "checking for updates is switched off in the plugin's settings" (`reason` `not_permitted`). At most one request per ten minutes, shared with the OpenWebif page: inside that the command succeeds, nothing is fetched and `update` stands as the last check left it. The answer is `update` changing once the check has run, as for every command. **The payload is ignored**: nothing from the broker names where to look |
| `relay` - since 0.4.0 | `{"id": "a1b2c3d4e5f6", "version": "0.4.1", "url": "http://192.0.2.5:8123/api/enigma2_mqtt/relay/<token>", "expires": <epoch>}` | Home Assistant's answer to this receiver's `relay_request` (§1): where to download the package. Taken only as the answer the receiver is waiting for, and it then starts the install that asked (below) | **Never refused on `last_error`, and never clears it**: an answer that is not the awaited one is logged and dropped, and the wait goes on. What comes of an answer that is taken is `cmd/update`'s |

Every one of them is refused when it arrives retained, as above.

### A command's id - added after 0.4.0 (unreleased), capability `command_id`

A sender may give a command an id, and a refusal of that command says it back as `last_error.id`
(section 1, [ADR-0016](adr/0016-a-commands-id-comes-in-an-envelope-and-only-a-refusal-says-it-back.md)).
It is there to tell refusals apart: two commands of one name sent close together, or a refusal
that arrives after the next command has been sent, have the same `last_error.cmd`. **Refusals
only.** A command that worked says its id nowhere, and its answer is its state topic, as before.
The two answers that are not a state topic say it too: `stream_playable`, the answer to
`cmd/stream_playable` (above), carries the id of the question it answers, and `list_answer`, the
answer to `cmd/list` (below), the id of the list it answers - which is how a sender tells its list
from another one.

The id travels in an **envelope** around the command's own payload, the same for every command:

```json
{"id": "7f9c2e4a-3b1d-4c8e-9f60-2d5a1b7c8e90", "payload": "PRESS"}
```

```json
{"id": "zap-17", "payload": {"sref": "1:0:19:283D:3FB:1:C00000:0:0:0:"}}
```

- **An envelope is a JSON object with exactly the members `id` and `payload`, and its `payload`
  is a string or an object.** Anything else - a bare string, a JSON object with other members,
  one of the two alone, the two with a third, a `payload` that is a number, a boolean, an array
  or `null` - is the command's own payload, without an id, and is handled exactly as it always
  was. No command's own payload has
  that shape, and none will be given it.
- **`payload` is what the sender would have published without the envelope.** A string is that
  text, character for character: `"PRESS"`, `"standby"`, `"40"`, a service reference, a node
  id. The empty string is the empty payload, and it is the convention for a command that ignores
  its payload. An object stands for its JSON text - `{"level": 42}`: the plugin reads it and
  writes it again without spaces, so the command gets the same value and not the same bytes - a
  number inside it may be spelled differently (`1e2` arrives as `100.0`), and of two members
  with one name the last is kept. A sender that needs its exact text sends it as a string, and
  a command that takes a JSON object may be given a string that holds it. A command that takes
  a number or a keyword takes it as a string here, as it does bare: `"payload": "40"`, not
  `"payload": 40`. A string that UTF-8 cannot carry - JSON can spell half a surrogate pair -
  makes the message no envelope: it is the command's own payload, without an id. An envelope is
  opened once: one inside another is the command's payload.
- **`id` is a string of 1 to 64 characters** out of `A`-`Z`, `a`-`z`, `0`-`9`, `.`, `_`, `:` and
  `-` - a UUID, a ULID, random hex. It is the sender's to choose and to keep apart
  from its other ids for as long as it waits. Use random ids without a fixed alphabetic prefix:
  an id that holds a registered secret is dropped (below), and a prefix that happens to hold a
  short password would lose every id of that household. The plugin compares it with nothing and remembers
  none: two commands with one id are two commands.
- **An id the plugin cannot use is dropped, and the command runs** as if it had carried none: an
  id of another type or shape, and one that holds a registered secret - a value the plugin keeps
  out of its log: the broker password and the OSCam web interface's password. The log says why
  it was dropped, without the id - once for each reason in a run of the plugin, and at debug
  level after that. A refusal of that command
  has no `id`, so a sender reads a refusal without `id` whose `cmd` is its command's the way it
  did before there were ids.
- **The 4096-byte limit counts the whole message**, envelope included, and a retained command is
  discarded with or without one.
- **Only on the broker.** A command run from the OpenWebif page or from the receiver's own screen
  has no envelope and no id, and a payload typed there that looks like one is the command's
  payload. `cmd/relay` takes no envelope either: it is an answer, not a command, it never reaches
  `last_error`, and the `id` in it is the relay request's.

**Which refusal says it.** Every `last_error` payload is about one command, and carries that
command's id when it had one. That holds for a refusal made while the command is handled - an
unknown command, a guard, a failure, the doors' sentence while an update is applied - and for one
published later:

| Command | The later refusal |
|---|---|
| `zap`, `zap_history` | The zap did not show up on `service` within 5 s; the receiver did not leave standby; the zap was refused once the receiver was awake |
| `screenshot` | `grab` failed, wrote nothing, or wrote a picture over the size limit |
| `softcam_restart` | The sequence failed on a later turn - the cam could not be started |
| `uninstall` | The removal stopped, and the fresh session says why |
| `list` | The list waited for the screen - another one was closing - and could not be shown then (`not_shown`) |
| `update` | However the update ended without `installed` - also when the plugin process that starts after the restart, or after the rollback, is the one that says it: the id is kept on the receiver with the update's own request |

A later refusal keeps the id of its own command whatever has been sent since: one published while
another command is handled, or after it, never takes that command's id. A newer zap replaces the
zap being verified, and the refusal is then about the newer one, with its id or without. What no
broker command asked for has no id: an install started at the television or on the page - also
when it ends in `no_relay` or `clock_skew` after waiting for Home Assistant - and the automatic
softcam restart. The empty payload that clears `last_error` carries nothing, as before.

🔴 **Read `info.capabilities` before sending an envelope.** A plugin without `command_id` - 0.4.0
and older - does not know the envelope and takes it for the command's payload: a command that reads
its payload refuses it, in its own words, and a command that ignores its payload runs. A consumer
sends the envelope to a plugin that claims `command_id` and the bare payload to every other. A
consumer that sends no id sees one difference, the new name in `info.capabilities`: no payload it
receives has a new member.

### `cmd/stream_stop` and `cmd/stream_playable` - added after 0.4.0 (unreleased)

**`cmd/stream_stop` ends one client's stream and is always allowed**
([ADR-0018](adr/0018-stopping-a-stream-client-needs-no-permission.md)). There is no setting that
permits it and none that forbids it: what it does is what a zap does to whoever is watching the
television, and less than standby. The payload names the client by the two things
`stream_clients` shows for it:

- `host` as the topic shows it - `192.0.2.20`. The image's own spelling of an IPv4 client,
  `::ffff:192.0.2.20`, is taken too, and the two name the same client whichever of them the image
  holds; letters in an address are compared without regard to case. The plugin finds the client
  in the image's list and hands the image its own spelling back, because the image compares it
  as text.
- `sref` as the topic shows it. Two references are the same service by the rule of `service` -
  the first eleven colon-separated fields - compared without regard to case. So a name behind
  the reference does not matter, nor does a missing trailing colon, nor `283d` for `283D`; a
  reference that differs in any of those fields is another service, and is refused as not
  connected.

A row whose `sref` is `null` cannot be named, so it cannot be stopped - by no spelling: a row
listed without its reference because that was longer than 512 characters is never the one a
command ends, also not when the reference sent is the same service by its first eleven fields.
When such a row and a listed one share a host and a service, the listed one is ended and the
other is left alone: the image is handed the listed row's own reference, and compares it as a
whole text.

**Two rows that are the same row.** One device with two players on one channel, or - on OpenATV -
a client of the RTSP server beside one of the stream server, can be two rows with one host and
one reference. Nothing tells them apart, here or in the image: one command ends the first the
image finds, the list is read again two seconds later, and the row that is still there is the
other one. When the same host is sent the same service twice in two spellings - with a name
behind the reference and without, say - the command ends the row whose `sref` it spells exactly
as the topic shows it. Only a reference that is spelled as none of the rows is - another case, a
missing trailing colon, another name behind it - falls back to the eleven-field rule, and then
the two rows are one client to this command, and the image ends the first it finds; sending the command again ends
the other. The image answers at once and the connection ends a moment later (measured: about
1.4 s), so the row is still on `stream_clients` until the list is read again, two seconds after
the command. **A player that reconnects by itself is back by then**, as a new connection the
topic cannot tell from the old one: stopping a client is not blocking it.

**`cmd/stream_playable` is a question, and has an answer of its own**
([ADR-0017](adr/0017-whether-a-service-can-be-played-is-asked-with-an-empty-second-reference.md)),
as `cmd/list` has - the other question, which is put to the viewer (below).
Every other command changes something and is answered by the state topic that shows the change.
This one changes nothing, so there is no state to show: its answer is one message on
`stream_playable` (section 1), which carries the command's id when the command had one. A consumer
that asks more than one question at a time gives each an id (below) and reads its answer by it; a
sender without ids reads it by `sref`.

What is asked is the image's own `isPlayable`, for one service, **with nothing assumed to be given
up**. The image's channel list asks the same function with the playing service named as the one
that would make room, and then nearly everything is playable - the television can always be
switched over. A stream is opened beside the television, so the plugin names nothing: the answer
is `false` when the service needs a tuner that the television, a recording or another stream is
holding. It is asked for one service a command, and for a playable service only. Refused, by
the test the channel list itself uses - the flags in the reference's second field - are: a
bouquet and any other directory, the list of bouquets and a service with alternatives among them
(the image would answer one of those for any one of its members, by walking it); a marker and a
numbered marker; an entry hidden in its bouquet; and text with fewer than four fields or with
flags that are no number. The plugin does not publish it for the channel list: it would be a second copy of the
list that is out of date with every zap.

### `cmd/update` semantics - since 0.4.0

[ADR-0015](adr/0015-signed-self-update.md) and [TRANSACTION.md](TRANSACTION.md) have the design;
this is what a consumer sees. `version` is a release number or `latest` (the newest compatible
release of the index the receiver holds); `sha256`, when given, must be the signed entry's;
`relay` is Home Assistant's short-lived download address, and a command carrying one is
`started_by: home_assistant` and needs no internet on the receiver. It is refused, before anything
changes and in this order:

| Refusal | `error` | `reason` |
|---|---|---|
| `update_allowed` is off (over MQTT) | "updates over MQTT are switched off in the plugin's settings" | `not_permitted` |
| no `self_update` capability | "this plugin was not installed by the package manager, so it cannot update itself" | `no_capability` |
| an update or an SSH install holds the transaction lock | "an update is already running on the receiver" - or, when the lock is an update's whose helper has provably stopped, "the previous update stopped without finishing; a new one is possible in about <n> minutes, when its lock on the receiver expires" | `busy` |
| an uninstall runs | "the plugin is being removed from the receiver" | `busy` |
| opkg's lock is held | "the receiver's package manager is busy" | `opkg_busy` |
| the receiver is in standby | "the receiver is in standby; an update restarts the interface, which wakes the receiver and may switch the television on" | `standby` |
| a recording runs / starts within ten minutes / the image will not say | the recording guard's sentence, as for `cmd/restart_gui` | `recording` / `recording_due` / `recording_unknown` |
| an EPG import runs (with `cmd/restart_gui`'s lapse) | "an EPG import is running" | `epg_import` |
| the image cannot restart the interface | `cmd/restart_gui`'s sentence | `cannot_restart` |
| the payload is not an object with a `version` | "the update request could not be read" | `bad_request` |
| no index held, or the version is not in it | "version <v> is not in the plugin's signed release index" | `unknown_version` |
| withdrawn | "version <v> has been withdrawn: <why>" | `withdrawn` |
| below the floor (the index's, raised by the integration's `plugin_min`) | "version <v> is below the lowest version this plugin can install" | `below_floor` |
| another contract major, or a newer integration needed | "version <v> is not compatible with this plugin's contract" / "... with the Home Assistant integration on this broker" | `incompatible` |
| a package it needs is not installed | "version <v> needs <package>, which is not installed on this receiver" | `depends` |
| lower than the running version | "a downgrade can only be started on the receiver or from Home Assistant's options" | `downgrade` |
| that release runs and is on disk (version and build commit both the signed entry's) | "version <v> is already installed and running" | `current` |
| `sha256` is not the signed entry's | "the requested checksum does not match the signed release index" | `checksum` |
| `relay` is not `http(s)://<host>[:port]/api/enigma2_mqtt/relay/<43 url-safe characters>` - the host a name of letters, digits, dots and hyphens or an IPv4 address, never a bracketed IPv6 literal and never a user part; the port, if any, 1 to 65535; nothing after the token - with a whole-number `expires` still ahead | "the download address from Home Assistant is not valid" | `relay` |
| free space under `/home/root` below twice the package plus the plugin's size plus 2.25 MiB | "there is not enough free space on the receiver" | `no_space` |
| an update ended less than ten minutes ago | "an update ran less than ten minutes ago" | `rate_limited` |
| started at the television or on the page, the release origin `unreachable`, and nobody to ask for the package - no broker connection, no integration's word on `enigma2mqtt/integration/<node>` (§3) - or no answer within 120 s (below) | "the receiver cannot reach the plugin's release origin and Home Assistant did not answer, so the update cannot be installed" | `no_relay` |
| started there, and the only answers carried an address of Home Assistant's shape that this receiver's clock calls expired (below) | "an answer arrived whose download address had already expired by the receiver's clock; if the receiver's clock is wrong, set it and try again" | `clock_skew` |

Accepted, the plugin starts its update helper outside enigma2 and follows it on `update`. The helper
judges the index again, downloads and verifies the package, keeps a rollback point and runs the
package manager. 🔴 **From `phase: installing` the doors are closed**: every command - over MQTT,
from the OpenWebif page - is refused with "an update is being applied on the receiver" (retained and
oversized commands are still discarded first), no feature area publishes, and the page and the setup
screen say only that. When the package manager fails, the old files go back and the doors open again
with a fresh session - unless the old files could not be put back: then the doors stay closed, and
every command is refused with "an update failed and the plugin's previous files could not be put
back; install the plugin again (from Home Assistant: force plugin reinstall)" - also when that
failure came and went between two readings of the helper's status. When the old code is back but not
all of opkg's records or the settings block (`reason` `restore_incomplete`), the doors stay closed
too, with "an update failed; the plugin's previous version is back, but not all of its package
records; install the plugin again (from Home Assistant: force plugin reinstall)". After
`restore_failed` and `restore_incomplete`, `last_error` carries the helper's own sentence, which
names the reinstall. So do they when the update helper stops once the package manager has started
(it may still be running): "an update stopped part-way, so the plugin's files may not be the running
version's; install the plugin again (from Home Assistant: force plugin reinstall)". When a rollback
put the previous version's files back under an interface that was never seen to stop (`reason`
`not_stopped`), a process the helper lists as left running (its pid in `record.unstopped`; with no
list, a process that does not run the version put back) is the one they changed under: the doors
stay closed with "an update was rolled back while the receiver's interface kept running, so the
plugin's files are no longer the ones it runs; restart the receiver's interface", `last_error`
carries the helper's sentence, and `cmd/restart_gui` - the repair - is the one command still
accepted, with its own guards; the page offers it under the sentence. The helper's account can be
wrong, so whatever the end, a plugin that ran through the update also compares the build it loaded
at its start with the build id on disk before it opens again: when they differ - or the file on disk
can no longer be read - it runs other code than the files underneath it, and its doors stay closed
with "the receiver's interface runs a different build of the plugin than the one now installed;
restart the receiver's interface" and the same `cmd/restart_gui` (with the reinstall sentence
instead when the old files were not all put back). Any other process - one that started since, on
the files on disk - reports the end and opens as usual. A plugin that only follows an update,
started by its restart, closes its doors too while the old files go back (`rolling_back`); they open
at the end on the same conditions, and when the helper stops or its records go missing meanwhile
they stay closed with the reinstall sentence - except in a plugin that R2 itself started, which runs
the files put back and opens when its build is the one on disk. After `interface_not_started`
nothing runs to read the end: the plugin that starts next reports it (`result: failed`) and opens as
after any other. At `restarting` the standby, recording and EPG-import guards are asked again, and
when one holds the update is withdrawn (`withdrawn_before_restart`, `reason` `standby`, `recording`
or `epg_import`) rather than restarting a receiver that went into standby or started recording
meanwhile. Otherwise the image's own restart: a clean quit that saves the settings, so the receiver
comes back on the channel it was showing. When the image asks on the television first (a recording,
a stream, timeshift) and nobody says yes within 60 s, the update is withdrawn -
`result: withdrawn_before_restart`, `reason` `question` - and the plugin opens a fresh session,
republishing everything. A new release that does not start within two minutes is rolled back
(`rolled_back`, `reason` `not_started`), and the channel and standby state are put back. A
rollback is not always that: when the restart comes so late that the update's 15 minutes run out
before the new release has had its two minutes, it is rolled back the same way with `reason`
`time_limit` - the update ran out of time and was undone, which says nothing against the release,
and `last_error` says the previous version is back. (`time_limit` with `result: failed` is the
other end of that code: the 15 minutes ran out before the restart, and nothing changed stays.)
A rollback can also carry `interrupted` or `internal_error`, and on an acceptance build `drill`;
[TRANSACTION.md](TRANSACTION.md) §7 lists which end each reason comes with, and
`update.transaction.reason` (§1) keeps it after `last_error` is cleared.

**From the receiver itself.** The receiver's "Plugin updates" screen and the plugin's OpenWebif
page run this same command, with `started_by` `screen` or `page`. They need no `update_allowed`
(the person at the television, or admitted by OpenWebif, could switch it on), and every other
refusal above applies to them unchanged, on `last_error` as for any command. They alone may install
a version **below** the running one, and only after a question that names what it takes away; the
consent to that is passed down the call by the screen or the page, never read from a payload, so no
`cmd/update` a broker client sends can carry one. A downgrade retracts every retained topic but
`availability` before the restart, as above.

**A receiver without internet - `relay_request` and `cmd/relay`.** An install started at the
television or on the OpenWebif page carries no download address, and the receiver fetches the
release itself - unless it cannot reach the release origin. Once the request has passed every
refusal above, the receiver looks at `update.origin` (§1). A probe's word of the last ten minutes
is taken as it is; otherwise - `unknown`, the default on a receiver that has never checked, a word
read back after a restart, or an older one - it probes once first: the check's own request for the
signature file, five seconds, while the install waits (at most 60 s, for a check that may already
be running). Starting the install is the consent to that probe - an explicit action at the
television or on the page; nothing else probes, and a `cmd/update` over MQTT never does.
`reachable`: the update helper fetches the release itself. `unreachable`: the receiver asks Home
Assistant for the package. An update helper whose download from the origin got no answer at all makes
`update.origin` `unreachable` as well. Its failed end starts the ten-minute limit between updates,
so that word is taken for the limit and ten minutes more: the next install at the television that
the limit lets through asks Home Assistant without probing. After that, or after a restart, the
origin is probed again.

To ask, the receiver publishes `relay_request` (§1) and waits at most **120 s** for `cmd/relay` -
but only while the integration's word is on `enigma2mqtt/integration/<node>` (§3), which only an
integration that relays publishes; without it the install is refused `no_relay` at once rather than
after two minutes. The answer is taken only when its `id` is the one asked with, its `version` the
one asked for, its `url` of the shape the `relay` refusal describes and its `expires` still ahead;
anything else - a retained message, another id, an id nobody is waiting for any more, another
version, another address - is a line in the plugin's log, changes nothing, and the wait goes on.
**The first answer that passes is taken, whoever sent it.** Nothing on the broker proves that an
answer comes from Home Assistant, and the id is readable by every client that can read
`relay_request`, so a broker client that answers first with a well-formed address of its own is
taken, and Home Assistant's answer after it is dropped. That client can deny and delay, not
install: the update helper verifies whatever the address serves against the signed entry (size and
sha256) before the package manager sees it, refuses anything else with nothing changed
(`bad_package`, or `download` when the address does not answer), and the failed update starts the
ten-minute limit between updates. The answer is transport, not an instruction: the refusals above
are asked once more (up to two minutes have passed - the receiver may have gone into standby), and
the install is still the one asked for at the television or on the page (`started_by` `screen` or
`page`; a downgrade chosen there stays one), with the relay address instead of the origin. A
refusal then goes on `last_error` as for `cmd/update`. Without an answer in 120 s - or without a
broker connection or the integration's word to ask on - the install is refused with "the receiver
cannot reach the plugin's release origin and Home Assistant did not answer, so the update cannot be
installed" (`reason` `no_relay`), and nothing has changed. When the only answers of Home
Assistant's shape carried an address that this receiver's clock already calls expired - a clock
ahead of Home Assistant's by more than the address lives - the refusal is `clock_skew` instead
(the row above). Its sentence says only what the receiver can know: such an answer may come from
any broker client, so it does not claim that Home Assistant answered. While a receiver probes or
waits, `cmd/update` and `cmd/uninstall` are refused `busy`; a wait whose timer did not run ends
by its age the next time anything asks. A `cmd/update` over MQTT never asks back: Home Assistant
sends its address with it, and a command without one asks the receiver to fetch. A request that
carries a relay address never makes the receiver contact the origin at all. "No IPv6 literal" in the `relay` refusal is a rule about the address as written:
a host name may resolve to any address, and whatever answers there, the bytes are verified.

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
channel is tuned and the command succeeds, so, like any successful command, it **clears** a
pending `last_error`, whichever command set it. It sets one only if the image's `playService`
itself raises: the exception's text is then the refusal. The 5 s verification on
`service` runs as for any zap and can still set `last_error` afterwards. Each exit is logged at
`info` as

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
| 6 | choosing the bouquet raised - reading the published bouquets, or the channel list's own `getRoot` | `the channel list could not be read` | also logged as an exception, `could not choose a bouquet to zap through` |
| 7 | the service is in no published bouquet - a radio station (`channels` holds television bouquets only), a bouquet `bouquets_for_select` leaves out, a reference in no bouquet | `it is in no published bouquet` | there is no bouquet to enter it through. A radio station zapped from Home Assistant is therefore never recorded; one zapped with the remote is |
| 8 | the image's `selectAndStartService` raised | `the channel list's zap raised` | also logged as an exception, `selectAndStartService raised`. Whatever the channel list did before it raised is not undone; what that leaves in the history is not measured |
| 9 | the channel list could not select the service, nothing was tuned, and the service is not protected by parental control | `the channel list could not select it` | preceded by a warning, `the channel list did not select <sref>; playing it directly`. The published bouquets are read once a minute, so a bouquet edited since then, or a list that hides the channel, leaves the selection on what is playing, and the channel list re-zaps that |
| 10 | the channel list tuned a different service | `the channel list tuned another service` | preceded by a warning, `the channel list tuned <tuned> instead of <sref>; playing it directly`. A stale bouquet can leave the selection on a neighbour, and the wrong channel on the television is worse than an unrecorded zap to the right one. The neighbour went through the channel list's zap, so it may be in the history while the requested channel is not (read from the code, not measured) |

**One case counts as recorded whatever happens next.** When nothing was tuned and the service is
protected by parental control - or the image cannot say whether it is - the PIN screen on the
television is what the zap is waiting for. The plugin logs nothing and leaves it to the PIN. The
channel is in the zap history at once, as a remote zap to it would be, whether or not the PIN is
ever entered: the image adds it to the history right after it hands the PIN request over, and the
play that follows a correct PIN adds nothing (read from the bytecode, not run). The 5 s
verification on `service` reports a zap whose PIN was not entered.

A session with no navigation, or a navigation with no `playService`, is not an exit: `cmd/zap` is
refused - "there is no session to zap with", "this image's navigation has no playService".

**An information popup is not a screen open.** When the only thing open is a popup directly over
the info bar - the receiver's own "Zapped to timer service", a recording or zap error, or what
`cmd/message` shows - the zap goes through the channel list and is recorded, and the popup stays
until its own timeout, as it does after the receiver's own zap-timer zap. A protected channel's
PIN is queued behind the popup and appears when it closes, as it would for a direct play. Exactly
this counts: the image's own
`Screens.MessageBox.MessageBox` (not a class built on it), of type information, warning or error,
with no answers to choose from, executing, with the info bar directly under it. What is under the
info bar does not matter: a screen a plugin opened when the interface started (the Vu+ HbbTV
plugin keeps an invisible one there) is not on the screen. When a zap is played directly because
of an open screen, the plugin's log says which of these conditions was not met. A question
(which is what a message box queued without a type is), a type the image does not know, a popup
over any other screen - the movie player, the channel list - and a popup already closing still
count as a screen open. A toast is not a dialog at all and never counts as a screen open: a zap
under a toast is recorded.

**A question stays open and unanswered.** While a yes/no question is open over the info bar,
`cmd/zap`, `cmd/zap_history` and the zap `cmd/bouquet` makes still change the channel - directly,
with `playService`, and unrecorded - and the plugin closes and answers nothing: the question stays
on the television, unanswered, for the household to answer with the remote. This is intended. The
household keeps control with the remote, and a zap from Home Assistant is an explicit request, so
it is carried out rather than refused. The same holds for the channel list, the EPG or a menu
left open, with two exceptions:

- `cmd/zap_history` is refused while a recording is played back ("a recording is being played
  back", reason `playback`), before anything is tuned. What a direct `cmd/zap` does to the movie
  player was not traced.
- `cmd/bouquet` moves the channel list to the new bouquet and saves it as the list's root whatever
  is open, so a channel list open on the television jumps to that bouquet.

That the question stays on the television is read from the code - the plugin's direct path opens,
closes and answers no dialog - and is covered by tests; it was **not measured** on a receiver
(the hardware drill had an information popup open, not a question). Timeshift is a separate case
with its own rules: see [below](#a-zap-from-home-assistant-during-timeshift).

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

### The recording library's commands - added after 0.4.0 (unreleased)

A recording is named by its **id** and never by a path: the plugin acts only on a recording its
own current library lists, so nothing that arrives from the broker names a file. An id the
library does not list - a recording made since the last read, or one left out of a folder for the
packet bound - is refused until the library lists it; `cmd/recordings_refresh` reads it again.

`cmd/play_recording` behaves as the receiver does when the recording is chosen in its movie list:
the image's movie player, with the image's resume setting - resume, ask, or start from the
beginning, whatever the receiver is set to. When it asks, the question is on the television.

`cmd/delete_recording` only ever moves a recording into the image's trash, and is refused when the
image's trash is switched off - nothing Home Assistant asks for is deleted for good. The image
moves a large file in a background job, so the delete is verified when the file has gone from
where it was, not when the move was asked for. Run from the OpenWebif page it needs no
permission, as every box-side permission (§1); every other refusal applies there too.

### What enters the receiver's zap history

The receiver records a zap only when it passes through its channel selection, which calls
`addToHistory`; the image's `playService` on its own never records. The plugin publishes the list
as the receiver keeps it (`zap_history`) and adds nothing to it, so a zap missing here is missing
from the receiver's own History Zap screen too.

The `cmd/*` rows are read from this plugin's code; the others are read from the bytecode of one
image, OpenViX 6.6. The parts marked **measured** were also checked on a receiver running it.
Other images may route their zaps differently.

| Zap | In the history? |
|---|---|
| The remote: a number, a channel chosen in the channel list, channel up and down, the History Zap screen | Yes. A radio station too, while the image's "e1-like" radio mode is on (its default); its `bouquet_name` is `null` |
| `cmd/zap` | Yes, filed under the bouquet chosen as [above](#cmdzap-goes-through-the-channel-list---since-030) - except the ten direct-play exits in that section. From standby, the zap is recorded and the channel the receiver slept on is not |
| `cmd/zap_history` | Yes: the entry moves to the front, as the History Zap screen moves it - also under an information popup, which stays open. With any other screen open over the info bar, a question included, it is played directly, the list is left as it is and the screen stays open - except during the playback of a recording, when it is refused (`playback`). During timeshift it leaves timeshift without asking, as the History Zap screen does (read from the code, not measured) |
| `cmd/bouquet`, when it has to tune | Yes, through the channel list's own zap - also under an information popup. With any other screen open over the info bar the channel is played directly and not recorded, and the screen stays open - but the channel list itself moves to the new bouquet, even when it is the screen that is open. Refused during timeshift and while the channel list is in radio mode |
| `cmd/history_clear` | The list is emptied and channel 1 is recorded, as the 0 key does. Refused during timeshift, reason `timeshift` (**measured**) |
| A zap timer, receiver awake, no timeshift | Yes. It is filed under the **first bouquet in the receiver's own bouquet order** that holds the channel - not the bouquet being browsed, and not only among the published ones, so `bouquet_name` is `null` when that first bouquet is one `bouquets_for_select` leaves out. **Measured:** the timer's zap entered the history, filed under that first bouquet. Read from the bytecode only: the channel list moves to that bouquet, so channel up and down walk it afterwards; and when the image's "show message when recording starts" setting is on (its default) and nothing is open over the info bar, the receiver shows "Zapped to timer service" for 5 s (the popup itself was seen on the drill). A `cmd/zap` in those 5 s goes under the popup and is recorded (the information-popup rule above). Before that rule it was measured played directly and unrecorded; the recorded behaviour is covered by tests and not yet measured on a receiver |
| A zap timer, receiver in standby | No. The receiver wakes and plays the timer's channel the way it restores the channel it slept on, with `playService` - or its start-up channel, when it is set to start on one after standby |
| A zap timer during timeshift | No. The receiver asks on the television, for 20 s: "Zap" and "Save timeshift and zap" play the channel directly; "Don't zap" leaves it alone, and the two remaining answers also disable or remove the timer. "Save timeshift and zap" is offered only when no save is already pending, and it is then the first answer - which is what the question takes when nobody answers within the 20 s: **unattended, the receiver saves the timeshift as a recording and zaps**, unrecorded |
| A zap timer for picture-in-picture | No. It plays in the small picture |
| The EPG, first OK on a channel | No. It is a preview |
| The EPG, a second OK on the same channel | Yes. It confirms the preview through the channel selection |
| Closing the EPG after a preview | No. The receiver returns to the channel it was on, with `playService`, while the image's EPG preview mode is on (its default for every EPG style). With preview mode off, closing confirms the preview instead, and that is recorded |
| After an EPG preview closed that way | A gap. Until the channel list is next used - a channel chosen in it, a confirmed EPG zap, or the image's zap back - a zap back to the channel that was playing before the preview is not recorded. That includes a `cmd/zap` to it, which the plugin counts as recorded because it only checks that the channel plays (read from the code, not run) |
| OpenWebif's own zap | Not read |

Only the parts marked measured and the timeshift section below were checked on a receiver. The
zap-timer rows for standby and timeshift, the EPG rows and the post-preview gap are read from the
bytecode and not run.

### A zap from Home Assistant during timeshift

While the receiver is in timeshift - the service is seekable and timeshift is enabled, or a
timeshift is waiting to be saved - `cmd/zap` plays the channel directly (exit 4 above). That is
every `cmd/zap`: the companion integration's channel select, the source list, `play_media`, the
zap action. Its history select sends `cmd/zap_history`, and its bouquet select `cmd/bouquet`;
both are below.

What happens, measured on OpenViX 6.6:

- The channel is tuned with **no question on the television**, and the receiver shows the new
  channel live.
- The zap is **not in the zap history**. The list is left as it was, so the channel playing is not
  its current entry, and a consumer showing the current entry has none to show (the companion
  integration's history select reads `unknown`) until the next recorded zap.
- **The timeshift buffer stays until the next channel change.** The buffer file
  (`pts_livebuffer_<n>` in the image's timeshift directory) and its `.sc` index file were still
  there right after the zap, no longer growing: same size and time ten seconds later - about 28 MB
  after roughly 25 s of timeshift on a high-definition channel. At the next channel change, about
  40 s later, the receiver deleted them: the directory was empty afterwards, its time that of that
  zap. The mechanism, read from the bytecode: on every service start the image deletes the
  `timeshift.*` and `pts_livebuffer_*` files in its timeshift directory that are older than 3 s
  (not in standby, and only when the directory is writable). On OpenViX 6.6 that is the only path
  that deletes the buffer: the image also creates a clean-up timer, but nothing ever starts it.
  The zap that ends timeshift cannot delete its own buffer, which is younger than that; the start
  of the next channel does. The plugin deletes nothing.
- `last_error`: the zap is a successful command, so it **clears** a pending one - on the drill,
  the `history_clear` timeshift refusal was cleared about 20 ms after the zap. It sets none unless
  the image's `playService` raises (exits above).

The plugin does this because the receiver's own zap would ask a question on the television with
no timeout, and a zap from a phone must not leave a question on somebody's screen. It also plays
directly where the receiver would not have asked - its "check timeshift" setting off, or a
timeshift already stopping. That is conservative and costs only the history entry.

What the receiver does on its own path, read from the bytecode and not something the plugin does:
with "check timeshift" on (the default) it asks. With the timeshift save action "ask user" (the
default) it offers four answers, in this order: two that save the timeshift as a recording (one
stops recording afterwards, one goes on recording), "Yes, but don't save", and "No". With any
other save action it asks Yes or No, and Yes applies that configured action, which may save the
timeshift as a recording. Every answer but "No" ends with the image erasing its `timeshift.`
files - after saving, for the answers that save. The `pts_livebuffer_` files are links to those
and are not touched then; they go at the next service start, exactly as after the plugin's direct
play. So "Yes, but don't save" leaves the disk no tidier than a zap from Home Assistant does. The
plugin answers none of these questions and saves nothing. **Careful when scripting `cmd/key`
against that question:** `KEY_OK` takes the highlighted answer, which is the first one and saves
the buffer as a recording; `KEY_EXIT` closes it without saving or zapping.

The other commands during timeshift:

- `cmd/zap_history` is recorded and leaves timeshift without asking, as the receiver's own History
  Zap screen does (read from the code, not measured).
- `cmd/bouquet` is refused: "timeshift is active; the receiver would ask on screen whether to leave
  it".
- `cmd/history_clear` is refused with reason `timeshift` (measured).

**Not measured:** more than one removal of the buffer - that only the start of a new channel
deletes it is read from the bytecode, and the deletion itself was seen once, with the directory's
change time matching the next zap; what a direct play does to a timeshift already
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

### `cmd/list` - a list of choices - added after 0.4.0 (unreleased), capability `list`

Home Assistant puts a question on the television - "Doorbell: open the gate / ignore", "TV off in
1 minute: +15 minutes / cancel" - as a list of labelled rows. The viewer picks one with the remote,
and the receiver answers **once** on `list_answer` (section 1) with the `id` of the row picked, or
how the list ended without one. It never learns what a row does and never calls a service. A
yes/no question is a list of two rows
([ADR-0023](adr/0023-a-list-of-choices-is-answered-by-row-id-once.md)).

```json
{"id": "a1b2c3d4e5f60718",
 "payload": {"title": "Doorbell", "text": "Someone is at the gate.", "timeout": 60,
             "default": "ignore",
             "rows": [{"id": "open", "label": "Open the gate", "key": "green", "icon": "gate"},
                      {"id": "ignore", "label": "Ignore", "key": "red"}]}}
```

The list's id is the command's id, from its envelope (above), and its answer says it back. A sender
gives every list an id - a list sent without one is shown and answered all the same, and its answer
has no `id`.

| Member | Type | Rule |
|---|---|---|
| `title` | string | Required. Cleaned by the text rule below and cut at **60** characters; blank after that is refused. Shown whole, on two lines where it needs them |
| `text` | string or `null` | Optional: lines under the title. Cleaned by the text rule, cut at **200**; absent, `null` or blank is no text. A line break stays one. Shown whole: the screen is as tall as the text needs |
| `rows` | list | **1 to 24** row objects, shown in this order |
| `rows[].id` | string | **1 to 32** characters out of `A`-`Z`, `a`-`z`, `0`-`9`, `.`, `_`, `:` and `-`; unique within the list - case matters, so `a` and `A` are two ids |
| `rows[].label` | string | Required; cleaned by the text rule and cut at **60**; blank is refused |
| `rows[].key` | string or `null` | Optional: a key that picks the row at once - `red`, `green`, `yellow`, `blue`, or one of `"1"` to `"9"` (a string, not a number); unique within the list |
| `rows[].icon` | string or `null` | Optional: one of the icon names of the receiver menu (section 3). Any other name is drawn with `neutral` and is **not** refused; absent or `null` is no icon |
| `timeout` | integer | Optional, seconds, **5 to 600**, **60** when absent. A JSON integer - not a boolean, not a fraction such as `60.0`, not a string, not `null`; anything outside the range is refused. **There is no list without a timeout** |
| `default` | string or `null` | Optional: the `id` of one of the rows. The selection starts on it, and a timeout answers it. An id that is no row's is refused |

Members of the payload or of a row that the plugin does not know are **ignored**, and its log says
so once a run with their count, so a newer sender can add some. Everything the table names is held
to its rule strictly: a refusal names the member and the row's index, counted from 0 as in JSON -
"rows[1].key is the key of an earlier row" - and never the sender's text, which is not in the
plugin's log either. The command's 4096-byte limit applies as for every command; 24 rows of 60
characters fit.

**The text rule.** `title`, `text` and every `rows[].label` are cleaned in this order, and only
then judged blank - the companion integration applies the same rule before it sends, so a list it
accepts is never refused here as blank, and the label it reports is the one on the screen:

1. every backslash is removed (the toast's rule: enigma2's renderer reads what follows one as an
   escape);
2. `text` only: `\r\n` and `\r` become `\n`;
3. every character of Unicode category **Cf** - bidirectional overrides and isolates, the zero-width
   space and joiners, the soft hyphen, the byte order mark - and **Cs** - a lone surrogate, which
   JSON can spell and no UTF-8 holds - is dropped;
4. every character of category **Cc**, **Zl** or **Zp** becomes a space - except `\n` in `text`,
   which stays a line break;
5. it is cut at its limit: 60 for `title` and `label`, 200 for `text`;
6. leading and trailing whitespace is stripped; what is empty then is blank - refused for `title`
   and `label`, no text for `text`.

**What the viewer sees and can do.** The list is shown **over whatever is on screen** - live
television, a playing recording, the receiver menu, the image's keyboard, any other screen - which
stays open underneath and gets the keys back when the list has gone. A header line says that the
question comes from Home Assistant through this plugin, so a list cannot pose as the receiver's own
dialog; under it the title, the text, the rows - each with its icon, its label and, on its right,
its key: a filled rounded button in the key's colour, or the digit in a small box - the remaining
time, counting down, and the keys' legend. With a `default` row the countdown names it - "Ignore in
45 s" - so the viewer sees what waiting answers; without one it reads "Closes in 45 s". UP and DOWN,
and the page keys, move the selection, which starts on the `default` row or else on the first. **OK
picks** the selected row; a row's colour or number key picks that row at once; **EXIT** dismisses.
Every other key does nothing - a colour or number that no row has as well - except the receiver's
own global keys, such as the volume. The power key does on the list what it does on the screen under
it: over live television it puts the receiver in standby, which closes the list. The plugin's own
screen has the look of the receiver menu, see-through as `menu_transparency` says (section 1).

**How it ends.** Exactly one answer for every list the plugin accepted - or, for one it could not
put on the television after all, one late refusal (below):

| `ending` | When | `row` |
|---|---|---|
| `picked` | OK on a row, or the row's own key | the row's `id` |
| `timeout` | `timeout` seconds after it was shown | the `default` row's `id`, or `null` without one: a timeout is never mistaken for a press |
| `dismissed` | EXIT | `null` |
| `replaced` | A newer `cmd/list` arrived: the open list is answered first, and the new one is shown once the old screen has gone. A newer list that is refused leaves the open one alone | `null` |
| `closed` | The receiver went to standby; the plugin stopped - an interface restart, a shutdown, a settings save that reconnects, an update closing its doors, the plugin being removed; or something else closed the screen. A save applied under the open session - every setting but the connection's and `enabled` - leaves the list on the screen. Published only while the session is up at that moment, and never later | `null` |

The first way to end wins: a timeout and an OK in the same instant, an EXIT on a list that was just
replaced, a pick on a list closed for standby - the later one finds the list answered and does
nothing. While the session is between two screens - one is closing - the list waits for it, at most
two seconds; a list that cannot be put on the television after all is refused on `last_error`
(`not_shown`), with the command's id, and has no answer. A list arriving while another is waiting
replaces that one too.

**Which screen.** The plugin's own screen where it can be built, and otherwise the image's own
choice list, with the same rows, ids and keys, and the header line, the title and the text as its
question. Where the image puts the question's first line in the list's window title - OpenViX and
OpenATV, from their sources - the window title reads "<header> - <countdown>", so the header stays;
where it puts the whole question in its text - OpenPLi - the window title is the countdown. Where
the image does not let the title be set, there is no countdown. `info.list.screen` (section 1) says
which. A skin may define a screen called `MQTTBridgeChoices` and restyle the plugin's own; one that
cannot be built with the plugin's widgets makes the image's list the one used.

**No permission and no setting**, as a message has none. Anyone who can publish on the broker can
put a list on the television, or publish an answer for one - so an automation never takes an answer
as proof of who pressed what ([SECURITY.md](../SECURITY.md)).

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
context has `cmd/bouquet`; neither broadens this settings API. The command accepts `publish_keys`,
`screenshot`, `screenshot_interval`, `screenshot_delay`, `cam_telemetry`, `oscam_telemetry`,
`softcam_autoheal`, `softcam_autoheal_seconds` and - added after 0.4.0 (unreleased) - `menu_hotkey`
and `menu_transparency`, with their JSON types unchanged. **Keys left out keep their value**: every
key is optional, so a consumer sends only what it changes. Up to 0.4.0 the first three were required
in every object; added after 0.4.0 (unreleased), they are optional like the rest, and an empty
object is valid, changes nothing and still publishes `info` again. A consumer that writes one key
alone checks first that `info.settings` carries that key - a plugin that publishes it accepts a
partial object - and a consumer that also serves older plugins goes on sending the three.
`menu_hotkey` only chooses which key opens a menu the receiver already offers; `menu_allowed`, which
decides whether it does, is refused here like every permission, and with it off the key opens
nothing. A new key is bound at once, without a reconnect. `menu_transparency` is an integer from 0
to 80, a multiple of 10 - anything between two steps is refused, never rounded - and the menu takes
it the next time it is opened; a menu on screen keeps its look. An object holding any other key is refused whole, with
`last_error` "the config object contains unknown settings: " and the keys it refused, as in
`the config object contains unknown settings: 'host', 'update_allowed'`. Their names only, never
a value: each in single quotes, sorted, separated by a comma and a space; the first five, and
then " and N more" for the rest. A name longer than 32 characters is cut to 32 and an ellipsis.
A character in a name that cannot be printed, and an apostrophe, is shown as `?`, so what is
between two quotes is always one name; a broker or OSCam password sent as a name, or inside one,
is shown as `***`. Up to 0.4.0 the sentence ended at "settings" and named none of them. The two
softcam keys only *tune* a restart the receiver has already permitted; with
`softcam_restart_allowed` off they change nothing, because the
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
   topics, every `channels/<bouquet_slug>` and `epg_grid/<bouquet_slug>`, the announcement, and every Home Assistant discovery
   payload named in the state file - and then the plugin forgets what it had announced. One
   payload is left alone (added after 0.4.0, unreleased): a discovery payload whose replacement
   would be over the packet bound (the conventions at the top of this file). Step 2 could not put
   it back, and emptying it deletes the device and its entities in Home Assistant.
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
  "plugin": "0.4.0",
  "boxtype": "vuuno4kse",
  "mac": "00:00:5e:00:53:01",
  "ip": "192.0.2.12",
  "capabilities": ["power", "service", "epg", "tuner", "softcam", "recording", "timers",
                   "volume", "hdd", "process", "channels", "channel_topics", "bouquet_context",
                   "epg_grid", "keys", "screenshot", "toast", "message", "command_id"],
  "ha_mode": "discovery"
}
```

`name` is the `friendly_name` setting - the device name a user sees. `base_topic` is repeated
here so that a consumer can find the state topics without being told where to look; a box behind
an MQTT bridge with a rewritten prefix is the case this serves.

The companion integration subscribes to `enigma2mqtt/discovery/#` and offers a config flow for
every announcement it sees. Anything else that wants to enumerate boxes on a broker can do the
same.

### Topics the plugin reads

Besides its own command topics, the plugin subscribes to topics it does not own: two always, and
four more while `menu_allowed` is on.

| Topic | Published by | What the plugin does with it |
|---|---|---|
| `enigma2mqtt/release_index` - since 0.4.0 | The companion integration, retained, after it verified a newer index: `{"index": "<base64 of the exact signed bytes>", "sig": "<base64 of the signature file>"}` | Judges it by the rule for accepting an index ([RELEASE-INDEX.md](RELEASE-INDEX.md)), exactly as an index it fetched itself, and keeps it when the rule accepts it - `update.index.source` is then `relay`. A refusal is a line in the plugin's log, never `last_error`. A payload over 88 KiB is dropped unread. It needs no setting and causes no connection, so a receiver without internet learns of releases this way. The plugin never publishes to it and never retracts it |
| `enigma2mqtt/integration/<node>` - since 0.4.0 | The companion integration, retained, QoS 1: `{"integration": "0.4.0", "contract": 1, "plugin_min": "0.2.0"}`; retracted when its entry is removed | Keeps it in memory and judges `update.available` and `cmd/update` by it: a release of another contract major than the integration's, or one whose `min_integration` is above `integration`, is `incompatible`, and `plugin_min` raises the floor. An empty payload forgets it, and an unreadable one is ignored. A forged one can only narrow or widen the choice among signed, non-withdrawn releases of one major. The plugin never publishes to it and never retracts it |
| `enigma2mqtt/menu/<node>` - added after 0.4.0 (unreleased), capability `menu` | The companion integration, retained, QoS 1: the receiver menu (below); an empty payload when there is no menu | Validates it whole and shows it; an invalid payload is ignored with one line in the log and the menu before it stays. Subscribed only while `menu_allowed` is on. The plugin never publishes to it and never retracts it |
| `<ha_discovery_prefix>/status` - added after 0.4.0 (unreleased), capability `menu` | Home Assistant's own birth and last will, `online` or `offline`, under the `ha_discovery_prefix` setting (`homeassistant/status` by default) | `offline` greys the receiver menu; `online`, or nothing heard since the broker connection last changed, does not. Subscribed only while `menu_allowed` is on, and read for nothing else |
| `enigma2mqtt/menu_catalogue/<node>` - added after 0.4.0 (unreleased), capability `menu_editor` | The companion integration, retained, QoS 1, while this receiver claims `menu_editor`: what the menu creator chooses from (below), or only that editing from the receiver is switched off in Home Assistant; an empty payload when there is none | Checks the envelope and keeps the exact bytes for the menu creator's page; an invalid one is ignored with one line in the log and the one before it stays. At most 1 MiB. Subscribed only while `menu_allowed` is on. The plugin never publishes to it and never retracts it |
| `enigma2mqtt/menu_edit_result/<node>` - added after 0.4.0 (unreleased), capability `menu_editor` | The companion integration, not retained, QoS 1: its answer to one `menu_edit` (below) | Hands it to the save waiting for that `id`; an answer for an id nothing waits for, a retained one and an unreadable one are ignored. Subscribed only while `menu_allowed` is on |

### The receiver menu - added after 0.4.0 (unreleased), capability `menu`

A "Home Assistant" menu on the receiver that Home Assistant keeps up to date: one level of groups,
each a list of rows, and rows at the top as well. Every row is a control bound to something in
Home Assistant - a light, a scene, a cover, a script. The viewer opens it with the remote - the
Extensions menu entry "Home Assistant", and a hotkey (`menu_hotkey`, a long HELP by default) in
live TV and the players - moves with the arrows and acts with OK. **The receiver never learns what a row does and never
sends a service call**: it sends a row's id and a value on `menu_action` (§1), and Home Assistant
performs the action and publishes the menu again
([ADR-0020](adr/0020-the-receiver-menu-is-drawn-on-the-receiver-and-decided-in-home-assistant.md)).

The menu is published by the companion integration, retained, at QoS 1, on
`enigma2mqtt/menu/<node>` - outside `<base>`, like `enigma2mqtt/integration/<node>`, because the
integration owns it. Each publish is the whole menu. An empty payload means "no menu".

```json
{"v": 1, "rev": 42, "title": "Home Assistant",
 "items": [
   {"type": "group", "id": "g_salon", "label": "Living room", "icon": "sofa",
    "rows": [
      {"type": "toggle", "id": "r_lamp", "label": "Lamp", "icon": "lamp",
       "value": true, "state": "on", "color": "green", "available": true},
      {"type": "slider", "id": "r_blind", "label": "Blind", "icon": "blinds",
       "value": 40, "min": 0, "max": 100, "step": 10, "unit": "%",
       "state": "40 %", "available": true},
      {"type": "selector", "id": "r_mode", "label": "Heating", "icon": "radiator",
       "value": "heat", "options": ["off", "heat", "auto"], "state": "heat",
       "available": true}]},
   {"type": "button", "id": "r_movie", "label": "Movie night", "icon": "movie",
    "state": null, "available": true},
   {"type": "indicator", "id": "r_door", "label": "Front door", "icon": "door",
    "value": false, "state": "closed", "color": "green", "available": true},
   {"type": "input", "id": "r_note", "label": "Note", "icon": "note",
    "value": "", "max_length": 60, "state": "", "available": true}]}
```

| Field | Rule |
|---|---|
| `v` | `1`. A plugin that does not know the number shows no menu and says so in its log, once |
| `rev` | Integer, higher with every publish of this node's menu within one run of the integration; a new run may start lower. Said back in `menu_action`; the receiver compares `value`s, never `rev` |
| `title` | Text, cut at 60; `"Home Assistant"` when absent |
| `items` | The top list: at most **24** entries, each a group or a row, at most **8** groups |
| `group` | `id`, `label`, `icon` and `rows` - at most 24 rows; a group holds no group. A group without rows opens to a line saying there are no controls in it |
| row `type` | `button`, `toggle`, `indicator`, `selector`, `slider` or `input`. An open enumeration: a row of a type the plugin does not know is shown with its label and state, OK on it sends nothing, and the plugin's log names the type once |
| `id` | `[A-Za-z0-9_-]{1,32}`, unique in the whole menu - groups included - and stable for the same row across publishes and restarts |
| `label` | Text, cut at 60 |
| `icon` | One of the icon names below; any other name, or none, is drawn with `neutral` |
| `state` | What the row shows on its right: text, cut at 24, or `null` |
| `color` | `green`, `red` or `null`, for a `toggle` and an `indicator`: the colour of `state`. Green means "all good" and red "needs attention"; Home Assistant decides which, row by row, and the receiver draws what it gets. Any other value is `null` |
| `value` | A `toggle` and an `indicator`: `true`, `false` or `null`. A `selector`: one of `options`, or `null`. A `slider`: a number or `null`. An `input`: text or `null`. A `button` has none |
| `options` | A `selector` only: at most 24 texts, each shown cut at 60 and sent back exactly as listed |
| `min`, `max`, `step`, `unit` | A `slider` only: numbers, `step` above 0, `max` at least `min`, at most 200 steps between them; `unit` text cut at 8, or `null` |
| `max_length` | An `input` only: 1-255 |
| `available` | `false` greys the row and nothing can be done with it; `true` when absent |

Fields the plugin does not know are ignored. Anything else that breaks a rule - a count over its
bound, a type that is wrong, an id that repeats, a `value` that is not one of the `options`, a
payload over 64 KiB or one that is not a JSON object - makes the whole payload invalid: it is
ignored with one line in the log, and the menu shown is the one before it. So is a payload nested
too deeply to parse, or a slider number too large for a float. Every text loses its backslashes
and line breaks before it is shown, as a toast's does (section 2), its invisible format
characters (Unicode category Cf - bidirectional overrides and isolates, zero-width characters, the
soft hyphen) and any lone surrogate (category Cs), which JSON can spell and no UTF-8 holds; an
option is still sent back exactly as listed.

**The icon names** are part of the contract; the integration offers exactly these: `neutral`,
`lamp`, `ceiling-light`, `blinds`, `scene`, `script`, `power`, `fan`, `lock`, `radiator`,
`thermometer`, `tv`, `speaker`, `door`, `gate`, `garage`, `alarm`, `camera`, `sofa`, `bed`,
`kitchen`, `garden`, `movie`, `note`, `home`. Each is drawn with an icon of Material Design Icons,
under Apache-2.0 ([NOTICE](../NOTICE)).

**What the viewer can do.** OK on a group opens its rows, and EXIT goes back; EXIT on the top
list closes the menu. OK on a `button` sends `press`, on a `toggle` `toggle` with the state the row
showed (`seen`, [section 1](#basenodemenu_action---added-after-040-unreleased)). OK on a `selector`
lists its options: OK sends the one chosen as `set`, EXIT discards. On a `slider`, LEFT and RIGHT
move a value on the receiver by `step` within `min` and `max`, shown and not sent; OK sends it as
`set`, EXIT discards it. OK on an `input` opens the image's keyboard with `value` in it: OK sends
the text as `set`, EXIT discards. **OK on the value the row already has sends nothing**: the option
already set, a slider moved back to (or held at) its value, typed text equal to it - the list or
keyboard closes, and there is no `menu_action`, no pending mark and no notice. While a pending
mark holds another value the viewer asked for, picking the row's value again is a change back and
is sent. A slider moved and not applied is discarded when the selection leaves its row. The keys
1-9 move the selection to the row of that number and do nothing else; OK acts. An `indicator`
only shows its state. The image's own list, where the plugin's screen cannot be built, has the
same rows and no number keys - it would act on a digit; a slider there lists its values.

**The pending mark.** After OK on a `toggle`, a `slider` or a `selector` the row shows a mark until
the menu carries a different `value` for it. When none comes within **5 s**, the row shows its
real value again and the footer says Home Assistant changed nothing.

**Greyed.** With no connection to the broker, or with Home Assistant's status `offline`, the menu
stays as it last was, greyed, with a notice, and nothing in it can be acted on. A status of
`online`, or none heard yet, does not grey it. Home Assistant's birth is not retained, so the
plugin forgets the status whenever its broker connection goes or comes back - an `online`
published while the receiver was away would otherwise never undo an `offline` from before - and a
menu that arrives live (retain flag 0: the integration published it just now) counts as `online`.
A retained menu delivered on subscribing proves nothing about Home Assistant and does not count,
nor does an invalid one. A menu that never came, or an empty payload, is "no menu yet": nothing to
act on, and nothing greyed.

**Open while it changes.** A new payload while the menu is open is drawn in place: the selection
stays on its row, or on the nearest one when that row has gone, and a slider being moved keeps its
value until OK or EXIT. A list of options keeps the selection on the same option when the options
are reordered, and OK sends nothing when the highlighted option has gone.

**How it looks.** The background of the plugin's own screen and of its list can be made
see-through with `menu_transparency` (§1) - 20 % by default, 0 is opaque, at most 80. The text, the
icons and the bar on the selected row stay opaque. The image's own list, the fallback, is drawn as
the image draws it.

### The menu creator - added after 0.4.0 (unreleased), capability `menu_editor`

A page in the receiver's OpenWebif - the Menu subpage of the plugin's page,
[ADR-0022](adr/0022-the-menu-creator-page-runs-a-script-every-other-page-stays-script-free.md) -
where the receiver menu is built from a browser: groups and rows, each row an entity with a control,
a label and an icon, as the companion integration's menu options offer them. **Home Assistant stays
the owner of the menu and the receiver never holds a Home Assistant token**: the integration
publishes a catalogue of what can be chosen and the menu as it stores it; the page edits a copy and
the receiver publishes the edit on `menu_edit` (§1); the integration validates it with the rules of
its own menu options, stores it or refuses it, and answers. **Editing from the receiver is off by
default**, and switched on in Home Assistant, in the integration's receiver menu options. While it is
off the catalogue says so and nothing else, and every edit is refused with `disabled`. Switched on,
anyone who can reach the receiver's web interface, or publish `menu_edit` on the broker, can choose
which entities the television controls and then operate them ([SECURITY.md](../SECURITY.md)).

**The catalogue**, retained, QoS 1, on `enigma2mqtt/menu_catalogue/<node>`, published by the
integration and retracted - an empty payload - when its entry is removed or the node id changes.
While editing from the receiver is switched off in Home Assistant it is this alone, whether or not
the receiver has claimed `menu_editor` or reported at all:

```json
{"v": 1, "editing": false}
```

and while it is on, published while this receiver claims `menu_editor` and retracted when the
claim goes:

```json
{"v": 1,
 "editing": true,
 "revision": "3f9c2e4a1b7d5c60",
 "areas": ["Kitchen", "Living room"],
 "left_out": 0,
 "menu": [
   {"type": "group", "id": "g_0a1b2c3d4e", "label": "Living room", "icon": "sofa",
    "rows": [
      {"type": "row", "id": "r_5f6a7b8c9d", "entity": "light.living_room", "control": "slider",
       "label": null, "icon": null}]},
   {"type": "row", "id": "r_1a2b3c4d5e", "entity": "scene.movie_night", "control": "button",
    "label": "Movie", "icon": "movie"},
   {"type": "row", "id": "r_9e8d7c6b5a", "entity": "0123456789abcdef0123456789abcdef",
    "control": "toggle", "label": null, "icon": null, "missing": true}],
 "entities": [
   {"e": "light.living_room", "n": "Living room lamp", "a": 1,
    "c": ["toggle", "slider", "indicator"]},
   {"e": "sensor.outdoor_temperature", "n": "Outdoor temperature", "a": null,
    "c": ["indicator"], "h": true, "k": "diagnostic"}]}
```

| Field | Rule |
|---|---|
| `v` | `1`. A plugin that does not know the number - above 1 - ignores the catalogue, and the page says it comes from a newer integration; one below 1, or not a number, makes it invalid |
| `editing` | `true` or `false`: whether editing from the receiver is switched on in Home Assistant. `false` comes alone, with none of the fields below, and the page shows no editor. A catalogue without it is read as `true` |
| `revision` | 16 lowercase hexadecimal digits: the start of the sha256 of the menu as the integration stores it. It changes exactly when the stored menu does |
| `areas` | The area names, at most 60 characters each, referred to by their place in the list |
| `left_out` | How many entities are not in `entities` because the catalogue would have been over 1 MiB: hidden ones first, then diagnostic, then configuration entities, then the rest from the end of the name order. The page says how many |
| `menu` | The menu as stored: groups and rows, with their ids. A row's `entity` is the entity id it is bound to now; a row whose entity cannot be found carries what it was bound to and `"missing": true`. `label` and `icon` are a row's own choices, `null` for the defaults |
| `entities` | Every entity a row can be bound to, sorted by name: `e` the entity id, `n` the name a row shows by default (cut at 60), `a` the area's place in `areas` or `null`, `c` the controls it can take - the first is the default - and `h` (`true` when hidden) and `k` (`config` or `diagnostic`), absent otherwise |

The plugin accepts at most 1 MiB (1,048,576 bytes), checks the envelope - an object of `v` 1 and
either `"editing": false` or a `revision`, a `menu` and an `entities` list - and keeps the exact
bytes for the page. A number of more than 32 digits, or NaN or an infinity, makes it invalid. One that breaks
it, or cannot be parsed, is ignored with one line in the log, and the catalogue before it stays.
**Every string in it is untrusted text** - anyone who can publish on the broker can write this
topic - and the page puts it on screen as text, never as markup.

**The answer**, not retained, QoS 1, on `enigma2mqtt/menu_edit_result/<node>`, one for each
`menu_edit` whose `id` the integration could read:

```json
{"id": "8c1d2e3f4a5b", "result": "stale", "revision": "a1b2c3d4e5f60718",
 "path": null, "detail": null}
```

| Field | Rule |
|---|---|
| `id` | The edit's `id` |
| `result` | `saved` - stored; `stale` - the menu changed since `base`; `invalid` - a rule refused it; `busy` - another save of this receiver is being handled, or one was stored less than two seconds ago; `not_claimed` - the integration does not see this receiver claim `menu_editor`; `disabled` - editing from the receiver is switched off in its options. An open enumeration |
| `revision` | The menu's revision after the answer: the new one after `saved`, where it is required; `null` after `disabled` and `not_claimed`, and may be `null` after any answer but `saved`. A `saved` without a revision is not an answer, and the save waits on |
| `path` | `invalid` only: where, as in `menu[2].rows[5].control`; `null` otherwise |
| `detail` | `invalid` only: one English sentence of at most 200 characters; `null` otherwise |

**What the page does with it.** A save waits for its answer at most **10 s**; without one the page
says Home Assistant did not answer and offers to reload - a late save may still have landed, and the
reload shows it. After `saved` the page waits for the catalogue with the new `revision`, at most
5 s, so the ids of new groups and rows are known before the next save. After `stale` it asks to
reload, and keeps the unsaved edit on screen until the reload. A saved edit replaces the menu the
integration's own options hold; a save from those options afterwards, made from a copy older than
the edit, replaces it again.

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
| `channel_select` | select | `service` | Options are the channel names of the configured bouquets; selecting one publishes `cmd/zap`. Left out when the payload would not fit one packet with it - below |
| `history_clear` | button | - | Since 0.3.0, when `history_clear` is a capability. Publishes `cmd/history_clear`: the receiver switches to channel 1 and empties its zap history |
| `screen` | image | `screen` | `image/jpeg` |
| `screenshot`, `restart_gui`, `refresh_discovery` | button | - | And `deep_standby` and `reboot` **only** when `deep_standby_allowed` is on |
| `clear_error` | button | - | Added after 0.4.0 (unreleased). Publishes `cmd/clear_error`. A configuration entity, always announced: it needs no capability and no permission |
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

**The channel select is left out when the payload does not fit with it.** Added after 0.4.0
(unreleased). Its options are every channel name, which makes it the one component that grows with
the receiver. When the device payload with the select would be over the packet bound of 1,000,000
bytes (the conventions at the top of this file), the payload is published **without the select**,
and every other entity is announced as usual - where a payload that is not published at all would
leave a first install with no device. A select that had been announced is removed by name, the way
any component that is no longer announced is (below), so Home Assistant does not keep it with the
options it last had; and it is announced again, with its options, the next time the discovery
payload is published with names that fit - on the next connect, after a settings save such as a
narrower `bouquets_for_select`, or on `cmd/discovery`. A bouquet file edited on the receiver does
not republish discovery by itself. **Removing the select deletes the entity in Home Assistant, and
announcing it again creates a new one**: a name, an area, an icon or a place on a dashboard that
somebody gave the old one is not carried over. The receiver's log says so once, when it starts:
`the channel select is left out of the discovery payload: with 9412 channel name(s) it would be
over the 1000000 byte packet limit`. `cmd/zap` by name does not depend on the select. Only a
payload that is too big even without the select is not published, and is then named in
`info.not_published` (§1).

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

`stream`, `stream_clients` and `stream_playable` (added after 0.4.0, unreleased) have **no
discovery components**: a list of clients that changes shape, a command that needs the row it is
about, and a question whose answer is an event are the companion integration's to build from the
topics. The new members of `service` and `volume.passthrough` are in no discovery payload either
- the channel sensor's attributes are the five listed below, and the volume number and the mute
switch read `level` and `muted`. A plugin-only install reads them from the broker.

`list` (added after 0.4.0, unreleased) has **no discovery components** either: a list of choices
is a question with rows, a timeout and an answer, which an automation or the companion
integration's action asks and waits for - not a button or a sensor.

### The attributes the sensors carry

Two of those components carry JSON attributes rather than only a state, because the state is a
name and the useful identifier is not. They are part of this contract - SETUP.md's `universal`
media_player recipe reads `sensor.<box>_channel|sref`, and that only works because the attribute
is promised here.

| Component | State | JSON attributes |
|---|---|---|
| Channel sensor | the channel name | `sref`, `bouquet`, `provider`, `width`, `height` - five members of the `service` payload, and not the codec members added after 0.4.0 |
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
node has published retained**: the state topics, every `channels/<bouquet_slug>` and
`epg_grid/<bouquet_slug>`, the announcement, and every Home Assistant discovery payload. Beside
that list it keeps indexes that a topic name alone cannot answer: the **slugs** of the EPG grids
and of the per-bouquet channel lists it has published, so a
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

This section is where a planned addition is written down before it is built, so that a consumer can
be written against its shape; the previous ones - `uninstall_allowed` and `cmd/uninstall`
([ADR-0004](adr/0004-remote-uninstall.md)), `info.build` and `info.contract`, and the updates
from a signed release index ([ADR-0015](adr/0015-signed-self-update.md)) - the check
(`update_check`, the `update` topic, `cmd/update_check` and the relayed
`enigma2mqtt/release_index`), the install (`update_allowed`, the capability `self_update`,
`cmd/update`, `update.transaction` and the subscription to `enigma2mqtt/integration/<node>`) and
the relay handshake of a receiver without internet (`relay_request` and `cmd/relay`) - are in §1,
§2 and §3 now. Until a capability is in `info.capabilities`, the box does not have it - that rule
is unchanged, and it is how a consumer tells a plan from a feature. Nothing is planned at the
moment.

| Kind | Name | Shape |
|---|---|---|

---

## 6. Worth knowing

- **A retained state topic is a snapshot, not a heartbeat.** `service` showing a channel means
  that is the last channel the box tuned - check `availability` before believing it is on.
- **An ACL-denied publish looks exactly like a successful one.** Mosquitto drops it silently.
  When the topics are missing and the log shows no error, subscribe as a privileged user and
  publish as the box's user to find out which it is.
- **Nothing here is a request/response protocol.** If a command seems not to have worked, read
  its state topic and then `last_error`; there is no per-command reply to wait for. An id on a
  command (section 2) changes one thing: a refusal says which command it refuses. A command that
  worked is still answered by its state topic alone. The two exceptions are commands that are a
  question: `cmd/stream_playable` has no state to change, and its answer is a message on
  `stream_playable`; `cmd/list` asks the viewer, and its answer is a message on `list_answer`.
