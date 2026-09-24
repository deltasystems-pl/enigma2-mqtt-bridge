#!/bin/bash
#
# Build the IPK, put it on a receiver, and optionally restart the GUI.
#
#   tools/deploy-to-box.sh <box-ip> [options]
#
#   --restart              restart enigma2 afterwards and wait for it to answer
#   --no-build             use the IPK already in dist/
#   --provision <file>     install a provisioning JSON as /etc/enigma2/mqttbridge.json
#   -h, --help             this text
#
# Environment:
#   BOX_HOST       the receiver, if not given as the first argument
#   BOX_USER       default root
#   BOX_PASSWORD   the receiver's root password
#   --password-file <path>   read it from a file instead, so it stays out of the
#                            environment and out of the shell history
#
# The password is never echoed, never passed on a command line, and never
# written to a file by this script.
#
# A GUI restart kills a running recording, and nothing on the box can undo that.
# So before anything is copied this script reads the receiver's own timer list
# and refuses to restart while a timer is running or is due within ten minutes.
# It refuses just as firmly when it cannot read the timer list at all: not
# knowing is not the same as knowing it is safe.
#
set -euo pipefail

# Git Bash rewrites anything that looks like a Unix path into a Windows one,
# which turns /tmp/x.ipk on the receiver into C:/Program Files/Git/tmp/x.ipk.
export MSYS_NO_PATHCONV=1

PLUGIN_DIR=/usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge
BACKUP_DIR=/home/root/mqttbridge-backups
PROVISION_TARGET=/etc/enigma2/mqttbridge.json
LOG_PATH=/home/root/mqttbridge.log

TIMER_WINDOW_SECONDS=600
RESTART_TIMEOUT_SECONDS=120

BOX_HOST="${BOX_HOST:-}"
BOX_USER="${BOX_USER:-root}"
RESTART=0
BUILD=1
PROVISION=""
PASSWORD_FILE=""

usage() { sed -n '2,26p' "$0" | sed 's/^# \{0,1\}//'; }

while [ $# -gt 0 ]; do
    case "$1" in
        --restart) RESTART=1 ;;
        --no-build) BUILD=0 ;;
        --provision) PROVISION="${2:-}"; shift ;;
        --password-file) PASSWORD_FILE="${2:-}"; shift ;;
        -h|--help) usage; exit 0 ;;
        -*) echo "deploy-to-box.sh: unknown option: $1" >&2; exit 2 ;;
        *) BOX_HOST="$1" ;;
    esac
    shift
done

if [ -z "$BOX_HOST" ]; then
    echo "deploy-to-box.sh: no receiver given." >&2
    echo "                  tools/deploy-to-box.sh <box-ip>, or set BOX_HOST." >&2
    exit 2
fi

PASSWORD=""
if [ -n "$PASSWORD_FILE" ]; then
    [ -r "$PASSWORD_FILE" ] || { echo "deploy-to-box.sh: cannot read $PASSWORD_FILE" >&2; exit 2; }
    # A password file written on Windows ends its line with CR, and a CR in the
    # password is an authentication failure with nothing in it to see.
    PASSWORD=$(head -n 1 "$PASSWORD_FILE" | tr -d '\r')
else
    PASSWORD="${BOX_PASSWORD:-}"
fi
if [ -z "$PASSWORD" ]; then
    echo "deploy-to-box.sh: no password. Set BOX_PASSWORD or pass --password-file." >&2
    exit 2
fi

for tool in sshpass ssh scp python3; do
    command -v "$tool" > /dev/null 2>&1 || { echo "deploy-to-box.sh: $tool is not installed" >&2; exit 2; }
done

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

SSH_OPTIONS=(-o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null -o LogLevel=ERROR
             -o ConnectTimeout=10)

# `ssh -n` because a nested ssh otherwise eats the driving script's stdin.
box_ssh() { SSHPASS="$PASSWORD" sshpass -e ssh -n "${SSH_OPTIONS[@]}" "$BOX_USER@$BOX_HOST" "$@"; }
# `scp -O`: dropbear has no SFTP subsystem, and OpenSSH 9 defaults to SFTP.
box_scp() { SSHPASS="$PASSWORD" sshpass -e scp -O "${SSH_OPTIONS[@]}" "$1" "$BOX_USER@$BOX_HOST:$2"; }

say() { printf '\n== %s\n' "$*"; }

# ------------------------------------------------------------------- build ----

if [ "$BUILD" -eq 1 ]; then
    say "building the package"
    ./tools/build-ipk.sh --allow-unreleased > /dev/null
fi

IPK=$(ls -1t dist/*.ipk 2>/dev/null | head -n 1 || true)
[ -n "$IPK" ] || { echo "deploy-to-box.sh: no IPK in dist/ (drop --no-build?)" >&2; exit 2; }
IPK_NAME=$(basename "$IPK")
echo "   package  $IPK_NAME"

# ------------------------------------------------------------- timer guard ----

say "asking $BOX_HOST what it is recording"
set +e
python3 - "$BOX_HOST" "$TIMER_WINDOW_SECONDS" <<'PY'
import json
import sys
import time
import urllib.request

host, window = sys.argv[1], int(sys.argv[2])
url = "http://%s/api/timerlist" % host
try:
    with urllib.request.urlopen(url, timeout=10) as response:
        data = json.load(response)
except Exception as error:
    print("   cannot read %s (%s)" % (url, error))
    sys.exit(3)

timers = data.get("timers", []) if isinstance(data, dict) else data
now = time.time()
blocking = []
for timer in timers or []:
    if timer.get("disabled"):
        continue
    name = timer.get("name") or "(unnamed)"
    if timer.get("state") == 2:
        blocking.append("running now: %s" % name)
        continue
    begin = timer.get("begin") or 0
    if 0 < begin - now <= window:
        blocking.append("due in %d s: %s" % (int(begin - now), name))

if blocking:
    for line in blocking:
        print("   %s" % line)
    sys.exit(1)
print("   %d timer(s), none running and none due within %d s" % (len(timers or []), window))
PY
GUARD=$?
set -e

case "$GUARD" in
    0) ;;
    1)
        echo "deploy-to-box.sh: the receiver is recording, or is about to. Refusing." >&2
        exit 2
        ;;
    *)
        if [ "$RESTART" -eq 1 ]; then
            echo "deploy-to-box.sh: could not read the timer list, and --restart would" >&2
            echo "                  kill a recording it cannot see. Refusing." >&2
            exit 2
        fi
        echo "   continuing without a restart, so a recording is not at risk"
        ;;
esac

# ------------------------------------------------------------------ install ----

say "backing up the installed plugin"
# The stamp is expanded here, not on the box: a `$(date)` inside the single
# quotes of the remote command is a literal, and the first version of this script
# produced a directory called exactly `MQTTBridge.bak-$(date +%Y%m%d-%H%M%S)`.
STAMP=$(date +%Y%m%d-%H%M%S)
# Outside Extensions/ on purpose: enigma2 walks that directory at start-up and
# tries to import every subdirectory in it as a plugin.
# Three kept, older ones removed: a receiver's flash is small and a backup of a
# build from six deploys ago is not something anyone will roll back to. The
# stamp sorts chronologically, so `sort -r | tail -n +4` is everything but the
# three newest - and `tail -n +N` is the form busybox has.
box_ssh "set -e
         if [ -d '$PLUGIN_DIR' ]; then
             mkdir -p '$BACKUP_DIR'
             cp -a '$PLUGIN_DIR' '$BACKUP_DIR/MQTTBridge.bak-$STAMP'
             cd '$BACKUP_DIR'
             ls -1d MQTTBridge.bak-* 2>/dev/null | sort -r | tail -n +4 |
                 while read -r old; do rm -rf \"\$old\"; done
             ls -1d MQTTBridge.bak-*
         else
             echo '   nothing installed yet'
         fi"

say "copying $IPK_NAME"
box_scp "$IPK" "/tmp/$IPK_NAME"

say "installing"
box_ssh "opkg install --force-reinstall '/tmp/$IPK_NAME' && rm -f '/tmp/$IPK_NAME'"

if [ -n "$PROVISION" ]; then
    [ -r "$PROVISION" ] || { echo "deploy-to-box.sh: cannot read $PROVISION" >&2; exit 2; }
    say "installing the provisioning file"
    # It holds a broker password, so it is never readable by anyone else - not
    # even for the moment it spends in /tmp. scp applies the box's umask to what
    # it creates, and a `chmod` in the *next* ssh call is a window, so the
    # directory it lands in is made 0700 before the copy. The plugin deletes the
    # file as soon as it has read it.
    box_ssh "rm -rf /tmp/mqttbridge-provision && mkdir -m 700 /tmp/mqttbridge-provision"
    box_scp "$PROVISION" "/tmp/mqttbridge-provision/mqttbridge.json"
    box_ssh "set -e
             umask 077
             cp /tmp/mqttbridge-provision/mqttbridge.json '$PROVISION_TARGET'
             chmod 600 '$PROVISION_TARGET'
             rm -rf /tmp/mqttbridge-provision
             ls -l '$PROVISION_TARGET'"
fi

# ------------------------------------------------------------------ restart ----

if [ "$RESTART" -eq 1 ]; then
    say "restarting enigma2 (init respawns it)"
    box_ssh "killall enigma2 || true" || true

    say "waiting for the receiver to answer again"
    python3 - "$BOX_HOST" "$RESTART_TIMEOUT_SECONDS" <<'PY'
import sys
import time
import urllib.request

host, limit = sys.argv[1], int(sys.argv[2])
url = "http://%s/api/about" % host
started = time.time()
# Give it a moment to actually go down, or the first poll answers the old process.
time.sleep(5)
while time.time() - started < limit:
    try:
        with urllib.request.urlopen(url, timeout=5) as response:
            response.read(1)
        print("   back after %d s" % int(time.time() - started))
        sys.exit(0)
    except Exception:
        time.sleep(3)
print("   %s did not answer within %d s" % (url, limit))
sys.exit(1)
PY

    say "the last 40 lines of $LOG_PATH"
    box_ssh "tail -n 40 '$LOG_PATH' 2>/dev/null || echo '   (no log yet)'"
else
    echo
    echo "Not restarted. enigma2 only looks for plugins at start-up, so the new"
    echo "build is on the box but is not running. Re-run with --restart, or"
    echo "restart the GUI from the receiver's own menu."
fi

echo
