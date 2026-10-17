"""Find every B42 mod that ships a map, and where each map's cells are.

Mod folders are resolved the way B42 does it (ZomboidFileSystem and
ChooseGameInfo): a mod folder has a "common" subfolder and version
subfolders such as "42" or "42.15". The game uses the highest version folder
that is not newer than the game, and reads media/maps from both "common"
and that version folder.
"""

import json
import os
import re

from .paths import version_int
from .lots import unreadable
from .shifters import LOT_NAME

MANIFEST = "mapmover.json"
MIN_VERSION = 42000


class MapDir:
    def __init__(self, name, path):
        self.name = name
        self.path = path
        self.info = read_kv(os.path.join(path, "map.info"))
        self.title = self.info.get("title", name)
        # One lots= line per entry, no splitting ("lots=Muldraugh, KY" is one name).
        self.lots = []
        info_path = os.path.join(path, "map.info")
        if os.path.isfile(info_path):
            with open(info_path, encoding="utf-8", errors="replace") as f:
                for line in f:
                    line = line.strip().lstrip("﻿")
                    if line.startswith("lots="):
                        self.lots.append(line[5:].strip())
        self.cells = []
        self.bad_cells = {}  # cell -> why the game can't read its lotheader
        for f in os.listdir(path):
            m = LOT_NAME.match(f)
            if m:
                c = (int(m.group(1)), int(m.group(2)))
                why = unreadable(os.path.join(path, f))
                if why:
                    self.bad_cells[c] = why
                else:
                    self.cells.append(c)
        self.cells.sort()

    def to_json(self):
        return {"name": self.name, "title": self.title, "lots": self.lots,
                "cells": len(self.cells), "path": self.path}


class Mod:
    def __init__(self, folder, source, workshop_id=None):
        self.folder = folder
        self.source = source  # "workshop" or "local"
        self.workshop_id = workshop_id
        self.version_dir_name = None
        self.info = {}
        self.id = None
        self.name = None
        self.require = []
        self.maps = []  # MapDir, the ones the game would load
        self.manifest = None  # set when this folder is one of our moved copies

    @property
    def cells(self):
        out = set()
        for m in self.maps:
            out.update(m.cells)
        return out

    def to_json(self):
        return {
            "id": self.id, "name": self.name, "source": self.source,
            "workshop_id": self.workshop_id, "folder": self.folder,
            "maps": [m.to_json() for m in self.maps],
            "require": self.require,
        }


def read_kv(path):
    """Read a key=value file (mod.info, map.info) like the game does."""
    out = {}
    if not os.path.isfile(path):
        return out
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip().lstrip("﻿")
            if "=" in line:
                k, v = line.split("=", 1)
                k = k.strip()
                if k == "description" and k in out:
                    out[k] += v
                elif k not in out:
                    out[k] = v.strip()
    return out


def pick_version_dir(folder, game_version_int):
    best_name = "42.0"
    best = MIN_VERSION
    try:
        names = os.listdir(folder)
    except OSError:
        return best_name
    for name in names:
        v = version_int(name)
        if best <= v <= game_version_int:
            best_name = name
            best = v
    return best_name


def load_mod(folder, source, workshop_id, game_version_int):
    vname = pick_version_dir(folder, game_version_int)
    vdir = os.path.join(folder, vname)
    cdir = os.path.join(folder, "common")
    info_path = os.path.join(vdir, "mod.info")
    if not os.path.isfile(info_path):
        info_path = os.path.join(cdir, "mod.info")
        if not os.path.isfile(info_path):
            return None  # not a B42 mod
    mod = Mod(folder, source, workshop_id)
    mod.version_dir_name = vname
    mod.info = read_kv(info_path)
    mod.id = mod.info.get("id") or os.path.basename(folder)
    mod.name = mod.info.get("name") or mod.id
    mod.require = [r.strip().lstrip("\\") for r in mod.info.get("require", "").replace("\\", "").split(",") if r.strip()]

    # Version folder wins over common for a map folder of the same name.
    seen = {}
    for base in (vdir, cdir):
        maps_root = os.path.join(base, "media", "maps")
        if not os.path.isdir(maps_root):
            continue
        for name in sorted(os.listdir(maps_root)):
            p = os.path.join(maps_root, name)
            if name in seen or not os.path.isfile(os.path.join(p, "map.info")):
                continue
            seen[name] = MapDir(name, p)
    mod.maps = list(seen.values())

    man = os.path.join(folder, MANIFEST)
    if os.path.isfile(man):
        try:
            with open(man, encoding="utf-8") as f:
                mod.manifest = json.load(f)
        except (OSError, ValueError):
            mod.manifest = {"broken": True}
    return mod


def scan_mods(cfg):
    gv = version_int(cfg["game_version"])
    mods = []
    ws = cfg.get("workshop_dir")
    if ws and os.path.isdir(ws):
        for item in sorted(os.listdir(ws)):
            mods_root = os.path.join(ws, item, "mods")
            if not os.path.isdir(mods_root):
                continue
            for sub in sorted(os.listdir(mods_root)):
                folder = os.path.join(mods_root, sub)
                if os.path.isdir(folder):
                    m = load_mod(folder, "workshop", item, gv)
                    if m:
                        mods.append(m)
    local = cfg["mods_dir"]
    if os.path.isdir(local):
        for sub in sorted(os.listdir(local)):
            folder = os.path.join(local, sub)
            if os.path.isdir(folder) and sub.lower() != "examplemod":
                m = load_mod(folder, "local", None, gv)
                if m:
                    mods.append(m)
    return mods


def scan_vanilla(cfg):
    out = []
    root = os.path.join(cfg["game_dir"], "media", "maps")
    if not os.path.isdir(root):
        return out
    for name in sorted(os.listdir(root)):
        p = os.path.join(root, name)
        if os.path.isfile(os.path.join(p, "map.info")):
            md = MapDir(name, p)
            if md.cells:
                out.append(md)
    return out


# ---------------------------------------------------------------- default.txt


_SECTION = re.compile(r"^(\s*)(mods|maps)\s*$")


def read_modlist(path):
    """Return (mod_ids, map_names) from Zomboid/mods/default.txt."""
    mods, maps = [], []
    if not os.path.isfile(path):
        return mods, maps
    section = None
    with open(path, encoding="utf-8", errors="replace") as f:
        for line in f:
            s = line.strip()
            m = _SECTION.match(line)
            if m:
                section = m.group(2)
                continue
            if s == "}":
                section = None
                continue
            km = re.match(r"^(mod|map)\s*=\s*(.*?),?\s*$", s)
            if km and section:
                val = km.group(2)
                if km.group(1) == "mod" and section == "mods":
                    mods.append(val.split("\\")[-1])
                elif km.group(1) == "map" and section == "maps":
                    maps.append(val)
    return mods, maps
