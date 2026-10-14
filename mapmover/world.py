"""What the world looks like square by square once a layout is applied.

The game fills each square from the highest-priority map that has something
there, then lower maps, then world generation (CellLoader.LoadCellBinaryChunk).
WorldSampler copies that: each world cell has a list of sources (map dirs at
their planned offsets, higher layer first, then mod-list order, vanilla last) and a square's class
comes from the first source that isn't empty there.

Classes are small ints, see the constants in cells.py. Moved maps are read from
their original files, shifted, so nothing has to be built first.
"""

import threading
from collections import OrderedDict

from . import cells
from .cells import (BLOCKED, EMPTY, FENCE, NAMES, OPEN, PAVED, ROAD, TREE, WATER,  # noqa: F401
                    classify)
from .layout import VANILLA_MAP, in_vanilla_world, plan_for
from .lots import CELL

# Read cells are shared by every sampler: the same few hundred cells get read
# for each preview, route and Apply. Cleared on rescan. Behind this sits the
# disk cache in cells.py, which survives restarts.
_PARTS = OrderedDict()
_PARTS_LOCK = threading.Lock()
_PARTS_MAX = 3000   # about 200 MB


def clear_cache():
    with _PARTS_LOCK:
        _PARTS.clear()


def _keep(key, part):
    with _PARTS_LOCK:
        _PARTS[key] = part
        while len(_PARTS) > _PARTS_MAX:
            _PARTS.popitem(last=False)


def _shared_part(map_dir, x, y):
    key = (map_dir, x, y)
    with _PARTS_LOCK:
        part = _PARTS.get(key)
        if part is not None:
            _PARTS.move_to_end(key)
            return part
    part = cells.load(map_dir, x, y)
    _keep(key, part)
    return part


def preload_parts(keys):
    """Read many (map_dir, x, y) cells at once, in the worker processes."""
    with _PARTS_LOCK:
        todo = list(dict.fromkeys(k for k in keys if k not in _PARTS))
    if todo:
        for k, part in cells.load_many(todo).items():
            _keep(k, part)


class WorldCell:
    """A world cell after priority: merged classes and who owns it."""

    def __init__(self, owner, parts):
        self.owner = owner            # group key, "vanilla", or None (nobody: world generation)
        self.codes = bytearray(CELL * CELL)
        self.road = {}
        self.zombies = None
        for p in parts:               # highest priority first
            if self.zombies is None and p.zombies is not None:
                self.zombies = p.zombies
        if not parts:
            return
        if len(parts) == 1:
            self.codes[:] = parts[0].codes
            self.road = dict(parts[0].road)
            return
        for i in range(CELL * CELL):
            for p in parts:
                c = p.codes[i]
                if c != EMPTY:
                    self.codes[i] = c
                    if c == ROAD:
                        self.road[i] = p.road[i]
                    break


class WorldSampler:
    """Square classes for the whole Kentucky world under a layout."""

    def __init__(self, state, plans, vanilla_dir, only=None):
        """only: a group key, to leave every other group out."""
        self.sources = {}   # (cx, cy) -> [(owner, map_dir, file_x, file_y)] by priority
        self.group_cells = {}  # group key -> set of world cells at the planned spot
        self.no_go = set()     # cells no road may enter (loadorder.road_no_go)
        self._cells = {}
        self._lock = threading.Lock()
        order = {name: i for i, name in enumerate(state.get("active_maps") or [])}
        entries = []
        for g in state["groups"]:
            p = plan_for(g, plans)
            if only is not None and g.key != only:
                continue
            if not in_vanilla_world(g, p) and g.key != only:
                continue
            dx, dy = p["offset"]
            cells = self.group_cells.setdefault(g.key, set())
            for m in g.originals.values():
                for d in m.maps:
                    if d.name == VANILLA_MAP:
                        continue
                    for (x, y) in d.cells:
                        cells.add((x + dx, y + dy))
                        entries.append(((-p["layer"], order.get(d.name, len(order))), (x + dx, y + dy),
                                        (g.key, d.path, x, y)))
        entries.sort(key=lambda e: e[0])
        for _, wc, src in entries:
            self.sources.setdefault(wc, []).append(src)
        if vanilla_dir:
            for (x, y) in state.get("vanilla_cells") or ():
                self.sources.setdefault((x, y), []).append(("vanilla", vanilla_dir, x, y))

    def preload(self, cell_list):
        """Read these world cells' sources now, all at once, so a scan over a
        big map doesn't read them one by one."""
        keys = []
        with self._lock:
            for c in cell_list:
                if c not in self._cells:
                    keys += [(d, x, y) for _, d, x, y in self.sources.get(c, [])]
        preload_parts(keys)

    def owner(self, cx, cy):
        src = self.sources.get((cx, cy))
        return src[0][0] if src else None

    def cell(self, cx, cy):
        with self._lock:
            wc = self._cells.get((cx, cy))
            if wc is None:
                src = self.sources.get((cx, cy), [])
                parts = [_shared_part(d, x, y) for _, d, x, y in src]
                wc = self._cells[(cx, cy)] = WorldCell(src[0][0] if src else None, parts)
            return wc

    def code(self, wx, wy):
        cx, cy = wx // CELL, wy // CELL
        return self.cell(cx, cy).codes[(wy - cy * CELL) * CELL + (wx - cx * CELL)]

    def road_tile(self, wx, wy):
        cx, cy = wx // CELL, wy // CELL
        return self.cell(cx, cy).road.get((wy - cy * CELL) * CELL + (wx - cx * CELL))

    def group_only(self, key):
        """A sampler that sees only one group's own squares (for its exits)."""
        s = WorldSampler.__new__(WorldSampler)
        s.sources = {c: [x for x in v if x[0] == key] for c, v in self.sources.items()
                     if any(x[0] == key for x in v)}
        s.group_cells = {key: self.group_cells.get(key, set())}
        s.no_go = set()
        s._cells, s._lock = {}, threading.Lock()
        return s


def home_sampler(state, key, vanilla_dir):
    """One group at its original spot, over vanilla if it was built into
    Kentucky, on its own otherwise."""
    g = next(g for g in state["groups"] if g.key == key)
    return WorldSampler(state, {key: {"offset": [0, 0], "join": True}},
                        vanilla_dir if g.kentucky else None, only=key)
