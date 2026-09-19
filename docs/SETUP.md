# Configuring the plugin

Everything lives under `config.plugins.mqttbridge.*` in enigma2's own settings file
(`/etc/enigma2/settings`) and is edited on the box through *Menu → Plugins → MQTT Bridge*. There
is no separate configuration file to maintain, and settings survive a plugin upgrade.

## Settings

| Setting | Default | What it does |
|---|---|---|
| `enabled` | `on` | Master switch. Off means the plugin loads and does nothing — reachable from the setup screen even when the broker is unreachable, which is the point |
| `host` | — | Broker address. Empty means the plugin stays idle and says so once in the log |
| `port` | `1883` | `8883` for TLS |
| `tls` | `off` | TLS to the broker. Client certificates are not in v1 |
| `ca_file` | — | Path to a CA bundle on the box, for a broker with a private CA |
| `username` | — | Broker login. Give the box **its own**, not Home Assistant's |
| `password` | — | Entered as a password field and never written to the log |
| `node_id` | `<boxtype>_<mac6>` | For example `vuuno4kse_005301`. Lowercase ASCII, stable across reinstalls, and the unique identity everything keys on. Changing it orphans the old retained topics — run `cmd/reset` first |
| `friendly_name` | the box type | The device name a user sees in Home Assistant |
| `base_topic` | `enigma2` | State publishes under `<base_topic>/<node_id>/` |
| `ha_discovery_prefix` | `homeassistant` | Match your Home Assistant MQTT integration if you changed it there |
| `ha_mode` | `discovery` | `discovery` / `integration` / `off` — see [TOPICS.md](TOPICS.md#cmdha_mode-semantics) |
| `publish_keys` | `on` | Remote-key events on the `key` topic. Off if you do not automate on them — it is a log of what is pressed |
| `screenshot` | `on zap` | `off`, `on zap`, or `interval N s`. At most one capture per five seconds whatever this says |
| `screenshot_delay` | `4` | Seconds to wait after a zap before capturing. Another zap restarts the wait |
| `cam_telemetry` | `off` | Publish bounded current-service conditional-access status from `/tmp/ecm.info` |
| `oscam_telemetry` | `off` | Publish privacy-reduced OSCam software and reader/server health |
| `oscam_port` | `8888` | Receiver-local OSCam WebIf port; the host is fixed to `127.0.0.1` |
| `oscam_username` | — | OSCam WebIf login, used only on the receiver |
| `oscam_password` | — | OSCam WebIf password, masked in Setup and excluded from MQTT and logs |
| `epg_grid_events` | `4` | Events per channel in the EPG grid. `0` turns the grid off and drops it from `capabilities` |
| `bouquets_for_select` | all TV bouquets | Which bouquets feed the channel list, the `zap`-by-name lookup and the EPG grid — which publishes **one retained topic per bouquet**, `epg_grid/<bouquet_slug>`. Narrow it if you have hundreds of services. Dropping or renaming a bouquet retracts the topic it owned |
| `deep_standby_allowed` | `off` | Gate for `cmd/deep_standby` and `cmd/reboot`. Off by default because waking the box again needs Wake-on-LAN and that is worth testing before you rely on it |
| `log_level` | `info` | `error` / `warning` / `info` / `debug` |

The log is `/home/root/mqttbridge.log`, capped at 1 MB with two rotations kept, so a debug
session cannot fill the flash.

OSCam telemetry does not enable or change WebIf. Enable OSCam's JSON API yourself, restrict WebIf
to loopback or an explicit local allowlist, and set OSCam `httpreadonly = 1`; the bridge itself
requests only the `status` and `readerlist` views, but cannot enforce OSCam's global policy. The
login is stored by enigma in `/etc/enigma2/settings`: `ConfigPassword` masks it in the UI, not on
disk. It is never sent to Home Assistant, MQTT, provisioning acknowledgements or the log.

## Provisioning file

For headless installs — and for the companion integration's guided installer — write
`/etc/enigma2/mqttbridge.json` before the plugin starts:

```json
{
  "host": "192.0.2.10",
  "port": 1883,
  "username": "enigma2box",
  "password": "the-broker-password",
  "node_id": "vuuno4kse_005301",
  "friendly_name": "Living room receiver",
  "base_topic": "enigma2",
  "ha_mode": "discovery",
  "publish_keys": true,
  "screenshot": "on zap",
  "deep_standby_allowed": false
}
```

Only the keys you include are written; the rest keep their defaults. At start-up the plugin
imports them into its configuration and then **deletes the file**, because it holds a password in
clear. The setup screen shows the same values afterwards and is the way to change them later.

A file it could not use at all — every key misspelt, say — is **kept**, with one line in the log
saying so: a typo should cost you a correction, not the only copy of what you wrote.

Write it with `0600` permissions, and be aware it is on flash between the write and the next
start of enigma2:

```sh
scp -O mqttbridge.json root@<box-ip>:/etc/enigma2/mqttbridge.json
ssh root@<box-ip> 'chmod 600 /etc/enigma2/mqttbridge.json && init 4 && sleep 3 && init 3'
```

## Broker access

Give the receiver a dedicated login with an ACL limited to its own topics. For the Mosquitto
add-on, in the ACL file, with `<node_id>` replaced by the plugin's node id:

```
user enigma2box
topic readwrite enigma2/<node_id>/#
topic write enigma2mqtt/discovery/<node_id>/#
topic write homeassistant/device/<node_id>/#
topic write homeassistant/device_automation/<node_id>/#
```

Then **verify it by effect**. Subscribe as a privileged user to a topic the box has no business
touching and publish there as the box's user: the message must not arrive. Mosquitto drops an
ACL-denied publish silently and the publishing client sees success either way, so an ACL that is
too tight and one that works look identical from the box.

---

# A media player without the custom integration

In `discovery` mode the plugin gives you a switch, a mute switch, a volume number, a channel
select, sensors and buttons — but Home Assistant's MQTT discovery has no way to express a
`media_player`. Home Assistant's own `universal` platform can assemble one out of those entities.

This is the **fallback**. If you can install
[hass-enigma2-mqtt](https://github.com/deltasystems-pl/hass-enigma2-mqtt), use it instead: it
gives a native player with channel browsing, picons, a remote and device triggers, and none of
the plumbing below. The recipe is here for people using another automation system's HA bridge, or
who would rather not add a custom integration.

Save as `config/packages/enigma2_mqtt_player.yaml` and make sure `packages: !include_dir_named
packages` is under `homeassistant:` in your `configuration.yaml`.

Substitute two things throughout: the entity ids, which Home Assistant derives from the
`friendly_name` you set on the box (a box called *Living room receiver* gives
`switch.living_room_receiver_power` and friends — check *Developer tools → States*), and
`enigma2/vuuno4kse_005301` in the MQTT topics, which is `<base_topic>/<node_id>`.

```yaml
# The universal player does no arithmetic, so volume 0-100 becomes 0.0-1.0 here.
template:
  - sensor:
      - name: "Receiver volume level"
        unique_id: enigma2_mqtt_receiver_volume_level
        state: >-
          {{ (states('number.living_room_receiver_volume') | float(0)) / 100 }}

media_player:
  - platform: universal
    name: "Receiver"
    unique_id: enigma2_mqtt_receiver_universal
    device_class: receiver

    state_template: >-
      {% set power = states('switch.living_room_receiver_power') %}
      {%- if power in ['unknown', 'unavailable'] %}off
      {%- elif power == 'off' %}standby
      {%- else %}playing
      {%- endif %}

    attributes:
      source: sensor.living_room_receiver_channel
      source_list: select.living_room_receiver_channel|options
      volume_level: sensor.receiver_volume_level
      is_volume_muted: switch.living_room_receiver_mute
      media_title: sensor.living_room_receiver_programme
      media_channel: sensor.living_room_receiver_channel
      media_content_id: sensor.living_room_receiver_channel|sref

    commands:
      turn_on:
        action: switch.turn_on
        target: {entity_id: switch.living_room_receiver_power}
      turn_off:
        action: switch.turn_off
        target: {entity_id: switch.living_room_receiver_power}

      volume_set:
        action: number.set_value
        target: {entity_id: number.living_room_receiver_volume}
        data:
          value: "{{ (volume_level * 100) | int }}"
      volume_mute:
        action: "switch.turn_{{ 'on' if is_volume_muted else 'off' }}"
        target: {entity_id: switch.living_room_receiver_mute}

      select_source:
        action: select.select_option
        target: {entity_id: select.living_room_receiver_channel}
        data:
          option: "{{ source }}"

      media_next_track:
        action: mqtt.publish
        data:
          topic: enigma2/vuuno4kse_005301/cmd/key
          payload: KEY_CHANNELUP
          qos: 1
      media_previous_track:
        action: mqtt.publish
        data:
          topic: enigma2/vuuno4kse_005301/cmd/key
          payload: KEY_CHANNELDOWN
          qos: 1

      play_media:
        action: mqtt.publish
        data:
          topic: enigma2/vuuno4kse_005301/cmd/zap
          payload: "{{ media_id }}"
          qos: 1
```

`play_media` takes a service reference as its `media_content_id`; pass the `sref` the channel
sensor carries as an attribute — the attributes both sensors carry are listed in
[TOPICS.md](TOPICS.md#the-attributes-the-sensors-carry), which is why `|sref` above is safe to
rely on. `media_content_type` is ignored.

What this recipe cannot give you, and the integration can: browsing bouquets and channels with
picons, the screenshot as the player's artwork, remote keys as device triggers, on-screen
messages as a `notify` target, and an `update` entity that tells you the box is running an old
plugin.

## Privacy

Before you leave this file: the programme sensor, the key events and the screenshot image all go
into Home Assistant's recorder database by default. Exclude what you would not want to read back
in six months.

**With the companion integration installed**, the remote keys are an `event` entity and the
screenshot is an `image`, so both can be named:

```yaml
recorder:
  exclude:
    entities:
      - event.living_room_receiver_key
      - image.living_room_receiver_screen
```

**In plain `discovery` mode there is no `event.<box>_key` to exclude.** The key topic becomes MQTT
*device triggers*, which are not entities and which the recorder never stores, so the first line
above would name something that does not exist. Turn **`publish_keys` off** on the box instead —
which is the stronger control in either case, because the information then never reaches the
broker at all. The screenshot *is* an entity in discovery mode, so the second line applies; on the
box, `screenshot: off` is its equivalent.
