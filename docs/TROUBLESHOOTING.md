# Troubleshooting

Two windows solve almost everything: the plugin's log on the box, and a subscription to the
broker.

```sh
ssh root@<box-ip> 'tail -f /home/root/mqttbridge.log'
mosquitto_sub -h <broker> -u <user> -P <password> -v -t 'enigma2/#'
```

If the log says the plugin is doing something and the subscription disagrees, the problem is
between the plugin and the broker - credentials, ACL, or the network. If the log is silent, the
plugin is not running.

## The log

`/home/root/mqttbridge.log`, capped at 1 MB with two rotations (`mqttbridge.log.1`, `.2`), so
leaving `debug` on cannot fill the flash. Levels are `error`, `warning`, `info` (default) and
`debug`; set it on the setup screen.

- **`info`** logs the lifecycle: start, connection epochs, disconnect reasons, reconnect and
  snapshot timings, mode changes, disk-probe timings and refused commands.
- **`warning`** adds slow main-loop dispatches, slow snapshot publishers and event-loop stalls.
- **`debug`** adds publish metadata (QoS, retention and byte count), command names and key-input
  classification. It does not log MQTT topics, node ids, broker addresses or payload bodies.

The password is never written at any level. If you see it there, that is a bug worth a security
report.

**The start line counts what was ready at the start.** Each start logs
`starting: ha_mode=... plugin=... capabilities=<n> build=...`, and `<n>` is the number of
capabilities bound at that moment. Some bind only once enigma2 has built the screen behind them -
`bouquet_context`, `zap_history` and `history_clear` read the receiver's own channel list - so
after an interface restart the start line can count fewer than `info.capabilities` holds once
they bind (22 against 25 on the receiver this was seen on), and a start inside a running
interface - after the settings were saved - counts them all. `info` is published again when a
late one arrives ([TOPICS.md](TOPICS.md#1-state-topics), "A capability can also arrive late"),
and the log has a line of its own, `capabilities=<n> now; the start line counted those bound by
then`, whenever a capability binds or goes after the start and the count is not the one last
logged since that start. It is written with or without a session to the broker: before the
connect, the connect's `info` is what carries the list. The last such line after a start is the
count `info.capabilities` holds; a start with no such line after it counted them all. A release
up to 0.4.0 logs the start line only, so there `info.capabilities` is the list to go by.

## Nothing happens at all

**The plugin never loaded.** `opkg install` reporting success only means the files landed;
enigma2 looks for plugins at start-up. Restart the GUI (`init 4 && sleep 3 && init 3`) and check
that *Menu -> Plugins* lists **MQTT Bridge**.

**The plugin is idle because the configuration is invalid.** A missing broker host, an
unparseable port, an empty node id - the plugin writes one line saying so and then does nothing
for the rest of the session. This is deliberate: **the GUI must never fail to start because of
the bridge**, so there is no retry storm and no dialog. Fix the setting and restart the GUI.

```sh
grep -i 'mqttbridge' /home/root/mqttbridge.log | head
grep 'config.plugins.mqttbridge' /etc/enigma2/settings
```

**The provisioning file was ignored.** It is read once at start-up and then deleted - but only
once at least one setting in it has been applied. If `/etc/enigma2/mqttbridge.json` is still
there, either the plugin has not started since you wrote it, or it could not use a single key in
it and left it for you to correct; the log says which. If it is gone but nothing looks different,
the log names the keys it rejected.

## The broker refuses the connection

The log names the reason paho gives back. The three common ones:

- **Connection refused, not authorised** - wrong username or password, or the broker has no such
  user. Test the same credentials from your PC with `mosquitto_sub -u ... -P ...`.
- **Connection refused, protocol** - something that is not an MQTT broker on that port, or TLS
  expected and not configured. Check `port` and `tls`.
- **Connection timed out / no route** - the box cannot reach the broker. `ping` it from the box;
  receivers on a separate VLAN are the usual cause.

A reconnect is attempted with a 1 -> 60 second backoff, forever. A box that comes back after an
outage republishes its whole state on connect, so nothing needs to be prodded.

## The receiver pauses or ignores the remote

Start at `info`, not `debug`. The bounded diagnostic lines answer different questions:

- `mqtt epoch ... connected in ...` measures the connection or reconnection, and includes lifetime
  enqueue-to-main-loop delay and backlog aggregates;
- `snapshot complete ...` measures the retained-state burst after a connection;
- `slow main-loop dispatch ...` means a paho callback waited at least 250 ms for enigma2's main
  loop;
- `event loop stalled ... stack ...` is a watcher-observed pause of at least two seconds;
- `event loop resumed; heartbeat gap ...` preserves a pause even when native code prevented the
  watcher itself from running; it deliberately makes no claim about the cause;
- `recording disk probe timing ...` separates the filesystem read in the plugin's daemon worker
  from the small delay returning its result to the main loop.

The recording-disk probe cannot block the interface: mount and free-space calls run in its daemon
worker, with one probe in flight. That does **not** make a network recording mount harmless to the
rest of enigma2. The image's video, timeshift, recording list or another plugin can still access
the same NFS path synchronously on the UI thread. During a network outage that appears as a
kernel NFS/RPC wait and can freeze video and remote handling until the mount call times out. A
slow disk-probe line alongside an event-loop gap is correlation, not proof that MQTT caused it;
inspect the safe stack in the event-loop warning or sample the receiver's task wait channels.

EPG-grid refreshes yield between batches of at most four channels and keep the previous complete
grid until the replacement is ready. A full refresh may therefore take several seconds while the
interface remains responsive; only a slow-batch or event-loop warning indicates a problem.

## The Home Assistant menu does not open, or shows nothing

- **No "Home Assistant" in the Extensions menu**: `menu_allowed` is off, or `info.capabilities`
  has no `menu` - the log then says why no menu screen could be built.
- **The hotkey does nothing**: read `info.menu`. `hotkey` `null` with a reason says why: switched
  off, an image that does not let a plugin bind a key, or a key the receiver's own button setup
  gives a function - which runs instead, by design. The hotkey works in live TV and while a
  recording or a media file plays, not over another screen, and not on a key the player uses
  itself (OpenATV's media player seeks back on a long RED). The Extensions entry opens the menu in
  any case.
- **The press combo does nothing**: read `info.menu.combo` - `null` with `combo_reason` says why.
  The presses must be the key the receiver receives: watch the `key` topic while you press it
  (a television's Back arrives as `KEY_PREVIOUS` or `KEY_EXIT`), and press faster than
  `menu_combo_gap_ms` between presses. It counts in live TV and while a recording or a media file
  plays, not over another screen. A held key counts once from the receiver's remote; over HDMI-CEC
  a held key may arrive as several presses, and those count.

## A key stopped working in live TV

- **Back (or EXIT, PLAY/PAUSE, STOP) does nothing in live TV or in a player, but works in the
  menus**: it is the press combo's key (`menu_combo_key`). While a key is set there it only counts
  toward the Home Assistant menu in live TV and the players, and its own function there - on
  OpenViX, Back zapping to the previous channel - and anything assigned to it in the receiver's
  button setup do not run. `info.menu.combo.replaces` lists them. Set `menu_combo_key` to `off`
  (or to another key) to give it back; the change applies at once.
- **"There is no menu from Home Assistant yet."**: nothing is retained on
  `enigma2mqtt/menu/<node>`, or it was emptied - the integration's options define no menu, or its
  entry was removed. A payload the receiver refused leaves the menu before it and one line in the
  log, `ignoring an invalid menu`, with the place it broke the contract.
- **Greyed, "Home Assistant is not reachable"**: Home Assistant's status topic said `offline`
  (`homeassistant/status`, or your discovery prefix) since the receiver's broker connection last
  came up. It ungreys on the next `online`, which Home Assistant publishes when it starts, on the
  next menu the integration publishes - any change to a row in the menu - and when the receiver's
  broker connection goes and comes back, since the status is forgotten then. The log says
  `Home Assistant says offline` when it greyed and `a menu arrived live from Home Assistant` when a
  menu lifted it.
- **A row does nothing on OK and the log says `rows of a type this plugin does not know`**: the
  integration is newer than the plugin and sends a kind of row this version cannot work; it stays
  shown, inert, until the plugin is updated.
- **OK on a list or the keyboard closes it and nothing is sent**: the choice, number or text was
  already the row's value, so there was nothing to change.
- **A row shows its mark and then "Home Assistant changed nothing."**: the integration refused the
  action or the device did not follow; the integration's log says which.
- **`info.menu.screen` is `fallback`**: the plugin's own screen could not be built on this image
  or skin, and the receiver's plain list is used. The log has the reason; please report it with
  the image and the skin.
- **The background is not see-through, or looks different from the setting**: the transparency
  (`menu_transparency`) applies the next time the menu is opened, not to a menu on screen. A skin
  can restyle the plugin's screen by its name, `MQTTBridgeList`, and then its colours win; and an
  image whose screen does not blend the plugin's alpha over the picture shows the background
  opaque. Please report an image that does, with the image and the skin.

## The menu creator does not load or does not save

- **"Home Assistant has not sent a catalogue for this receiver yet."**: nothing is retained on
  `enigma2mqtt/menu_catalogue/<node>`. The companion integration publishes it only while the
  receiver claims `menu_editor` in `info.capabilities`, which needs `menu_allowed` on and the
  OpenWebif page mounted; an integration without the menu creator never publishes one. A catalogue
  the receiver refused leaves the one before it and the log line `ignoring an invalid menu
  catalogue`.
- **"Editing the menu from the receiver is switched off in Home Assistant"**: it is off by default.
  The companion integration's options, under the receiver menu, switch it on - read SECURITY.md
  first.
- **"...a newer Home Assistant integration than this plugin understands"**: update the plugin.
- **The page says it needs JavaScript**: the browser runs no script, or blocks it. The settings on
  the page work without it.
- **"Home Assistant did not answer."**: no answer came within ten seconds. Reload: the save may have
  landed. The plugin's log says `menu edit ... did not answer`; Home Assistant's log says why it did
  not answer, if it got the edit.
- **"The menu was changed elsewhere..."**: Home Assistant's menu changed since the page loaded it -
  in its own options, in another tab, or another save. Reload, then make your change again.
- **"Home Assistant refused the menu."** with what and where: the menu breaks one of the rules of
  the integration's menu options - an entity that cannot be used now, a control it does not take,
  too many rows. Nothing was saved.

## The receiver's memory use keeps climbing

**Watch the curve before believing anything about it.** The plugin publishes the enigma2 process's
own counters on `<base>/<node>/process` - resident set, its high-water mark, threads, open file
descriptors and the epoch second the process started - in the snapshot on every connect, then every
300 seconds, and early whenever the resident set moves by 4 MiB. The 300-second publish is a ceiling
on the gap rather than a heartbeat: like every state topic it is only sent when something changed, so
a flat stretch in the curve is a receiver with nothing to report, not a plugin that stopped. In discovery mode the entity to
look at is **Process memory**, in MiB; it is the one of the five that is enabled by default, so a
recorder is already keeping its history. **Process open files** and **Process threads** are next to
it, disabled, and are what to enable for a fortnight if the memory line turns upwards, because a
descriptor or thread count that climbs with it says something the memory figure alone does not.
**Process started** is how to tell a slow climb from a restart that reset the counter.

The numbers are the *process's*, not the plugin's. enigma2 is one process: the image, every other
plugin and this one share that resident set and nothing in `/proc` can attribute a kilobyte to any
of them. A rising line is a question, and the paragraph below is the first thing to rule out.

Most of it is the receiver, not this plugin. The one cost worth knowing is the screenshot:
**every capture leaves about 22 kB in enigma2 permanently, whoever takes it** - measured on
OpenViX 6.6 with 60 captures from the box's own shell and the plugin idle (+1 320 kB, never
returned), and the same 60 taken through the plugin cost no more. If a box that is never
restarted is short of memory, set `screenshot` to a long `interval` and take one on demand with
`cmd/screenshot`. `off` is stronger and stops captures altogether - the publisher does not start,
so `cmd/screenshot` is refused too; see [SETUP.md](SETUP.md#what-a-screenshot-costs). Plugin loads that
were measured against a 20-minute idle baseline - commands, EPG-grid and channel-list rebuilds,
OSCam polling, settings writes - all stayed at or below the receiver's own background variation.

## The box connects but no topics appear

This is almost always the **ACL**, and it is the nastiest failure in this document because
nothing anywhere reports an error: **Mosquitto drops an ACL-denied publish silently** and the
publishing client sees success. The plugin thinks it published, the log says it published, and
the topic never exists.

Diagnose it from outside, as a privileged user:

```sh
mosquitto_sub -h <broker> -u <admin-user> -P <password> -v -t '#' | grep -i mqttbridge
mosquitto_pub -h <broker> -u <the-box-user> -P <box-password> -t 'enigma2/<node_id>/test' -m hi
```

If the box's own user cannot publish to its own topic tree, the ACL is wrong. Compare it with
the snippet in [SETUP.md](SETUP.md#broker-access), and remember the four entries: the node's own
tree, the announcement, and the two Home Assistant discovery prefixes. An ACL that covers
`enigma2/<node>/#` but not `homeassistant/device/<node>/#` produces exactly the symptom „the
sensors work but Home Assistant never discovers the device".

`availability` is the canary: it is published first and by the last will, so if even that is
missing the login or the ACL is the problem, not the plugin's hooks.

## Entities keep going unavailable and coming back

Over and over, for as long as the receiver is on, and the broker's log says why:
`disconnected: oversize packet`. **Mosquitto 2.1** - the version in the current Home Assistant
add-on - refuses a packet over 2,000,000 bytes (`max_packet_size`) by closing the connection. The
last will then says `offline`, the plugin reconnects, publishes everything again, and is cut off at
the same payload. A receiver with a very large channel list gets there: `channels` carries every
bouquet with every service while `bouquets_for_select` is empty, each `epg_grid/<bouquet_slug>`
carries a bouquet's programme guide, and in `discovery` mode the device's discovery payload
carries every channel name.

A plugin after 0.4.0 does not send such a payload. It holds every packet to 1,000,000 bytes,
says in its log which topic was too big - `not publishing epg_grid/astra: 1204611 bytes is over
the 1000000 byte packet limit` - and lists it in `info.not_published`
([TOPICS.md](TOPICS.md#basenodeinfo)), so the connection stays up and everything else works. The
log has that line once, when the topic becomes too big, not at every attempt.

What a household sees depends on which payload was too big:

- **The channel list.** `channels` - every bouquet with its list - is not on the broker, and is
  named in `info.not_published`; an older, smaller copy is retracted. The lists are still
  published, one bouquet a topic, on `channels/<bouquet_slug>`, and `bouquets` is their index:
  every bouquet with the slug of its topic and how many services it has
  ([TOPICS.md](TOPICS.md#basenodechannels---since-m2)). A consumer that reads those has every
  channel. One that reads only `channels` - the companion integration up to 0.4.0 - has no
  channel list, so its channel selects are empty until the list is narrowed. A single bouquet too
  big for one packet by itself is missing from `channels/<bouquet_slug>` as well, and named there
  too.
- **A grid.** An `epg_grid/<bouquet_slug>` that is too big is cut to fewer events per channel -
  the most that fits one packet - and published; its `events_per_channel` says how many, and the
  log has `epg grid: Astra is cut from 4 to 2 event(s) per channel to fit one packet`. The guide
  for that bouquet is shorter, and the other bouquets keep theirs. Only a grid that does not fit
  with one event on each channel is not on the broker - an older, smaller copy is retracted - and
  is named in `info.not_published`.
- **The device's discovery payload**, in `discovery` mode. It is published without the channel
  select, whose options - every channel name - are what made it too big. The device and all its
  other entities are there, also on a first install; the "Channel list" select is missing, or
  disappears if it was there before, and the log says `the channel select is left out of the
  discovery payload`. It comes back by itself once the names fit. Zapping by name still works,
  from an automation or a script publishing `cmd/zap`.

To get back what is missing:

- set `bouquets_for_select` to the bouquets the household uses - that shrinks the channel list,
  the grids and the discovery payload at once. It is still needed for the channel select in
  `discovery` mode, for a consumer that reads only `channels`, and for a guide with all its
  events;
- split a bouquet that is in `info.not_published` by itself into smaller ones, or leave it out;
- `epg_grid_events` no longer needs lowering for the size - the plugin cuts a grid that is too
  big by itself - and `0` still switches the grid off.

Both settings are on the setup screen and on the plugin's OpenWebif page ([SETUP.md](SETUP.md)).
With a plugin up to 0.4.0, `bouquets_for_select` and a lower `epg_grid_events` are the way out, or
a higher `max_packet_size` on the broker.

**After going back to 0.4.0 by hand**, `bouquets` and the `channels/<bouquet_slug>` topics stay on
the broker, retained, with the lists they had: 0.4.0 does not publish them, so it does not update
them either, though it still knows their names from the state file. `cmd/reset` on the downgraded
plugin retracts them, and so does an uninstall.

## Entities Home Assistant will never update again

**Retained ghosts.** A retained topic belongs to the broker, and it outlives whatever created it.
Change the node id, rename the box, uninstall the plugin without resetting first - and the old
retained payloads sit there forever. Home Assistant keeps the entities, permanently stale, and
nothing will ever correct them.

```sh
mosquitto_sub -h <broker> -u <admin-user> -v -t 'enigma2/#' --retained-only
mosquitto_sub -h <broker> -u <admin-user> -v -t 'homeassistant/device/#' --retained-only
```

The cure while the plugin is still installed:

```sh
mosquitto_pub -h <broker> -u <user> -P <password> -t 'enigma2/<node_id>/cmd/reset' -m PRESS -q 1
```

which retracts every retained topic the node owns, including the discovery payloads and EPG-grid
bouquets it remembers in `/etc/enigma2/mqttbridge-state.json` - and then **immediately
republishes**: availability,
the whole state snapshot, the announcement and, in `discovery` mode, the discovery payloads, in
the same order as on a fresh connect. That is what makes a reset safe to run at any time: the
node's topics are gone for the width of one publish burst, not until the box next reconnects.
Home Assistant shows the entities go unavailable and come back, exactly as it does across a
reboot. Settings are untouched.

If the plugin is already gone, retract by hand: publish an **empty** retained message
(`mosquitto_pub -r -n -t ...`) to each leftover topic. There is no other way - a broker will not
forget a retained topic on its own.

## Duplicated entities

You are in `discovery` mode and running the companion integration at the same time. The
integration switches the box to `integration` mode itself and the plugin retracts its discovery
payloads first, so this should not happen - but it does if the mode was changed by hand, or if
the retraction did not reach the broker (see the ACL section). Check `info.ha_mode`, then
publish to `cmd/ha_mode` with the mode you want; the switch always retracts before it announces.

## A recording is missing from Home Assistant, or the library is late

**It was made, moved or deleted on the receiver.** The library is read when the plugin starts,
when a recording starts or ends, after a delete from Home Assistant, when a movie location is
mounted, and when *Refresh recordings* is pressed - and never on a timer, so that the recording
disk can sleep. A recording deleted, moved or renamed in the receiver's own movie list, through
OpenWebif or over FTP raises none of those, and Home Assistant sees the change after the next one.
Press *Refresh recordings*, or send `cmd/recordings_refresh`.

**It is on a network share.** A movie location on NFS or SMB is read only while it is mounted:
`recordings.locations[].mounted` says whether it is, from `/proc/mounts`. An automounted share is
unmounted by the receiver when it has been idle, and the library then keeps the recordings it read
last, with `mounted` `false`, rather than reading the share - which would mount it. The refresh
reads it anyway.

A share that stops answering holds up only the library's own threads, never the picture and never
the `hdd` topic; what happens then depends on how it is mounted:

- **A hard mount** (NFS's default) waits for the server for ever. A read that walks into it never
  comes back: after ten minutes the library gives it up, says so in the log ("the recording
  library read has not finished within 600 s"), publishes nothing from it and reads what was asked
  for meanwhile - which may walk into the same share again. The minute's free-space check that
  waits on it is skipped until it returns ("still being read" in the log). The thread stays stuck
  in the kernel until the share answers or the receiver restarts.
- **A soft mount** gives up after its own timeout (`timeo` and `retrans`, a minute or two by
  default): the read finishes with what it could read, the files on the share missing until it
  answers again.

The image's own move into the trash runs on the main loop, as it does from the movie list, so a
delete from Home Assistant of a recording on a hung share freezes the picture as the movie list's
own delete would.

**It is in a folder with thousands of recordings.** One folder is one topic, held to the packet
bound of 1,000,000 bytes: about three thousand recordings. Beyond that the oldest are left out,
`recordings.locations[].folders[].cut` says how many, and those recordings cannot be played or
deleted from Home Assistant until the folder is smaller - split it into subfolders on the
receiver, each of which is a topic of its own. Reading a large library takes a few seconds on the
receiver; the log says how long: `recording library read: 5000 recording(s) in 1 location(s),
4.210s (start)`.

**It is a media file, not a recording.** Only `.ts` files, and `.stream` for IPTV, are listed.

**Its channel is in a bouquet Home Assistant hides.** The receiver lists it; the companion
integration leaves it out of what it shows, as it does in its channel lists.

**A delete is refused.** `last_error.reason` says why: `not_permitted` - `recording_delete_allowed`
is off; `trash_off` - the receiver's trash is switched off, and a delete is never made for good;
`in_use` - it is being recorded or played; `not_in_library` - the library does not list it, so
press *Refresh recordings*; `file_gone`; `in_progress` - another delete is still waiting for its
move; `busy` - an update is being applied. A refusal without a code - the receiver has no trash, or could not move
the recording into it - says why in its sentence. The recording is untouched in every case.

## A restart lost a recording

`cmd/restart_gui`, `cmd/reboot` and `cmd/deep_standby` are all refused while a recording is
running or a timer is due within ten minutes, and `last_error` says so. But **a GUI restart from
anywhere else - the receiver's menu, `init 4`, `opkg`, your own script - kills a running
recording**, and nothing in the plugin can prevent it.

So: check the `recording` topic before restarting anything, and never let a package script or a
cron job restart enigma2 unconditionally. This is why `postinst` only prints a message.

## Two copies of the softcam after a restart

Some images start the softcam with a liveness check that looks it up by process name, and the
kernel keeps only the first 15 characters of that name. A softcam binary with a longer name - an
OSCam build named after its version, for example - is never found, so every start of the
interface starts **another copy**, and an update's restart is no exception. The plugin says so in
its log when it starts:

```text
<binary> is longer than 15 characters, so the image's own liveness check cannot find it and starts another copy at every interface start
```

and the `softcam` topic reports `manager_check_on_start: true` and the number of copies in
`running_instances` ([TOPICS.md](TOPICS.md)).

Two copies can run side by side while encrypted channels still decode. When you clean up, **stop
every copy and start one**: the plugin's softcam restart does exactly that - `cmd/softcam_restart`
with `softcam_restart_allowed` on, or the action on its OpenWebif page
([SETUP.md](SETUP.md#what-the-softcam-restart-does)). Do not keep the older copy and stop the
newer one. On the receiver where this was examined (OSCam on OpenViX 6.6), the copy from before
the restart still answered on its web interface but no longer decoded anything for the restarted
interface, so stopping the new copy froze every encrypted channel until the softcam was restarted.

## OSCam runs, but `oscam.software_running` is false

`oscam.software_running` is read from the process list, and only while OSCam's web interface does
not answer does it matter: a web interface that answers is the proof on its own. A process is
taken for OSCam when its name - the 15 characters the kernel keeps of it - is `oscam`, or starts
with `oscam-`, or starts with `oscam_` **and** the file it runs has a name starting with `oscam`
(any case). So these are not found:

- a binary named any other way - `oscam.bin`, `oscamd`, `OSCam11` - or started under another name;
- a process named `oscam_...` whose executable is called something else, which is what a shell
  script of that name is, or whose executable cannot be read.

And the name is all there is to go by, so anything named like the cam counts as the cam: a helper
binary called `oscam_...`, and any process at all named `oscam-...`. The softcam the image is set
to start is not consulted here - `softcam.running_instances` on the `softcam` topic counts exactly
that binary, where the image has a softcam manager the plugin can read
([TOPICS.md](TOPICS.md#1-state-topics)).

## An update or install is refused with "standby"

Every install and update is refused while the receiver is in standby, because its restart would
wake the receiver - and, with HDMI-CEC, may switch the television on. With HDMI-CEC set to follow
the television, **switching the television off puts the receiver into standby** too, and so does
a television that switches itself off at night. Wake the receiver first - the remote, or
`cmd/power` `on` - and then install. Waking it may switch the television on.

## The update check says the origin cannot be reached

The receiver checks the release origin's certificate against the image's own CA certificates. An
image whose CA bundle is missing, or too old to hold the root the origin's certificate chains to,
cannot verify it, and that looks exactly like no internet:
`update.origin` is `unreachable`, the check's `check_error` is `unreachable`, and the log has a
line starting `the release origin cannot be reached:` with the TLS error. Such a receiver still
learns of releases from the index the companion integration relays over MQTT, and an install
started on the television or the OpenWebif page asks Home Assistant for the package instead
([SETUP.md](SETUP.md#installing-a-release-from-the-receiver)). Without the integration, update the
image's CA certificates package, or install by hand ([INSTALL.md](INSTALL.md#updating)).

## Commands appear to do nothing

There is no acknowledgement topic. A command's answer is the state topic changing; a refusal is
`last_error`. In order:

1. Subscribe to `enigma2/<node_id>/last_error` - a guard that refused the command says so there,
   with the reason. It is retained, so look at its `ts`: it stays until a command succeeds, until
   the plugin starts again (after 0.4.0) or until `cmd/clear_error` empties it (after 0.4.0;
   *Clear last error* on the OpenWebif page and, in discovery mode, in Home Assistant).
2. Check the payload form in [TOPICS.md](TOPICS.md#2-commands). `cmd/zap` by name is refused
   unless exactly one service matches within the configured bouquets; `cmd/key` is refused for an
   unknown key name. `cmd/config` refuses the whole object when one key is not on its list - the
   receiver-only permissions such as `update_allowed` included - with "the config object contains
   unknown settings: " and the keys it refused, each in single quotes (up to 0.4.0 the sentence
   names none); the keys it
   takes are listed in [TOPICS.md](TOPICS.md#cmdconfig-semantics).
3. Check `info.capabilities`. If the hook a command needs is not in that list, this image did not
   give it to the plugin and the command cannot work - say so in an issue with your image name.
4. Make sure you are not publishing the command **retained**. The plugin logs a retained command
   and discards it without executing it - which is the right answer, because the broker would
   hand it back on every reconnect - so a retained command looks exactly like a command that did
   nothing. The log line names it. Clear it by publishing an empty retained payload to that
   topic, then send it again without `-r`.

## The volume moves on the screen and nothing gets quieter

The receiver is handing the audio to the television or an amplifier undecoded - "passthrough" in
its audio settings - and its own volume does not act on a bitstream. It depends on the channel:
a track in MPEG audio is decoded and the volume works, a Dolby Digital one on the same receiver
is passed on and it does not. `volume.passthrough` says which it is for the track being played:
`true`, `false`, or `null` when the plugin cannot tell
([TOPICS.md](TOPICS.md), `volume`). Use the television's or the amplifier's volume, or set the
receiver to "downmix" for that codec in its own audio settings.

## A channel is missing from the zap history

The zap history is the receiver's own list, and the receiver records only zaps that pass through
its channel selection. A zap from Home Assistant that the plugin had to play directly tunes the
channel but is not in the list, and the log says why:

```text
zapping to <sref> without the channel list: <reason>
zapping to <sref> without the channel list, so it is not in the zap history: it is in no published bouquet
```

The usual reasons are a menu, a list or a question open on the television (`a screen is open on
the receiver` - a plain information popup alone does not count), timeshift (`timeshift is
active`), and a radio station or a channel outside the published bouquets (the second line,
logged once per channel). None of them is an error, and none sets `last_error`.

If a question was on the television, it is still there after the zap, unanswered. That is
intended: a zap from Home Assistant changes the channel and leaves the question to the household
and its remote.

A zap timer that fires while the receiver is in standby or in timeshift, and an EPG preview that
was not confirmed with a second OK, are not recorded by the receiver either.
[TOPICS.md](TOPICS.md#what-enters-the-receivers-zap-history) has the complete list.

After a zap from Home Assistant during timeshift, the timeshift buffer files stay on the recordings
disk until the next channel change, and the receiver then deletes them
([details](TOPICS.md#a-zap-from-home-assistant-during-timeshift)).

## Reporting a problem

Open an issue with: the image and its version, the plugin version, the `info` payload (its
`capabilities` list is the interesting part), the relevant `info`/`warning` diagnostic lines, and
what you expected instead. Enable `debug` only when key classification or publish metadata is
needed. For anything with security implications, use a private advisory instead - see
[SECURITY.md](../SECURITY.md).
