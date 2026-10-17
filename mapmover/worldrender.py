"""The isometric world render, the kind pzmap.org shows.

The drawing is done by pzmap2dzi (https://github.com/cff29546/pzmap2dzi, MIT),
the engine behind pzmap.org. It draws every tile sprite from the game's
texture packs into a Deep Zoom image, one per floor, and ships a viewer that
shows vanilla with each mod map as an overlay you switch on and off.

This module feeds it the world the game really loads: the active mods of
default.txt, the moved copies instead of the workshop originals, and the
texture packs every active mod ships. It writes the engine's config and runs
its steps. The engine and its output live outside this repo, by default in
D:/ZomboidWorldRender (engine/, venv/, out/). The full vanilla isometric
render is about 350 GB and several hours. Dropping the two sharpest zoom
levels at quality 70, the default here, puts a big modded world near
50 GB. It still renders at full size first, so it takes as long.

    python -m mapmover.worldrender conf           write the engine config
    python -m mapmover.worldrender unpack         cut texture packs into sprites
    python -m mapmover.worldrender render [jobs]  e.g. base_top, or base
        --cells x,y,w,h   only these cells (repeat for more)
        --maps a,b        only these mod maps (folder names); none = vanilla only
        --omit 2          sharpest zoom levels to drop, default 2; each one
                          leaves about a quarter of the disk
        --quality 70      jpg and webp quality, default 70
        --once            draw each cell only for the first map that has it.
                          Faster, but wrong: the game fills each square from
                          the first map with something on that square, so a
                          partly filled cell shows the maps below it. By
                          default every map draws all its cells and the
                          overlays stack per square.
"""

import argparse
import json
import os
import re
import subprocess
import sys

from .paths import ROOT, load_config
from .scan import read_modlist, scan_mods
from .testload import server_maps

VANILLA_MAP = "Muldraugh, KY"
DEFAULT_HOME = r"D:\ZomboidWorldRender"
CONF_NAME = "mapmover.yaml"
MAPS_DIR = "mapmover_maps"


def home(cfg):
    return os.path.normpath(cfg.get("world_render_dir") or DEFAULT_HOME)


def key_of(name, used):
    """A section name the engine accepts in per-map options: [A-Za-z0-9_]."""
    k = re.sub(r"[^A-Za-z0-9_]", "_", name).strip("_") or "map"
    base, n = k, 2
    while k in used or k == "default":
        k = f"{base}_{n}"
        n += 1
    used.add(k)
    return k


def texture_dirs(mod):
    out = []
    for base in (mod.version_dir_name, "common"):
        if not base:
            continue
        d = os.path.join(mod.folder, base, "media", "texturepacks")
        if os.path.isdir(d) and any(f.lower().endswith(".pack") for f in os.listdir(d)):
            out.append(d)
    return out


def world_plan(cfg, modlist="default.txt"):
    """What the game loads: map folders in maps { } order and the texture
    pack folders of every active mod. Mods missing on disk are listed."""
    ids, map_order = read_modlist(os.path.join(cfg["mods_dir"], modlist))
    by_id = {}
    for m in scan_mods(cfg):
        by_id.setdefault(m.id, m)
    maps, textures, missing = {}, [], []
    for i in ids:
        m = by_id.get(i)
        if not m:
            missing.append(i)
            continue
        for d in texture_dirs(m):
            textures.append({"mod": m.id, "path": d})
        for d in m.maps:
            if d.name != VANILLA_MAP:
                maps[d.name] = {"name": d.name, "title": d.title, "mod": m.id,
                                "path": d.path, "cells": len(d.cells), "lots": list(d.lots),
                                "bounds": bounds(d.cells) if d.cells else None}
    ky = kentucky(maps)
    # Top first, the way MapGroups stacks Kentucky's world (loadorder.stack).
    order = [n for n in server_maps(by_id, ids, map_order) if n in ky]
    order += sorted(n for n in ky if n not in order)
    return {"maps": [maps[n] for n in order if maps[n]["cells"]], "textures": textures,
            "other_worlds": sorted(n for n in maps if n not in ky and maps[n]["cells"]),
            "missing": missing, "stacked": True}


def kentucky(maps):
    """Map folders in the vanilla world: joined to it by lots= lines, either
    way round, like MapGroups puts folders that name each other in one group.
    The rest are worlds of their own."""
    link = {n: set() for n in maps}
    link[VANILLA_MAP] = set()
    for n, m in maps.items():
        for lot in m["lots"]:
            if lot in link:
                link[n].add(lot)
                link[lot].add(n)
    seen, todo = set(), [VANILLA_MAP]
    while todo:
        n = todo.pop()
        if n not in seen:
            seen.add(n)
            todo += link[n]
    seen.discard(VANILLA_MAP)
    return seen


def cached_plan(cfg, modlist="default.txt"):
    """The plan of the last run, while the mod list and mods folder are older
    than it. Scanning every installed mod takes a couple of minutes."""
    path = os.path.join(home(cfg), "world.json")
    try:
        t = os.path.getmtime(path)
        if max(os.path.getmtime(os.path.join(cfg["mods_dir"], modlist)),
               os.path.getmtime(cfg["mods_dir"])) > t:
            return None
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    keys = ("maps", "textures", "other_worlds", "missing", "stacked")
    if not all(k in data for k in keys):
        return None
    return {k: data[k] for k in keys}


def bounds(cells):
    xs = [c[0] for c in cells]
    ys = [c[1] for c in cells]
    return [min(xs), min(ys), max(xs) - min(xs) + 1, max(ys) - min(ys) + 1]


def lot_cells(path):
    """Cells a map folder has, from its x_y.lotheader files."""
    out = set()
    try:
        names = os.listdir(path)
    except OSError:
        return out
    for f in names:
        m = re.match(r"(-?\d+)_(-?\d+)\.lotheader$", f)
        if m:
            out.add((int(m[1]), int(m[2])))
    return out


def rects_of(cells):
    """[[x, y, w, h]] covering exactly these cells: runs along x per row,
    then equal runs on next rows merged."""
    runs = {}
    for y in sorted({c[1] for c in cells}):
        xs = sorted(c[0] for c in cells if c[1] == y)
        start = prev = xs[0]
        for x in xs[1:] + [None]:
            if x is not None and x == prev + 1:
                prev = x
                continue
            runs.setdefault((start, prev - start + 1), []).append(y)
            if x is not None:
                start = prev = x
    out = []
    for (x, w), ys in runs.items():
        y0 = prev = ys[0]
        for y in ys[1:] + [None]:
            if y is not None and y == prev + 1:
                prev = y
                continue
            out.append([x, y0, w, prev - y0 + 1])
            if y is not None:
                y0 = prev = y
    return sorted(out, key=lambda r: (r[1], r[0]))


def winners(cfg, plan, wanted, cells=None):
    """Which cells each map draws when every cell is drawn once, by the map
    the game shows there. The first map in the maps block wins a cell,
    vanilla only shows where no mod map has it. Returns ({name: cells},
    vanilla cells); maps that win nothing are left out."""
    area = None
    if cells:
        area = {(x + i, y + j) for x, y, w, h in cells for i in range(w) for j in range(h)}
    taken, own = set(), {}
    for m in plan["maps"]:
        c = lot_cells(m["path"]) - taken
        taken |= c
        if m["name"] in wanted:
            c = c & area if area is not None else c
            if c:
                own[m["name"]] = c
    van = lot_cells(os.path.join(cfg["game_dir"], "media", "maps", VANILLA_MAP)) - taken
    return own, (van & area if area is not None else van)


def _yaml_str(s):
    return "'" + str(s).replace("'", "''") + "'"


def write_conf(cfg, plan, maps=None, cells=None, workers="auto", omit=0, out=None,
               once=False, quality=None):
    """Write the engine config into engine/conf. maps: folder names to render
    as overlays (None = all, [] = vanilla only). cells: [[x, y, w, h]] to
    render, None = everything. omit: drop that many of the sharpest zoom
    levels, each one cuts the disk to about a quarter. out: output folder,
    default <home>/out. once: draw each cell only for the first map that has
    it, see winners(). quality: jpg and webp quality, 1 to 100.
    Returns the config path."""
    h = home(cfg)
    conf_dir = os.path.join(h, "engine", "conf")
    used = set()
    sections, keys = [], {}
    for i, t in enumerate(plan["textures"]):
        k = key_of("tex_" + t["mod"], used)
        sections.append(f"{k}:\n  texture: true\n  texture_path: {_yaml_str(t['path'])}\n"
                        f"  map_path: ''\n")
    for m in plan["maps"]:
        k = key_of(m["name"], used)
        keys[m["name"]] = k
        sections.append(f"{k}:\n  display_name: {_yaml_str(m['title'])}\n"
                        f"  map_path: {_yaml_str(m['path'])}\n")
    os.makedirs(os.path.join(conf_dir, MAPS_DIR), exist_ok=True)
    with open(os.path.join(conf_dir, MAPS_DIR, "maps.txt"), "w", encoding="utf-8") as f:
        f.write("# Written by the map mover from default.txt. Do not edit.\n\n")
        f.write("\n".join(sections))

    wanted = [m["name"] for m in plan["maps"]] if maps is None else maps
    own = van = None
    if once:
        own, van = winners(cfg, plan, set(wanted), cells)
        wanted = [n for n in wanted if n in own]
    lines = [
        "# Written by the map mover (mapmover/worldrender.py). Do not edit.",
        f"pz_root: {_yaml_str(cfg['game_dir'])}",
        f"output_root: {_yaml_str(out or os.path.join(h, 'out'))}",
        f"mod_root: {_yaml_str(cfg.get('workshop_dir', ''))}",
        "custom_root: .",
        f"save_game_root: {_yaml_str(os.path.join(cfg['user_dir'], 'Saves'))}",
        "output_entry: default",
        "output_route: map_data/",
        "map_conf_default: default_b42.txt",
        "map_conf:",
        "    - vanilla.txt",
        f"    - {MAPS_DIR}/",
        "use_depend_texture_only: false",
        "base_map: default",
        "mod_maps:",
    ]
    lines += [f"    - {keys[n]}" for n in wanted if n in keys] or ["    []"]
    if lines[-1] == "    []":
        lines[-2:] = ["mod_maps: []"]
    lines += [
        "save_games: []",
        "render_conf:",
        "    verbose: true",
        f"    worker_count: {workers}",
        "    break_key: ''",
        "    tile_size: 1024",
        "    tile_align_levels: 3",
        "    layer_range: all",
        "    dzi_cell_range[default]: all_mod_maps",
        "    dzi_cell_range: auto",
    ]
    if cells:
        lines.append("    render_cell_range:")
        lines += [f"        - [{', '.join(str(int(v)) for v in c)}]" for c in cells]
    else:
        lines.append("    render_cell_range: all")
    if once:
        # An empty list means everything to the engine, so a map with
        # nothing to draw gets one cell far off the map.
        for name, c in [("default", van)] + [(keys[n], own[n]) for n in wanted]:
            lines.append(f"    render_cell_range[{name}]:")
            lines += [f"        - {r}" for r in rects_of(c) or [[-999, -999, 1, 1]]]
    lines += [
        f"    omit_levels: {int(omit)}",
        "    image_fmt: webp",
        "    image_fmt_base_layer0: jpg",
        "    image_save_options: {}" if not quality else
        f"    image_save_options: {{jpg: {{quality: {int(quality)}}}, webp: {{quality: {int(quality)}}}}}",
        "    enable_cache: false",
        "    cache_limit_mb: 0",
        "    top_view_square_size: 1",
        "    plants_conf: {snow: false, large_bush: false, flower: false, season: summer2,",
        "                  tree_size: 2, jumbo_tree_size: 4, jumbo_tree_type: 1,",
        "                  no_ground_cover: false, unify_tree_type: 0}",
        "    texture_filters: []",
        "    top_view_color_mode: avg",
        "    default_font: arial.ttf",
        "    default_font_size: 20",
        "    use_mark: true",
        "",
    ]
    path = os.path.join(conf_dir, CONF_NAME)
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    with open(os.path.join(h, "world.json"), "w", encoding="utf-8") as f:
        json.dump({"keys": keys, "drawn": [keys[n] for n in wanted if n in keys], **plan}, f, indent=1)
    return path


def run_engine(cfg, *args):
    h = home(cfg)
    py = os.path.join(h, "venv", "Scripts", "python.exe")
    engine = os.path.join(h, "engine")
    cmd = [py, "main.py", "-c", os.path.join("conf", CONF_NAME), *args]
    print(">", " ".join(cmd[1:]), flush=True)
    return subprocess.call(cmd, cwd=engine)


def order_map_list(cfg, out=None):
    """The viewer stacks overlays in map_list.json order, each one hiding the
    ones before it where it has cells. The engine writes the list in folder
    name order, so put the game's winner last, as in the maps block."""
    folder = os.path.join(out or os.path.join(home(cfg), "out"), "html", "map_data", "mod_maps")
    try:
        with open(os.path.join(home(cfg), "world.json"), encoding="utf-8") as f:
            data = json.load(f)
        names = [n for n in os.listdir(folder) if os.path.isdir(os.path.join(folder, n))]
    except (OSError, ValueError):
        return
    rank = {data["keys"][m["name"]]: i for i, m in enumerate(data["maps"]) if m["name"] in data["keys"]}
    names.sort(key=lambda n: -rank.get(n, len(rank)))
    with open(os.path.join(folder, "map_list.json"), "w", encoding="utf-8") as f:
        json.dump(names, f)


def _cells(text):
    v = [int(x) for x in text.split(",")]
    if len(v) == 2:
        v += [1, 1]
    if len(v) != 4:
        raise argparse.ArgumentTypeError("cells are x,y or x,y,w,h")
    return v


def main(argv=None):
    ap = argparse.ArgumentParser(prog="mapmover.worldrender")
    ap.add_argument("step", choices=["conf", "deploy", "unpack", "render"])
    ap.add_argument("jobs", nargs="*")
    ap.add_argument("--cells", type=_cells, action="append")
    ap.add_argument("--maps", help="comma separated map folder names; '' for vanilla only")
    ap.add_argument("--workers", default="auto")
    ap.add_argument("--omit", type=int, default=2,
                    help="drop the sharpest zoom levels, each about 3/4 of the disk; "
                         "2 keeps the whole world near 50 GB, 0 is pzmap.org's full detail")
    ap.add_argument("--out", help="output folder instead of <home>/out")
    ap.add_argument("--once", action="store_true",
                    help="draw each cell only for the first map that has it (black where it's partly empty)")
    ap.add_argument("--quality", type=int, default=70, help="jpg and webp quality, 1 to 100")
    a = ap.parse_args(argv)
    cfg = load_config()
    plan = cached_plan(cfg) or world_plan(cfg)
    maps = None if a.maps is None else [m for m in a.maps.split(",") if m]
    path = write_conf(cfg, plan, maps, a.cells, a.workers, a.omit, a.out,
                      once=a.once, quality=a.quality)
    print(f"{len(plan['maps'])} map folders, {len(plan['textures'])} texture folders -> {path}")
    if plan["missing"]:
        print("not on disk:", ", ".join(plan["missing"]))
    if a.step == "conf":
        return 0
    if a.step == "render":
        code = run_engine(cfg, "render", *(a.jobs or ["base_top"]))
        order_map_list(cfg, a.out)
        return code
    return run_engine(cfg, a.step)


if __name__ == "__main__":
    sys.exit(main())
