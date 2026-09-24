# Installing

The package is `enigma2-plugin-extensions-mqttbridge`, `Architecture: all` - pure Python, so the
same file installs on every receiver. It lands in
`/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/`.

Pick one of the three routes below, then read [Activate](#activate) - the package deliberately
never restarts the GUI by itself.

## Before you start

**Change the box's root password.** Most images ship a well-known one, and from here on the box
holds a credential for your broker. This is the first thing, not the last.

**Some images ship SSH switched off**, with only telnet available (a few OpenATV builds do). Turn
SSH on before installing: *Menu -> Setup -> Network -> Network services -> SSH/Dropbear -> enabled*,
and set a root password if the image has not asked you for one. Everything below assumes SSH.
A telnet transport for the guided installer is planned for v1.1; it is not in v1.

## From a GitHub release

Every release carries the IPK and its SHA-256 on the
[releases page](https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases).

One line on the box, nothing to download to your PC first:

```sh
opkg install https://github.com/deltasystems-pl/enigma2-mqtt-bridge/releases/download/v0.2.0/enigma2-plugin-extensions-mqttbridge_0.2.0_all.ipk
```

Every release carries the IPK's SHA-256 next to it. To check it:

```sh
opkg download ... # or wget the IPK, then:
sha256sum enigma2-plugin-extensions-mqttbridge_0.2.0_all.ipk
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
OpenATV and their siblings already have configured - at that point it appears in the built-in
plugin browser with no feed to add at all.

## Manually, with scp

For a box with no route to the internet, or to install a build you made yourself:

```sh
tools/build-ipk.sh --allow-unreleased          # or download the release IPK
scp -O dist/enigma2-plugin-extensions-mqttbridge_0.2.0_all.ipk root@<box-ip>:/tmp/
ssh root@<box-ip> 'opkg install /tmp/enigma2-plugin-extensions-mqttbridge_0.2.0_all.ipk'
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

`opkg install` printing success means the files landed, not that the plugin loaded - enigma2 only
looks for plugins at start-up. After the restart, *Menu -> Plugins* lists **MQTT Bridge**; open it
and fill in the broker. Every setting is described in [SETUP.md](SETUP.md).

For a headless install, write the provisioning file **before** the restart and skip the setup
screen entirely - see [SETUP.md](SETUP.md#provisioning-file).

The package's `postinst` deliberately does not restart anything. A GUI restart during a recording
loses the recording, and `opkg` has no idea what the box is doing.

## Updating

From the feed: `opkg update && opkg upgrade enigma2-plugin-extensions-mqttbridge`, then restart
the GUI. Settings survive an upgrade; they live in enigma2's own settings file, not in the
package.

**The upgrade sweeps orphaned bytecode for you.** The image byte-compiles a plugin after
installing it, so the `.pyc` files in the plugin directory were written by the receiver and are not
in opkg's file list - opkg removes only what it installed. An upgrade that drops or moves a module
therefore leaves that module's compiled copy behind with no source next to it, and in Python 3 a
`.pyc` sitting beside where its source used to be is importable on its own. The old module would go
on being imported after the upgrade that was supposed to remove it. The package's `postinst` deletes
every `.pyc` and `.pyo` under
`/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/` whose `.py` is gone - both the legacy
same-directory form and `__pycache__/<name>.cpython-*.pyc` - and then gives back the directories
**that sweep itself emptied**, walking upward and stopping at the first one that still holds
something. A directory that was already empty before the upgrade is left where it is.

What it refuses to do is the other half:

- a compiled file whose `.py` is present is kept, including the plugin's own `plugin.pyc`;
- nothing outside the plugin directory is read, deleted or removed - every path is checked to be
  under it first, because a directory name may contain a newline and a naive line-by-line read
  would hand the second half of one to `rm` as a path relative to a working directory `opkg` never
  set;
- a symlink is neither followed nor removed, and a **different filesystem** mounted under the
  plugin directory - a USB stick, a network share - is not walked into. `find -xdev` compares device
  numbers, so a same-filesystem `mount --bind` is descended like any other directory and this is not
  a guard against one;
- a plugin directory with no `plugin.py` in it is left entirely alone. That is not an orphaned
  tree, it is a build-time packaging - OE strips sources out of a package into a separate one - and
  "every `.pyc` whose `.py` is missing" would there be every file the plugin has;
- nothing happens during an offline rootfs build, where the paths above would be the build host's;
- and no error in any of it can fail the install.

You do not have to do anything, and there is nothing to clean up by hand. If you want to see what it
removed, the lines are in the `opkg` output:

```
MQTT Bridge: removing orphaned bytecode client.pyc
```

## Uninstalling

**From Home Assistant or the OpenWebif page (since 0.3.0).** Switch on *Allow removing the plugin
remotely* (`uninstall_allowed`) on the receiver, then use the integration's options or the
OpenWebif page - or publish this receiver's node id to `enigma2/<node_id>/cmd/uninstall`. The
plugin retracts its own retained topics, ends on `offline`, removes its package and restarts the
interface; if any step fails it puts everything back and says why on `last_error`. What it leaves
behind, and why, is in [SETUP.md](SETUP.md#what-removing-the-plugin-remotely-does).

**By hand**, over SSH - for a plugin that cannot reach its broker, or with the permission off. Do
these in order:

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
that no longer exists - including the Home Assistant discovery payloads, so HA keeps showing
entities that nothing will ever update. The `prerm` script cannot do it for you, because by the
time `opkg` runs it the plugin may not be connected, and a package script has no business making
network calls.

**Removal takes the compiled plugin with it.** `opkg` deletes the files it installed, which are the
`.py` ones; the `.pyc` files beside them were written by the receiver after the install and are in
nobody's file list. On an OpenViX 6.6 receiver that used to leave forty files behind - the whole
plugin, still compiled, in the legacy same-directory form that Python 3 imports on its own without
a source next to it. enigma2's plugin loader finds a plugin by module name, so the GUI restart in
step 3 loaded the plugin that had just been removed and it reconnected to the broker, while
`opkg status` said nothing was installed.

The package's `prerm` now deletes every `.pyc` and `.pyo` under
`/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge/`, and the compiled copy of the OpenWebif
hook at `.../Plugins/Extensions/WebInterface/WebChilds/External/MQTTBridge.pyc`. Then it removes every
directory it leaves empty, deepest first and the plugin directory last, so that after `opkg` has
taken its own files there is nothing left to load. It runs on a removal only: an **upgrade** is
swept by `postinst` instead, after the new tree is unpacked, which is the only moment at which
„this bytecode has no source" is a true statement.

What it refuses to do, again, is the other half:

- **no `.py` and nothing else that is not bytecode is ever deleted** - those are `opkg`'s files, and
  they are all still on disk while this runs;
- a file of your own in the plugin directory keeps the directory, and every directory above it,
  rather than being swept up with the package. `opkg` then prints one line naming how many are
  left, once its own files are gone;
- a plugin directory that is a **symlink** is refused, and the refusal is printed rather than
  silent. This is where removal is stricter than the upgrade sweep: that one only deletes files,
  while this one removes directories, and a directory somebody deliberately put somewhere else is
  not a package script's to take. The same holds for a symlink on **any** component of
  `WebInterface/WebChilds/External`;
- nothing outside the plugin directory is read, deleted or removed, no symlink is followed, and a
  **different filesystem** mounted under the plugin directory is not walked into - though a
  same-filesystem `mount --bind` is descended like any other directory;
- `External/` is OpenWebif's directory and is not swept - the hook is addressed by its exact path,
  and nothing else in there is looked at;
- nothing happens during an offline rootfs build, and no error in any of it can fail the removal.

```
MQTT Bridge: removed 40 compiled files from the plugin directory.
MQTT Bridge: removed the compiled OpenWebif hook MQTTBridge.pyc
```

Three more lines are worth recognising if you see them, because each one means the sweep stopped
short and left you something to finish by hand:

```
MQTT Bridge: /usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge is a symlink to /media/usb/MQTTBridge,
so its compiled files were left in place; removing a directory somebody linked elsewhere is not this
package's to do.
```

The plugin directory is a link - onto a USB stick, or off a full flash. Nothing under it was
touched. Delete the named directory yourself once `opkg remove` has finished, and restart the GUI
afterwards.

```
MQTT Bridge: /usr/lib/enigma2/python/Plugins/Extensions/WebInterface/WebChilds is a symlink, so the
compiled OpenWebif hook under it was left in place; delete it by hand if you want it gone.
```

The same thing, one directory over: some part of the path to OpenWebif's `External/` is a link, so
`MQTTBridge.pyc` under it was not deleted. It is one file, and OpenWebif ignores it once its `.py`
is gone - but it is still there.

```
MQTT Bridge: this receiver's find has no -depth, so the empty directories under
/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge were left for opkg.
```

Every compiled file **was** deleted; only the emptied directories were left standing, because this
receiver's `find` cannot list a tree deepest-first safely. `opkg` removes the directories it
installed on its way out, so this usually resolves itself; if an empty
`.../Extensions/MQTTBridge/` is still there afterwards, `rmdir` it.

If you removed the package first, retract them by hand: publish an empty retained message to each
topic under `enigma2/<node_id>/#`, to `enigma2mqtt/discovery/<node_id>/config`, and to the
`homeassistant/device/<node_id>/config` and `homeassistant/device_automation/<node_id>/...` topics.
`mosquitto_sub -v -t 'enigma2/#' --retained-only` shows you what is left.

Uninstalling leaves the settings in `/etc/enigma2/settings` alone, so a reinstall finds its
configuration - the sweep above deletes compiled Python and nothing else, and no part of removing
the package reads or writes that file. Remove the `config.plugins.mqttbridge.*` lines by hand if you
want them gone; the broker password is one of them.
