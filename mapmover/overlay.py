"""What vanilla Kentucky has where: towns, water and roads, for drawing under the maps.

Everything comes from files the game itself loads:
  * Muldraugh, KY/worldmap.xml.bin: water, road surfaces, rail, buildings
    (the vanilla worldmap.xml is an old 300-tile copy the game ignores)
  * Muldraugh, KY/streets.xml: road names along centre lines
  * Muldraugh, KY/worldmap-annotations.lua: town, river and place labels
  * Translate/EN/MapLabel.json: English names for the MapLabel_ keys

The same reader gives a map mod's own roads, so the app can show where a
moved map's roads run off its edge.
"""

import json
import math
import os
import re
import struct
import xml.etree.ElementTree as ET

from .shifters import CELL, WorldMapBinError

VANILLA_DIR = "Muldraugh, KY"
TOWN_MIN_BUILDINGS = 8      # a cell with this many buildings counts as town
SIMPLIFY_TILES = 3          # drop points closer than this to the last kept one
MAJOR_STREET = re.compile(r"^(KY-|US-|I-\d|Interstate|Route)|Highway|Bypass|Bridge|Parkway|Pkwy", re.I)
MAJOR_STREET_WIDTH = 12

# worldmap feature properties -> drawing layer
ROAD_KINDS = {"primary": "highway", "secondary": "road", "tertiary": "minor"}


# ---------------------------------------------------------------- readers


def read_worldmap_bin(data):
    """Yield (cell_x, cell_y, geometry_type, rings, props) from an IGMB v2 file.

    rings is a list of flat (x0, y0, x1, y1, ...) tuples in tiles relative to
    the cell. Layout follows WorldMapBinary.read, same as shifters.parse_worldmap_bin.
    """
    if data[:4] != b"IGMB":
        raise WorldMapBinError("magic is not IGMB")
    pos = 4

    def take(fmt):
        nonlocal pos
        size = struct.calcsize(fmt)
        if pos + size > len(data):
            raise WorldMapBinError("unexpected end of file")
        v = struct.unpack_from(fmt, data, pos)
        pos += size
        return v

    version, cell_size, width, height, nstrings = take("<5i")
    if version != 2:
        raise WorldMapBinError(f"version {version}, only 2 is supported")
    if cell_size != CELL:
        raise WorldMapBinError(f"cell size {cell_size}")
    strings = []
    for _ in range(max(0, nstrings)):
        (n,) = take("<h")
        if n < 0 or pos + n > len(data):
            raise WorldMapBinError("bad string")
        strings.append(data[pos:pos + n].decode("utf-8", "replace"))
        pos += n

    def s(i):
        return strings[i] if 0 <= i < len(strings) else ""

    for _ in range(max(0, width) * max(0, height)):
        (x,) = take("<i")
        if x == -1:
            continue
        y, nfeatures = take("<2i")
        for _ in range(max(0, nfeatures)):
            (gtype,) = take("<h")
            (nrings,) = take("<B")
            rings = []
            for _ in range(nrings):
                (npoints,) = take("<h")
                rings.append(take(f"<{max(0, npoints) * 2}h"))
            (nprops,) = take("<B")
            props = {}
            for _ in range(nprops):
                k, v = take("<2h")
                props[s(k)] = s(v)
            yield x, y, s(gtype), rings, props


def read_worldmap_xml(data):
    """Same output as read_worldmap_bin, for maps that only ship worldmap.xml.
    The game reads XML cells as 256 tiles too (WorldMapXML has no cell size)."""
    root = ET.fromstring(data)
    for cell in root.iter("cell"):
        try:
            cx, cy = int(cell.get("x")), int(cell.get("y"))
        except (TypeError, ValueError):
            continue
        for feat in cell.iter("feature"):
            geo = feat.find("geometry")
            if geo is None:
                continue
            rings = []
            for coords in geo.iter("coordinates"):
                flat = []
                for p in coords.iter("point"):
                    try:
                        flat += [int(float(p.get("x"))), int(float(p.get("y")))]
                    except (TypeError, ValueError):
                        pass
                rings.append(tuple(flat))
            props = {p.get("name", ""): p.get("value", "") for p in feat.iter("property")}
            yield cx, cy, geo.get("type", ""), rings, props


def read_worldmap_dir(path):
    """Features from a map folder, preferring the .bin like the game does."""
    b = os.path.join(path, "worldmap.xml.bin")
    x = os.path.join(path, "worldmap.xml")
    if os.path.isfile(b):
        with open(b, "rb") as f:
            return list(read_worldmap_bin(f.read()))
    if os.path.isfile(x):
        with open(x, "rb") as f:
            return list(read_worldmap_xml(f.read()))
    return []


# ---------------------------------------------------------------- geometry


def simplify(ring, ox, oy, tol=SIMPLIFY_TILES):
    """Ring in cell tiles -> absolute tiles, dropping points within tol of the
    last kept one. Always keeps the first and last point."""
    n = len(ring) // 2
    if n == 0:
        return []
    out = [ring[0] + ox, ring[1] + oy]
    lx, ly = ring[0], ring[1]
    t2 = tol * tol
    for i in range(1, n):
        x, y = ring[2 * i], ring[2 * i + 1]
        if i == n - 1 or (x - lx) ** 2 + (y - ly) ** 2 >= t2:
            out += [x + ox, y + oy]
            lx, ly = x, y
    return out


def ring_area(ring):
    n = len(ring) // 2
    a = 0
    for i in range(n):
        j = (i + 1) % n
        a += ring[2 * i] * ring[2 * j + 1] - ring[2 * j] * ring[2 * i + 1]
    return abs(a) / 2


def layer_of(props):
    if "water" in props:
        return "water"
    if props.get("highway") in ROAD_KINDS:
        return ROAD_KINDS[props["highway"]]
    if "railway" in props:
        return "rail"
    return None


# ---------------------------------------------------------------- labels


_LABEL = re.compile(r'addUntranslatedText\(\s*"([^"]*)"\s*,\s*"([^"]*)"\s*,\s*(-?[\d.]+)\s*,\s*(-?[\d.]+)')
_SCALE = re.compile(r"setScale\(\s*([\d.]+)")


def read_labels(annotations_text, names):
    """[{name, style, x, y, scale}] in tiles. style is text-town, text-water-..., text-place...
    scale is how big the game draws it (the Ohio River label is 3, ponds under 1)."""
    out = []
    for block in annotations_text.split("symbolsAPI:")[1:]:
        m = _LABEL.match(block)
        if not m:
            continue
        key, style, x, y = m.groups()
        sm = _SCALE.search(block)
        name = names.get(key, key)
        name = re.sub(r"<br\s*/?>", " ", name, flags=re.I).strip()
        if name.isupper():
            name = name.title()
        out.append({"name": name, "style": style, "x": float(x), "y": float(y),
                    "scale": float(sm.group(1)) if sm else 1.0})
    return out


def read_label_names(game_dir):
    p = os.path.join(game_dir, "media", "lua", "shared", "Translate", "EN", "MapLabel.json")
    try:
        with open(p, encoding="utf-8") as f:
            return {k: str(v).title() for k, v in json.load(f).items()}
    except (OSError, ValueError):
        return {}


def read_streets(text):
    """[(name, width, [x0, y0, x1, y1, ...])] in absolute tiles."""
    out = []
    root = ET.fromstring(text)
    for st in root.iter("street"):
        pts = []
        for p in st.iter("point"):
            try:
                pts += [float(p.get("x")), float(p.get("y"))]
            except (TypeError, ValueError):
                pass
        try:
            width = float(st.get("width") or 0)
        except ValueError:
            width = 0
        out.append((st.get("name") or "", width, pts))
    return out


def cells_along(pts, step=16):
    """Cells a polyline passes through, sampled every `step` tiles."""
    cells = set()
    for i in range(0, len(pts) - 2, 2):
        x0, y0, x1, y1 = pts[i], pts[i + 1], pts[i + 2], pts[i + 3]
        n = max(1, int(math.hypot(x1 - x0, y1 - y0) // step))
        for k in range(n + 1):
            t = k / n
            cells.add((int((x0 + (x1 - x0) * t) // CELL), int((y0 + (y1 - y0) * t) // CELL)))
    return cells


# ---------------------------------------------------------------- towns


def town_areas(buildings, labels):
    """Group cells with many buildings into areas and name each from the
    nearest town label. Returns [{name, cells}] and {cell: area index}."""
    dense = {c for c, n in buildings.items() if n >= TOWN_MIN_BUILDINGS}
    towns = [l for l in labels if l["style"] == "text-town"]
    places = [l for l in labels if l["style"].startswith(("text-place", "text-building"))]
    areas, owner = [], {}
    for start in sorted(dense):
        if start in owner:
            continue
        idx = len(areas)
        stack, cells = [start], []
        owner[start] = idx
        while stack:
            cx, cy = stack.pop()
            cells.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (cx + dx, cy + dy)
                    if n in dense and n not in owner:
                        owner[n] = idx
                        stack.append(n)

        def dist(label):
            lx, ly = label["x"] / CELL, label["y"] / CELL
            return min(math.hypot(x + .5 - lx, y + .5 - ly) for x, y in cells)

        name = None
        if towns:
            best = min(towns, key=dist)
            if dist(best) <= 6:
                name = best["name"]
        if not name and places:
            best = min(places, key=dist)
            if dist(best) <= 1.5:
                name = best["name"]
        areas.append({"name": name or "Buildings", "cells": sorted(cells)})
    return areas, owner


def name_water(water_area, labels):
    """{cell: name} for watery cells. Each connected body of water takes names
    only from labels inside it, nearest first, with big labels (the Ohio River)
    reaching further than pond labels."""
    wet = {c for c, a in water_area.items() if a >= 0.02 * CELL * CELL}
    names, seen = {}, set()
    for start in sorted(wet):
        if start in seen:
            continue
        seen.add(start)
        stack, body = [start], []
        while stack:
            cx, cy = stack.pop()
            body.append((cx, cy))
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    n = (cx + dx, cy + dy)
                    if n in wet and n not in seen:
                        seen.add(n)
                        stack.append(n)
        cells = set(body)
        inside = [l for l in labels
                  if any((int(l["x"] // CELL) + dx, int(l["y"] // CELL) + dy) in cells
                         for dx in (-1, 0, 1) for dy in (-1, 0, 1))]
        for c in body:
            if not inside:
                names[c] = "Water"
                continue
            best = min(inside, key=lambda l: math.hypot(c[0] + .5 - l["x"] / CELL,
                                                        c[1] + .5 - l["y"] / CELL) / max(.3, l["scale"]))
            names[c] = best["name"]
    return names


# ---------------------------------------------------------------- vanilla


def vanilla_overlay(game_dir):
    """Everything the app draws for vanilla Kentucky. Missing files just give
    empty layers, so a damaged install still opens the app."""
    base = os.path.join(game_dir, "media", "maps", VANILLA_DIR)
    layers = {"water": [], "highway": [], "road": [], "rail": []}
    buildings, water_area, road_cells = {}, {}, {}
    feats = read_worldmap_dir(base) if os.path.isdir(base) else []
    for cx, cy, gtype, rings, props in feats:
        c = (cx, cy)
        if "building" in props:
            buildings[c] = buildings.get(c, 0) + 1
            continue
        layer = layer_of(props)
        if not layer or layer == "minor" or not rings:
            continue
        ox, oy = cx * CELL, cy * CELL
        if layer == "water" and gtype == "Polygon":
            a = ring_area(rings[0]) - sum(ring_area(r) for r in rings[1:])
            water_area[c] = water_area.get(c, 0) + max(0, a)
        if layer in ("highway", "road"):
            road_cells.setdefault(c, set()).add(layer)
        poly = [simplify(r, ox, oy) for r in rings]
        poly = [r for r in poly if len(r) >= 4]
        if poly:
            layers[layer].append(poly)

    names = read_label_names(game_dir)
    labels = []
    ann = os.path.join(base, "worldmap-annotations.lua")
    if os.path.isfile(ann):
        with open(ann, encoding="utf-8", errors="replace") as f:
            labels = read_labels(f.read(), names)

    major = {}  # cell -> set of road names
    st = os.path.join(base, "streets.xml")
    if os.path.isfile(st):
        with open(st, "rb") as f:
            for name, width, pts in read_streets(f.read()):
                if name and (MAJOR_STREET.search(name) or width >= MAJOR_STREET_WIDTH):
                    for c in cells_along(pts):
                        major.setdefault(c, set()).add(name)

    areas, owner = town_areas(buildings, labels)
    water_names = name_water(water_area, [l for l in labels if l["style"].startswith("text-water")])

    cells = {}
    for c in set(buildings) | set(water_area) | set(road_cells) | set(major):
        info = {}
        if buildings.get(c):
            info["b"] = buildings[c]
        if c in owner:
            info["t"] = owner[c]
        w = water_area.get(c, 0) / (CELL * CELL)
        if w >= 0.02:
            info["w"] = round(min(1.0, w), 2)
            if w >= 0.25:
                info["wn"] = water_names.get(c, "Water")
        if "highway" in road_cells.get(c, ()):
            info["hw"] = 1
        if "road" in road_cells.get(c, ()):
            info["rd"] = 1
        if c in major:
            info["s"] = sorted(major[c])
        if info:
            cells[f"{c[0]},{c[1]}"] = info

    return {
        "layers": layers,
        "labels": labels,
        "towns": [{"name": a["name"], "cells": [list(c) for c in a["cells"]]} for a in areas],
        "cells": cells,
    }


# ---------------------------------------------------------------- mod maps


def group_roads(map_dirs):
    """Road and water shapes of a mod's map folders at their ORIGINAL position.
    Returns {"highway": [...], "road": [...], "minor": [...], "water": [...]}."""
    out = {"highway": [], "road": [], "minor": [], "water": []}
    for path in map_dirs:
        try:
            feats = read_worldmap_dir(path)
        except (WorldMapBinError, ET.ParseError, OSError):
            continue
        for cx, cy, gtype, rings, props in feats:
            layer = layer_of(props)
            if layer not in out:
                continue
            poly = [simplify(r, cx * CELL, cy * CELL) for r in rings]
            poly = [r for r in poly if len(r) >= 4]
            if poly:
                out[layer].append(poly)
    return out
