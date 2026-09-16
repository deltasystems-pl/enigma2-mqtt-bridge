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
# the SHA-256 published next to a release can be checked by rebuilding.
#
set -euo pipefail

ALLOW_UNRELEASED=0
for arg in "$@"; do
    case "$arg" in
        --allow-unreleased) ALLOW_UNRELEASED=1 ;;
        -h|--help) sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
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

TAR_FLAGS=(--sort=name --owner=0 --group=0 --numeric-owner --mtime="@$SOURCE_DATE_EPOCH"
           --format=gnu --exclude=__pycache__ --exclude='*.pyc' --exclude='*.po')

# ---------------------------------------------------------------- staging ----
BUILD=$REPO_ROOT/build/ipk
DIST=$REPO_ROOT/dist
rm -rf "$BUILD"
mkdir -p "$BUILD/data/$PLUGIN_DIR" "$BUILD/control" "$DIST"

cp -r src/MQTTBridge/. "$BUILD/data/$PLUGIN_DIR/"
find "$BUILD/data" -name '__pycache__' -type d -prune -exec rm -rf {} +
find "$BUILD/data" -name '*.pyc' -delete

# Translations: .po lives in git, .mo is built here and never committed.
if compgen -G "$BUILD/data/$PLUGIN_DIR/locale/*/LC_MESSAGES/*.po" > /dev/null; then
    for po in "$BUILD/data/$PLUGIN_DIR"/locale/*/LC_MESSAGES/*.po; do
        mo="${po%.po}.mo"
        msgfmt -o "$mo" "$po"
        echo "build-ipk.sh: compiled $(basename "$(dirname "$(dirname "$po")")")/$(basename "$mo")"
    done
fi
find "$BUILD/data" -name '*.po' -delete

sed "s/@VERSION@/$VERSION/" CONTROL/control > "$BUILD/control/control"
cp CONTROL/postinst CONTROL/prerm "$BUILD/control/"

# Normalise modes so the archive does not inherit whatever filesystem it was
# staged on (a Windows drive reports everything as 0777).
find "$BUILD" -type d -exec chmod 755 {} +
find "$BUILD" -type f -exec chmod 644 {} +
chmod 755 "$BUILD/control/postinst" "$BUILD/control/prerm"

# ------------------------------------------------------------------ build ----
IPK=$DIST/${PACKAGE}_${VERSION}_all.ipk
rm -f "$IPK" "$IPK.sha256"

printf '2.0\n' > "$BUILD/debian-binary"

tar "${TAR_FLAGS[@]}" -cf "$BUILD/control.tar" -C "$BUILD/control" .
tar "${TAR_FLAGS[@]}" -cf "$BUILD/data.tar" -C "$BUILD/data" .
gzip -n -9 "$BUILD/control.tar" "$BUILD/data.tar"

( cd "$BUILD" && ar rcD "$IPK" debian-binary control.tar.gz data.tar.gz )

# ----------------------------------------------------------------- verify ----
MEMBERS=$(ar t "$IPK" | tr '\n' ' ')
case "$MEMBERS" in
    "debian-binary control.tar.gz data.tar.gz "*) ;;
    *) echo "build-ipk.sh: unexpected archive members: $MEMBERS" >&2; exit 1 ;;
esac

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
