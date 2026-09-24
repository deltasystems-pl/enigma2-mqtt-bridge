# Configuring the plugin

Everything lives under `config.plugins.mqttbridge.*` in enigma2's own settings file
(`/etc/enigma2/settings`) and is edited on the box through *Menu → Plugins → MQTT Bridge* or on
the [OpenWebif page](#the-openwebif-page). There is no separate configuration file to maintain,
and settings survive a plugin upgrade.

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
| `screenshot` | `on zap` | `off`, `on zap`, or `interval N s`. At most one capture per five seconds whatever this says. Each capture costs the receiver about 22 kB of memory it does not give back — see below |
| `screenshot_delay` | `4` | Seconds to wait after a zap before capturing. Another zap restarts the wait |
| `osd_toast` | `on` | Lets a sender ask `cmd/message` for a **discreet toast** instead of the popup: a small box in the top-right corner, headed „MQTT Bridge", that hides itself after a few seconds, never takes a key press and is not shown in standby. Off means the toast is never created, the `toast` capability is absent and a toast is refused; popups are unaffected. See below |
| `cam_telemetry` | `off` | Publish bounded current-service conditional-access status from `/tmp/ecm.info` |
| `oscam_telemetry` | `off` | Publish privacy-reduced OSCam software and reader/server health |
| `oscam_port` | `8888` | Receiver-local OSCam WebIf port; the host is fixed to `127.0.0.1` |
| `oscam_username` | — | OSCam WebIf login, used only on the receiver |
| `oscam_password` | — | OSCam WebIf password, masked in Setup and excluded from MQTT and logs |
| `epg_grid_events` | `4` | Events per channel in the EPG grid. `0` turns the grid off and drops it from `capabilities` |
| `bouquets_for_select` | all TV bouquets | Which bouquets feed the channel list, the `zap`-by-name lookup and the EPG grid — which publishes **one retained topic per bouquet**, `epg_grid/<bouquet_slug>`. Narrow it if you have hundreds of services. Dropping or renaming a bouquet retracts the topic it owned |
| `deep_standby_allowed` | `off` | Gate for `cmd/deep_standby` and `cmd/reboot`. Off by default because a receiver in deep standby may not wake again over the network: `info.wol.supported` says whether its image can, and when it is `false` only the remote, the front button or a timer brings it back |
| `wol_arm` | `off` | Switch on the image's own Wake-on-LAN setting („Wake On LAN" in its expert settings) at every start. Only where the image has one: on a receiver without it the label says „Wake-on-LAN is not available on this receiver" and the setting does nothing. Switching it off never switches the image's setting off — and while it is on, switching the image's own „Wake On LAN" off is undone at the next start or settings save, so switch this off first. Never writable over MQTT and not in `info.settings` — `info.wol` says what the receiver's switch actually is. No `ethtool` is run; see [ADR-0012](adr/0012-wake-on-lan-is-the-image-s-switch.md) |
| `cec_standby_workaround` | `off` | When the television switches itself off while the channel list is open, close the list so the receiver follows it into standby at once — and drop a television standby that is still waiting thirty seconds later behind any other screen, so it cannot fire when you next close that screen. Off by default; see below |
| `softcam_restart_allowed` | `off` | Gate for `cmd/softcam_restart` and for the automatic restart below. Off by default because it stops a running program on your receiver |
| `softcam_autoheal` | `off` | Restart the softcam by itself when the channel you are watching is encrypted and stops decoding. Does nothing unless the permission above is on |
| `softcam_autoheal_seconds` | `90` | How long a stuck decode has to hold before that happens, 30 to 600. A healthy encrypted channel refreshes its ECM file about every ten seconds, so anything much shorter is reading noise |
| `epg_import_allowed` | `off` | Gate for `cmd/epg_import`, which starts the image's EPG importer now. Off by default because the end of every import freezes the menus for two or three seconds; see below |
| `uninstall_allowed` | `off` | Gate for `cmd/uninstall`, which removes the plugin from the receiver. Off by default because it is a one-way door: afterwards only the receiver's own plugin menu or SSH can put the plugin back. See below |
| `log_level` | `info` | `error` / `warning` / `info` / `debug` |

🔴 **`deep_standby_allowed`, `softcam_restart_allowed`, `epg_import_allowed` and
`uninstall_allowed` are never writable over MQTT.** They are
set on the receiver — here, in the provisioning file, or on the [OpenWebif page](#the-openwebif-page)
— and published in `info.settings` so that a consumer can hide a control the box would always
refuse, but `cmd/config` rejects them like any other key outside its allowlist. The rule is the
same for all four: a setting that **enables** a command is granted on the receiver, and a setting
that only **tunes** a command already permitted — `softcam_autoheal` and its delay — may be
changed from the broker.

🔴 **`cec_standby_workaround` is never writable over MQTT, and it is not echoed at all.** It is
not a permission for a command — it switches on code that closes a screen you may be looking at,
which makes it the kill-switch for that code, and a kill-switch belongs on the receiver. A consumer
learns whether it is at work from the `cec_workaround` capability, not from `info.settings`.

🔴 **`osd_toast` is never writable over MQTT, and it is not echoed either.** It is the kill-switch
for a screen that lives inside the receiver's user interface, and a kill-switch reachable over the
broker is not one. A consumer learns whether toasts are available from the `toast` capability.

„On the receiver" includes the OpenWebif page, and that is not a loophole but a statement of fact:
whoever OpenWebif admits can already set every one of these through OpenWebif's own settings
endpoint. What stays true is the half that matters — **the broker can never grant itself one.**

The log is `/home/root/mqttbridge.log`, capped at 1 MB with two rotations kept, so a debug
session cannot fill the flash.

### What the CEC standby workaround does

It works around an enigma2 defect, not a defect of this plugin. When the television switches itself
off it tells the receiver over HDMI-CEC, and the image queues a standby that only the info bar
carries out. With the channel list open, the receiver therefore stays on — and when you later close
the list it goes to standby after all, and tells the television to switch off as well, because by
then the image has forgotten that the standby was the television's idea.

With the setting on:

- **The channel list, and only the channel list, is closed** when the television's standby arrives
  behind it — through the list's own exit, so you are back on the channel you were watching. A
  menu, the EPG, an input box, a message, a recording dialog, the service picker inside another
  dialog, or anything opened on top of the channel list is left exactly as it is.
- **A standby the television asked for that is still waiting after thirty seconds is dropped**, so
  that closing the menu you were in half an hour later does not put the receiver — and the
  television — to sleep.
- **A standby you asked for is never touched.** `cmd/power standby` from Home Assistant queues
  exactly the same thing as the television does; the plugin tells the two apart at the moment they
  are queued and only ever acts on the television's. The remote's power button does not go through
  the queue at all — it opens the standby screen directly — so the workaround never sees it.

It needs HDMI-CEC switched on in the image's own settings, with the image set to follow the
television into standby (on OpenViX 6.6 that second one is on by default). With either of them
off the image never queues the television's standby, so the plugin does not start the workaround
and does not claim the `cec_workaround` capability. Both are read when the plugin starts, so after
changing them restart the receiver's interface. Each intervention is logged at `info` and counted
on the `cec` topic — see [TOPICS.md](TOPICS.md).

### What the discreet toast does

A message from Home Assistant normally arrives as the receiver's own popup: it opens in the middle
of the screen, takes the remote until somebody dismisses it or it times out, and — because it goes
through the same queue as the image's other notifications — waits while the channel list is open
and then appears when you close it. A sender that asks for `"style": "toast"` gets something else:

- **A small box in the top-right corner**, above the channel list and the info bar, headed
  „MQTT Bridge" so that it cannot be mistaken for a message from the receiver itself. It has one
  appearance whatever the message says.
- **It hides itself** after 5 seconds, or the 1 to 30 the sender asks for. The remote cannot dismiss
  it, because nothing on it can take a key press — which is also why the channel list keeps moving
  normally while it is on screen.
- **A newer toast replaces the one on screen.** There is no queue.
- **It is not shown in standby.** It is hidden when the receiver goes to standby, and one sent
  while it is in standby is refused on `last_error` rather than kept for later.

A skin may define a screen called `MQTTBridgeToast` to restyle it, as it can any other screen.

### What the softcam restart does

It is not „run the init script" and not „restart the process". On the images that need it the
script is a stub and there may be several copies of the cam running, because the image's own
liveness check looks the cam up by process name — and the kernel keeps only the first fifteen
characters of that name, so a longer binary name can never be found and the check starts another
copy instead of leaving the running one alone.

So the command **collapses the copies to exactly one**: it signals every running instance of the
binary the image selected, waits up to five seconds, kills whatever is left, and starts one with
the image's own command line. The binary is resolved on the receiver from
`config.softcammanager.softcams_autostart`; nothing about it comes from the broker. The `softcam`
topic then reports how many instances are running, and whether your receiver is one of the ones
affected by that check — see [TOPICS.md](TOPICS.md).

Two things it deliberately does not touch: the cam's own runtime directory, because the line the
image's manual start screen uses would delete the live cam log with it, and the image's
„skip this cam" marker file, because on these images the manager is the only thing that starts the
cam at all and a marker left behind would disable it until the next interface restart.

### What the EPG import does

It asks the image's own EPG-Importer for an import now, exactly as the importer's „Manual" button
does, without the button's dialogs — the same selected sources, the same settings. The plugin adds
nothing of its own to the import and never loads the importer itself: it uses the one enigma2
already loaded, and where there is none — or where the image's guide cannot take imported events
and the importer would end by restarting the user interface — the command is simply not offered.

What you should know first, because it is the importer's behaviour and applies to every import,
scheduled ones included:

- **At the end of every import the menus freeze for two or three seconds** while the importer saves
  the guide on the thread that draws the picture. That is why the permission is off by default, and
  why the import is refused while recording, with a timer due within ten minutes, and within ten
  minutes of the importer's own scheduled run.
- **With the importer's „clear old EPG" setting on**, the guide is empty until the import finishes.
- **The importer's own deep-standby settings apply**, to an import started from Home Assistant as
  to a scheduled one: after **every** import, whoever started it, the importer checks whether to put the receiver into
  deep standby. It does so only when **all four** of its own conditions hold — its „shutdown"
  setting is on, its deep-standby setting is „wake up", its „deep standby after import" setting is
  on, and a timer woke the receiver — and then only if the receiver is in standby, nothing is
  recording and it is not already shutting down. The settings are all off by default. That is the
  importer, not this plugin.
- **A network recording mount can hold the press up.** Before its first download the importer reads
  `/proc/mounts` and asks the recording mount for its free space, on the main loop, to choose
  where to put the file. If that mount is a network share that has stopped answering, pressing the
  button can freeze the picture until the mount gives up — as the importer's scheduled run would.

A normal run takes a minute or two. The `epg_import` topic says `running` from the moment it starts —
whoever started it, including the importer's own schedule — and `done` with the number of events,
or `failed` with a sentence. The importer does not say which source failed, so neither can the
plugin. While an import runs the plugin also refuses its own deep standby, reboot and
user-interface restart, because a restart mid-import loses the run — except once its own start of
the import has failed, or the import has run past its 30-minute watchdog. The importer can go on
saying „running" after a start that failed part-way, until its next scheduled run, and a receiver
that cannot be restarted for a day because of it is the worse outcome.

### What removing the plugin remotely does

With `uninstall_allowed` on, `cmd/uninstall` — with this receiver's node id as its payload, so that
a message meant for another receiver removes nothing — takes the plugin off the receiver. The
companion integration offers it from 0.3.0 in the entry's options, behind a confirmation, and only
while the receiver says both that it may and that it can; the OpenWebif page offers it too, behind
its own confirmation. Deleting the integration's entry never removes anything. It is refused while a recording runs or is
due within ten minutes, while an EPG import runs, and on a plugin that the package manager did not install (the `uninstall`
capability says which).

It first stops everything that publishes, then empties every retained topic the plugin put on the
broker and ends with `offline`, waits until the broker has confirmed all of it, disconnects, removes
the package with `opkg remove`, and restarts the user interface. In Home Assistant the receiver then
looks switched off: its entities stay, unavailable.

**It can fail, and then nothing is lost.** If the broker does not confirm within 15 seconds, the
connection drops, or `opkg` refuses — it holds a lock that the image's own update check and plugin
browser take as well — the plugin reconnects, publishes everything again as after any restart, and
`last_error` says which step stopped it. Try again a minute later.

🔴 **opkg removes files one at a time.** If it is interrupted part way — it reports success even when
it was killed — the plugin checks that the package really is gone before restarting. When it is not,
some of its files may already be missing: `last_error` then says so and gives the command that puts
it back whole, `opkg install --force-reinstall enigma2-plugin-extensions-mqttbridge`, to run over
SSH.

🟡 **The restart can wait for you.** If something is streaming from the receiver, a background job
is running or timeshift is active, the receiver asks on the television whether to restart now, and
waits for an answer. The plugin is already disconnected and removed by then; answering either way
is fine, and the next restart finishes the job.

What stays on the receiver, on purpose:

| Left behind | Why |
|---|---|
| `config.plugins.mqttbridge.*` in `/etc/enigma2/settings` — **including the broker password** | So that a reinstall finds its configuration. Remove the lines by hand if you want them gone |
| `/etc/enigma2/mqttbridge-state.json`, emptied | It says nothing is published, so a reinstall has nothing to retract |
| `/home/root/mqttbridge.log*` | The only record of what the removal did |
| `/home/root/mqttbridge-backups/` | Snapshots made by the guided installer or by you. 🔴 They hold copies of the settings, so of the broker password too |
| `/etc/opkg/enigma2-mqtt-bridge.conf` | The feed. It is what lets the receiver's own plugin menu install the plugin again |

Reinstalling from the receiver's own menu (*Plugins → Download plugins → Extensions*) with the feed
still configured brings the plugin back on its kept broker, node id and Home Assistant mode, and an
integration entry that was kept picks it up again with every entity as it was.

### What a screenshot costs

**Every capture leaves about 22 kB in enigma2 that it does not give back, whoever takes it.**
That is the image's own `grab`, not this plugin: measured on OpenViX 6.6 by running 60 captures
from the receiver's own shell with the plugin idle — enigma2's resident memory rose by 1 320 kB
and stayed there. The same 60 captures taken *through* the plugin cost the same or less, so the
read-and-publish path here adds nothing measurable. The price is paid by anything that runs
`grab`, which presumably includes the image's own web interface.

`on zap` stays the default: it is what makes the picture in Home Assistant follow the television,
and on a receiver that is restarted every few weeks the arithmetic is small — 60 zaps a day is
about 1.3 MB a day. On a box that runs for months without a restart, or one that is short of
memory to begin with, set `interval` with a long period and take a capture on demand with
`cmd/screenshot` when you actually want one.

**`off` means off, including on demand.** It does not leave a manual shutter behind: the
screenshot publisher does not start at all, `screenshot` leaves `capabilities`, the retained
`screen` topic is retracted, and `cmd/screenshot` is refused with *screenshots are not available
on this box*. Choose it when you want no captures; choose a long `interval` when you want them
only when you ask.

OSCam telemetry does not enable or change WebIf. Enable OSCam's JSON API yourself, restrict WebIf
to loopback or an explicit local allowlist, and set OSCam `httpreadonly = 1`; the bridge itself
requests only the `status` and `readerlist` views, but cannot enforce OSCam's global policy. The
login is stored by enigma in `/etc/enigma2/settings`: `ConfigPassword` masks it in the UI, not on
disk. It is never sent to Home Assistant, MQTT, provisioning acknowledgements or the log.

## The OpenWebif page

If the receiver runs OpenWebif, the plugin adds a page to it: `http://<receiver-address>/mqttbridge`.
OpenWebif's extras menu entry *MQTT Bridge* opens it **inside OpenWebif**, in a frame with a fixed
height that scrolls, with a link under it to open it in a new tab; the address typed or bookmarked
opens the page on its own. It is the recovery tool — it is served by the receiver's web interface,
not by the broker session, so it keeps working when the broker settings are wrong.

**What it shows.** The plugin version; whether the bridge is running, and if it is idle, why;
whether it is connected; the broker's address and port and whether TLS is on; the node id, base
topic, discovery prefix and Home Assistant mode; the capabilities; the current `last_error`; the
settings as `info.settings` publishes them; the connection diagnostics; the last payload published
on every retained topic, exactly as a subscriber received it (a raw topic such as the screenshot is
named, not shown); and a sanitised tail of the plugin's log.

Beside *Take a screenshot* it shows the **last screenshot the plugin sent to the broker**, with the
time the capture finished and a link to the full size (`<mount>/screen.jpg`). While a capture is
running the page says so and reloads itself every two seconds, for at most twenty, until the new
picture is there — no script involved. A capture that fails leaves the previous picture and its
time, and the reason is on `last_error`. The picture is kept in memory, so after the plugin
restarts the page has none until the next capture, while the broker still holds the retained one.

**What it changes.** Every setting on this page's table above, in the setup screen's order. The
passwords are **write-only**: their fields are always empty, an empty field keeps the stored
password, and clearing one is done on the setup screen. Every text value refuses control characters
and line breaks — enigma2's settings file has no escaping, so a line break would plant a second
setting. A change to the node id, the base topic or the discovery prefix asks for a confirmation
first, because every retained topic moves and Home Assistant sees a new device. A change inside
`cmd/config`'s subset applies at once; any other change saves and reconnects, which can take a few
seconds, and `info` follows when it has. Only the fields you actually changed are saved: the form
remembers what it showed, so a page left open while a setting was changed elsewhere — over
`cmd/config`, or a permission switched off at the television — does not write its old value back.
Only the changed fields are checked, too, so a value stored before these rules (a longer host, say)
does not block saving something else; it is checked when you edit it. If another tab or window
saved first, the page says it is out of date — reload it and make the change again.

**What it does.** Every command the plugin accepts over MQTT, run through the same code with the
same household-safety guards — a recording, a timer due, the softcam's one-a-minute limit. Deep
standby, reboot, restarting the interface, deleting a timer, stopping a recording, restarting the
softcam, changing the Home Assistant mode, resetting the retained topics and removing the plugin
each ask for a confirmation that says what the household loses; for the removal the page fills in
the node id itself. The one difference from MQTT: a command from this
page **does not need** `deep_standby_allowed`, `softcam_restart_allowed`, `epg_import_allowed` or
`uninstall_allowed`. While the bridge is idle
the commands are shown disabled, with the reason.

**Who can open it.** Exactly whoever OpenWebif lets in — the page has no login of its own and reads
no OpenWebif setting ([ADR-0009](adr/0009-the-openwebif-page-trusts-openwebif.md)):

| OpenWebif setting | Who reaches the page |
|---|---|
| Authentication **off** (OpenWebif's default for HTTP) | A client in one of the receiver's own networks; any private address when OpenWebif's VPN access is on; a process on the receiver. Everybody else gets OpenWebif's own refusal |
| Authentication **on** (OpenWebif's default for HTTPS) | A client that logged in with a system user's password, or a process on the receiver |

🔴 **With authentication off, OpenWebif judges a client by the `X-Forwarded-For` header the client
sends itself**, so its „local network" rule is advisory against anyone who can open the receiver's
port 80. Keep that port off anything you do not trust.

🔴 **The page's own checks protect the page, not the receiver.** It answers only when the address
in the browser is the receiver's IP address, `localhost`, or the receiver's hostname (bare or with
`.local`), which is what stops a hostile web site from reaching it through a name it controls; and
every change must come from the page itself (its `Origin`), carrying the token the page put into
the form. That token is kept in the browser's OpenWebif session, is accepted only from the POST
body — never from the address; the page reads every field from the body alone — and is replaced after every change that took effect; a refused
request leaves it as it was, so the page you are looking at still works. A confirmation step
carries a separate token that is good for exactly one attempt at exactly that action. But while
OpenWebif authentication is off, any web page a household member opens can already switch the
receiver off, or grant `deep_standby_allowed`, through OpenWebif's own endpoints — this page cannot
make the receiver safer than its web interface. If that matters, switch OpenWebif authentication on.

🔴 **The same holds for the broker password.** Whoever OpenWebif admits can point `host` at a
server of their own (or switch TLS off) on this page, and the receiver will then send its stored
broker password there on the next connect. That is no worse than OpenWebif's own `/web/settings`,
which prints the password to anybody it admits — but it is the reason the page is not a safe place
to leave open to people you would not give the broker login to.

The screenshot on the page shows nobody anything new: whoever OpenWebif admits can already take a
fresh picture of the television at any time through OpenWebif's own `/grab`.

**Known limitation.** Because of the address check, the page refuses to answer under a DNS name of
your own or behind a reverse proxy, with a message naming the addresses that work. Opened from
OpenWebif's menu under such a name, the frame still appears and shows that message inside it. This is deliberate and there is no setting for it. A second click on the
menu entry does nothing until another OpenWebif panel has been opened, and reloading OpenWebif does
not reopen the page; both are OpenWebif's behaviour for every entry.

🟡 On an image where the **original** WebInterface is installed instead of OpenWebif, the page is
mounted under that interface's authentication, not OpenWebif's; this has not been measured.

🟡 If the setup screen is open on the television while the page saves, both edit the same settings:
the television's *Cancel* restores what the page saved, and its *Save* writes whatever it shows.

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
  "deep_standby_allowed": false,
  "softcam_restart_allowed": false,
  "epg_import_allowed": false,
  "uninstall_allowed": false
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
