"""Draw a map cell from its lot files, for maps that ship no terrain image.

Each level 0 square becomes one pixel. Its colour comes from palette.json,
which tools/learn_palette.py learns from vanilla's own terrain image: the
average colour under each floor tile, plus one colour each for squares with
a building room, a wall or a tree. Empty squares stay transparent.
"""

import json
import os
import re
import struct
import zlib

from .lots import CELL, LotPack, read_header

_PALETTE = None
_INDEX = re.compile(r"_\d+$")

# Used when a tile is in neither the learned floors nor the learned sheets.
FALLBACK = [
    (re.compile(r"street|road|asphalt|parking", re.I), (92, 92, 92)),
    (re.compile(r"water", re.I), (60, 90, 130)),
    (re.compile(r"sand|beach", re.I), (190, 170, 120)),
    (re.compile(r"dirt|gravel|mud", re.I), (120, 100, 70)),
    (re.compile(r"floor|tile|carpet|wood", re.I), (150, 140, 125)),
]
DEFAULT = (95, 110, 70)
FEATURE_DEFAULT = {"room": (140, 120, 110), "wall": (110, 100, 95), "tree": (60, 80, 45)}


def sheet_of(name):
    """blends_natural_01_16 -> blends_natural_01."""
    return _INDEX.sub("", name)


def feature_of(names, in_room):
    """What sits on a square, by priority: a room, a wall, a tree, or nothing."""
    if in_room:
        return "room"
    wall = tree = False
    for n in names[1:]:
        if n.startswith("walls_"):
            wall = True
        elif "tree" in n:
            tree = True
    return "wall" if wall else "tree" if tree else ""


def palette():
    global _PALETTE
    if _PALETTE is None:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "palette.json")
        try:
            with open(path, encoding="utf-8") as f:
                _PALETTE = json.load(f)
        except (OSError, ValueError):
            _PALETTE = {"floor": {}, "sheet": {}, "feature": {}}
    return _PALETTE


def colour_of(name):
    pal = palette()
    c = pal["floor"].get(name) or pal["sheet"].get(sheet_of(name))
    if c:
        return tuple(c)
    for rx, col in FALLBACK:
        if rx.search(name):
            return col
    return DEFAULT


def render_cell(header_bytes, pack_bytes):
    """RGBA bytes, 256 x 256, row by row."""
    h = read_header(header_bytes)
    from .cells import rooms_of
    return render_squares(h, LotPack(pack_bytes, h).level0(), rooms_of(h))


def render_squares(h, squares, rooms):
    """RGBA bytes for a parsed cell: its level 0 squares and room squares."""
    pal = palette()
    feat_col = {k: tuple(pal["feature"].get(k) or v) for k, v in FEATURE_DEFAULT.items()}
    room = bytes((*feat_col["room"], 255))
    seen = {}   # tile stack -> pixel, worked out once per cell
    px = bytearray(CELL * CELL * 4)
    n_tiles = len(h.tiles)
    for (x, y), tiles in squares.items():
        i = (y * CELL + x) * 4
        if (x, y) in rooms:
            px[i:i + 4] = room
            continue
        k = tuple(tiles)
        c = seen.get(k)
        if c is None:
            names = [h.tiles[t] if 0 <= t < n_tiles else "" for t in tiles]
            feat = feature_of(names, False)
            rgb = feat_col[feat] if feat else colour_of(names[0])
            c = seen[k] = bytes((rgb[0], rgb[1], rgb[2], 255))
        px[i:i + 4] = c
    return bytes(px)


def png_rgba(width, height, rgba):
    """Encode RGBA bytes as a PNG with no filtering."""
    row = width * 4
    raw = b"".join(b"\0" + rgba[y * row:(y + 1) * row] for y in range(height))

    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    return (b"\x89PNG\r\n\x1a\n"
            + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 6))
            + chunk(b"IEND", b""))


SIZES = {0: 256, 2: 64, 4: 16}  # level -> cell image size in pixels


def shrink(rgba, size):
    """Nearest-pixel downscale of a 256 x 256 RGBA image."""
    step = CELL // size
    out = bytearray(size * size * 4)
    for y in range(size):
        src = (y * step * CELL) * 4
        row = rgba[src:src + CELL * 4]
        o = y * size * 4
        for x in range(size):
            s = x * step * 4
            out[o + x * 4:o + x * 4 + 4] = row[s:s + 4]
    return bytes(out)
