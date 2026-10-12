"""The Windows installer's contract: per user, the versions layout, and Row-Bot.exe.

Inno Setup cannot run on the Linux PR lane, so this reads the script's
declarative sections. Its [Code] behaviour (removing a 5.x install for all
users, /NOLAUNCH, state.json) is covered by installer-verify, which builds,
installs, starts and uninstalls the real package
(.github/actions/smoke-windows-installer).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest


pytestmark = [pytest.mark.contract, pytest.mark.installer]

ISS = Path("installer/row_bot_setup.iss")


def _text() -> str:
    return ISS.read_text(encoding="utf-8")


def _section(name: str) -> list[str]:
    match = re.search(rf"^\[{re.escape(name)}\]\s*$(.*?)(?=^\[|\Z)", _text(), re.M | re.S)
    assert match, name
    return [line.strip() for line in match.group(1).splitlines() if line.strip() and not line.strip().startswith(";")]


def _setup() -> dict[str, str]:
    return dict(line.split("=", 1) for line in _section("Setup"))


def _define(name: str) -> str:
    match = re.search(rf'^#define\s+{name}\s+(.+)$', _text(), re.M)
    assert match, name
    return match.group(1).strip()


def _entries(section: str) -> list[dict[str, str]]:
    entries = []
    for line in _section(section):
        fields = dict(re.findall(r'(\w+):\s*("[^"]*"|[^;]+)', line))
        entries.append({key: value.strip().strip('"') for key, value in fields.items()})
    return entries


def _expand(value: str) -> str:
    version_dir = "{app}\\versions\\{#MyAppVersion}"
    return value.replace("{#VersionDir}", version_dir)


def test_it_installs_per_user_without_administrator_rights() -> None:
    setup = _setup()

    assert setup["PrivilegesRequired"] == "lowest"
    assert "PrivilegesRequiredOverridesAllowed" not in setup
    # {autopf} is %LOCALAPPDATA%\Programs for a per-user install.
    assert setup["DefaultDirName"] == "{autopf}\\{#MyAppName}"
    assert setup["Compression"] == "lzma2"
    for page in ("DisableWelcomePage", "DisableDirPage", "DisableReadyPage", "DisableFinishedPage"):
        assert setup[page] == "yes", page


def test_each_version_gets_its_own_folder_beside_row_bot_exe() -> None:
    assert _define("VersionDir") == '"{app}\\versions\\" + MyAppVersion'
    destinations = {entry["Source"]: _expand(entry["DestDir"]) for entry in _entries("Files")}

    assert destinations == {
        "build\\Row-Bot.exe": "{app}",
        "{#AppStageDir}\\*": "{app}\\versions\\{#MyAppVersion}\\app",
        "build\\python\\*": "{app}\\versions\\{#MyAppVersion}\\python",
        "build\\version.json": "{app}\\versions\\{#MyAppVersion}",
    }


def test_shortcuts_start_row_bot_exe_with_the_windows_app_id() -> None:
    icons = {Path(entry["Name"].replace("\\", "/")).parent.name: entry for entry in _entries("Icons")}

    assert set(icons) == {"{autoprograms}", "{autodesktop}"}
    for icon in icons.values():
        assert icon["Filename"] == "{app}\\Row-Bot.exe"
        assert icon["AppUserModelID"] == "{#MyAppUserModelID}"
    assert _define("MyAppUserModelID") == '"ai.row-bot.desktop"'
    # An upgrade never brings back a desktop shortcut the user removed.
    assert icons["{autodesktop}"]["Check"] == "IsFirstInstall"
    assert "Check" not in icons["{autoprograms}"]


def test_row_bot_starts_after_every_install_even_a_silent_one() -> None:
    (run,) = _entries("Run")

    assert run["Filename"] == "{app}\\Row-Bot.exe"
    flags = run["Flags"].split()
    # 5.1's updater runs the next installer with /SILENT; skipifsilent and
    # postinstall entries would leave Row-Bot closed after the update.
    assert "skipifsilent" not in flags and "postinstall" not in flags
    assert run["Check"] == "ShouldLaunch"


def test_a_reinstall_replaces_the_version_folder_and_nothing_outside_the_install_root() -> None:
    install_delete = [entry["Name"] for entry in _entries("InstallDelete")]
    uninstall_delete = [entry["Name"] for entry in _entries("UninstallDelete")]

    assert "{app}\\versions\\{#MyAppVersion}" in install_delete
    assert "{app}\\versions" in uninstall_delete
    for path in install_delete + uninstall_delete:
        # User data lives in %USERPROFILE%\.row-bot, never under {app}.
        assert path == "{app}" or path.startswith("{app}\\"), path


def test_the_install_for_all_users_it_replaces_is_the_same_app() -> None:
    # The per-user install keeps the 5.x AppId, so the 5.x entry it removes is Row-Bot's own.
    app_id = _setup()["AppId"].lstrip("{")

    assert app_id in _define("LegacyUninstallKey")
