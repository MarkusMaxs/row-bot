#!/usr/bin/env bash
# =============================================================================
# build_mac_dmg.sh - the release DMG: Row-Bot.app next to an Applications link
#
# Usage: installer/build_mac_dmg.sh <Row-Bot.app> <output.dmg> [signing identity]
#
# The identity defaults to $CODESIGN_IDENTITY; without one the DMG is unsigned.
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

WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT
mkdir -p "$WORK/dmg-root" "$(dirname "$DMG")"
# ditto keeps the bundle's signature, extended attributes and stapled ticket.
ditto "$APP" "$WORK/dmg-root/Row-Bot.app"
ln -s /Applications "$WORK/dmg-root/Applications"

rm -f "$DMG"
hdiutil create -volname "Row-Bot" -srcfolder "$WORK/dmg-root" -format UDZO -ov "$DMG"

if [ -n "$IDENTITY" ]; then
    codesign --force --timestamp --options runtime --sign "$IDENTITY" "$DMG"
    codesign --verify --strict "$DMG"
else
    echo "::warning::No signing identity: $DMG is not signed"
fi
echo "DMG: $DMG ($(du -h "$DMG" | cut -f1))"
