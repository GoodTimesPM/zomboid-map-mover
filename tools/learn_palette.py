"""Learn tile colours for the lot renderer from vanilla's own terrain image.

Vanilla ships both its lot files and pyramid.zip at 1 pixel per square, so
the average image colour under each floor tile is the colour to paint that
tile on maps that have no image. Writes mapmover/palette.json.

Needs Pillow. Only the developer runs this; the app reads the JSON.

    py -3 tools/learn_palette.py [cells to sample, default 600]
"""

import io
import json
import os
import random
import sys
import zipfile

from PIL import Image

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from mapmover.lots import LotError, LotPack, read_header  # noqa: E402
from mapmover.overlay import VANILLA_DIR  # noqa: E402
from mapmover.paths import ROOT, load_config  # noqa: E402
from mapmover.render import feature_of, sheet_of  # noqa: E402

MIN_SQUARES = 40


def main():
    want = int(sys.argv[1]) if len(sys.argv) > 1 else 600
    vdir = os.path.join(load_config()["game_dir"], "media", "maps", VANILLA_DIR)
    zf = zipfile.ZipFile(os.path.join(vdir, "pyramid.zip"))
    names = set(zf.namelist())
    cells = []
    for f in os.listdir(vdir):
        if f.endswith(".lotheader"):
            x, y = map(int, f[:-10].split("_"))
            if f"0/tile{x}x{y}.png" in names:
                cells.append((x, y))
    random.seed(1)
    random.shuffle(cells)
    sums = {}  # key -> [r, g, b, n]

    def add(key, px):
        s = sums.setdefault(key, [0, 0, 0, 0])
        s[0] += px[0]
        s[1] += px[1]
        s[2] += px[2]
        s[3] += 1

    for i, (cx, cy) in enumerate(cells[:want]):
        try:
            h = read_header(open(os.path.join(vdir, f"{cx}_{cy}.lotheader"), "rb").read())
            pack = LotPack(open(os.path.join(vdir, f"world_{cx}_{cy}.lotpack"), "rb").read(), h)
            squares = pack.level0()
        except (OSError, LotError) as e:
            print(f"skip {cx},{cy}: {e}")
            continue
        img = Image.open(io.BytesIO(zf.read(f"0/tile{cx}x{cy}.png"))).convert("RGB")
        if img.size != (256, 256):
            continue
        px = img.load()
        rooms = set()
        for room in h.rooms:
            if room["level"] == 0:
                for rx, ry, rw, rh in room["rects"]:
                    rooms.update((x, y) for x in range(rx, rx + rw) for y in range(ry, ry + rh))
        for (x, y), tiles in squares.items():
            names_ = [h.tiles[t] for t in tiles]
            feat = feature_of(names_, (x, y) in rooms)
            c = px[x, y]
            if feat:
                add("feature:" + feat, c)
            else:
                add("floor:" + names_[0], c)
                add("sheet:" + sheet_of(names_[0]), c)
        if i % 50 == 0:
            print(f"{i}/{min(want, len(cells))} cells")

    out = {"floor": {}, "sheet": {}, "feature": {}}
    for key, (r, g, b, n) in sorted(sums.items()):
        if n < MIN_SQUARES:
            continue
        kind, name = key.split(":", 1)
        out[kind][name] = [round(r / n), round(g / n), round(b / n)]
    path = os.path.join(ROOT, "mapmover", "palette.json")
    parts = []
    for kind in sorted(out):
        rows = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in sorted(out[kind].items()))
        parts.append(f"{json.dumps(kind)}: {{\n{rows}\n}}")
    with open(path, "w", encoding="utf-8") as f:
        f.write("{\n" + ",\n".join(parts) + "\n}\n")
    print(f"wrote {path}: {len(out['floor'])} floors, {len(out['sheet'])} sheets, "
          f"features {out['feature']}")


if __name__ == "__main__":
    main()
