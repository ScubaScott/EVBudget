"""Regenerate the app icons (evbudget.png, .ico, .icns, and the copy the web pages use).

Needs Pillow (pip install pillow). Only for rebuilding the icons; the app itself doesn't use it.
Run from anywhere:  python assets/make_icons.py
"""

import os
from PIL import Image, ImageDraw, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
SIZE = 1024
BLUE, GREEN = (59, 130, 246), (16, 185, 129)   # --accent-blue and --accent-green in dashboard.css
FONTS = [
    r"C:\Windows\Fonts\arialbd.ttf", "/System/Library/Fonts/Helvetica.ttc",
    "/Library/Fonts/Arial Bold.ttf", "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def gradient() -> Image.Image:
    """Diagonal blue-to-green, like the header logo."""
    small = Image.new("RGB", (256, 256))
    px = small.load()
    for y in range(256):
        for x in range(256):
            t = (x + y) / 510
            px[x, y] = tuple(round(a + (b - a) * t) for a, b in zip(BLUE, GREEN))
    return small.resize((SIZE, SIZE), Image.BICUBIC)


def logo() -> Image.Image:
    """The header logo's rounded square with a B that doubles as a dollar sign: a bar runs through the letter."""
    img = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 0))
    margin = int(SIZE * 0.06)  # leave room for the soft edge macOS and Windows draw around icons
    mask = Image.new("L", (SIZE * 2, SIZE * 2), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        [margin * 2, margin * 2, SIZE * 2 - margin * 2, SIZE * 2 - margin * 2], radius=int(SIZE * 0.40), fill=255
    )
    img.paste(gradient(), (0, 0), mask.resize((SIZE, SIZE), Image.LANCZOS))

    font = next((ImageFont.truetype(f, int(SIZE * 0.58)) for f in FONTS if os.path.exists(f)), None)
    if font is None:
        raise SystemExit("No bold font found; add one to FONTS.")

    # Work at 2x and shrink, so the edges are smooth
    big = SIZE * 2
    letter = Image.new("L", (big, big), 0)
    ImageDraw.Draw(letter).text((big / 2, big / 2), "B", font=ImageFont.truetype(font.path, int(SIZE * 1.16)), fill=255, anchor="mm")
    left, top, right, bottom = letter.getbbox()

    # Like the stroke of a dollar sign: one white bar down the middle, longer than the letter at both ends.
    # It merges with the B's strokes and shows in its two openings and above and below it.
    bar_w, reach = int(SIZE * 0.024), int(SIZE * 0.14)
    cx = big // 2
    ImageDraw.Draw(letter).rounded_rectangle([cx - bar_w, top - reach, cx + bar_w, bottom + reach], radius=bar_w, fill=255)
    white = Image.new("RGBA", (big, big), "white")
    white.putalpha(letter)
    img.alpha_composite(white.resize((SIZE, SIZE), Image.LANCZOS))
    return img


def main() -> None:
    img = logo()
    img.save(os.path.join(HERE, "evbudget.png"))
    img.save(os.path.join(HERE, "evbudget.ico"), sizes=[(s, s) for s in (16, 24, 32, 48, 64, 128, 256)])
    img.save(os.path.join(HERE, "evbudget.icns"))
    # The same logo for the page header and the browser tab
    web_img = os.path.join(HERE, "..", "web", "img")
    os.makedirs(web_img, exist_ok=True)
    img.resize((256, 256), Image.LANCZOS).save(os.path.join(web_img, "evbudget.png"))
    print("Wrote evbudget.png, evbudget.ico, evbudget.icns in", HERE, "and web/img/evbudget.png")


if __name__ == "__main__":
    main()
