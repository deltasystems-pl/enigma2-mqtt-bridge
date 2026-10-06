#!/bin/sh
#
# Remove the older backups tools/deploy-to-box.sh made on a receiver.
#
#   rotate-box-backups.sh <backup-directory> [<how-many-to-keep>]
#
# deploy-to-box.sh does not copy this file to the receiver: it sends this text
# as part of the command it runs there, with the two arguments set in front of
# it. So it is written for the receiver's shell - busybox - and uses nothing
# but the shell itself and `rm`. tests/test_rotate_box_backups.py runs it as a
# file, under every POSIX shell on the machine, against a fixture directory.
#
# A receiver's flash is small, and a backup of a build from six deploys ago is
# not something anyone will roll back to, so three are kept by default. What
# is counted, and what may be removed, is only what the deploy script itself
# makes: a directory named `MQTTBridge.bak-YYYYMMDD-HHMMSS`, eight digits and
# six and nothing after them. The first version rotated everything called
# `MQTTBridge.bak-*` by name, and a name sorts a backup somebody made by hand -
# `MQTTBridge.bak-pre-<reason>-<date>` - before every dated one: on a receiver
# with seven of those it would have removed seven of ten, the one just made
# among them.
#
# What it promises, and what the tests hold it to:
#
#   * only directories whose whole name is that pattern are counted or
#     removed; a named backup, a file, a symlink, or a name with anything after
#     the stamp is never touched, whatever it sorts next to;
#   * the newest are kept, by the stamp in the name. The stamp is fixed-width,
#     so the order of the names is the order of the dates - and a directory's
#     modification time is not used, because `cp -a` gives every backup the
#     time of the plugin directory it copied;
#   * nothing outside the directory given is read or removed, and a directory
#     that cannot be entered removes nothing.

directory="${1:-}"
keep="${2:-3}"

[ -n "$directory" ] || { echo "rotate-box-backups.sh: no backup directory given" >&2; exit 2; }
case "$keep" in
    ''|*[!0-9]*) echo "rotate-box-backups.sh: '$keep' is not a number to keep" >&2; exit 2 ;;
esac
cd "$directory" 2>/dev/null || { echo "rotate-box-backups.sh: cannot enter $directory" >&2; exit 2; }

DAY='[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]'
TIME='[0-9][0-9][0-9][0-9][0-9][0-9]'

# A pattern that matches nothing is left as it is, and is then no directory.
own() { [ -d "$1" ] && [ ! -L "$1" ]; }

total=0
for entry in MQTTBridge.bak-$DAY-$TIME; do
    own "$entry" && total=$((total + 1))
done

# The shell expands a pattern in the order of the names: the oldest first.
excess=$((total - keep))
for entry in MQTTBridge.bak-$DAY-$TIME; do
    [ "$excess" -gt 0 ] || break
    own "$entry" || continue
    rm -rf -- "$entry"
    excess=$((excess - 1))
done
exit 0
