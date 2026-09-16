#!/bin/bash
#
# Build the opkg package for the MQTT Bridge plugin.
#
#   tools/build-ipk.sh [--allow-unreleased]
#
# The version comes from src/MQTTBridge/version.py and from nowhere else. The
# build refuses to run unless CHANGELOG.md has a section for that version, so a
# release cannot escape undocumented; --allow-unreleased lifts that for the CI
# builds that happen on every push.
#
# The output is reproducible: the same commit produces byte-identical IPKs, so
# the SHA-256 published next to a release can be checked by rebuilding. That now
# holds across filesystems too - the tree is staged on a native one and every
# mode is written into the archive explicitly, so a build from a Windows
# checkout and a build from an ext4 one agree byte for byte.
#
set -euo pipefail

ALLOW_UNRELEASED=0
for arg in "$@"; do
    case "$arg" in
        --allow-unreleased) ALLOW_UNRELEASED=1 ;;
        -h|--help) sed -n '2,16p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "build-ipk.sh: unknown argument: $arg" >&2; exit 2 ;;
    esac
done

REPO_ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$REPO_ROOT"

PACKAGE=enigma2-plugin-extensions-mqttbridge
PLUGIN_DIR=usr/lib/enigma2/python/Plugins/Extensions/MQTTBridge

# ---------------------------------------------------------------- version ----
VERSION=$(python3 - <<'PY'
import re
import sys

src = open("src/MQTTBridge/version.py", encoding="utf-8").read()
m = re.search(r'^__version__\s*=\s*["\']([^"\']+)["\']', src, re.M)
if not m:
    sys.exit("version.py: no __version__ found")
print(m.group(1))
PY
)
[ -n "$VERSION" ] || { echo "build-ipk.sh: empty version" >&2; exit 1; }

if ! grep -qF "## [$VERSION]" CHANGELOG.md; then
    if [ "$ALLOW_UNRELEASED" -eq 0 ]; then
        echo "build-ipk.sh: CHANGELOG.md has no '## [$VERSION]' section." >&2
        echo "               Add one, or pass --allow-unreleased for a development build." >&2
        exit 1
    fi
    echo "build-ipk.sh: no changelog section for $VERSION (allowed: development build)"
fi

# ------------------------------------------------------------ determinism ----
# Every timestamp in the archive comes from the last commit, so two builds of
# the same tree agree byte for byte.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
    if SOURCE_DATE_EPOCH=$(git log -1 --format=%ct 2>/dev/null) && [ -n "$SOURCE_DATE_EPOCH" ]; then
        :
    else
        SOURCE_DATE_EPOCH=0
    fi
fi
export SOURCE_DATE_EPOCH

# Permissions are written into the archive by tar rather than read off the
# staging directory, because the staging directory cannot be trusted to have
# any. A checkout on a Windows drive - /mnt/c under WSL, or any DrvFs/NTFS
# mount - reports every file as 0777 and accepts chmod without doing anything,
# so an archive built from one would ship world-writable code into the plugin
# directory, and its SHA-256 would differ from the same tree built on ext4. Both
# halves of that are fixed here: --mode below, and a native staging tree further
# down. Either alone would do; together the modes are a property of the build
# instead of a property of whoever ran it.
#
#   u=rwX,go=rX  ->  0755 for directories and for anything already executable,
#                    0644 for everything else.
TAR_FLAGS=(--sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH"
           --mode='u=rwX,go=rX'
           --format=gnu --exclude=__pycache__ --exclude='*.pyc' --exclude='*.po')

# ---------------------------------------------------------------- staging ----
# Deliberately not under $REPO_ROOT: see above. mktemp gives us a directory on
# a real filesystem, where chmod means something and the X in --mode has a
# truthful executable bit to read.
STAGE=$(mktemp -d "/tmp/$PACKAGE.XXXXXXXXXX")
trap 'rm -rf "$STAGE"' EXIT

DIST=$REPO_ROOT/dist
rm -rf "$REPO_ROOT/build"          # where the tree used to be staged
mkdir -p "$STAGE/data/$PLUGIN_DIR" "$STAGE/control" "$DIST"

cp -r src/MQTTBridge/. "$STAGE/data/$PLUGIN_DIR/"
find "$STAGE/data" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$STAGE/data" -name '*.pyc' -delete

# Translations: .po lives in git, .mo is built here and never committed.
if compgen -G "$STAGE/data/$PLUGIN_DIR/locale/*/LC_MESSAGES/*.po" > /dev/null; then
    command -v msgfmt > /dev/null || {
        echo "build-ipk.sh: msgfmt not found - install gettext to compile the translations" >&2
        exit 1
    }
    for po in "$STAGE/data/$PLUGIN_DIR"/locale/*/LC_MESSAGES/*.po; do
        mo="${po%.po}.mo"
        msgfmt -o "$mo" "$po"
        echo "build-ipk.sh: compiled $(basename "$(dirname "$(dirname "$po")")")/$(basename "$mo")"
    done
fi
find "$STAGE/data" -name '*.po' -delete

sed "s/@VERSION@/$VERSION/" CONTROL/control > "$STAGE/control/control"
cp CONTROL/postinst CONTROL/prerm "$STAGE/control/"

# Belt to the --mode braces: normalise the staging tree as well, so what tar is
# told and what tar reads agree and a future flag change cannot quietly matter.
find "$STAGE" -type d -exec chmod 755 {} +
find "$STAGE" -type f -exec chmod 644 {} +
chmod 755 "$STAGE/control/postinst" "$STAGE/control/prerm"

# ------------------------------------------------------------------ build ----
IPK=$DIST/${PACKAGE}_${VERSION}_all.ipk
rm -f "$IPK" "$IPK.sha256"

printf '2.0\n' > "$STAGE/debian-binary"

tar "${TAR_FLAGS[@]}" -cf "$STAGE/control.tar" -C "$STAGE/control" .
tar "${TAR_FLAGS[@]}" -cf "$STAGE/data.tar" -C "$STAGE/data" .
gzip -n -9 "$STAGE/control.tar" "$STAGE/data.tar"

( cd "$STAGE" && ar rcD "$IPK" debian-binary control.tar.gz data.tar.gz )

# ----------------------------------------------------------------- verify ----
MEMBERS=$(ar t "$IPK" | tr '\n' ' ')
case "$MEMBERS" in
    "debian-binary control.tar.gz data.tar.gz "*) ;;
    *) echo "build-ipk.sh: unexpected archive members: $MEMBERS" >&2; exit 1 ;;
esac

# Modes, asserted from the archive itself. Reading them back is the only check
# that survives a change of staging filesystem, tar version or flag.
check_modes() {
    local archive=$1 allowed=$2 bad
    bad=$(tar tzvf "$archive" | awk '{print $1, $NF}' | grep -vE "^($allowed) " || true)
    if [ -n "$bad" ]; then
        echo "build-ipk.sh: unexpected modes in $(basename "$archive"):" >&2
        echo "$bad" >&2
        exit 1
    fi
}
check_modes "$STAGE/data.tar.gz" 'drwxr-xr-x|-rw-r--r--'
check_modes "$STAGE/control.tar.gz" 'drwxr-xr-x|-rw-r--r--|-rwxr-xr-x'

for script in ./postinst ./prerm; do
    mode=$(tar tzvf "$STAGE/control.tar.gz" | awk -v f="$script" '$NF == f {print $1}')
    [ "$mode" = "-rwxr-xr-x" ] || {
        echo "build-ipk.sh: $script is $mode, expected -rwxr-xr-x" >&2; exit 1
    }
done

( cd "$DIST" && sha256sum "$(basename "$IPK")" > "$(basename "$IPK").sha256" )

echo
echo "  package   $PACKAGE"
echo "  version   $VERSION"
echo "  members   $MEMBERS"
echo "  size      $(wc -c < "$IPK") bytes"
echo "  sha256    $(cut -d' ' -f1 < "$IPK.sha256")"
echo "  epoch     $SOURCE_DATE_EPOCH"
echo "  written   $IPK"
echo
