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
