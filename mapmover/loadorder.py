"""The order the game stacks map folders in, and the world load crash that
some orders cause in 42.x.

Copied from the decompiled game:
  * IsoMetaGrid.getLotDirectories and MapGroups: folders are grouped by
    their lots= chains. Inside a group, each folder nobody else points at
    puts its own lots= chain in order (setPriority), then the maps { } list
    in default.txt sorts what it names (setOrder). Kentucky is the group
    holding "Muldraugh, KY".
  * Some map mods also put themselves on top from Lua when the world loads,
    getWorld():setMap('X' .. ';' .. getWorld():getMap()). The maps list and
    layers can't move those down.
  * MapFiles.postLoad marks a 300x300 block of a folder as "has cells" when
    the folder has the 256 cells (a, b) and (a+1, b+1) under the block's
    corner. LotHeader.getZombieIntensityForChunk walks a cell's folders from
    the top one down and reads the header of the first folder whose block
    covers the chunk. When that folder has no such cell the header is null
    and the game dies loading the world (MapCollisionData.init, a
    NullPointerException in the console).
"""

import os
import re

from .layout import MOVED_SUFFIX, VANILLA_MAP, map_layers, order_maps, plan_for

_SETMAP = re.compile(r"""setMap\(\s*['"]([^'"]+)['"]\s*\.\.\s*['"];['"]\s*\.\.\s*getWorld\(\):getMap\(\)""")
_lua_cache = {}


def lua_prepends(mod):
    """Map folder names this mod's Lua puts on top of the map list."""
    key = (mod.folder, mod.version_dir_name)
    if key in _lua_cache:
        return _lua_cache[key]
    out = []
    for base in ("common", mod.version_dir_name):
        root = os.path.join(mod.folder, base or "", "media", "lua")
        for dp, dirs, files in os.walk(root):
            dirs.sort()
            for f in sorted(files):
                if not f.endswith(".lua"):
                    continue
                try:
                    with open(os.path.join(dp, f), encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
                except OSError:
                    continue
                out += _SETMAP.findall(text)
    _lua_cache[key] = out
    return out


def stack(state, plans, mod_ids, map_list, extra=()):
    """Folders of the Kentucky world, top first, as [(name, set of cells)],
    and the set of folder names some mod's Lua puts on top.

    mod_ids: original ids in mod list order. map_list: the maps { } block as
    Apply writes it. extra: [(name, cells)] put on top first (the road links).
    Groups sit at their planned offsets."""
    group_of = {oid: g for g in state["groups"] for oid in g.originals}
    dirs = {}      # name -> (lots, cells); the last mod with a folder wins
    seen = []
    prepends = []
    for i in mod_ids:
        g = group_of.get(i)
        m = g.originals[i] if g else state["originals"].get(i) or state["by_id"].get(i)
        if not m:
            continue
        p = plan_for(g, plans) if g else {"offset": (0, 0), "join": False}
        dx, dy = p["offset"]
        join = bool(g) and p["join"] and not g.kentucky
        for d in m.maps:
            if d.name == VANILLA_MAP:
                continue
            lots = list(d.lots) + ([VANILLA_MAP] if join and VANILLA_MAP not in d.lots else [])
            dirs[d.name] = (lots, {(x + dx, y + dy) for x, y in d.cells})
            if d.name not in seen:
                seen.append(d.name)
        prepends += lua_prepends(m)
    dirs[VANILLA_MAP] = ([], set(state["vanilla_cells"]))
    seen.append(VANILLA_MAP)

    def chain(name, out):
        if name in out:
            return
        out.append(name)
        for lot in dirs.get(name, ((), ()))[0]:
            if lot in dirs:
                chain(lot, out)

    groups = []
    for n in seen:
        names = []
        chain(n, names)
        g = next((g for g in groups if any(x in g for x in names)), None)
        if g is None:
            g = []
            groups.append(g)
        g += [x for x in names if x not in g]

    def set_priority(g, wanted):
        ordered = [n for n in wanted if n in g]
        for i, n in enumerate(g):
            if n in wanted:
                g[i] = ordered.pop(0)

    ky = next(g for g in groups if VANILLA_MAP in g)
    for n in list(ky):
        if any(n in dirs[o][0] for o in ky if o != n):
            continue
        wanted = []
        chain(n, wanted)
        set_priority(ky, wanted)
    if map_list:
        set_priority(ky, list(dict.fromkeys(map_list)))

    order = list(ky)
    for n in prepends:
        if n in dirs:
            order = [n] + [x for x in order if x != n]
    out = [(n, set(c)) for n, c in extra if c]
    out += [(n, dirs[n][1]) for n in order if dirs[n][1]]
    return out, set(prepends)


def _blocks(cells):
    """The 300x300 blocks MapFiles.postLoad marks for a folder."""
    out = set()
    for (x, y) in cells:
        # Only blocks whose corner cell is (x, y) can be marked by it.
        for bx in range(x * 256 // 300, (x + 1) * 256 // 300 + 1):
            for by in range(y * 256 // 300, (y + 1) * 256 // 300 + 1):
                if bx * 300 // 256 == x and by * 300 // 256 == y and (x + 1, y + 1) in cells:
                    out.add((bx, by))
    return out


def _chunk_blocks(cx, cy):
    """Blocks the 32x32 chunks of cell (cx, cy) fall in."""
    xs = {(cx * 256 + i * 8) // 300 for i in range(32)}
    ys = {(cy * 256 + i * 8) // 300 for i in range(32)}
    return [(bx, by) for bx in xs for by in ys]


def crashes(folders, forced=()):
    """Cells that crash the world load. folders: stack()'s list, forced: its
    Lua set. Returns [{"top": name, "asked": name, "cells": [[x, y], ...],
    "forced": the top one is put there by Lua}]."""
    blocks = [_blocks(c) for _, c in folders]
    top = {}
    for i, (_, cells) in enumerate(folders):
        for c in cells:
            top.setdefault(c, i)
    bad = {}
    for (cx, cy), i in top.items():
        for k in _chunk_blocks(cx, cy):
            for j in range(i, len(folders)):
                if k in blocks[j]:
                    if (cx, cy) not in folders[j][1]:
                        bad.setdefault((folders[i][0], folders[j][0]), set()).add((cx, cy))
                    break
    return [{"top": t, "asked": a, "cells": sorted([x, y] for x, y in cs), "forced": t in forced}
            for (t, a), cs in sorted(bad.items())]


def check(state, plans, plan, extra=()):
    """crashes() for what Apply would leave. plan: build.make_plan's result.
    extra: [(folder, cells)] of the road links mod, which loads on top."""
    return crashes(*stack(*_after_apply(state, plans, plan), extra))


def road_no_go(state, plans, plan):
    """Cells the road links mod must stay out of. It loads on top, so the
    game starts from it at each of its cells. At these cells the first
    folder below whose 300x300 block covers a chunk has no such cell, and
    the world load crashes."""
    folders, _ = stack(*_after_apply(state, plans, plan))
    first = {}
    for j, (_, cells) in enumerate(folders):
        for k in _blocks(cells):
            first.setdefault(k, j)
    out = set()
    for (bx, by), j in first.items():
        for cx in range(bx * 300 // 256, (bx * 300 + 299) // 256 + 1):
            for cy in range(by * 300 // 256, (by * 300 + 299) // 256 + 1):
                if (cx, cy) not in folders[j][1] and (bx, by) in _chunk_blocks(cx, cy):
                    out.add((cx, cy))
    return out


def _after_apply(state, plans, plan):
    """stack()'s arguments for the mod list and maps Apply would write."""
    ids = []
    for i in plan["final_ids"]:
        base = i[:-len(MOVED_SUFFIX)] if i.endswith(MOVED_SUFFIX) else i
        ids.append(base if base in plan["copies"] else i)
    # Apply drops our road folders from maps { } and puts them back in front.
    names = [n for n in state["active_maps"] if not n.startswith("mapmover_roads_")] + plan["added_maps"]
    maps = order_maps(names, map_layers(state["groups"], plans))
    return state, plans, ids, maps
