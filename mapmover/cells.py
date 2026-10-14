"""Read a cell's lot files once for everything the app needs from it: square
classes for road routing, and terrain image tiles for maps that ship no
pyramid.zip.

Both are cached on disk under data/, named by the lotpack's size and time,
so a map update reads afresh. Cells that aren't cached yet are read in
worker processes, so a big map uses every core instead of one. After a scan,
warm() reads every map's cells in the background, so most are ready before
anyone looks at them.

MAPMOVER_CELL_CACHE=0 turns the disk cache and the workers off (tests use it).
"""

import hashlib
import json
import os
import struct
import threading
import zlib
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, wait

from .lots import CELL, LotError, LotPack, read_header
from .paths import DATA_DIR

EMPTY, OPEN, ROAD, PAVED, TREE, WATER, FENCE, BLOCKED = range(8)
NAMES = ["empty", "open", "road", "paved", "tree", "water", "fence", "blocked"]

CLASS_VERSION = "c1"   # bump when classify() changes, so old caches are left alone
_MAGIC = b"MMC1"


def classify(names, in_room):
    """Class of one level 0 square from its tile names, floor first."""
    if in_room:
        return BLOCKED
    floor = names[0]
    fence = tree = False
    for n in names[1:]:
        if n.startswith("walls_") or n.startswith("fixtures_doors") or n.startswith("fixtures_windows"):
            return BLOCKED
        if n.startswith("fencing_"):
            fence = True
        elif "tree" in n:
            tree = True
    if floor.startswith("blends_street"):
        return ROAD
    if fence:
        return FENCE
    if floor.startswith("blends_natural_02") or "water" in floor:
        return WATER
    if floor.startswith("floors_exterior_street") or floor.startswith("floors_exterior_tiles"):
        return PAVED
    if floor.startswith("floors_") or floor.startswith("carpentry_"):
        return BLOCKED  # interior or built floor outside a room: a porch, a deck
    return TREE if tree else OPEN


class Classes:
    """One cell from one map dir: classes, road tile names, zombie bytes."""

    def __init__(self, codes=None, road=None, zombies=None):
        self.codes = codes if codes is not None else bytearray(CELL * CELL)
        self.road = road if road is not None else {}  # square index -> tile name, for ROAD squares
        self.zombies = zombies


def rooms_of(h):
    rooms = set()
    for room in h.rooms:
        if room["level"] == 0:
            for rx, ry, rw, rh in room["rects"]:
                rooms.update((sx, sy) for sx in range(rx, rx + rw) for sy in range(ry, ry + rh))
    return rooms


def classes_from(h, squares, rooms):
    out = Classes(zombies=h.zombies)
    codes, road = out.codes, out.road
    tiles_of = h.tiles
    n = len(tiles_of)
    seen = {}  # the same stack of tiles repeats all over a cell, so class it once
    for (sx, sy), tiles in squares.items():
        i = sy * CELL + sx
        if (sx, sy) in rooms:
            codes[i] = BLOCKED
            continue
        k = tuple(tiles)
        c = seen.get(k)
        if c is None:
            c = seen[k] = classify([tiles_of[t] if 0 <= t < n else "" for t in tiles], False)
        codes[i] = c
        if c == ROAD:
            road[i] = tiles_of[tiles[0]] if 0 <= tiles[0] < n else ""
    return out


def _pack(cl):
    names = sorted(set(cl.road.values()))
    idx = {t: i for i, t in enumerate(names)}
    road = zlib.compress(json.dumps([names, [[i, idx[t]] for i, t in cl.road.items()]]).encode(), 3)
    codes = zlib.compress(bytes(cl.codes), 3)
    z = cl.zombies or b""
    return _MAGIC + struct.pack("<III", len(z), len(codes), len(road)) + z + codes + road


def _unpack(data):
    if data[:4] != _MAGIC:
        raise ValueError("not a class cache")
    nz, nc, nr = struct.unpack_from("<III", data, 4)
    p = 16
    z = data[p:p + nz] or None
    p += nz
    codes = bytearray(zlib.decompress(data[p:p + nc]))
    p += nc
    names, pairs = json.loads(zlib.decompress(data[p:p + nr]))
    if len(codes) != CELL * CELL:
        raise ValueError("bad class cache")
    return Classes(codes, {i: names[t] for i, t in pairs}, z)


# ------------------------------------------------------------------ cache paths

def caching():
    return os.environ.get("MAPMOVER_CELL_CACHE", "1") != "0"


def _files(map_dir, x, y):
    return (os.path.join(map_dir, f"{x}_{y}.lotheader"),
            os.path.join(map_dir, f"world_{x}_{y}.lotpack"))


def stamp(map_dir, x, y):
    """The lotpack's size and time, or None when the cell's files are missing."""
    hp, pp = _files(map_dir, x, y)
    try:
        st = os.stat(pp)
        os.stat(hp)
    except OSError:
        return None
    return f"{st.st_size:x}_{st.st_mtime_ns:x}"


def _sub(root, map_dir):
    return os.path.join(root, hashlib.sha1(os.path.abspath(map_dir).lower().encode()).hexdigest()[:12])


def class_path(map_dir, x, y, st):
    return os.path.join(_sub(os.path.join(DATA_DIR, "cells", CLASS_VERSION), map_dir), f"{x}_{y}_{st}.bin")


_render_root = None


def tile_path(map_dir, x, y, z, st):
    global _render_root
    if _render_root is None:
        from .render import palette
        pal = json.dumps(palette(), sort_keys=True).encode()
        _render_root = os.path.join(DATA_DIR, "render", hashlib.sha1(pal).hexdigest()[:10])
    return os.path.join(_sub(_render_root, map_dir), f"{x}_{y}_{z}_{st}.png")


def _write(path, data):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except OSError:
        pass


# ------------------------------------------------------------------ building

def build(map_dir, x, y, tiles=False):
    """Read one cell, cache its classes, and its terrain tiles too when asked.
    Runs in a worker process. Returns Classes (empty when the cell can't be
    read, so a broken cell reads as nothing there)."""
    from .render import SIZES, png_rgba, render_squares, shrink
    st = stamp(map_dir, x, y)
    hp, pp = _files(map_dir, x, y)
    try:
        with open(hp, "rb") as f:
            h = read_header(f.read())
        with open(pp, "rb") as f:
            squares = LotPack(f.read(), h).level0()
    except (OSError, LotError) as e:
        print(f"Couldn't read cell {x},{y} of {map_dir}: {e}")
        return Classes()
    rooms = rooms_of(h)
    cl = classes_from(h, squares, rooms)
    if st and caching():
        _write(class_path(map_dir, x, y, st), _pack(cl))
        if tiles:
            rgba = render_squares(h, squares, rooms)
            for z, size in SIZES.items():
                _write(tile_path(map_dir, x, y, z, st),
                       png_rgba(size, size, rgba if size == CELL else shrink(rgba, size)))
    return cl


def _cached(map_dir, x, y):
    if not caching():
        return None
    st = stamp(map_dir, x, y)
    if not st:
        return Classes()
    try:
        with open(class_path(map_dir, x, y, st), "rb") as f:
            return _unpack(f.read())
    except (OSError, ValueError, zlib.error, struct.error):
        return None


_pool = None
_pool_lock = threading.Lock()


def workers():
    return max(1, min(32, (os.cpu_count() or 2) - 2))


def pool():
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ProcessPoolExecutor(max_workers=workers())
        return _pool


def load(map_dir, x, y):
    """Classes of one cell, from the cache or read now."""
    cl = _cached(map_dir, x, y)
    return cl if cl is not None else build(map_dir, x, y)


def load_many(keys):
    """{(map_dir, x, y): Classes}. Cells not cached yet are read in the
    worker processes, all at once."""
    out, todo = {}, []
    for k in keys:
        cl = _cached(*k)
        if cl is None:
            todo.append(k)
        else:
            out[k] = cl
    if len(todo) > 2 and caching():
        futs = {pool().submit(build, *k): k for k in todo}
        for f, k in futs.items():
            try:
                out[k] = f.result()
            except Exception as e:   # a dead worker: read it here instead
                print(f"Worker failed on cell {k[1]},{k[2]}: {e}")
                out[k] = build(*k)
    else:
        for k in todo:
            out[k] = build(*k)
    return out


def tile(map_dir, x, y, z):
    """PNG of one cell's terrain, drawn from its lot files, or None."""
    from .render import SIZES, png_rgba, render_squares, shrink
    if z not in SIZES:
        return None
    st = stamp(map_dir, x, y)
    if not st:
        return None
    path = tile_path(map_dir, x, y, z, st)
    try:
        with open(path, "rb") as f:
            return f.read()
    except OSError:
        pass
    if caching():
        try:
            pool().submit(build, map_dir, x, y, True).result()
            with open(path, "rb") as f:
                return f.read()
        except Exception:
            pass
    # No cache or no workers: draw it here.
    hp, pp = _files(map_dir, x, y)
    with open(hp, "rb") as f:
        h = read_header(f.read())
    with open(pp, "rb") as f:
        squares = LotPack(f.read(), h).level0()
    rgba = render_squares(h, squares, rooms_of(h))
    size = SIZES[z]
    return png_rgba(size, size, rgba if size == CELL else shrink(rgba, size))


def _done(map_dir, x, y, tiles):
    st = stamp(map_dir, x, y)
    if not st:
        return True
    if not os.path.isfile(class_path(map_dir, x, y, st)):
        return False
    return not tiles or os.path.isfile(tile_path(map_dir, x, y, 0, st))


_warm_gen = 0


def warm(jobs, log=print):
    """Read [(map_dir, x, y, tiles)] into the cache in the background, a few
    at a time so cells someone is waiting on don't queue behind them. A new
    call stops the one before."""
    global _warm_gen
    if not caching():
        return
    _warm_gen += 1
    threading.Thread(target=_warm, args=(list(jobs), _warm_gen, log), daemon=True).start()


def _warm(jobs, gen, log):
    todo = [j for j in jobs if not _done(*j)]
    if not todo:
        return
    log(f"Reading {len(todo)} map cells in the background, this happens once.")
    inflight = set()
    p = pool()
    n = workers()
    for j in todo:
        if gen != _warm_gen:
            return
        while len(inflight) >= n:
            _, inflight = wait(inflight, return_when=FIRST_COMPLETED)
        try:
            inflight.add(p.submit(build, *j))
        except RuntimeError:   # the pool is shutting down
            return
    wait(inflight)
    if gen == _warm_gen:
        log(f"Done reading {len(todo)} map cells.")
