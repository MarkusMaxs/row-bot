#!/usr/bin/env bash
# =============================================================================
# build_mac_dmg.sh - the release DMG: Row-Bot.app next to an Applications link, on the
# designed background (installer/macos/dmg-background*.png, laid out by dmg_layout.py)
#
# Usage: installer/build_mac_dmg.sh <Row-Bot.app> <output.dmg> [signing identity]
#
# The identity defaults to $CODESIGN_IDENTITY; without one the DMG is unsigned.
# DMG_PYTHON names the Python (with pip) for the layout step; it defaults to python3.
# release.yml and notarize-finish.yml build the DMG from the notarized and
# stapled app, then notarize and staple the DMG itself (scripts/macos_notarize.py).
# =============================================================================

set -euo pipefail

APP="${1:?usage: build_mac_dmg.sh <Row-Bot.app> <output.dmg> [identity]}"
DMG="${2:?usage: build_mac_dmg.sh <Row-Bot.app> <output.dmg> [identity]}"
IDENTITY="${3:-${CODESIGN_IDENTITY:-}}"

if [ ! -d "$APP/Contents" ]; then
    echo "::error::Not an app bundle: $APP"
    exit 1
fi

HERE="$(cd "$(dirname "$0")" && pwd)"
PYTHON="${DMG_PYTHON:-python3}"
WORK="$(mktemp -d)"
MOUNT=""
cleanup() {
    [ -n "$MOUNT" ] && hdiutil detach "$MOUNT" -quiet -force >/dev/null 2>&1 || true
    rm -rf "$WORK"
}
trap cleanup EXIT
mkdir -p "$WORK/dmg-root/.background" "$(dirname "$DMG")"
# ditto keeps the bundle's signature, extended attributes and stapled ticket.
ditto "$APP" "$WORK/dmg-root/Row-Bot.app"
ln -s /Applications "$WORK/dmg-root/Applications"
# The window's background (U1), with its 2x picture for Retina screens.
tiffutil -cathidpicheck "$HERE/macos/dmg-background.png" "$HERE/macos/dmg-background@2x.png" \
    -out "$WORK/dmg-root/.background/background.tiff" >/dev/null

# The layout tool's two pure-Python packages, pinned by hash, in a throwaway folder.
"$PYTHON" -m pip install --quiet --disable-pip-version-check --no-deps --target "$WORK/layout-lib" \
    --require-hashes -r "$HERE/macos/dmg-layout-requirements.txt"

rm -f "$DMG"
hdiutil create -volname "Row-Bot" -srcfolder "$WORK/dmg-root" -format UDRW -ov "$WORK/layout.dmg"
# Mounted under its own name, so the background's alias resolves wherever the DMG is opened.
MOUNT="$(hdiutil attach "$WORK/layout.dmg" -nobrowse -noverify -noautoopen | awk -F '\t' '/\/Volumes\// {print $NF}' | tail -1)"
if [ -z "$MOUNT" ] || [ ! -d "$MOUNT" ]; then
    echo "::error::Could not mount the layout image"
    exit 1
fi
PYTHONPATH="$WORK/layout-lib" "$PYTHON" "$HERE/macos/dmg_layout.py" "$MOUNT"
hdiutil detach "$MOUNT" -quiet
MOUNT=""
hdiutil convert "$WORK/layout.dmg" -format UDZO -o "$DMG" -ov

if [ -n "$IDENTITY" ]; then
    codesign --force --timestamp --options runtime --sign "$IDENTITY" "$DMG"
    codesign --verify --strict "$DMG"
else
    echo "::warning::No signing identity: $DMG is not signed"
fi
echo "DMG: $DMG ($(du -h "$DMG" | cut -f1))"
