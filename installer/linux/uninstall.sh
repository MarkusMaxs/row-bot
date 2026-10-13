#!/usr/bin/env bash
# Removes Row-Bot's application files for the current user: every release,
# state.json, the command and the desktop entry. User data in ~/.row-bot (or
# ROW_BOT_DATA_DIR) is left untouched.
set -euo pipefail

APP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/row-bot"
BIN_HOME="${HOME}/.local/bin"
DESKTOP_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/applications"
ICON_HOME="${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor/256x256/apps"

rm -f "$BIN_HOME/row-bot"
rm -f "$DESKTOP_HOME/ai.row-bot.RowBot.desktop"
rm -f "$ICON_HOME/row-bot.png"
rm -rf "$APP_HOME"

command -v update-desktop-database >/dev/null 2>&1 && update-desktop-database "$DESKTOP_HOME" >/dev/null 2>&1 || true
command -v gtk-update-icon-cache >/dev/null 2>&1 && gtk-update-icon-cache "${XDG_DATA_HOME:-$HOME/.local/share}/icons/hicolor" >/dev/null 2>&1 || true

echo "Row-Bot application files removed. User data in ~/.row-bot was left untouched."
