"""Draw the DMG window background, 660x420 at 1x and 2x (installer/macos/dmg-background*.png).

Run once by hand when the design changes, with Pillow and the Segoe UI fonts (Windows):
``python installer/macos/draw_dmg_background.py installer/macos``. The build only uses the PNGs.
"""
import math
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFilter, ImageFont

OUT = Path(sys.argv[1])
W, H = 660, 420
APP_X, APPS_X, ICON_Y = 180, 480, 236  # icon centres, as dmg_layout.py places them
FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"
FONT = "C:/Windows/Fonts/segoeui.ttf"


def draw(scale: int) -> Image.Image:
    w, h = W * scale, H * scale
    img = Image.new("RGB", (w, h))
    px = img.load()
    # A mid-tone slate-teal: Finder's black labels (light mode) and white labels (dark mode) both read on it.
    top, bottom = (104, 138, 152), (78, 108, 122)
    for y in range(h):
        t = y / (h - 1)
        row = tuple(round(top[i] + (bottom[i] - top[i]) * t) for i in range(3))
        for x in range(w):
            # A soft lighter glow behind the two icons.
            d = min(math.hypot(x - APP_X * scale, y - ICON_Y * scale),
                    math.hypot(x - APPS_X * scale, y - ICON_Y * scale)) / (150 * scale)
            glow = max(0.0, 1.0 - d) ** 2 * 18
            px[x, y] = tuple(min(255, round(c + glow)) for c in row)

    lines = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ld = ImageDraw.Draw(lines)
    cyan = (120, 230, 255)
    # Faint circuit traces at the edges, the brand's motif.
    for y0, x0, x1, step in ((22, 0, 96, 12), (360, 540, 660, -20), (390, 0, 90, 0), (26, 580, 660, 0)):
        pts = [(x0 * scale, y0 * scale), (x1 * scale, y0 * scale), ((x1 + 20) * scale, (y0 + step) * scale)]
        ld.line(pts, fill=cyan + (70,), width=2 * scale)
        ex, ey = pts[-1]
        ld.ellipse((ex - 3 * scale, ey - 3 * scale, ex + 3 * scale, ey + 3 * scale), fill=cyan + (110,))

    # The arrow from the app to Applications, with a glow.
    arrow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ad = ImageDraw.Draw(arrow)
    x0, x1, y = (APP_X + 92) * scale, (APPS_X - 92) * scale, ICON_Y * scale
    ad.line([(x0, y), (x1 - 14 * scale, y)], fill=(235, 252, 255, 255), width=5 * scale)
    ad.polygon([(x1, y), (x1 - 22 * scale, y - 14 * scale), (x1 - 22 * scale, y + 14 * scale)],
               fill=(235, 252, 255, 255))
    glow = arrow.filter(ImageFilter.GaussianBlur(6 * scale))
    tint = Image.new("RGBA", (w, h), cyan + (0,))
    tint.putalpha(glow.getchannel("A").point(lambda a: int(a * 0.9)))

    base = img.convert("RGBA")
    base = Image.alpha_composite(base, lines)
    base = Image.alpha_composite(base, tint)
    base = Image.alpha_composite(base, arrow)

    td = ImageDraw.Draw(base)
    title = ImageFont.truetype(FONT_BOLD, 26 * scale)
    sub = ImageFont.truetype(FONT, 15 * scale)
    t1 = "Drag Row-Bot to Applications"
    t2 = "Then open it from Applications or Launchpad."
    for text, font, ty, fill in ((t1, title, 52, (255, 255, 255)), (t2, sub, 92, (226, 244, 250))):
        tw = td.textlength(text, font=font)
        td.text(((w - tw) / 2 + 1 * scale, ty * scale + 1 * scale), text, font=font, fill=(30, 50, 60, 120))
        td.text(((w - tw) / 2, ty * scale), text, font=font, fill=fill)
    return base.convert("RGB")


OUT.mkdir(parents=True, exist_ok=True)
draw(1).save(OUT / "dmg-background.png", optimize=True)
draw(2).save(OUT / "dmg-background@2x.png", optimize=True)
print("drawn")
