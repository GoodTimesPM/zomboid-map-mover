"""The whole world as one flat PNG, 1 pixel per square, with each map's name
written over it.

Each map uses the game's own map art (pyramid.zip) when it ships one, and
otherwise its lot files coloured with the app's palette (render.py), the same
two sources the map mover's grid draws from. Maps stack the way the game fills
squares: per square, the first map in the load order that has something on
that square wins, and empty squares show the map below. Vanilla is at the
bottom.

It draws what the game loads, so the moved copies of the last Apply, not the
layout on screen.

Needs Pillow and numpy (pip install pillow numpy). The rest of the app
doesn't.

    python -m mapmover.worldpng [out.png] [--scale N] [--no-labels]
--scale 2 halves each side (nearest pixel), for a smaller image.
"""

import io
import json
import math
import os
import re
import sys
import time
import zipfile

from . import cells
from .lots import CELL
from .paths import DATA_DIR, load_config
from .pyramid import Pyramid, find_pyramid
from .roads import MAP_NAMES as ROADS
from .worldrender import VANILLA_MAP, cached_plan, lot_cells, world_plan

# Road links (they have no place of their own) and RV interiors get no name.
NO_LABEL = set(ROADS) | {"map_distanciado", "map_rv"}
ALPHA = 170   # name opacity, of 255; the outline gets half of it

# Map folders whose map.info title is a placeholder or a test name.
RENAME = {"PIElots": "Indiana (PIE)", "AshenwoodNewB42": "Ashenwood", "Serenitycove_roadtest": "Serenity Cove",
          "muldraughmilitarybaseas24": "Fort Preston", "lvinternationalairport": "LV International Airport",
          "Project Russia": "Project Russia", "Manhattan, NY": "Manhattan, NY"}


def missing_libs():
    """The Python packages this needs that aren't installed."""
    out = []
    for name, pip in (("PIL", "pillow"), ("numpy", "numpy")):
        try:
            __import__(name)
        except ImportError:
            out.append(pip)
    return out


def default_path(cfg):
    if cfg.get("world_png"):
        return os.path.normpath(cfg["world_png"])
    pics = os.path.join(os.path.expanduser("~"), "Pictures")
    return os.path.join(pics if os.path.isdir(pics) else DATA_DIR, "zomboid_world_map.png")


# ------------------------------------------------------------------ names

def mod_name(map_dir):
    """name= from the mod.info of the mod that holds this map folder."""
    d = map_dir
    for _ in range(5):
        d = os.path.dirname(d)
        f = os.path.join(d, "mod.info")
        if os.path.isfile(f):
            try:
                for line in open(f, encoding="utf-8", errors="replace"):
                    if line.lower().startswith("name="):
                        return line[5:].strip()
            except OSError:
                pass
            return None
    return None


def tidy(name):
    """A title without the build tags and move notes mods put in them."""
    name = re.sub(r"\s*\((moved|Build)[^)]*\)|\s*\[B4[^\]]*\]", "", name)
    name = re.sub(r"\s*\bB42( version)?\b|(?<=[a-z])(new)?b?42$", "", name, flags=re.I)
    name = re.sub(r"Spawn(?=,| |$)", "", name).replace(",KY", ", KY")
    return name.strip(" -")


def good_title(t):
    return bool(t) and not t.startswith("See media") and t not in ("MyMap", "maplots")


def labels(plan, cells_of):
    """[(text, cells)]: one per mod, or per group of its folders that touch."""
    by_mod = {}
    for m in plan["maps"]:
        if m["name"] in NO_LABEL or not cells_of.get(m["path"]):
            continue
        by_mod.setdefault(m["mod"], []).append(m)
    out = []
    for ms in by_mod.values():
        groups = []   # [[maps], cells]
        for m in ms:
            c = cells_of[m["path"]]
            near = {(x + dx, y + dy) for x, y in c for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
            g = [[m], set(c)]
            for h in [g2 for g2 in groups if g2[1] & near]:
                g[0] += h[0]
                g[1] |= h[1]
                groups.remove(h)
            groups.append(g)
        for maps, cs in groups:
            maps.sort(key=lambda m: -len(cells_of[m["path"]]))
            names = [RENAME[m["name"]] for m in maps if m["name"] in RENAME]
            names = names or [m["title"] for m in maps if good_title(m["title"])]
            names = list(dict.fromkeys(tidy(t) for t in names))
            names = names or [tidy(mod_name(maps[0]["path"]) or maps[0]["name"])]
            out.append((" / ".join(names), cs))
    return out


def _font(size):
    from PIL import ImageFont
    path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "arial.ttf")
    for p in (path, "DejaVuSans.ttf", "Arial.ttf"):
        try:
            return ImageFont.truetype(p, size)
        except OSError:
            pass
    return ImageFont.load_default(size)


def draw_labels(img, found, x0, y0, scale):
    """Each name over the middle of its map, nudged off any name already
    written. Big maps go first and keep the middle. Thin and see-through, so
    the map shows under it."""
    from PIL import Image, ImageDraw
    d = ImageDraw.Draw(img)
    placed = []

    def free(b):
        return not any(b[0] < p[2] and p[0] < b[2] and b[1] < p[3] and p[1] < b[3] for p in placed)

    steps = [(0, 0), (0, -1), (0, 1), (0, -2), (0, 2), (-1, 0), (1, 0), (0, -3), (0, 3),
             (-1, -1), (1, -1), (-1, 1), (1, 1)]   # middle, then up and down, then sideways
    for text, cs in sorted(found, key=lambda f: -len(f[1])):
        n = len(cs)
        cx = (sum(x for x, _ in cs) / n - x0 + 0.5) * CELL / scale
        cy = (sum(y for _, y in cs) / n - y0 + 0.5) * CELL / scale
        size = max(8, int(min(180, max(48, 30 * math.sqrt(n))) / scale))
        spot = None
        while spot is None and size >= 28 / scale:
            font = _font(size)
            stroke = max(1, size // 16)
            b = d.textbbox((0, 0), text, font=font, anchor="mm", stroke_width=stroke)
            h = b[3] - b[1]
            for ox, oy in steps:
                x, y = cx + ox * (b[2] - b[0]) * 0.6, cy + oy * h * 1.05
                box = (x + b[0], y + b[1], x + b[2], y + b[3])
                if free(box):
                    spot = (x, y, box)
                    break
            else:
                size = int(size * 0.8)
        if spot is None:   # nowhere clear: write it in the middle anyway
            size = max(size, 8)
            font = _font(size)
            stroke = max(1, size // 16)
            spot = (cx, cy, d.textbbox((cx, cy), text, font=font, anchor="mm", stroke_width=stroke))
        placed.append(spot[2])
        # Draw on a small see-through patch and blend it in; a whole-image
        # overlay would double the memory.
        bx0, by0, bx1, by1 = (int(v) for v in spot[2])
        bx0, by0 = max(bx0 - 2, 0), max(by0 - 2, 0)
        bx1, by1 = min(bx1 + 2, img.width), min(by1 + 2, img.height)
        if bx1 <= bx0 or by1 <= by0:
            continue
        patch = Image.new("RGBA", (bx1 - bx0, by1 - by0), (0, 0, 0, 0))
        pd = ImageDraw.Draw(patch)
        pos = (spot[0] - bx0, spot[1] - by0)
        pd.text(pos, text, font=font, anchor="mm", fill=(0, 0, 0, ALPHA // 2),
                stroke_width=stroke, stroke_fill=(0, 0, 0, ALPHA // 2))
        pd.text(pos, text, font=font, anchor="mm", fill=(255, 255, 255, ALPHA))
        region = img.crop((bx0, by0, bx1, by1)).convert("RGBA")
        region.alpha_composite(patch)
        img.paste(region.convert("RGB"), (bx0, by0))


# ------------------------------------------------------------------ drawing

def cell_job(map_dir, x, y, want_rgba):
    """(mask bytes or None, rgba bytes or None) for one cell. Runs in a worker
    process, and fills the app's cell caches on the way."""
    from PIL import Image
    if cells.caching() and not cells._done(map_dir, x, y, want_rgba):
        cells.build(map_dir, x, y, want_rgba)
    cl = cells.load(map_dir, x, y)
    if not any(cl.codes):
        return None, None
    mask = bytes(1 if c else 0 for c in cl.codes)
    rgba = None
    if want_rgba:
        st = cells.stamp(map_dir, x, y)
        png = None
        if st and cells.caching():
            try:
                with open(cells.tile_path(map_dir, x, y, 0, st), "rb") as f:
                    png = f.read()
            except OSError:
                pass
        if png is None:
            from .lots import LotPack, read_header
            from .render import render_squares
            hp, pp = cells._files(map_dir, x, y)
            with open(hp, "rb") as f:
                h = read_header(f.read())
            with open(pp, "rb") as f:
                squares = LotPack(f.read(), h).level0()
            return mask, bytes(render_squares(h, squares, cells.rooms_of(h)))
        rgba = Image.open(io.BytesIO(png)).convert("RGBA").tobytes()
    return mask, rgba


class Art:
    """Level 0 of a pyramid.zip, sampled per world square."""

    def __init__(self, path):
        import numpy as np
        self.np = np
        p = Pyramid(path)
        self.z = zipfile.ZipFile(path)
        self.b, self.img = p.bounds, p.image
        self.tpp = (self.b[2] - self.b[0]) / self.img[0]
        self.tiles = {(x, y) for z, x, y in p.tiles if z == 0}
        self.cache = {}

    def tile(self, tx, ty):
        from PIL import Image
        k = (tx, ty)
        if k not in self.cache:
            if len(self.cache) > 400:
                self.cache.clear()
            a = None
            if k in self.tiles:
                im = Image.open(io.BytesIO(self.z.read(f"0/tile{tx}x{ty}.png"))).convert("RGBA")
                a = self.np.zeros((256, 256, 4), self.np.uint8)
                a[:im.height, :im.width] = self.np.asarray(im)
            self.cache[k] = a
        return self.cache[k]

    def cell(self, cx, cy):
        """CELL x CELL x 4 for world cell (cx, cy); alpha 0 outside the art."""
        np = self.np
        wx = cx * CELL + np.arange(CELL)
        wy = cy * CELL + np.arange(CELL)
        ix = np.floor((wx - self.b[0]) / self.tpp).astype(int)
        iy = np.floor((wy - self.b[1]) / self.tpp).astype(int)
        okx = (wx >= self.b[0]) & (wx < self.b[2])
        oky = (wy >= self.b[1]) & (wy < self.b[3])
        out = np.zeros((CELL, CELL, 4), np.uint8)
        if not okx.any() or not oky.any():
            return out
        for ty in np.unique(iy[oky] // 256):
            for tx in np.unique(ix[okx] // 256):
                t = self.tile(tx, ty)
                if t is None:
                    continue
                ry = oky & (iy // 256 == ty)
                rx = okx & (ix // 256 == tx)
                out[np.ix_(ry, rx)] = t[np.ix_(iy[ry] % 256, ix[rx] % 256)]
        return out


def _save(img, path, log):
    """Save, or beside it under a new name when the file is open elsewhere
    (an image viewer holds it locked)."""
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    tmp = path + ".tmp.png"
    img.save(tmp, compress_level=6)
    try:
        os.replace(tmp, path)
        return path
    except OSError:
        root, ext = os.path.splitext(path)
        alt = f"{root}_{time.strftime('%Y%m%d-%H%M%S')}{ext}"
        os.replace(tmp, alt)
        log(f"{os.path.basename(path)} is open in another program, so this one is {os.path.basename(alt)}.")
        return alt


def export(cfg, out_path, scale=1, names=True, log=print, progress=None, stop=None):
    """Draw the world into out_path. Returns {"path", "w", "h", "names", "mb"}.
    progress(done, total) is called as cells are drawn."""
    import numpy as np
    from PIL import Image
    Image.MAX_IMAGE_PIXELS = None
    t0 = time.time()
    plan = cached_plan(cfg)
    if plan is None:
        log("Reading the mod list and every active map mod, this takes a minute or two...")
        plan = world_plan(cfg)
    van_dir = os.path.join(cfg["game_dir"], "media", "maps", VANILLA_MAP)
    # Bottom first: vanilla, then the maps from last loaded to first. The road
    # links mod's Lua puts its folders above every other map.
    stack = [("vanilla", van_dir)] + [(m["name"], m["path"]) for m in reversed(plan["maps"])]
    stack = [s for s in stack if s[0] not in ROADS] + [s for s in stack if s[0] in ROADS]
    cells_of = {d: {c for c in lot_cells(d) if c[0] >= 0} for _, d in stack}   # west of 0 can't load
    allc = set().union(*cells_of.values())
    if not allc:
        raise RuntimeError("no map cells found, is the game folder right in config.json?")
    x0, x1 = min(c[0] for c in allc), max(c[0] for c in allc) + 1
    y0, y1 = min(c[1] for c in allc), max(c[1] for c in allc) + 1
    W, H = (x1 - x0) * CELL // scale, (y1 - y0) * CELL // scale
    total = sum(len(c) for c in cells_of.values())
    log(f"{len(stack)} maps, {len(allc)} cells: {W} x {H} pixels.")
    canvas = np.zeros((H, W, 3), np.uint8)
    s = CELL // scale
    done = 0
    pool = cells.pool()
    for name, d in stack:
        art_path = find_pyramid(d)
        art = Art(art_path) if art_path else None
        todo = sorted(cells_of[d])
        futs = [pool.submit(cell_job, d, x, y, art is None) for x, y in todo]
        n = 0
        for (x, y), fut in zip(todo, futs):
            if stop is not None and stop.is_set():
                for f in futs:
                    f.cancel()
                raise RuntimeError("stopped")
            mask, rgba = fut.result()
            done += 1
            if progress and done % 20 == 0:
                progress(done, total)
            if not mask:
                continue
            m = np.frombuffer(mask, np.uint8).reshape(CELL, CELL).astype(bool)
            px = np.zeros((CELL, CELL, 4), np.uint8)
            if art is not None:
                px = art.cell(x, y)
                hole = m & (px[..., 3] == 0)
                if hole.any():   # the art doesn't cover a square the lots have
                    _, r = cell_job(d, x, y, True)
                    if r:
                        pal = np.frombuffer(r, np.uint8).reshape(CELL, CELL, 4)
                        px[hole] = pal[hole]
            elif rgba:
                px = np.frombuffer(rgba, np.uint8).reshape(CELL, CELL, 4)
            m &= px[..., 3] > 0
            if scale > 1:
                m, px = m[::scale, ::scale], px[::scale, ::scale]
            ox, oy = (x - x0) * s, (y - y0) * s
            region = canvas[oy:oy + s, ox:ox + s]
            region[m] = px[..., :3][m]
            n += 1
        log(f"{time.time() - t0:5.0f} s  {name}: {n} cells{' (map art)' if art else ''}")
    if progress:
        progress(total, total)
    img = Image.fromarray(canvas)
    del canvas
    found = []
    if names:
        found = labels(plan, cells_of)
        draw_labels(img, found, x0, y0, scale)
        log(f"Wrote {len(found)} map names.")
    log("Saving the PNG...")
    path = _save(img, out_path, log)
    with open(os.path.splitext(path)[0] + ".json", "w") as f:
        json.dump({"cell_x0": x0, "cell_y0": y0, "px_per_square": 1 / scale, "w": W, "h": H}, f)
    mb = os.path.getsize(path) / 1e6
    log(f"Done in {time.time() - t0:.0f} s, {mb:.0f} MB: {path}")
    return {"path": path, "w": W, "h": H, "names": len(found), "mb": round(mb)}


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    scale = 1
    if "--scale" in argv:
        i = argv.index("--scale")
        scale = int(argv[i + 1])
        del argv[i:i + 2]
    names = "--no-labels" not in argv
    args = [a for a in argv if not a.startswith("--")]
    miss = missing_libs()
    if miss:
        sys.exit("Needs " + " and ".join(miss) + ": pip install " + " ".join(miss))
    cfg = load_config()
    export(cfg, args[0] if args else default_path(cfg), scale=scale, names=names)


if __name__ == "__main__":
    main()
