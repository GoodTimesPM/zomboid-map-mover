"""Top-down terrain images the game ships as pyramid.zip, for drawing under the grid.

A pyramid.zip holds pyramid.txt plus PNG tiles named <level>/tile<x>x<y>.png.
pyramid.txt gives `bounds` (absolute world tiles x0 y0 x1 y1) and `imageSize`
(the level 0 image in pixels, the bounds size when missing). Every level is
cut into 256-pixel tiles, and each level halves the one before it. Vanilla's
image is 1 pixel per tile, so a level 0 tile covers exactly one cell.

forest.pyramid.zip and spawnSelectImagePyramid.zip are other layers and are
not used here.
"""

import os
import re
import threading
import zipfile

TILE_PX = 256
_TILE = re.compile(r"^(\d+)/tile(\d+)x(\d+)\.png$")


class PyramidError(Exception):
    pass


def parse_pyramid_txt(text):
    """{"bounds": [x0, y0, x1, y1], "image": [w, h]} from pyramid.txt."""
    vals = {}
    for line in text.splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            vals[k.strip()] = v.split()
    try:
        b = [int(float(n)) for n in vals["bounds"][:4]]
    except (KeyError, ValueError):
        raise PyramidError("pyramid.txt has no usable bounds")
    if len(b) != 4 or b[2] <= b[0] or b[3] <= b[1]:
        raise PyramidError(f"bad bounds {b}")
    try:
        img = [int(n) for n in vals["imageSize"][:2]]
    except (KeyError, ValueError):
        img = [b[2] - b[0], b[3] - b[1]]
    if len(img) != 2 or img[0] <= 0 or img[1] <= 0:
        img = [b[2] - b[0], b[3] - b[1]]
    return {"bounds": b, "image": img}


class Pyramid:
    """One open pyramid.zip. Reads are locked, so server threads can share it."""

    def __init__(self, path):
        self.path = path
        self._lock = threading.Lock()
        self._zip = zipfile.ZipFile(path)
        names = self._zip.namelist()
        txt = next((n for n in names if n.lower().endswith("pyramid.txt")), None)
        if not txt:
            raise PyramidError("no pyramid.txt")
        info = parse_pyramid_txt(self._zip.read(txt).decode("utf-8", "replace"))
        self.bounds, self.image = info["bounds"], info["image"]
        self.tiles = set()
        levels = set()
        for n in names:
            m = _TILE.match(n)
            if m:
                z, x, y = map(int, m.groups())
                self.tiles.add((z, x, y))
                levels.add(z)
        if 0 not in levels:
            raise PyramidError("no level 0 tiles")
        self.levels = max(levels) + 1

    def to_json(self):
        return {"bounds": self.bounds, "image": self.image, "levels": self.levels}

    def tile(self, z, x, y):
        if (z, x, y) not in self.tiles:
            return None
        with self._lock:
            return self._zip.read(f"{z}/tile{x}x{y}.png")


def find_pyramid(map_dir):
    p = os.path.join(map_dir, "pyramid.zip")
    return p if os.path.isfile(p) else None
