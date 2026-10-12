#!/usr/bin/env bash
# Installs this Row-Bot release for the current user, without root:
#   $XDG_DATA_HOME/row-bot/releases/<version>/  this folder
#   $XDG_DATA_HOME/row-bot/current               the active release
#   $XDG_DATA_HOME/row-bot/state.json            active, and previous (the release this replaces)
#   ~/.local/bin/row-bot                          -> current/bin/row-bot
# Releases older than previous are removed. User data in ~/.row-bot is never touched.
set -euo pipefail

SOURCE_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VERSION="$(SOURCE_DIR="$SOURCE_DIR" "$SOURCE_DIR/python/bin/python3" -I -S - <<'PY'
import json, os
from pathlib import Path
print(json.loads((Path(os.environ['SOURCE_DIR']) / 'install_info.json').read_text())['version'])
PY
)"
APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/row-bot"
RELEASES_DIR="$APP_HOME/releases"
TARGET="$RELEASES_DIR/$VERSION"
STAGING="$RELEASES_DIR/.installing-$VERSION-$$"
BIN_HOME="${HOME}/.local/bin"
DESKTOP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

mkdir -p "$RELEASES_DIR" "$BIN_HOME" "$DESKTOP_HOME" "$ICON_HOME"
rm -rf "$STAGING"
cp -a "$SOURCE_DIR" "$STAGING"
rm -rf "$TARGET"
mv "$STAGING" "$TARGET"
chmod +x "$TARGET/bin/row-bot"
chmod +x "$TARGET/install.sh" "$TARGET/uninstall.sh" 2>/dev/null || true

# state.json and `current` (src/row_bot/install_layout.py, run by the release just installed).
"$TARGET/python/bin/python3" -I -S "$TARGET/app/src/row_bot/install_layout.py" \
    record-install --root "$APP_HOME" --kind linux --version "$VERSION" --prune >/dev/null
ln -sfn "$APP_HOME/current/bin/row-bot" "$BIN_HOME/row-bot"

cp "$TARGET/share/applications/ai.row-bot.RowBot.desktop" "$DESKTOP_HOME/ai.row-bot.RowBot.desktop"
sed -i "s|^Exec=.*|Exec=$BIN_HOME/row-bot|" "$DESKTOP_HOME/ai.row-bot.RowBot.desktop"
cp "$TARGET/share/icons/hicolor/256x256/apps/row-bot.png" "$ICON_HOME/row-bot.png" 2>/dev/null || true

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_HOME" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

if [ "${ROW_BOT_SUPPRESS_INSTALL_PATH_HINT:-0}" != "1" ]; then
    LAUNCH_CMD="row-bot"
    case ":${PATH}:" in
        *":${BIN_HOME}:"*) ;;
        *)
            LAUNCH_CMD="$BIN_HOME/row-bot"
            echo "[WARN] $BIN_HOME is not on PATH. Run $BIN_HOME/row-bot now, or add this to your shell profile:"
            echo '       export PATH="$HOME/.local/bin:$PATH"'
            echo "       Open a new terminal after updating your profile."
            ;;
    esac

    echo "Row-Bot $VERSION installed. Run: $LAUNCH_CMD"
fi
