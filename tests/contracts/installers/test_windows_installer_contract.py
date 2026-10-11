"""The Windows installer's contract: per user, the versions layout, and Row-Bot.exe.

Inno Setup cannot run on the Linux PR lane, so this reads the script's
sections; installer-verify builds, installs, starts and uninstalls the real
package (.github/actions/smoke-windows-installer).
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
    assert "launch_row_bot" not in _text()


def test_shortcuts_start_row_bot_exe_with_the_windows_app_id() -> None:
    window_app_id = re.search(r'SetCurrentProcessExplicitAppUserModelID\("([^"]+)"\)',
                              Path("src/row_bot/launcher.py").read_text(encoding="utf-8")).group(1)
    launcher_app_id = re.search(r'#define APP_USER_MODEL_ID L"([^"]+)"',
                                Path("installer/windows/row_bot_launcher.c").read_text(encoding="utf-8")).group(1)

    icons = _entries("Icons")
    assert icons
    for icon in icons:
        assert icon["Filename"] == "{app}\\Row-Bot.exe"
        assert icon["AppUserModelID"] == "{#MyAppUserModelID}"
    assert _define("MyAppUserModelID") == f'"{window_app_id}"' == f'"{launcher_app_id}"'


def test_row_bot_starts_after_every_install_even_a_silent_one() -> None:
    (run,) = _entries("Run")

    assert run["Filename"] == "{app}\\Row-Bot.exe"
    flags = run["Flags"].split()
    # 5.1's updater runs the next installer with /SILENT; skipifsilent and
    # postinstall entries would leave Row-Bot closed after the update.
    assert "skipifsilent" not in flags and "postinstall" not in flags
    assert run["Check"] == "ShouldLaunch"
    assert re.search(r"function ShouldLaunch: Boolean;\s*begin\s*Result := not HasParam\('/NOLAUNCH'\);", _text())


def test_a_reinstall_replaces_the_version_folder_and_nothing_outside_the_install_root() -> None:
    install_delete = [entry["Name"] for entry in _entries("InstallDelete")]
    uninstall_delete = [entry["Name"] for entry in _entries("UninstallDelete")]

    assert "{app}\\versions\\{#MyAppVersion}" in install_delete
    assert "{app}\\versions" in uninstall_delete
    for path in install_delete + uninstall_delete:
        # User data lives in %USERPROFILE%\.row-bot, never under {app}.
        assert path == "{app}" or path.startswith("{app}\\"), path


def test_an_install_for_all_users_is_removed_first() -> None:
    app_id = _setup()["AppId"].lstrip("{")

    assert app_id in _define("LegacyUninstallKey")
    assert re.search(r"function PrepareToInstall\(var NeedsRestart: Boolean\): String;\s*begin\s*"
                     r"Result := RemoveLegacyInstall;", _text())
