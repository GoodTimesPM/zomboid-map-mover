"""Turn a layout plan into moved mod copies and an updated default.txt.

Nothing under the Steam workshop folder is ever written. A moved map is a
copy in Zomboid/mods/<folder>_moved with mod id <id>_moved, so the original
stays installed and can be switched back to at any time.

Steps for one Apply:
  1. Work out which active mods need a copy (moved groups, plus anything
     that requires a copied mod, so its require= doesn't pull the original
     back in).
  2. Build each copy into <dest>.building, shifting map files on the way.
  3. Check the copy against the source (cell lists, map bounds, Lua).
  4. Swap it into place.
  5. Back up default.txt and rewrite the mod ids in place.
  6. Delete copies that are no longer needed.
"""

import datetime
import hashlib
import json
import os
import re
import shutil
import subprocess
import time

from . import shifters as sh
from .layout import MAX_CELL, MIN_CELL, MOVED_SUFFIX, VANILLA_MAP, map_layers, order_maps, out_of_bounds, plan_for
from .luashift import LuaTokenError, tokenize
from .paths import DATA_DIR
from .loadorder import check as crash_check, road_no_go
from .scan import MANIFEST, MapDir
from .roads import MAP_NAMES, MANUAL_KEY, ROADS_ID, cells_of, manual_routes, plan_links, wants_roads, write_mod
from .roads import vanilla_dir as roads_vanilla_dir

TOOL_VERSION = 1
CELL = sh.CELL


class BuildError(Exception):
    pass


# ---------------------------------------------------------------- planning


def clear_leftovers(mods_dir, log=print):
    """Remove half-built folders an Apply left when it was stopped before it
    finished (the app closed, the PC slept). None of them is enabled, since
    default.txt is written last."""
    if not os.path.isdir(mods_dir):
        return
    for name in sorted(os.listdir(mods_dir)):
        base, dot, tail = name.rpartition(".")
        if not dot or tail != "building":
            continue
        if base.endswith(MOVED_SUFFIX) or base == ROADS_ID:
            log(f"removing {name}, left over from an Apply that didn't finish")
            rmtree(os.path.join(mods_dir, name))


def make_plan(state, plans, add_ids=()):
    """Decide the final id for every mod in the active list.

    Returns a dict:
      copies:  {original_id: {"mod": Mod, "offset": (dx,dy), "join": bool}}
      final_ids: the new mods list for default.txt, in order
      added_maps: map folder names to append to default.txt
      errors: list of strings (nothing is built if non-empty)
    """
    errors = []
    by_id = state["by_id"]
    originals = state["originals"]
    group_of = {oid: g for g in state["groups"] for oid in g.originals}

    def orig_id(i):
        m = by_id.get(i)
        if m and m.manifest and m.manifest.get("original_id"):
            return m.manifest["original_id"]
        return i

    active = [orig_id(i) for i in state["active_ids"]]
    off = set(state.get("off_ids") or ())
    for a in add_ids:
        if a in off:
            continue
        if a not in originals:
            errors.append(f"mod {a} is not installed")
            continue
        for r in originals[a].require:
            r = r.strip()
            if r and r not in active:
                active.append(r)
        if a not in active:
            active.append(a)

    copies = {}
    for g in state["groups"]:
        p = plan_for(g, plans)
        if p["offset"] != (0, 0) or (p["join"] and not g.kentucky):
            bad = out_of_bounds(g, p["offset"])
            if bad:
                errors.append(f"{g.title()}: {len(bad)} cells would land outside {MIN_CELL}..{MAX_CELL}")
            for oid, m in g.originals.items():
                if m.manifest:
                    errors.append(f"{oid}: the original mod is no longer installed, only the moved copy is")
                    continue
                copies[oid] = {"mod": m, "offset": p["offset"], "join": p["join"] and not g.kentucky}

    # Anything that requires a copied mod gets copied too (offset 0 unless it
    # belongs to a moved group already), repeated until nothing changes.
    changed = True
    while changed:
        changed = False
        for i in active:
            if i in copies or i not in originals:
                continue
            m = originals[i]
            if any(r.strip() in copies for r in m.require):
                g = group_of.get(i)
                p = plan_for(g, plans) if g else {"offset": (0, 0), "join": False}
                copies[i] = {"mod": m, "offset": p["offset"],
                             "join": bool(g) and p["join"] and not g.kentucky}
                changed = True

    final_ids = [i + MOVED_SUFFIX if i in copies else i for i in active]

    added_maps = []
    current_maps = set(state["active_maps"])
    for a in add_ids:
        m = originals.get(a)
        if not m or a in off:
            continue
        for d in m.maps:
            if d.name != VANILLA_MAP and d.name not in current_maps and d.name not in added_maps:
                added_maps.append(d.name)

    return {"copies": copies, "final_ids": final_ids, "added_maps": added_maps, "errors": errors}


# ---------------------------------------------------------------- helpers


def game_running():
    try:
        # No console window flashing up from the desktop app (pythonw).
        flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        out = subprocess.run(["tasklist", "/FI", "IMAGENAME eq ProjectZomboid64.exe", "/NH"],
                             capture_output=True, text=True, timeout=15, creationflags=flags).stdout
    except (OSError, subprocess.SubprocessError):
        return False
    return "ProjectZomboid64.exe" in out


def source_signature(folder):
    h = hashlib.sha1()
    count = 0
    for root, dirs, files in os.walk(folder):
        dirs.sort()
        for f in sorted(files):
            p = os.path.join(root, f)
            st = os.stat(p)
            h.update(f"{os.path.relpath(p, folder)}|{st.st_size}|{int(st.st_mtime)}\n".encode("utf-8"))
            count += 1
    return f"{count}:{h.hexdigest()}"


def folder_size(folder):
    total = 0
    for root, _, files in os.walk(folder):
        for f in files:
            total += os.path.getsize(os.path.join(root, f))
    return total


def rmtree(path):
    def onerror(func, p, exc):
        os.chmod(p, 0o666)
        func(p)
    if os.path.isdir(path):
        shutil.rmtree(path, onerror=onerror)


def copy_name(cfg, mod):
    """Destination folder for a mod's copy. Reuses an existing copy of the
    same original; otherwise <folder>_moved, with the workshop id added if
    that name is taken by something else."""
    base = os.path.basename(mod.folder.rstrip("\\/"))
    candidates = [base + MOVED_SUFFIX]
    if mod.workshop_id:
        candidates.append(f"{base}_{mod.workshop_id}{MOVED_SUFFIX}")
    for name in candidates:
        dest = os.path.join(cfg["mods_dir"], name)
        if not os.path.exists(dest):
            return dest
        man = read_manifest(dest)
        if man and man.get("original_id") == mod.id:
            return dest
    raise BuildError(f"no free folder name for a copy of {mod.id}")


def read_manifest(folder):
    try:
        with open(os.path.join(folder, MANIFEST), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- mod.info


_ID_LIST_KEYS = ("require=", "incompatible=", "loadModAfter=", "loadModBefore=")


def rewrite_mod_info(text, new_id, new_name, id_map):
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        end = line[len(body):]
        stripped = body.lstrip("﻿")
        bom = body[:len(body) - len(stripped)]
        if stripped.startswith("id="):
            body = bom + "id=" + new_id
        elif stripped.startswith("name="):
            body = bom + "name=" + new_name
        else:
            for key in _ID_LIST_KEYS:
                k = stripped.find(key)
                if k >= 0:
                    head = stripped[:k + len(key)]
                    parts = stripped[k + len(key):].split(",")
                    new_parts = []
                    for part in parts:
                        m = re.match(r"^(\s*\\?)(.*?)(\s*)$", part)
                        lead, ident, trail = m.groups()
                        new_parts.append(lead + id_map.get(ident, ident) + trail)
                    body = bom + head + ",".join(new_parts)
                    break
        out.append(body + end)
    return "".join(out)


def referenced_ids(folder):
    """Every mod id named in any mod.info inside folder (require=,
    incompatible=, loadModAfter=, loadModBefore=)."""
    out = set()
    for root, _, files in os.walk(folder):
        for f in files:
            if f.lower() != "mod.info":
                continue
            text = sh.decode_text(open(os.path.join(root, f), "rb").read())[0]
            for line in text.splitlines():
                for key in _ID_LIST_KEYS:
                    k = line.find(key)
                    if k >= 0:
                        for part in line[k + len(key):].split(","):
                            part = part.strip().lstrip("\\").strip()
                            if part:
                                out.add(part)
                        break
    return out


# ---------------------------------------------------------------- one copy


class CopyReport:
    def __init__(self, mod_id):
        self.mod_id = mod_id
        self.counts = {}
        self.warnings = []
        self.dropped = []

    def bump(self, key, n=1):
        self.counts[key] = self.counts.get(key, 0) + n


def _map_dir_of(rel_parts):
    """If rel path (split) points inside <base>/media/maps/<dir>/..., return
    (index_of_dir_name, dir_name). Otherwise None."""
    for i in range(len(rel_parts) - 2):
        if rel_parts[i].lower() == "media" and rel_parts[i + 1].lower() == "maps" and i + 3 < len(rel_parts):
            return i + 2, rel_parts[i + 2]
    return None


def _shift_map_file(name, sub, data, dx, dy, join, report):
    """Transform one file inside a map folder.

    name: file name. sub: list of subfolders between the map folder and the
    file. Returns (new_name, new_bytes) or (None, None) to drop the file.
    """
    lname = name.lower()
    moved = (dx, dy) != (0, 0)
    if not sub:
        new = sh.shifted_cell_filename(name, dx, dy) if moved else None
        if new:
            report.bump("cell files")
            return new, data
    elif len(sub) == 1 and sub[0].lower() == "maps":
        new = sh.shifted_cell_filename(name, dx, dy, in_maps_subdir=True) if moved else None
        if new:
            report.bump("biome maps")
            return new, data

    if lname == "map.info":
        text, bom, enc = sh.decode_text(data)
        if moved:
            text = sh.shift_map_info(text, dx, dy)
        if join and not re.search(r"^\s*lots=Muldraugh, KY\s*$", text, re.MULTILINE):
            if text and not text.endswith(("\n", "\r")):
                text += "\r\n" if "\r\n" in text else "\n"
            text += "lots=" + VANILLA_MAP + ("\r\n" if "\r\n" in text else "\n")
            report.bump("joined to the vanilla world")
        report.bump("map.info")
        return name, sh.encode_text(text, bom, enc)

    if not moved:
        return name, data

    if lname.endswith(".lua"):
        try:
            out, stats, warns = sh.shift_lua_bytes(data, dx, dy)
        except LuaTokenError as e:
            raise BuildError(f"{'/'.join(sub + [name])}: could not read the Lua ({e})")
        for k, v in stats.items():
            if v:
                report.bump("lua " + k, v)
        for w in warns:
            report.warnings.append(f"{'/'.join(sub + [name])}: {w}")
        return name, out

    if lname == "streets.xml":
        out, n = sh.shift_streets_xml(data, dx, dy)
        report.bump("street points", n)
        return name, out

    if lname.endswith(".xml"):
        if b"<cell" in data or lname.startswith("worldmap"):
            out, n = sh.shift_worldmap_xml(data, dx, dy)
            report.bump("world map cells (xml)", n)
            return name, out
        report.warnings.append(f"{'/'.join(sub + [name])}: unknown XML file, copied unchanged")
        return name, data

    if lname.endswith(".bin") and data[:4] == b"IGMB":
        try:
            out, n = sh.shift_worldmap_bin(data, dx, dy)
        except sh.WorldMapBinError as e:
            # The game falls back to the .xml next to it, which is shifted.
            report.dropped.append(f"{'/'.join(sub + [name])} ({e}); the game will read the .xml instead")
            return None, None
        report.bump("world map cells (bin)", n)
        return name, out

    if lname.endswith(".zip"):
        try:
            out, old, new = sh.shift_pyramid_zip(data, dx, dy)
        except (ValueError, KeyError) as e:
            report.warnings.append(f"{'/'.join(sub + [name])}: not a map image zip ({e}), copied unchanged")
            return name, data
        report.bump("map image bounds")
        return name, out

    if not sub and lname.endswith((".png", ".txt", ".bak", ".jpg", ".ini")):
        return name, data
    if sub and sub[0].lower().endswith(".pzeditor"):
        return name, data
    if not (len(sub) == 1 and sub[0].lower() == "maps"):
        report.warnings.append(f"{'/'.join(sub + [name])}: not a known map file, copied unchanged")
    return name, data


def _coordinate_hits(text, xr, yr):
    xs = ys = 0
    for m in re.finditer(r"(?<![\w.])(\d{4,6})(?![\w.])", text):
        v = int(m.group(1))
        if xr[0] <= v <= xr[1]:
            xs += 1
        if yr[0] <= v <= yr[1]:
            ys += 1
    return min(xs, ys)


def build_copy(cfg, entry, id_map, tile_box, progress=None):
    """Build one mod copy into <dest>.building and return (building, dest, report)."""
    mod = entry["mod"]
    dx, dy = entry["offset"]
    join = entry["join"]
    dest = copy_name(cfg, mod)
    building = dest + ".building"
    rmtree(building)
    report = CopyReport(mod.id)
    new_id = id_map[mod.id]
    sign = lambda v: f"+{v}" if v >= 0 else str(v)
    if (dx, dy) != (0, 0):
        new_name = f"{mod.name} (moved {sign(dx)},{sign(dy)})"
    elif join:
        new_name = f"{mod.name} (in Kentucky)"
    else:
        new_name = f"{mod.name} (for moved maps)"

    src_root = mod.folder
    for root, dirs, files in os.walk(src_root):
        dirs.sort()
        rel_dir = os.path.relpath(root, src_root)
        parts = [] if rel_dir == "." else rel_dir.split(os.sep)
        md = _map_dir_of(parts + ["_"])
        out_dir = os.path.join(building, *parts) if parts else building
        os.makedirs(out_dir, exist_ok=True)
        for f in sorted(files):
            sp = os.path.join(root, f)
            if md and md[1] != VANILLA_MAP:
                sub = parts[md[0] + 1:]
                with open(sp, "rb") as fh:
                    data = fh.read()
                new_name_f, out = _shift_map_file(f, sub, data, dx, dy, join, report)
                if new_name_f is None:
                    continue
                with open(os.path.join(out_dir, new_name_f), "wb") as fh:
                    fh.write(out)
            elif f.lower() == "mod.info":
                text, bom, enc = sh.decode_text(open(sp, "rb").read())
                text = rewrite_mod_info(text, new_id, new_name, id_map)
                with open(os.path.join(out_dir, f), "wb") as fh:
                    fh.write(sh.encode_text(text, bom, enc))
            else:
                shutil.copyfile(sp, os.path.join(out_dir, f))
                if (dx, dy) != (0, 0) and f.lower().endswith(".lua") and tile_box:
                    try:
                        text = open(sp, encoding="utf-8", errors="replace").read()
                    except OSError:
                        text = ""
                    n = _coordinate_hits(text, tile_box[0], tile_box[1])
                    if n >= 2:
                        rel = os.path.relpath(sp, src_root).replace(os.sep, "/")
                        report.warnings.append(
                            f"{rel}: has {n} numbers that look like map coordinates; "
                            "this script is not shifted, check it if something in the map misbehaves")
            if progress:
                progress(os.path.getsize(sp))
    return building, dest, report


# ---------------------------------------------------------------- verify


def _cells_in(folder):
    out = set()
    for f in os.listdir(folder):
        m = sh.LOT_NAME.match(f)
        if m:
            out.add((int(m.group(1)), int(m.group(2))))
    return out


def _xml_cells(data):
    out = set()
    for m in re.finditer(rb'<cell\b[^>]*?\sx\s*=\s*"(-?\d+)"[^>]*?\sy\s*=\s*"(-?\d+)"', data):
        out.add((int(m.group(1)), int(m.group(2))))
    return out


def verify_copy(src_root, building, dx, dy, join):
    """Compare every map folder in the copy with the source. Returns a list
    of problems (empty means good)."""
    problems = []
    for root, dirs, files in os.walk(src_root):
        rel = os.path.relpath(root, src_root)
        parts = [] if rel == "." else rel.split(os.sep)
        if len(parts) < 3 or parts[-2].lower() != "maps" or parts[-3].lower() != "media":
            continue
        if parts[-1] == VANILLA_MAP or "map.info" not in files:
            continue
        name = parts[-1]
        out = os.path.join(building, *parts)
        if not os.path.isdir(out):
            problems.append(f"{name}: map folder missing from the copy")
            continue
        src_cells = _cells_in(root)
        want = {(x + dx, y + dy) for x, y in src_cells}
        got = _cells_in(out)
        if want != got:
            problems.append(f"{name}: cell files don't match ({len(want ^ got)} differ)")
        for kind, fmt in (("world_%d_%d.lotpack", None), ("chunkdata_%d_%d.bin", None)):
            for x, y in src_cells:
                if os.path.exists(os.path.join(root, kind % (x, y))) and \
                        not os.path.exists(os.path.join(out, kind % (x + dx, y + dy))):
                    problems.append(f"{name}: {kind % (x + dx, y + dy)} missing")
                    break
        bsrc = os.path.join(root, "maps")
        if os.path.isdir(bsrc):
            want = set()
            for f in os.listdir(bsrc):
                new = sh.shifted_cell_filename(f, dx, dy, in_maps_subdir=True)
                want.add(new or f)
            bout = os.path.join(out, "maps")
            got = set(os.listdir(bout)) if os.path.isdir(bout) else set()
            if want != got:
                problems.append(f"{name}/maps: biome map files don't match ({len(want ^ got)} differ)")
        for f in files:
            lf = f.lower()
            sp, op = os.path.join(root, f), os.path.join(out, f)
            if lf.endswith(".xml") and lf != "streets.xml":
                data = open(sp, "rb").read()
                if b"<cell" in data:
                    want = {(x + dx, y + dy) for x, y in _xml_cells(data)}
                    if _xml_cells(open(op, "rb").read()) != want:
                        problems.append(f"{name}/{f}: world map cells don't match")
            elif lf.endswith(".bin") and open(sp, "rb").read(4) == b"IGMB":
                if not os.path.exists(op):
                    continue  # dropped on purpose, reported in the build log
                try:
                    a = sh.parse_worldmap_bin(open(sp, "rb").read())
                    b = sh.parse_worldmap_bin(open(op, "rb").read())
                except sh.WorldMapBinError as e:
                    problems.append(f"{name}/{f}: {e}")
                    continue
                if [(x + dx, y + dy) for _, x, y in a] != [(x, y) for _, x, y in b]:
                    problems.append(f"{name}/{f}: binary world map cells don't match")
            elif lf.endswith(".zip"):
                try:
                    a = sh.read_pyramid_bounds(open(sp, "rb").read())
                except Exception:
                    continue
                if a:
                    b = sh.read_pyramid_bounds(open(op, "rb").read())
                    want = (a[0] + dx * CELL, a[1] + dy * CELL, a[2] + dx * CELL, a[3] + dy * CELL)
                    if b != want:
                        problems.append(f"{name}/{f}: map image bounds {b}, expected {want}")
            elif lf.endswith(".lua"):
                try:
                    ta = [t.text for t in tokenize(sh.decode_text(open(sp, "rb").read())[0])
                          if t.kind == "op"]
                    tb = [t.text for t in tokenize(sh.decode_text(open(op, "rb").read())[0])
                          if t.kind == "op"]
                except LuaTokenError as e:
                    problems.append(f"{name}/{f}: {e}")
                    continue
                # Only minus signs in front of shifted numbers may disappear.
                if [t for t in ta if t != "-"] != [t for t in tb if t != "-"]:
                    problems.append(f"{name}/{f}: Lua structure changed")
            elif lf == "map.info" and join:
                if "lots=" + VANILLA_MAP not in open(op, encoding="utf-8", errors="replace").read():
                    problems.append(f"{name}/map.info: lots=Muldraugh, KY not added")
    return problems


# ---------------------------------------------------------------- default.txt


def rewrite_modlist(text, final_ids, added_maps, front_maps=(), drop_maps=(), layers=None):
    """Rewrite the mods { } block to final_ids (same order as the file, with
    new ids appended) and append added_maps to the maps { } block. front_maps
    go first in the maps block (the first map wins a square), drop_maps and
    front_maps are taken out wherever else they are. layers ({map: layer})
    sorts the other map lines, higher layer first, ties kept in order; each
    line keeps its slot, so comments between them stay put. Every other line
    is kept as it is."""
    gone = set(front_maps) | set(drop_maps)
    slots = []   # (index in out, map name, line) of the map lines to sort
    front_done = not front_maps
    lines = text.splitlines(keepends=True)
    nl = "\r\n" if "\r\n" in text else "\n"
    out = []
    section = None
    indent = "    "
    mod_lines_done = False
    for line in lines:
        s = line.strip()
        m = re.match(r"^\s*(mods|maps)\s*$", line)
        if m:
            section = m.group(1)
            out.append(line)
            continue
        if section and s == "}":
            if section == "mods" and not mod_lines_done:
                for i in final_ids:
                    out.append(f"{indent}mod = {i},{nl}")
                mod_lines_done = True
            if section == "maps":
                for name in added_maps:
                    slots.append((len(out), name, f"{indent}map = {name},{nl}"))
                    out.append(None)
            section = None
            out.append(line)
            continue
        if section == "mods" and re.match(r"^\s*mod\s*=", line):
            indent = line[:len(line) - len(line.lstrip())]
            continue  # replaced as a block at the closing brace
        if section == "maps" and re.match(r"^\s*map\s*=", line):
            indent = line[:len(line) - len(line.lstrip())]
            name = re.match(r"^\s*map\s*=\s*(.*?),?\s*$", line).group(1)
            if name in gone:
                continue
            slots.append((len(out), name, line))
            out.append(None)
            continue
        out.append(line)
        if section == "maps" and s == "{" and not front_done:
            for name in front_maps:
                out.append(f"{indent}map = {name},{nl}")
            front_done = True
    if not mod_lines_done:
        raise BuildError("default.txt has no mods { } block")
    lines_of = {name: line for _, name, line in slots}
    names = [name for _, name, _ in slots]
    if layers:
        names = order_maps(names, layers)
    for (i, _, _), name in zip(slots, names):
        out[i] = lines_of[name]
    return "".join(out)


# ---------------------------------------------------------------- apply


def _build_links(cfg, state, plans, log, manual=(), no_go=()):
    """Route the road links and write their mod to a building folder.
    manual: links drawn by hand. Returns (links by group, building folder or
    None when there are none, the hand links as they stand now)."""
    dest = os.path.join(cfg["mods_dir"], ROADS_ID)
    man = read_manifest(dest)
    if os.path.exists(dest) and not (man and man.get("kind") == "roads"):
        raise BuildError(f"{dest} exists and isn't the map mover's road links mod")
    if not any(wants_roads(g, {**plan_for(g, plans), **(plans.get(g.key) or {})})
               for g in state["groups"]) and not manual:
        return {}, None, list(manual)
    log("Finding road links...")
    sampler, links = plan_links(state, plans, roads_vanilla_dir(cfg), log=log, manual=manual, no_go=no_go)
    kept = list(manual)
    if manual:
        log(f"Paving {len(manual)} link{'' if len(manual) == 1 else 's'} drawn by hand...")
        routes, kept = manual_routes(sampler, state, plans, manual, log=log)
        links[MANUAL_KEY] = {"routes": routes, "missed": []}
    for key, res in links.items():
        if key == MANUAL_KEY:
            continue
        n = len(res["routes"])
        log(f"{key}: {n} road link{'' if n == 1 else 's'}, "
            f"{sum(len(r['squares']) for r in res['routes'])} squares paved"
            + (f", {len(res['missed'])} road end{'' if len(res['missed']) == 1 else 's'} with nothing in reach"
               if res["missed"] else ""))
    cells = cells_of(links)
    if not cells:
        return links, None, kept
    building = dest + ".building"
    rmtree(building)
    write_mod(building, cells, sampler)
    return links, building, kept


def apply_plan(cfg, state, plans, add_ids=(), log=print, progress=None, dry_run=False, manual=()):
    """Do the whole Apply. manual: road links drawn by hand. Returns a
    summary dict; its "manual" is those links as they stand now (one routed
    again because a map moved keeps its new path)."""
    plan = make_plan(state, plans, add_ids)
    if plan["errors"]:
        for e in plan["errors"]:
            log("ERROR " + e)
        raise BuildError("; ".join(plan["errors"]))
    if not dry_run and game_running():
        raise BuildError("Project Zomboid is running. Close it first, the game reads these files at startup.")

    if not dry_run:
        clear_leftovers(cfg["mods_dir"], log)

    copies = plan["copies"]
    id_map = {oid: oid + MOVED_SUFFIX for oid in copies}

    # What is already built and still right? A copy only depends on the ids
    # its own mod.info names, so a change elsewhere doesn't force a rebuild.
    todo = []
    for oid, entry in copies.items():
        mod = entry["mod"]
        refs = referenced_ids(mod.folder) | {oid}
        entry["id_map"] = {k: v for k, v in id_map.items() if k in refs}
        dest = copy_name(cfg, mod)
        man = read_manifest(dest)
        sig = source_signature(mod.folder)
        entry["signature"] = sig
        entry["dest"] = dest
        if man and man.get("tool_version") == TOOL_VERSION and man.get("signature") == sig \
                and tuple(man.get("offset", ())) == tuple(entry["offset"]) \
                and bool(man.get("joined")) == entry["join"] \
                and man.get("id_map") == entry["id_map"]:
            log(f"{oid}: copy is up to date")
            continue
        todo.append(oid)

    need = sum(folder_size(copies[o]["mod"].folder) for o in todo)
    free = shutil.disk_usage(cfg["mods_dir"]).free if os.path.isdir(cfg["mods_dir"]) else 0
    if todo and need * 1.1 > free:
        raise BuildError(f"needs {need / 1e9:.1f} GB free in {cfg['mods_dir']}, only {free / 1e9:.1f} GB left")
    log(f"{len(todo)} copies to build ({need / 1e6:.0f} MB), {len(copies) - len(todo)} already up to date")

    done_bytes = [0]

    def tick(n):
        done_bytes[0] += n
        if progress:
            progress(done_bytes[0], need)

    reports = []
    built = []
    try:
        for oid in todo:
            entry = copies[oid]
            mod = entry["mod"]
            dx, dy = entry["offset"]
            group = next((g for g in state["groups"] if oid in g.originals), None)
            box = None
            if group and group.cells:
                xs = [c[0] for c in group.cells]
                ys = [c[1] for c in group.cells]
                box = ((min(xs) * CELL, (max(xs) + 1) * CELL), (min(ys) * CELL, (max(ys) + 1) * CELL))
            log(f"{oid}: building copy at offset {dx},{dy}" + (" and joining the vanilla world" if entry["join"] else ""))
            building, dest, report = build_copy(cfg, entry, id_map, box, tick)
            problems = verify_copy(mod.folder, building, dx, dy, entry["join"])
            if problems:
                for p in problems:
                    log(f"ERROR {oid}: {p}")
                rmtree(building)
                raise BuildError(f"{oid}: the copy failed its check, nothing was changed")
            manifest = {
                "tool": "zomboid-map-mover", "tool_version": TOOL_VERSION,
                "original_id": oid, "original_folder": mod.folder,
                "workshop_id": mod.workshop_id, "offset": list(entry["offset"]),
                "joined": entry["join"], "signature": entry["signature"],
                "id_map": entry["id_map"], "built": datetime.datetime.now().isoformat(timespec="seconds"),
                "counts": report.counts, "warnings": report.warnings, "dropped": report.dropped,
            }
            with open(os.path.join(building, MANIFEST), "w", encoding="utf-8") as f:
                json.dump(manifest, f, indent=2)
            for k, v in sorted(report.counts.items()):
                log(f"    {k}: {v}")
            for d in report.dropped:
                log(f"    dropped {d}")
            for w in report.warnings:
                log(f"    note: {w}")
            reports.append(report)
            built.append((building, dest))
            if dry_run:
                continue
    except Exception:
        for building, _ in built:
            rmtree(building)
        raise

    # Road links, worked out before anything is swapped in so a failure here
    # changes nothing.
    try:
        links, roads_building, manual = _build_links(cfg, state, plans, log, manual,
                                                     road_no_go(state, plans, plan))
    except Exception:
        for building, _ in built:
            rmtree(building)
        raise

    # The game's world load crash (loadorder.py), now with the real road cells.
    extra = []
    if roads_building:
        for name in MAP_NAMES:
            d = os.path.join(roads_building, "42", "media", "maps", name)
            if os.path.isdir(d):
                extra.append((name, set(MapDir(name, d).cells)))
    crash = crash_check(state, plans, plan, extra)
    for c in crash:
        log(f"WARNING the game will crash loading a new world: {c['top']} is on top at cells "
            f"{', '.join(f'{x},{y}' for x, y in c['cells'])} and the game then reads {c['asked']} there, "
            f"which has no such cell (a 42.x bug).")

    if dry_run:
        if roads_building:
            rmtree(roads_building)
        return {"plan": plan, "built": built, "reports": reports, "links": links, "manual": manual,
                "crashes": crash}

    # Swap the new copies in.
    for building, dest in built:
        old = dest + ".old"
        rmtree(old)
        if os.path.exists(dest):
            os.replace(dest, old)
        os.replace(building, dest)
        rmtree(old)

    roads_dest = os.path.join(cfg["mods_dir"], ROADS_ID)
    if roads_building:
        old = roads_dest + ".old"
        rmtree(old)
        if os.path.exists(roads_dest):
            os.replace(roads_dest, old)
        os.replace(roads_building, roads_dest)
        rmtree(old)
    elif os.path.exists(roads_dest):
        log("removing the road links mod, there are no road links")
        rmtree(roads_dest)
    final_ids = [i for i in plan["final_ids"] if i != ROADS_ID] + ([ROADS_ID] if roads_building else [])

    # default.txt
    path = os.path.join(cfg["mods_dir"], "default.txt")
    with open(path, "rb") as f:
        raw = f.read()
    text, bom, enc = sh.decode_text(raw)
    new_text = rewrite_modlist(text, final_ids, plan["added_maps"],
                               front_maps=MAP_NAMES if roads_building else (),
                               drop_maps=list(MAP_NAMES) + state.get("off_maps", []),
                               layers=map_layers(state["groups"], plans))
    if new_text != text:
        bdir = os.path.join(DATA_DIR, "backups")
        os.makedirs(bdir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        shutil.copyfile(path, os.path.join(bdir, f"default-{stamp}.txt"))
        tmp = path + ".mapmover.tmp"
        with open(tmp, "wb") as f:
            f.write(sh.encode_text(new_text, bom, enc))
        os.replace(tmp, path)
        log(f"default.txt updated (backup in data/backups/default-{stamp}.txt)")
    else:
        log("default.txt already right")

    # Remove copies nobody uses any more. A switched-off map keeps its copy,
    # so switching it back on needs no rebuild.
    keep = {os.path.normcase(copies[o]["dest"]) for o in copies}
    off = set(state.get("off_ids") or ())
    for m in state["mods"]:
        if m.manifest and m.manifest.get("tool") == "zomboid-map-mover" \
                and m.manifest.get("original_id") not in off \
                and os.path.normcase(m.folder) not in keep:
            log(f"removing unused copy {os.path.basename(m.folder)}")
            rmtree(m.folder)
    return {"plan": plan, "built": built, "reports": reports, "links": links, "manual": manual,
            "crashes": crash}
