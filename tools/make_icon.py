"""Draw mapmover.ico: a grid of map cells with one cell being moved.
py -3 tools/make_icon.py (needs Pillow; the app itself doesn't)."""

import os

from PIL import Image, ImageDraw

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
S = 1024  # drawn large, then scaled down per icon size


def draw():
    im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.rounded_rectangle((32, 32, S - 32, S - 32), radius=180, fill=(23, 30, 26, 255),
                        outline=(70, 96, 74, 255), width=24)
    # 3x3 cells of land, the middle-right one empty: that's where a map left.
    pad, gap = 150, 34
    cell = (S - 2 * pad - 2 * gap) // 3
    greens = [(74, 122, 70), (92, 140, 80), (64, 108, 66)]
    for gy in range(3):
        for gx in range(3):
            x0, y0 = pad + gx * (cell + gap), pad + gy * (cell + gap)
            box = (x0, y0, x0 + cell, y0 + cell)
            if (gx, gy) == (2, 1):
                d.rounded_rectangle(box, radius=28, outline=(150, 170, 150, 255), width=14)
                continue
            d.rounded_rectangle(box, radius=28, fill=greens[(gx + gy) % 3] + (255,))
    # A road across the middle row.
    y = pad + cell + gap + cell // 2
    d.rectangle((pad, y - 16, pad + 2 * cell + gap, y + 16), fill=(60, 60, 64, 255))
    # The moved map, lifted up and to the right, in amber.
    x0, y0 = pad + 2 * (cell + gap) - 40, pad - 90
    d.rounded_rectangle((x0, y0, x0 + cell, y0 + cell), radius=28, fill=(232, 160, 60, 255),
                        outline=(255, 214, 138, 255), width=12)
    # Arrow from the empty slot to it.
    ax, ay = pad + 2 * (cell + gap) + cell // 2, pad + cell + gap + cell // 2
    d.line((ax, ay, ax + 10, y0 + cell + 50), fill=(255, 214, 138, 255), width=30)
    d.polygon([(ax + 10 - 60, y0 + cell + 70), (ax + 10 + 60, y0 + cell + 70), (ax + 10, y0 + cell - 10)],
              fill=(255, 214, 138, 255))
    return im


if __name__ == "__main__":
    im = draw()
    out = os.path.join(ROOT, "mapmover.ico")
    im.save(out, sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
    im.resize((256, 256), Image.LANCZOS).save(os.path.join(ROOT, "web", "icon.png"))
    print("wrote", out)
