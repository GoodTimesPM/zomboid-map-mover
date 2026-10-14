"""The road links mod: paved squares joining moved maps to nearby roads.

The squares go in a mod of their own, mapmover_roads, whose map folders come
first in the maps list so they win over every other map on the squares they
pave. A cell only lists its road squares; the game fills the rest of the cell
from the next map down, or from world generation, square by square.

The cells are split over two map folders by even and odd cell x. When one
map folder has two cells diagonally next to each other, the game drops the
buildings of lower maps in that area (IsoMetaGrid's 300 square blocks), and
the split means neither folder ever has such a pair.
"""

import datetime
import json
import os
import secrets

from .connect import connect_group, find_cut_roads, find_exits, pave_path, route_between
from .layout import VANILLA_MAP, in_vanilla_world, plan_for
from .lots import CELL, LotHeader, write_header, write_pack
from .scan import MANIFEST
from .world import WorldSampler, home_sampler

ROADS_ID = "mapmover_roads"
MAP_NAMES = ("mapmover_roads_a", "mapmover_roads_b")
MANUAL_KEY = "manual links"   # where hand-drawn links sit among the per-map ones
VERSION_DIR = "42"

MAP_INFO = """title=Map Mover road links
lots=Muldraugh, KY
description=Roads the map mover paved to join moved maps to the roads around them.
fixed2x=true
"""

MOD_INFO = """name=Map Mover road links
id=mapmover_roads
description=Roads the map mover paved to join moved maps to the roads around them. Made by zomboid-map-mover, rebuilt on every Apply.
"""


def wants_roads(g, plan):
    """Only moved maps in the Kentucky world get links."""
    return bool(plan.get("roads")) and in_vanilla_world(g, plan) and tuple(plan["offset"]) != (0, 0)


def vanilla_dir(cfg):
    return os.path.join(cfg["game_dir"], "media", "maps", VANILLA_MAP)


def plan_links(state, plans, vdir, keys=None, log=print, manual=(), no_go=()):
    """Routes for each group that asks for road links. Road ends linked by
    hand (manual) are left out. no_go: cells no road may enter.

    Returns (sampler, {key: {"routes": [...], "missed": [[x, y]]}}).
    """
    sampler = WorldSampler(state, plans, vdir)
    sampler.no_go = set(no_go)
    skip = set()
    for link in manual:
        for p in (link["a"], link["b"]):
            if p.get("dir"):
                xy = link_point(state, plans, p)
                if xy:
                    skip.add(xy)
    out = {}
    for g in state["groups"]:
        p = plan_for(g, plans)
        p["roads"] = (plans.get(g.key) or {}).get("roads", False)
        if keys is not None and g.key not in keys:
            continue
        if not wants_roads(g, p):
            continue
        home = home_sampler(state, g.key, vdir)
        routes, missed = connect_group(sampler, home, g.key, p["offset"], log=log, skip=skip)
        out[g.key] = {"routes": routes, "missed": missed}
    return sampler, out


# ---------------------------------------------------------------- links by hand
#
# A link drawn by hand runs from a road end of one map to a road end of
# another, or to any square. Its ends are kept in home coordinates (where
# the square is with its map at offset 0) with the map's key, so they move
# with their maps. A point outside every map has key None and is a world
# square. The routed path is kept too: Apply paves it as it is while
# neither map has moved and nothing new is in the way, and routes it again
# otherwise.


def group_of(state, key):
    return next((g for g in state["groups"] if g.key == key), None)


def link_point(state, plans, p):
    """World square of a link end, or None when its map is gone or no
    longer in the Kentucky world."""
    if p.get("key") is None:
        return p["hx"], p["hy"]
    g = group_of(state, p["key"])
    if not g:
        return None
    pl = plan_for(g, plans)
    if not in_vanilla_world(g, pl):
        return None
    dx, dy = pl["offset"]
    return p["hx"] + dx * CELL, p["hy"] + dy * CELL


def offsets_of(state, plans, keys):
    out = {}
    for k in keys:
        g = group_of(state, k)
        if g:
            out[k] = list(plan_for(g, plans)["offset"])
    return out


def road_ends(sampler, state, plans, key, vdir):
    """Road ends of one map at its planned spot, for picking by hand. Roads
    that stop inside the map (at a river, say) count too, with "inner", and
    so do roads under the map that it cuts off, with "cut" and no key."""
    g = group_of(state, key)
    if not g:
        raise ValueError("No such map.")
    p = plan_for(g, plans)
    if not in_vanilla_world(g, p):
        return []
    dx, dy = p["offset"]
    home = home_sampler(state, key, vdir)
    out = []
    for e in find_exits(sampler, home, key, p["offset"], inner=True):
        out.append({"key": key, "x": e["x"], "y": e["y"], "hx": e["x"] - dx * CELL, "hy": e["y"] - dy * CELL,
                    "dir": list(e["dir"]), "width": e["width"], "tile": e["tile"], "tiles": e["tiles"],
                    "inner": bool(e.get("inner"))})
    cells = {(cx + dx, cy + dy) for cx, cy in g.cells}
    for e in find_cut_roads(sampler, key, cells):
        out.append({"key": None, "x": e["x"], "y": e["y"], "hx": e["x"], "hy": e["y"], "dir": list(e["dir"]),
                    "width": e["width"], "tile": e["tile"], "tiles": e["tiles"], "cut": True})
    return out


def _forbid(link):
    """Maps the road must stay off: the one it starts from, and the one it
    runs into head on. A point picked inside a map is a target, not a wall,
    and a road that stops inside its map has to cross that map to get out."""
    keys = []
    for p in (link["a"], link["b"]):
        if p.get("dir") and p.get("key") and not p.get("inner") and p["key"] not in keys:
            keys.append(p["key"])
    return keys


def make_link(sampler, state, plans, a, b):
    """Route a link by hand from road end a to b (another road end, or a
    point {"key": None, "hx", "hy"} in world squares). Returns (link, None)
    or (None, a reason for the user)."""
    link, _, err = _route_link(sampler, state, plans, a, b)
    return link, err


def _route_link(sampler, state, plans, a, b):
    a = dict(a)
    b = dict(b)
    pa = link_point(state, plans, a)
    if not pa:
        return None, None, "The first road end's map isn't in the Kentucky world."
    if b.get("key") is None and not b.get("dir") and not b.get("cut"):
        # A square picked on the map: tie it to the map that owns it, so it moves with it.
        # A road cut off by a map stays put: the road under it doesn't move.
        cx, cy = b["hx"] // CELL, b["hy"] // CELL
        owner = sampler.owner(cx, cy)
        g = group_of(state, owner) if owner not in (None, "vanilla") else None
        if g:
            dx, dy = plan_for(g, plans)["offset"]
            b = {"key": g.key, "hx": b["hx"] - dx * CELL, "hy": b["hy"] - dy * CELL}
    pb = link_point(state, plans, b)
    if not pb:
        return None, None, "The second road end's map isn't in the Kentucky world."
    if b.get("dir") and b.get("width"):
        a["width"] = min(a["width"], b["width"])   # a wide road can't run into a narrow street head on
    link = {"id": secrets.token_hex(4), "a": a, "b": b}
    ra, rb = dict(a, x=pa[0], y=pa[1]), dict(b, x=pb[0], y=pb[1])
    r, err = route_between(sampler, ra, rb, _forbid(link))
    if err and b.get("dir") and err.startswith("No way through"):
        # No room to run into that end head on: let it arrive from the side.
        r, err2 = route_between(sampler, ra, {k: v for k, v in rb.items() if k != "dir"}, _forbid(link))
        err = err2 and err
    if err:
        return None, None, err
    link.update({"at": offsets_of(state, plans, {a["key"], b.get("key")} - {None}),
                 "path": [list(p) for p in r["path"]], "width": a["width"], "tile": a["tile"],
                 "tiles": a.get("tiles") or {a["tile"]: 1}, "squares": len(r["squares"]),
                 "length": r["length"], "bridge": r.get("bridge", 0)})
    return link, r["squares"], None


def manual_routes(sampler, state, plans, links, log=print):
    """Squares for every link drawn by hand, for Apply. Returns (routes,
    links as they stand now: a link routed again here keeps its new path)."""
    routes, kept = [], []
    for link in links:
        a, b = link["a"], link["b"]
        pa, pb = link_point(state, plans, a), link_point(state, plans, b)
        to = f"{b['key']} road end" if b.get("dir") else f"a spot in {b['key']}" if b.get("key") else "a spot"
        name = f"link {a['key']} -> {to}"
        if not pa or not pb:
            log(f"{name}: skipped, a map at one end is gone or out of the Kentucky world")
            kept.append(link)
            continue
        same = all(offsets_of(state, plans, [k]).get(k) == v for k, v in link.get("at", {}).items())
        squares = None
        if same and link.get("path"):
            squares = pave_path(sampler, link["path"], a["dir"], link["width"], link["tiles"], _forbid(link))
        if squares is None:
            fresh, squares, err = _route_link(sampler, state, plans, a, b)
            if err:
                log(f"{name}: couldn't route it again: {err}")
                kept.append(link)
                continue
            log(f"{name}: a map moved or something is in the way, routed again")
            fresh["id"] = link.get("id") or fresh["id"]
            link = fresh
        kept.append(link)
        routes.append({"squares": squares})
        log(f"{name}: {len(squares)} squares paved")
    return routes, kept


def cells_of(links):
    """{(cx, cy): {(x, y): tile}} with cell-relative squares."""
    cells = {}
    for res in links.values():
        for r in res["routes"]:
            for (wx, wy), tile in r["squares"].items():
                cx, cy = wx // CELL, wy // CELL
                cells.setdefault((cx, cy), {})[(wx - cx * CELL, wy - cy * CELL)] = tile
    return cells


def write_mod(folder, cells, sampler):
    """Write the mod into folder (which must not exist yet)."""
    vdir = os.path.join(folder, VERSION_DIR)
    os.makedirs(os.path.join(folder, "common"))
    os.makedirs(vdir)
    with open(os.path.join(vdir, "mod.info"), "w", encoding="utf-8") as f:
        f.write(MOD_INFO)
    for name in MAP_NAMES:
        d = os.path.join(vdir, "media", "maps", name)
        os.makedirs(d)
        with open(os.path.join(d, "map.info"), "w", encoding="utf-8") as f:
            f.write(MAP_INFO)
    for (cx, cy), squares in sorted(cells.items()):
        d = os.path.join(vdir, "media", "maps", MAP_NAMES[cx % 2])
        tiles = sorted(set(squares.values()))
        index = {t: i for i, t in enumerate(tiles)}
        # The top map's header sets the cell's zombie counts, so keep the ones
        # the cell had before.
        zombies = sampler.cell(cx, cy).zombies or bytes(1024)
        h = LotHeader(tiles=tiles, min_level=0, max_level=0, zombies=zombies)
        with open(os.path.join(d, f"{cx}_{cy}.lotheader"), "wb") as f:
            f.write(write_header(h))
        with open(os.path.join(d, f"world_{cx}_{cy}.lotpack"), "wb") as f:
            f.write(write_pack({(x, y, 0): [index[t]] for (x, y), t in squares.items()}))
    with open(os.path.join(folder, MANIFEST), "w", encoding="utf-8") as f:
        json.dump({"tool": "zomboid-map-mover", "kind": "roads",
                   "built": datetime.datetime.now().isoformat(timespec="seconds"),
                   "cells": len(cells), "squares": sum(len(s) for s in cells.values())}, f, indent=2)
