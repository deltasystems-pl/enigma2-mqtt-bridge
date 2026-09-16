# Troubleshooting

Two windows solve almost everything: the plugin's log on the box, and a subscription to the
broker.

```sh
ssh root@<box-ip> 'tail -f /home/root/mqttbridge.log'
mosquitto_sub -h <broker> -u <user> -P <password> -v -t 'enigma2/#'
```

If the log says the plugin is doing something and the subscription disagrees, the problem is
between the plugin and the broker — credentials, ACL, or the network. If the log is silent, the
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

## Nothing happens at all

**The plugin never loaded.** `opkg install` reporting success only means the files landed;
enigma2 looks for plugins at start-up. Restart the GUI (`init 4 && sleep 3 && init 3`) and check
that *Menu → Plugins* lists **MQTT Bridge**.

**The plugin is idle because the configuration is invalid.** A missing broker host, an
unparseable port, an empty node id — the plugin writes one line saying so and then does nothing
for the rest of the session. This is deliberate: **the GUI must never fail to start because of
the bridge**, so there is no retry storm and no dialog. Fix the setting and restart the GUI.

```sh
grep -i 'mqttbridge' /home/root/mqttbridge.log | head
grep 'config.plugins.mqttbridge' /etc/enigma2/settings
```

**The provisioning file was ignored.** It is read once at start-up and then deleted — but only
once at least one setting in it has been applied. If `/etc/enigma2/mqttbridge.json` is still
there, either the plugin has not started since you wrote it, or it could not use a single key in
it and left it for you to correct; the log says which. If it is gone but nothing looks different,
the log names the keys it rejected.

## The broker refuses the connection

The log names the reason paho gives back. The three common ones:

- **Connection refused, not authorised** — wrong username or password, or the broker has no such
  user. Test the same credentials from your PC with `mosquitto_sub -u … -P …`.
- **Connection refused, protocol** — something that is not an MQTT broker on that port, or TLS
  expected and not configured. Check `port` and `tls`.
- **Connection timed out / no route** — the box cannot reach the broker. `ping` it from the box;
  receivers on a separate VLAN are the usual cause.

A reconnect is attempted with a 1 → 60 second backoff, forever. A box that comes back after an
outage republishes its whole state on connect, so nothing needs to be prodded.

## The receiver pauses or ignores the remote

Start at `info`, not `debug`. The bounded diagnostic lines answer different questions:

- `mqtt epoch … connected in …` measures the connection or reconnection, and includes lifetime
  enqueue-to-main-loop delay and backlog aggregates;
- `snapshot complete …` measures the retained-state burst after a connection;
- `slow main-loop dispatch …` means a paho callback waited at least 250 ms for enigma2's main
  loop;
- `event loop stalled … stack …` is a watcher-observed pause of at least two seconds;
- `event loop resumed; heartbeat gap …` preserves a pause even when native code prevented the
  watcher itself from running; it deliberately makes no claim about the cause;
- `recording disk probe timing …` separates the filesystem read in the plugin's daemon worker
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

## Entities Home Assistant will never update again

**Retained ghosts.** A retained topic belongs to the broker, and it outlives whatever created it.
Change the node id, rename the box, uninstall the plugin without resetting first — and the old
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
bouquets it remembers in `/etc/enigma2/mqttbridge-state.json` — and then **immediately
republishes**: availability,
the whole state snapshot, the announcement and, in `discovery` mode, the discovery payloads, in
the same order as on a fresh connect. That is what makes a reset safe to run at any time: the
node's topics are gone for the width of one publish burst, not until the box next reconnects.
Home Assistant shows the entities go unavailable and come back, exactly as it does across a
reboot. Settings are untouched.

If the plugin is already gone, retract by hand: publish an **empty** retained message
(`mosquitto_pub -r -n -t …`) to each leftover topic. There is no other way — a broker will not
forget a retained topic on its own.

## Duplicated entities

You are in `discovery` mode and running the companion integration at the same time. The
integration switches the box to `integration` mode itself and the plugin retracts its discovery
payloads first, so this should not happen — but it does if the mode was changed by hand, or if
the retraction did not reach the broker (see the ACL section). Check `info.ha_mode`, then
publish to `cmd/ha_mode` with the mode you want; the switch always retracts before it announces.

## A restart lost a recording

`cmd/restart_gui`, `cmd/reboot` and `cmd/deep_standby` are all refused while a recording is
running or a timer is due within ten minutes, and `last_error` says so. But **a GUI restart from
anywhere else — the receiver's menu, `init 4`, `opkg`, your own script — kills a running
recording**, and nothing in the plugin can prevent it.

So: check the `recording` topic before restarting anything, and never let a package script or a
cron job restart enigma2 unconditionally. This is why `postinst` only prints a message.

## Commands appear to do nothing

There is no acknowledgement topic. A command's answer is the state topic changing; a refusal is
`last_error`. In order:

1. Subscribe to `enigma2/<node_id>/last_error` — a guard that refused the command says so there,
   with the reason.
2. Check the payload form in [TOPICS.md](TOPICS.md#2-commands). `cmd/zap` by name is refused
   unless exactly one service matches within the configured bouquets; `cmd/key` is refused for an
   unknown key name.
3. Check `info.capabilities`. If the hook a command needs is not in that list, this image did not
   give it to the plugin and the command cannot work — say so in an issue with your image name.
4. Make sure you are not publishing the command **retained**. The plugin logs a retained command
   and discards it without executing it — which is the right answer, because the broker would
   hand it back on every reconnect — so a retained command looks exactly like a command that did
   nothing. The log line names it. Clear it by publishing an empty retained payload to that
   topic, then send it again without `-r`.

## Reporting a problem

Open an issue with: the image and its version, the plugin version, the `info` payload (its
`capabilities` list is the interesting part), the relevant `info`/`warning` diagnostic lines, and
what you expected instead. Enable `debug` only when key classification or publish metadata is
needed. For anything with security implications, use a private advisory instead — see
[SECURITY.md](../SECURITY.md).
