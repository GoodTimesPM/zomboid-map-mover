"""Group active map mods into blocks that must move together, and track offsets.

Two mods land in the same group when:
  * they ship a map folder with the same name (the game uses only the last
    loaded mod's copy of a folder, so a patch and its base must stay aligned), or
  * one map folder lists another in map.info "lots=" (spawn folders and
    multi-part worlds point at the folder holding the cells).
The vanilla world, "Muldraugh, KY", never joins a group and never moves.

A group's key is the sorted list of ORIGINAL mod ids, so it stays the same
after a move (a moved copy records its original id in mapmover.json).
"""

import json
import os

from .paths import DATA_DIR
from .scan import read_modlist, scan_mods, scan_vanilla

VANILLA_MAP = "Muldraugh, KY"
MOVED_SUFFIX = "_moved"


class Group:
    def __init__(self, key):
        self.key = key
        self.mods = []        # active Mod objects (copies when moved)
        self.originals = {}   # original id -> original Mod (workshop or local)
        self.offset = (0, 0)  # offset the active copies were built with
        self.joined = False   # copies had lots=Muldraugh, KY added
        self.kentucky = False # the original already loads with the vanilla world
        self.mixed = False    # copies disagree on offset (needs a rebuild)
        self.companions = []  # active non-map mods that require a member
        self.cells = set()    # cells at the ORIGINAL position
        self.map_names = set()
        self.added = False    # not active yet, Apply turns it on
        self.active = False   # in default.txt now
        self.off = False      # switched off: Apply takes it out of default.txt
        self.notes = []       # warnings saved when the active copies were built

    def title(self):
        names = sorted({m.name for m in self.originals.values()})
        return " + ".join(names)

    def to_json(self):
        return {
            "key": self.key,
            "title": self.title(),
            "mods": sorted(self.originals),
            "maps": sorted(self.map_names),
            "offset": list(self.offset),
            "joined": self.joined,
            "kentucky": self.kentucky,
            "mixed": self.mixed,
            "companions": sorted(original_id(m) for m in self.companions),
            "cells": sorted(self.cells),
            "added": self.added,
            "active": self.active,
            "off": self.off,
            "notes": self.notes,
        }


def original_id(mod):
    if mod.manifest and mod.manifest.get("original_id"):
        return mod.manifest["original_id"]
    return mod.id


def build_state(cfg, extra_ids=(), off_keys=()):
    """extra_ids: installed map mods the user wants to add. They are grouped
    as if active so they can be placed before Apply turns them on.

    off_keys: group keys switched off. Those groups go in "off_groups", not
    "groups", and their mods (with companions only they need) and map
    folders leave "active_ids" and "active_maps". Everything downstream
    (Apply, the crash check, road links) then sees the world without them.
    A switched-off group that Apply already took out of default.txt is
    still grouped from its original mods, so it stays listed."""
    off_keys = set(off_keys)
    # The road links mod is ours and rebuilt on every Apply, never a map to move.
    mods = [m for m in scan_mods(cfg) if not (m.manifest and m.manifest.get("kind") == "roads")]
    vanilla = scan_vanilla(cfg)
    active_ids, active_maps = read_modlist(os.path.join(cfg["mods_dir"], "default.txt"))
    active_set = set(active_ids)

    by_id = {}
    for m in mods:
        # Search order is workshop first, then local, like the game.
        by_id.setdefault(m.id, m)
    originals = {m.id: m for m in mods if not m.manifest}

    def is_map_mod(m):
        return any(d.name != VANILLA_MAP for d in m.maps)

    active_map_mods = [by_id[i] for i in active_ids if i in by_id and is_map_mod(by_id[i])]
    present = {original_id(m) for m in active_map_mods}
    active_orig = set(present)
    added = []
    off_ids = [i for k in off_keys for i in k.split("|")]
    for i in list(extra_ids) + off_ids:
        m = originals.get(i)
        if m and is_map_mod(m) and i not in present:
            active_map_mods.append(m)
            present.add(i)
            if i in extra_ids:
                added.append(i)

    # Union-find over map folder names.
    parent = {}

    def find(a):
        parent.setdefault(a, a)
        while parent[a] != a:
            parent[a] = parent[parent[a]]
            a = parent[a]
        return a

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[ra] = rb

    for m in active_map_mods:
        names = [d.name for d in m.maps if d.name != VANILLA_MAP]
        for n in names:
            find("dir:" + n)
            union("dir:" + n, "mod:" + original_id(m))
        for d in m.maps:
            if d.name == VANILLA_MAP:
                continue
            for lot in d.lots:
                if lot != VANILLA_MAP:
                    union("dir:" + d.name, "dir:" + lot)

    groups = {}
    for m in active_map_mods:
        root = find("mod:" + original_id(m))
        groups.setdefault(root, []).append(m)

    out = []
    for members in groups.values():
        key = "|".join(sorted(original_id(m) for m in members))
        g = Group(key)
        g.mods = members
        offsets = set()
        for m in members:
            oid = original_id(m)
            orig = originals.get(oid, m)
            g.originals[oid] = orig
            off = tuple(m.manifest.get("offset", (0, 0))) if m.manifest else (0, 0)
            offsets.add(off)
            if m.manifest and m.manifest.get("joined"):
                g.joined = True
            if m.manifest and tuple(m.manifest.get("offset", (0, 0))) != (0, 0):
                g.notes += m.manifest.get("warnings", []) +                     ["dropped " + d for d in m.manifest.get("dropped", [])]
            # Judge "already in the vanilla world" from the original's map.info.
            for d in orig.maps:
                if VANILLA_MAP in d.lots:
                    g.kentucky = True
            for d in m.maps:
                if d.name == VANILLA_MAP:
                    continue
                g.map_names.add(d.name)
                for (x, y) in d.cells:
                    g.cells.add((x - off[0], y - off[1]))
        if len(offsets) > 1:
            g.mixed = True
        g.offset = sorted(offsets)[0] if offsets else (0, 0)
        if g.cells:
            out.append(g)
        else:
            # A group with no cells (spawn points into vanilla only) never moves.
            pass
    out.sort(key=lambda g: g.title().lower())
    for g in out:
        g.added = all(oid in added for oid in g.originals)
        g.active = any(oid in active_orig for oid in g.originals)
        g.off = g.key in off_keys

    vanilla_cells = set()
    for d in vanilla:
        vanilla_cells.update(d.cells)

    # Active mods that are not map mods but require one. If the map moved
    # and they didn't, their require= would pull the original back in, so
    # they are copied along with the group (no files shifted).
    group_of = {oid: g for g in out for oid in g.originals}
    for i in active_ids:
        m = by_id.get(i)
        if not m or is_map_mod(m):
            continue
        for r in m.require:
            r = r.strip()
            base = r[:-len(MOVED_SUFFIX)] if r.endswith(MOVED_SUFFIX) else r
            g = group_of.get(base)
            if g and m not in g.companions:
                g.companions.append(m)

    inactive = []
    group_ids = {oid for g in out for oid in g.originals}
    for m in originals.values():
        if m.id in group_ids or not is_map_mod(m):
            continue
        cells = sum(len(d.cells) for d in m.maps if d.name != VANILLA_MAP)
        if cells:
            inactive.append({"id": m.id, "name": m.name, "cells": cells,
                             "workshop_id": m.workshop_id,
                             "maps": [d.name for d in m.maps if d.name != VANILLA_MAP]})
    inactive.sort(key=lambda x: x["name"].lower())

    off = [g for g in out if g.off]
    out = [g for g in out if not g.off]
    needed = {id(m) for g in out for m in g.companions}
    gone = {oid for g in off for oid in g.originals}
    gone |= {original_id(m) for g in off for m in g.companions if id(m) not in needed}
    off_maps = sorted({n for g in off for n in g.map_names})
    listed_ids = list(active_ids)
    active_ids = [i for i in active_ids if (original_id(by_id[i]) if i in by_id else i) not in gone]
    active_maps = [n for n in active_maps if n not in off_maps]

    return {
        "mods": mods,
        "by_id": by_id,
        "originals": originals,
        "groups": out,
        "off_groups": off,
        "off_ids": sorted(gone),
        "off_maps": off_maps,
        "vanilla_cells": vanilla_cells,
        "active_ids": active_ids,
        "listed_ids": listed_ids,  # default.txt as it is, off maps included
        "active_maps": active_maps,
        "inactive": inactive,
        "added": added,
    }


# ---------------------------------------------------------------- saved layout


LAYOUT_PATH = os.path.join(DATA_DIR, "layout.json")


def load_layout():
    try:
        with open(LAYOUT_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {"plans": {}, "add_ids": []}


def save_layout(layout):
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = LAYOUT_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(layout, f, indent=2, sort_keys=True)
    os.replace(tmp, LAYOUT_PATH)


# The game's world generator (WorldGenParams) covers cells -250..250.
MIN_CELL = -250
MAX_CELL = 249


def in_vanilla_world(g, plan):
    return g.kentucky or plan.get("join", g.joined)


def plan_for(g, plans):
    p = plans.get(g.key) or {}
    return {"offset": tuple(p.get("offset", g.offset)), "join": bool(p.get("join", g.joined)),
            "layer": int(p.get("layer", 1))}


def map_layers(groups, plans):
    """{map folder name: layer}. Where maps overlap, the higher layer wins
    each square. Maps on the same layer keep their mod list order."""
    return {name: plan_for(g, plans)["layer"] for g in groups for name in g.map_names}


def order_maps(names, layers):
    """Map folder names in load order: higher layer first, ties as listed."""
    return sorted(names, key=lambda n: -layers.get(n, 1))


def overlaps(groups, vanilla_cells, plans):
    """Collisions at the planned positions, for maps that load together.

    Returns {group_key: {"conflict": [[x,y],...], "vanilla": [[x,y],...]}}.
    "conflict" is mod against mod: only one of them can own the cell.
    "vanilla" is mod over a vanilla cell: the mod wins (most map mods are
    built that way on purpose), shown so it isn't a surprise.
    Maps outside the vanilla world are their own world and never collide.
    """
    owner = {}
    hits = {}

    def hit(key, kind, c):
        hits.setdefault(key, {"conflict": set(), "vanilla": set()})[kind].add(c)

    for g in groups:
        p = plan_for(g, plans)
        if not in_vanilla_world(g, p):
            continue
        dx, dy = p["offset"]
        for (x, y) in g.cells:
            c = (x + dx, y + dy)
            if c in vanilla_cells:
                hit(g.key, "vanilla", c)
            other = owner.get(c)
            if other and other != g.key:
                hit(g.key, "conflict", c)
                hit(other, "conflict", c)
            else:
                owner[c] = g.key
    return {k: {kind: sorted(v) for kind, v in d.items()} for k, d in hits.items()}


def out_of_bounds(g, offset):
    dx, dy = offset
    return [(x + dx, y + dy) for (x, y) in g.cells
            if not (MIN_CELL <= x + dx <= MAX_CELL and MIN_CELL <= y + dy <= MAX_CELL)]
