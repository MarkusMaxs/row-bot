"""The macOS bundle declares the macOS version its bundled wheels need (D18).

Several wheels in the lock (av, faiss-cpu, onnxruntime) require macOS 14, so
the app says so instead of failing at import on older systems.
"""
from __future__ import annotations

import plistlib
import re
from pathlib import Path

import pytest


pytestmark = [pytest.mark.contract, pytest.mark.installer]

MINIMUM = "14.0"


def test_the_app_requires_macos_14_everywhere_it_is_declared() -> None:
    build = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    generated = re.search(r"<key>LSMinimumSystemVersion</key>\s*<string>([^<]+)</string>", build)
    template = plistlib.loads(Path("installer/Row-Bot.app/Contents/Info.plist").read_bytes())

    assert generated and generated.group(1) == MINIMUM
    assert template["LSMinimumSystemVersion"] == MINIMUM
    assert re.findall(r"-mmacosx-version-min=([0-9.]+)", build) == [MINIMUM]


def _generated_info_plist() -> dict:
    build = Path("installer/build_mac_app.sh").read_text(encoding="utf-8")
    body = re.search(r'cat > "\$CONTENTS/Info\.plist" << PLIST\n(.*?)\nPLIST', build, re.S)
    assert body, "build_mac_app.sh no longer writes Info.plist inline"
    return plistlib.loads(body.group(1).replace("${VERSION}", "5.3.0").encode("utf-8"))


def test_the_app_window_takes_row_bots_name_and_icon_from_the_bundle() -> None:
    """The window runs as the bundle's own program (row-bot --run-python), so the Dock, Cmd-Tab and
    Force Quit name it after the bundle (B385); programs that open no window stay out of the Dock."""
    generated = _generated_info_plist()
    template = plistlib.loads(Path("installer/Row-Bot.app/Contents/Info.plist").read_bytes())

    for info in (generated, template):
        assert info["CFBundleName"] == info["CFBundleDisplayName"] == "Row-Bot"
        assert info["CFBundleIdentifier"] == "ai.row-bot.assistant"
        assert info["CFBundleExecutable"] == "row-bot"
        assert info["CFBundleIconFile"] == "row-bot"
    assert generated["LSUIElement"] is True
