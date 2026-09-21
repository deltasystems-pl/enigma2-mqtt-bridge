# Installing

The package is `enigma2-plugin-extensions-mqttbridge`, `Architecture: all` — pure Python, so the
same file installs on every receiver. It lands in
`/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/`.

Pick one of the three routes below, then read [Activate](#activate) — the package deliberately
never restarts the GUI by itself.

## Before you start

**Change the box's root password.** Most images ship a well-known one, and from here on the box
holds a credential for your broker. This is the first thing, not the last.

**Some images ship SSH switched off**, with only telnet available (a few OpenATV builds do). Turn
SSH on before installing: *Menu → Setup → Network → Network services → SSH/Dropbear → enabled*,
and set a root password if the image has not asked you for one. Everything below assumes SSH.
A telnet transport for the guided installer is planned for v1.1; it is not in v1.

## From a GitHub release

**Available from v0.1.0, which is not yet released.** Until it is, the
[releases page](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases) is empty and the
URL below returns a 404 — build the package yourself with
[the scp route](#manually-with-scp) in the meantime.

One line on the box, nothing to download to your PC first:

```sh
opkg install https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/download/v0.1.0/enigma2-plugin-extensions-mqttbridge_0.1.0_all.ipk
```

Every release carries the IPK's SHA-256 next to it. To check it:

```sh
opkg download ... # or wget the IPK, then:
sha256sum enigma2-plugin-extensions-mqttbridge_0.1.0_all.ipk
```

and compare with the `.sha256` file from the same release page. The build is reproducible, so
that hash can also be reproduced from the tag.

## From the opkg feed

This is the route that gets you **updates through the receiver's normal plugin browser**. Add the
feed once:

```sh
echo 'src/gz enigma2-mqtt-bridge https://deltasystems-pl.github.io/enigma2-mqtt-bridge/feed' \
    > /etc/opkg/enigma2-mqtt-bridge.conf
opkg update
opkg install enigma2-plugin-extensions-mqttbridge
```

From then on `opkg upgrade`, and the image's own *Software management* screens, see new versions
as they are released.

From v1.0 the plugin is also submitted to the OE-Alliance third-party feed, which OpenViX,
OpenATV and their siblings already have configured — at that point it appears in the built-in
plugin browser with no feed to add at all.

## Manually, with scp

For a box with no route to the internet, or to install a build you made yourself:

```sh
tools/build-ipk.sh --allow-unreleased          # or download the release IPK
scp -O dist/enigma2-plugin-extensions-mqttbridge_0.1.0_all.ipk root@<box-ip>:/tmp/
ssh root@<box-ip> 'opkg install /tmp/enigma2-plugin-extensions-mqttbridge_0.1.0_all.ipk'
```

**`scp -O` is not optional.** Receivers run dropbear, which has no SFTP subsystem; OpenSSH 9 and
later default to SFTP and a plain `scp` fails with a confusing message about the remote shell.
`-O` forces the legacy protocol. For the same reason, tools that assume SFTP (most graphical file
managers, `rsync` without `--rsh` tricks) do not work against these boxes.

## Activate

```sh
init 4 && sleep 3 && init 3       # restart enigma2 only
```

or restart the GUI from the receiver's own menu. A full reboot works too and is never wrong.

`opkg install` printing success means the files landed, not that the plugin loaded — enigma2 only
looks for plugins at start-up. After the restart, *Menu → Plugins* lists **MQTT Bridge**; open it
and fill in the broker. Every setting is described in [SETUP.md](SETUP.md).

For a headless install, write the provisioning file **before** the restart and skip the setup
screen entirely — see [SETUP.md](SETUP.md#provisioning-file).

The package's `postinst` deliberately does not restart anything. A GUI restart during a recording
loses the recording, and `opkg` has no idea what the box is doing.

## Updating

From the feed: `opkg update && opkg upgrade enigma2-plugin-extensions-mqttbridge`, then restart
the GUI. Settings survive an upgrade; they live in enigma2's own settings file, not in the
package.

**The upgrade sweeps orphaned bytecode for you.** The image byte-compiles a plugin after
installing it, so the `.pyc` files in the plugin directory were written by the receiver and are not
in opkg's file list — opkg removes only what it installed. An upgrade that drops or moves a module
therefore leaves that module's compiled copy behind with no source next to it, and in Python 3 a
`.pyc` sitting beside where its source used to be is importable on its own. The old module would go
on being imported after the upgrade that was supposed to remove it. The package's `postinst` deletes
every `.pyc` and `.pyo` under
`/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/` whose `.py` is gone — both the legacy
same-directory form and `__pycache__/<name>.cpython-*.pyc` — and removes the directories that leaves
empty. Compiled files whose source is present are kept, nothing outside that directory is touched,
and a failure there can never fail the install.

You do not have to do anything, and there is nothing to clean up by hand. If you want to see what it
removed, the lines are in the `opkg` output:

```
MQTT Bridge: removing orphaned bytecode client.pyc
```

## Uninstalling

Do these in order:

```sh
# 1. while the plugin is still running and connected, retract its retained topics
mosquitto_pub -h <broker> -u <user> -P <password> \
    -t 'enigma2/<node_id>/cmd/reset' -m PRESS -q 1

# 2. then remove the package
opkg remove enigma2-plugin-extensions-mqttbridge

# 3. restart the GUI
init 4 && sleep 3 && init 3
```

**Step 1 matters and cannot be done afterwards.** Retained topics belong to the broker, not to
the package: remove the plugin without resetting and the broker keeps serving a snapshot of a box
that no longer exists — including the Home Assistant discovery payloads, so HA keeps showing
entities that nothing will ever update. The `prerm` script cannot do it for you, because by the
time `opkg` runs it the plugin may not be connected, and a package script has no business making
network calls.

If you removed the package first, retract them by hand: publish an empty retained message to each
topic under `enigma2/<node_id>/#`, to `enigma2mqtt/discovery/<node_id>/config`, and to the
`homeassistant/device/<node_id>/config` and `homeassistant/device_automation/<node_id>/…` topics.
`mosquitto_sub -v -t 'enigma2/#' --retained-only` shows you what is left.

Uninstalling leaves the settings in `/etc/enigma2/settings` alone, so a reinstall finds its
configuration. Remove the `config.plugins.mqttbridge.*` lines if you want them gone — the broker
password is one of them.
