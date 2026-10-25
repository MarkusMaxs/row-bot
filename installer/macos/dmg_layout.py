"""Write the Finder layout of a mounted DMG: its background, window size and icon places (U1).

Usage: dmg_layout.py <mounted volume>

The volume holds Row-Bot.app, an Applications link and .background/background.tiff. Finder reads
the layout from the volume's .DS_Store; the background is found by an alias, made here while the
volume is mounted under its own name. Positions match installer/macos/draw_dmg_background.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

from ds_store import DSStore
from mac_alias import Alias

WIDTH, HEIGHT = 660, 420
ICON_Y = 236
PLACES = {"Row-Bot.app": (180, ICON_Y), "Applications": (480, ICON_Y)}


def write_layout(volume: Path) -> None:
    background = volume / ".background" / "background.tiff"
    if not background.is_file():
        raise SystemExit(f"Missing {background}")
    for name in PLACES:
        if not (volume / name).exists():
            raise SystemExit(f"Missing {volume / name}")
    with DSStore.open(str(volume / ".DS_Store"), "w+") as store:
        store["."]["vSrn"] = ("long", 1)
        store["."]["bwsp"] = {
            "ShowStatusBar": False, "ShowToolbar": False, "ShowPathbar": False, "ShowSidebar": False,
            "ShowTabView": False, "ContainerShowSidebar": False, "PreviewPaneVisibility": False,
            "SidebarWidth": 0, "WindowBounds": f"{{{{200, 160}}, {{{WIDTH}, {HEIGHT}}}}}",
        }
        store["."]["icvp"] = {
            "viewOptionsVersion": 1, "backgroundType": 2,
            "backgroundImageAlias": Alias.for_file(str(background)).to_bytes(),
            "backgroundColorRed": 1.0, "backgroundColorGreen": 1.0, "backgroundColorBlue": 1.0,
            "arrangeBy": "none", "gridOffsetX": 0.0, "gridOffsetY": 0.0, "gridSpacing": 100.0,
            "iconSize": 128.0, "textSize": 13.0, "labelOnBottom": True, "showIconPreview": False,
            "showItemInfo": False, "scrollPositionX": 0.0, "scrollPositionY": 0.0,
        }
        store["."]["icvl"] = ("type", b"icnv")
        for name, place in PLACES.items():
            store[name]["Iloc"] = place


if __name__ == "__main__":
    if len(sys.argv) != 2:
        raise SystemExit("usage: dmg_layout.py <mounted volume>")
    write_layout(Path(sys.argv[1]))
