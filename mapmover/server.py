"""Local web app: python -m mapmover.server, then open http://127.0.0.1:18765

Only listens on 127.0.0.1. One Apply or test load can run at a time. If the port is taken
by another program, the next free one up to +9 is used. config.json "port"
picks a different start.
"""

import json
import mimetypes
import os
import subprocess
import sys
import threading
import time
import traceback
import uuid
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .build import BuildError, apply_plan, game_running, make_plan
from .layout import (MAX_CELL, MIN_CELL, VANILLA_MAP, build_state, in_vanilla_world, load_layout,
                     map_layers, order_maps, out_of_bounds, plan_for, save_layout)
from .loadorder import check as crash_check, road_no_go
from .overlay import group_roads, vanilla_overlay
from .overlay import VANILLA_DIR
from .lots import LotError
from .paths import DATA_DIR, ROOT, load_config
from .pyramid import Pyramid, PyramidError, find_pyramid
from . import worldrender
from .roads import MAP_NAMES, hand_end, link_options, plan_links, road_ends, wants_roads
from .roads import vanilla_dir as roads_vanilla_dir
from . import cells, testload, world, worldpng
from .world import WorldSampler

HOST, DEFAULT_PORT = "127.0.0.1", 18765
PORT = DEFAULT_PORT  # the port actually bound, set in main()
WEB = os.path.join(ROOT, "web")

_lock = threading.Lock()
_cfg = None
_state = None
_state_error = None
_job = {"running": False, "log": [], "done": 0, "total": 0, "error": None, "finished": False, "id": None,
        "kind": None, "result": None}
_stop = threading.Event()  # set to stop a test load
_ready = threading.Event()  # set once the first scan is done
APPLY_LOG = os.path.join(DATA_DIR, "apply.log")
TEST_LOG = os.path.join(DATA_DIR, "testload.log")
JOB_NAMES = {"apply": "an Apply", "test": "a test load", "png": "a world image"}
_overlay = {}  # game_dir -> vanilla overlay json
_roads = {}    # group key -> its own roads at the original spot
_links = {}    # layout signature -> road links preview
_ends = {}     # layout signature -> road ends of one map
_pyr = None    # {"list": [Pyramid], "dirs": [map dir], "json": {...}} built on first use after a scan
_scan_gen = 0  # bumped on every scan, part of tile URLs so old cached tiles don't match


def _clean_layout(raw):
    plans = {}
    for key, p in (raw.get("plans") or {}).items():
        try:
            off = [int(p.get("offset", [0, 0])[0]), int(p.get("offset", [0, 0])[1])]
        except (TypeError, ValueError, IndexError):
            continue
        plans[str(key)] = {"offset": off, "join": bool(p.get("join", False))}
        if p.get("roads"):
            plans[str(key)]["roads"] = True
        try:
            layer = int(p.get("layer", 1))
        except (TypeError, ValueError):
            layer = 1
        if layer != 1:
            plans[str(key)]["layer"] = max(1, min(99, layer))
    add_ids = [str(i) for i in (raw.get("add_ids") or []) if isinstance(i, str)]
    off = sorted({str(k) for k in (raw.get("off") or []) if isinstance(k, str)})
    links = [c for c in (_clean_link(l) for l in (raw.get("links") or []) if isinstance(l, dict)) if c]
    return {"plans": plans, "add_ids": add_ids, "off": off, "links": links}


def _num(v):
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise ValueError
    return int(v)


def _clean_point(p, end):
    """A link end: {"key", "hx", "hy"}, plus the road's "dir" (and for the
    first end its width and tiles) when it is a road end."""
    if not isinstance(p, dict):
        raise ValueError
    key = p.get("key")
    out = {"key": str(key) if key is not None else None, "hx": _num(p["hx"]), "hy": _num(p["hy"])}
    if key is None and p.get("cut"):
        out["cut"] = True
    d = p.get("dir")
    if d is not None:
        d = [_num(d[0]), _num(d[1])]
        if abs(d[0]) + abs(d[1]) != 1:
            raise ValueError
        out["dir"] = d
        if p.get("inner") and out["key"] is not None:
            out["inner"] = True
        if not end and p.get("width") is not None:
            out["width"] = max(1, min(_num(p["width"]), 16))   # the road to arrive at
    if end:
        if "dir" not in out:
            raise ValueError
        out["width"] = max(1, min(_num(p["width"]), 16))
        out["tile"] = str(p["tile"])
        out["tiles"] = {str(k): max(1, _num(v)) for k, v in (p.get("tiles") or {out["tile"]: 1}).items()}
    return out


def _clean_link(raw):
    try:
        link = {"id": str(raw.get("id") or "")[:16], "a": _clean_point(raw["a"], True),
                "b": _clean_point(raw["b"], False)}
        link["at"] = {str(k): [_num(v[0]), _num(v[1])] for k, v in (raw.get("at") or {}).items()}
        link["path"] = [[_num(q[0]), _num(q[1])] for q in (raw.get("path") or [])]
        a = link["a"]
        link.update(width=a["width"], tile=a["tile"], tiles=a["tiles"],
                    squares=_num(raw.get("squares", 0)), length=_num(raw.get("length", 0)),
                    bridge=_num(raw.get("bridge", 0)))
        return link
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return None


def rescan(light=False):
    """Read the mods again. light: only maps were switched on or off, so the
    files are the same and the terrain images and their ids stay."""
    global _cfg, _state, _state_error, _pyr, _scan_gen
    cfg = load_config()
    layout = _clean_layout(load_layout())
    try:
        if not os.path.isfile(os.path.join(cfg["mods_dir"], "default.txt")):
            raise BuildError(f"No mod list found at {cfg['mods_dir']}\\default.txt. "
                             "Start the game once and enable some mods first.")
        st = build_state(cfg, layout["add_ids"], layout["off"])
        err = None
    except Exception as e:
        traceback.print_exc()
        st, err = None, str(e)
    with _lock:
        _cfg, _state, _state_error = cfg, st, err
        _ends.clear()
        _links.clear()
        if light and st:
            return
        _roads.clear()
        world.clear_cache()
        _ends.clear()
        _links.clear()
        _pyr = None
        _scan_gen += 1
    _ready.set()
    if st:
        cells.warm(_warm_jobs(cfg, st, layout))


def _warm_jobs(cfg, st, layout):
    """Every cell the page or the road search may want, the ones seen first
    first: maps with no terrain image (classes and image), then the other
    maps in Kentucky, then vanilla."""
    first, later = [], []
    for g in st["groups"]:
        kentucky = in_vanilla_world(g, plan_for(g, layout["plans"]))
        for m in g.originals.values():
            for d in m.maps:
                if d.name == VANILLA_MAP or not d.cells:
                    continue
                if not find_pyramid(d.path):
                    first += [(d.path, x, y, True) for x, y in d.cells]
                elif kentucky:
                    later += [(d.path, x, y, False) for x, y in d.cells]
    vdir = roads_vanilla_dir(cfg)
    later += [(vdir, x, y, False) for x, y in st.get("vanilla_cells") or ()]
    return first + later


def overlay_json():
    with _lock:
        game_dir = _cfg["game_dir"] if _cfg else None
        cached = _overlay.get(game_dir)
    if cached is None:
        try:
            cached = vanilla_overlay(game_dir) if game_dir else {}
        except Exception as e:
            traceback.print_exc()
            cached = {"error": f"Couldn't read the vanilla map: {e}"}
        with _lock:
            _overlay[game_dir] = cached
    return cached


def roads_json(key):
    with _lock:
        st = _state
        cached = _roads.get(key)
    if cached is not None:
        return cached
    g = next((g for g in (st["groups"] if st else []) if g.key == key), None)
    if not g:
        return None
    dirs = [d.path for m in g.originals.values() for d in m.maps if d.name != VANILLA_MAP]
    out = group_roads(dirs)
    with _lock:
        _roads[key] = out
    return out


def pyramids():
    """Terrain images: vanilla's and each group's, by id. Ids are list indexes
    and change on rescan, so the page asks again after one.

    A map dir without a pyramid.zip gets {"lots": id, "cells": [...]}: the page
    asks /api/ltile for each cell, drawn from its lot files.
    """
    global _pyr
    with _lock:
        if _pyr is not None:
            return _pyr
        cfg, st, gen = _cfg, _state, _scan_gen
    found, dirs, out = [], [], {"vanilla": None, "groups": {}, "gen": gen}

    def add(path):
        try:
            p = Pyramid(path)
        except (PyramidError, OSError, ValueError) as e:
            print(f"Skipping terrain image {path}: {e}")
            return None
        found.append(p)
        return dict(p.to_json(), id=len(found) - 1)

    if cfg:
        v = find_pyramid(os.path.join(cfg["game_dir"], "media", "maps", VANILLA_DIR))
        if v:
            out["vanilla"] = add(v)
    # On and off maps together, in title order, so switching one keeps the ids.
    for g in sorted(st["groups"] + st["off_groups"] if st else [], key=lambda g: g.title().lower()):
        imgs = []
        for m in g.originals.values():
            for d in m.maps:
                if d.name == VANILLA_MAP:
                    continue
                path = find_pyramid(d.path)
                j = add(path) if path else None
                if j:
                    imgs.append(j)
                elif d.cells:
                    dirs.append(d.path)
                    imgs.append({"lots": len(dirs) - 1, "cells": d.cells})
        if imgs:
            out["groups"][g.key] = imgs
    with _lock:
        _pyr = {"list": found, "dirs": dirs, "json": out}
    return _pyr


def lot_tile(i, x, y, z):
    """PNG of one cell drawn from lot files, or None."""
    dirs = pyramids()["dirs"]
    if not 0 <= i < len(dirs):
        return None
    try:
        return cells.tile(dirs[i], x, y, z)
    except (LotError, OSError) as e:
        print(f"Couldn't draw cell {x},{y} of {dirs[i]}: {e}")
        return None


def crashes_json(st, layout):
    """Where the game would crash loading a new world after Apply (see
    loadorder.py), with each folder's map title. The road links mod is left
    out here; Apply checks again once it has routed them."""
    if not st:
        return []
    plan = make_plan(st, layout["plans"], layout["add_ids"])
    found = crash_check(st, layout["plans"], plan)
    by_name = {n: g for g in st["groups"] for n in g.map_names}
    for c in found:
        for side in ("top", "asked"):
            g = by_name.get(c[side])
            c[side + "_key"] = g.key if g else None
            c[side + "_title"] = g.title() if g else ("the vanilla map" if c[side] == VANILLA_MAP else c[side])
    return found


def state_json():
    _ready.wait(600)
    with _lock:
        cfg, st, err = _cfg, _state, _state_error
    layout = _clean_layout(load_layout())
    out = {
        "game_version": cfg["game_version"],
        "version_guessed": bool(cfg.get("game_version_guessed")),
        "mods_dir": cfg["mods_dir"],
        "max_cell": MAX_CELL,
        "min_cell": MIN_CELL,
        "error": err,
        "layout": layout,
        "game_running": game_running(),
    }
    if not st:
        out.update(groups=[], vanilla=[], inactive=[])
        return out
    groups = []
    pos = {name: i for i, name in enumerate(st["active_maps"])}
    for g in st["groups"]:
        j = g.to_json()
        # Where it sits in maps { } now: on the same layer, earlier wins.
        j["order"] = min((pos.get(n, len(pos)) for n in g.map_names), default=len(pos))
        j["thumb"] = any(os.path.isfile(os.path.join(d.path, "thumb.png"))
                         for m in g.originals.values() for d in m.maps if d.name != VANILLA_MAP)
        groups.append(j)
    out["groups"] = groups
    out["off_groups"] = [g.to_json() for g in st["off_groups"]]
    out["vanilla"] = sorted(st["vanilla_cells"])
    in_groups = {oid for g in st["groups"] + st["off_groups"] for oid in g.originals}
    out["inactive"] = [m for m in st["inactive"] if m["id"] not in in_groups]
    out["crashes"] = crashes_json(st, layout)
    out["maps_rebuilt"] = st.get("maps_rebuilt", False)
    return out


def links_json(layout, key):
    """Road links one group would get under this layout, for drawing.
    Remembers the last few answers: the search takes seconds."""
    with _lock:
        cfg, st = _cfg, _state
    if not st:
        return {"error": "Nothing scanned yet."}
    g = next((g for g in st["groups"] if g.key == key), None)
    if not g:
        return {"error": "No such map."}
    plans = dict(layout["plans"])
    plans[key] = {**plan_for(g, plans), **(plans.get(key) or {}), "roads": True}
    if not wants_roads(g, plans[key]):
        return {"routes": [], "missed": [], "note": "Only maps moved within the Kentucky world get road links."}
    sig = json.dumps([key, plans, layout["links"], _scan_gen], sort_keys=True, default=list)
    with _lock:
        hit = _links.get(sig)
    if hit is not None:
        return hit
    notes = []
    _, links = plan_links(st, plans, roads_vanilla_dir(cfg), keys={key}, log=notes.append,
                          manual=layout["links"], no_go=_no_go(st, layout))
    res = links.get(key, {"routes": [], "missed": []})
    out = {"routes": [{k: v for k, v in r.items() if k != "squares"} | {"squares": len(r["squares"])}
                      for r in res["routes"]],
           "missed": res["missed"], "notes": notes}
    with _lock:
        if len(_links) > 20:
            _links.clear()
        _links[sig] = out
    return out


def _no_go(st, layout):
    return road_no_go(st, layout["plans"], make_plan(st, layout["plans"], layout["add_ids"]))


def _sampler(cfg, st, layout):
    s = WorldSampler(st, layout["plans"], roads_vanilla_dir(cfg))
    s.no_go = _no_go(st, layout)
    return s


def ends_json(layout, keys, loose=False):
    """Road ends of each map at its planned spot, for picking links by hand.
    Points come in home squares (hx, hy) too, so the page can move them
    with the map while it is dragged. loose looks harder."""
    with _lock:
        cfg, st = _cfg, _state
    if not st:
        return {"error": "Nothing scanned yet."}
    out = {}
    sampler = None
    for key in keys[:12]:
        sig = json.dumps([key, layout["plans"], _scan_gen, loose], sort_keys=True, default=list)
        with _lock:
            hit = _ends.get(sig)
        if hit is None:
            sampler = sampler or _sampler(cfg, st, layout)
            try:
                hit = {"ends": road_ends(sampler, st, layout["plans"], key, roads_vanilla_dir(cfg), loose)}
            except ValueError as e:
                hit = {"ends": [], "error": str(e)}
            with _lock:
                if len(_ends) > 60:
                    _ends.clear()
                _ends[sig] = hit
        out[key] = hit
    return {"maps": out}


def end_json(layout, key, hx, hy):
    """A road end made by hand on a map's road near home square hx, hy."""
    with _lock:
        cfg, st = _cfg, _state
    if not st:
        return {"error": "Nothing scanned yet."}
    try:
        hx, hy = _num(hx), _num(hy)
    except (TypeError, ValueError):
        return {"error": "That spot doesn't look right."}
    e, err = hand_end(_sampler(cfg, st, layout), st, layout["plans"], str(key), roads_vanilla_dir(cfg), hx, hy)
    return {"error": err} if err else {"end": e}


def connect_json(layout, a, b):
    """Roads to pick from for a link by hand from road end a to b, best first."""
    with _lock:
        cfg, st = _cfg, _state
    if not st:
        return {"error": "Nothing scanned yet."}
    try:
        a, b = _clean_point(a, True), _clean_point(b, False)
    except (KeyError, TypeError, ValueError, IndexError, AttributeError):
        return {"error": "Those points don't look right. Pick the road ends again."}
    links, err = link_options(_sampler(cfg, st, layout), st, layout["plans"], a, b)
    return {"error": err} if err else {"links": links}


def preview(layout):
    """What Apply would do, without doing it."""
    with _lock:
        st = _state
    if not st:
        return {"errors": ["Nothing scanned yet."], "copies": [], "changes": []}
    plan = make_plan(st, layout["plans"], layout["add_ids"])
    copies = []
    for oid, e in sorted(plan["copies"].items()):
        copies.append({"id": oid, "name": e["mod"].name, "offset": list(e["offset"]), "join": e["join"]})
    before = list(st.get("listed_ids", st["active_ids"]))
    changes = [i for i in plan["final_ids"] if i not in before]
    removed = [i for i in before if i not in plan["final_ids"]]
    links = [g.title() for g in st["groups"]
             if wants_roads(g, {**plan_for(g, layout["plans"]), **(layout["plans"].get(g.key) or {})})]
    layers = map_layers(st["groups"], layout["plans"])
    now = [n for n in st["active_maps"] if n not in MAP_NAMES] + plan["added_maps"]
    layered = sorted(((plan_for(g, layout["plans"])["layer"], g.title()) for g in st["groups"]
                      if plan_for(g, layout["plans"])["layer"] != 1), reverse=True)
    reorder = order_maps(now, layers) != now
    return {"errors": plan["errors"], "copies": copies, "added": changes, "removed": removed,
            "layers": [[t, n] for n, t in layered], "reorder": reorder,
            "added_maps": plan["added_maps"], "links": links, "manual": len(layout["links"]),
            "off": [g.title() for g in st["off_groups"] if g.active],
            "crashes": crashes_json(st, layout)}


def _run_apply(layout):
    # The window has no console, so Apply also writes to data/apply.log.
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        logf = open(APPLY_LOG, "a", encoding="utf-8")
        logf.write(f"\n==== Apply {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except OSError:
        logf = None

    def log(line):
        with _lock:
            _job["log"].append(line)
        print(line)
        if logf:
            try:
                logf.write(line + "\n")
                logf.flush()
            except OSError:
                pass

    def progress(done, total):
        with _lock:
            _job["done"], _job["total"] = done, total

    try:
        log("Rescanning mods...")
        cfg = load_config()
        st = build_state(cfg, layout["add_ids"], layout["off"])
        keys = {g.key for g in st["groups"]}
        plans = {k: v for k, v in layout["plans"].items() if k in keys}
        res = apply_plan(cfg, st, plans, layout["add_ids"], log=log, progress=progress, manual=layout["links"])
        # Added mods are active now, so they leave the add list. Hand links
        # routed again on the way keep their new paths.
        saved = _clean_layout(load_layout())
        saved["add_ids"] = [i for i in saved["add_ids"] if i not in layout["add_ids"]]
        fresh = {l["id"]: l for l in res.get("manual") or []}
        saved["links"] = [_clean_link(fresh.get(l["id"], l)) or l for l in saved["links"]]
        save_layout(saved)
        n = len(res["built"])
        log(f"Done. {n} cop{'y' if n == 1 else 'ies'} built. Start a NEW save to see moved maps; "
            "old saves keep the map where it was.")
    except BuildError as e:
        log(f"Stopped: {e}")
        with _lock:
            _job["error"] = str(e)
    except Exception as e:
        log("Stopped by an unexpected error:")
        for line in traceback.format_exc().splitlines():
            log("  " + line)
        with _lock:
            _job["error"] = f"{type(e).__name__}: {e}"
    finally:
        rescan()
        with _lock:
            _job["running"] = False
            _job["finished"] = True
        if logf:
            logf.close()


def _run_png(scale, names):
    """worldpng in the background: the world as one PNG with map names."""
    def log(line):
        with _lock:
            _job["log"].append(line)
        print(line)

    def progress(done, total):
        with _lock:
            _job["done"], _job["total"] = done, total

    try:
        cfg = load_config()
        r = worldpng.export(cfg, worldpng.default_path(cfg), scale=scale, names=names,
                            log=log, progress=progress, stop=_stop)
        with _lock:
            _job["result"] = r
    except Exception as e:
        if _stop.is_set():
            log("Stopped.")
        else:
            log("Stopped by an unexpected error:")
            for line in traceback.format_exc().splitlines():
                log("  " + line)
        with _lock:
            _job["error"] = "stopped" if _stop.is_set() else f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _job["running"] = False
            _job["finished"] = True


def _run_test():
    """testload in the background: mirror the mods, start the hidden
    dedicated server on the mod list as it is now, report how it went."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        logf = open(TEST_LOG, "w", encoding="utf-8")
        logf.write(f"==== Test load {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
    except OSError:
        logf = None

    def log(line):
        with _lock:
            _job["log"].append(line)
        print(line)
        if logf:
            try:
                logf.write(line + "\n")
                logf.flush()
            except OSError:
                pass

    try:
        cfg = load_config()
        root = testload.cache_root(cfg)
        log(f"Copying the active mods into {root} (only what changed since last time)...")
        missing = testload.prepare(cfg, root, log=log)
        if missing:
            log("Not installed, left out: " + ", ".join(missing))
        ok, lines = testload.run(cfg, root, log=log, stop=_stop)
        log("World loaded" if ok else "World load FAILED")
        for line in lines:
            log("  " + line[:300])
        with _lock:
            _job["result"] = {"ok": ok, "lines": lines, "stopped": _stop.is_set()}
    except Exception as e:
        log("Stopped by an unexpected error:")
        for line in traceback.format_exc().splitlines():
            log("  " + line)
        with _lock:
            _job["error"] = f"{type(e).__name__}: {e}"
    finally:
        with _lock:
            _job["running"] = False
            _job["finished"] = True
        if logf:
            logf.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "MapMover/1"

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, body, ctype="application/json; charset=utf-8", cache=False):
        if isinstance(body, (dict, list)):
            body = json.dumps(body).encode("utf-8")
        elif isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        # Tile URLs carry the scan number, so a cached tile is never a stale one.
        self.send_header("Cache-Control", "max-age=600" if cache else "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > 5_000_000:
            raise ValueError("request too large")
        raw = self.rfile.read(n) if n else b"{}"
        return json.loads(raw.decode("utf-8") or "{}")

    def _same_origin(self):
        # Block other websites from posting to this local server.
        origin = self.headers.get("Origin")
        return origin in (None, f"http://{HOST}:{PORT}", f"http://localhost:{PORT}")

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        try:
            if url.path == "/api/ping":
                return self._send(200, {"app": "mapmover"})
            if url.path == "/api/state":
                return self._send(200, state_json())
            if url.path == "/api/overlay":
                return self._send(200, overlay_json())
            if url.path == "/api/roads":
                key = urllib.parse.parse_qs(url.query).get("key", [""])[0]
                r = roads_json(key)
                return self._send(200, r) if r is not None else self._send(404, {"error": "no such map"})
            if url.path == "/api/pyramids":
                return self._send(200, pyramids()["json"])
            if url.path == "/api/ptile":
                q = urllib.parse.parse_qs(url.query)
                try:
                    i, z, x, y = (int(q.get(k, ["-1"])[0]) for k in ("id", "z", "x", "y"))
                except ValueError:
                    return self._send(400, {"error": "bad tile"})
                lst = pyramids()["list"]
                data = lst[i].tile(z, x, y) if 0 <= i < len(lst) else None
                if data is None:
                    return self._send(404, {"error": "no such tile"})
                return self._send(200, data, "image/png", cache=True)
            if url.path == "/api/ltile":
                q = urllib.parse.parse_qs(url.query)
                try:
                    i, z, x, y = (int(q.get(k, ["-1"])[0]) for k in ("id", "z", "x", "y"))
                except ValueError:
                    return self._send(400, {"error": "bad tile"})
                data = lot_tile(i, x, y, z)
                if data is None:
                    return self._send(404, {"error": "no such cell"})
                return self._send(200, data, "image/png", cache=True)
            if url.path == "/api/progress":
                with _lock:
                    j = dict(_job)
                    j["log"] = list(_job["log"])
                return self._send(200, j)
            if url.path == "/api/thumb":
                key = urllib.parse.parse_qs(url.query).get("key", [""])[0]
                with _lock:
                    st = _state
                g = next((g for g in (st["groups"] if st else []) if g.key == key), None)
                if g:
                    for m in g.originals.values():
                        for d in m.maps:
                            p = os.path.join(d.path, "thumb.png")
                            if d.name != VANILLA_MAP and os.path.isfile(p):
                                with open(p, "rb") as f:
                                    return self._send(200, f.read(), "image/png")
                return self._send(404, {"error": "no thumbnail"})
            if url.path == "/world" or url.path.startswith("/world/"):
                return self._world(url.path[len("/world"):])
            return self._static(url.path)
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})

    def _world(self, path):
        # The isometric render's viewer and tiles (mapmover/worldrender.py).
        root = os.path.join(worldrender.home(load_config()), "out", "html")
        if path in ("", "/"):
            path = "/pzmap.html"
        full = os.path.normpath(os.path.join(root, path.lstrip("/")))
        if not full.startswith(root + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not rendered"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript") or ctype.endswith("json"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype, cache=full.endswith((".jpg", ".webp", ".png")))

    def _static(self, path):
        if path in ("", "/"):
            path = "/index.html"
        full = os.path.normpath(os.path.join(WEB, path.lstrip("/")))
        if not full.startswith(WEB + os.sep) or not os.path.isfile(full):
            return self._send(404, {"error": "not found"})
        ctype = mimetypes.guess_type(full)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype.endswith("javascript"):
            ctype += "; charset=utf-8"
        with open(full, "rb") as f:
            return self._send(200, f.read(), ctype)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        if not self._same_origin():
            return self._send(403, {"error": "wrong origin"})
        try:
            body = self._body()
            if url.path == "/api/layout":
                layout = _clean_layout(body)
                old = _clean_layout(load_layout())
                save_layout(layout)
                if layout["add_ids"] != old["add_ids"]:
                    rescan()
                elif layout["off"] != old["off"]:
                    rescan(light=True)
                with _lock:
                    st = _state
                return self._send(200, {"ok": True, "crashes": crashes_json(st, layout)})
            if url.path == "/api/links":
                return self._send(200, links_json(_clean_layout(body.get("layout") or {}), str(body.get("key", ""))))
            if url.path == "/api/ends":
                keys = [str(k) for k in (body.get("keys") or []) if isinstance(k, str)]
                return self._send(200, ends_json(_clean_layout(body.get("layout") or {}), keys,
                                                 bool(body.get("loose"))))
            if url.path == "/api/end":
                return self._send(200, end_json(_clean_layout(body.get("layout") or {}), body.get("key"),
                                                body.get("hx"), body.get("hy")))
            if url.path == "/api/connect":
                return self._send(200, connect_json(_clean_layout(body.get("layout") or {}),
                                                    body.get("a"), body.get("b")))
            if url.path == "/api/preview":
                return self._send(200, preview(_clean_layout(body)))
            if url.path == "/api/rescan":
                with _lock:
                    busy = _job["running"]
                if busy:
                    return self._send(409, {"error": f"{JOB_NAMES[_job['kind']]} is running"})
                rescan()
                return self._send(200, {"ok": True})
            if url.path == "/api/apply":
                layout = _clean_layout(body)
                with _lock:
                    if _job["running"]:
                        return self._send(409, {"error": f"{JOB_NAMES[_job['kind']]} is already running"})
                    job = uuid.uuid4().hex
                    _job.update(running=True, log=[], done=0, total=0, error=None, finished=False, id=job,
                                kind="apply", result=None)
                save_layout(layout)
                threading.Thread(target=_run_apply, args=(layout,), daemon=True).start()
                return self._send(200, {"ok": True, "id": job})
            if url.path == "/api/testload":
                with _lock:
                    if _job["running"]:
                        return self._send(409, {"error": f"{JOB_NAMES[_job['kind']]} is already running"})
                    job = uuid.uuid4().hex
                    _job.update(running=True, log=[], done=0, total=0, error=None, finished=False, id=job,
                                kind="test", result=None)
                _stop.clear()
                threading.Thread(target=_run_test, daemon=True).start()
                return self._send(200, {"ok": True, "id": job})
            if url.path == "/api/worldpng":
                miss = worldpng.missing_libs()
                if miss:
                    return self._send(400, {"error": "needs " + " and ".join(miss)
                                                     + ". Run: pip install " + " ".join(miss)})
                scale = body.get("scale", 1)
                scale = scale if scale in (1, 2, 4) else 1
                with _lock:
                    if _job["running"]:
                        return self._send(409, {"error": f"{JOB_NAMES[_job['kind']]} is already running"})
                    job = uuid.uuid4().hex
                    _job.update(running=True, log=[], done=0, total=0, error=None, finished=False, id=job,
                                kind="png", result=None)
                _stop.clear()
                threading.Thread(target=_run_png, args=(scale, body.get("names", True) is not False),
                                 daemon=True).start()
                return self._send(200, {"ok": True, "id": job})
            if url.path == "/api/worldpng/show":
                # Only ever the image the last export wrote.
                with _lock:
                    r = _job["result"] if _job["kind"] == "png" else None
                path = r and r.get("path")
                if not path or not os.path.isfile(path):
                    return self._send(404, {"error": "no image yet"})
                if body.get("folder"):
                    subprocess.Popen(["explorer", "/select,", os.path.normpath(path)])
                else:
                    os.startfile(path)
                return self._send(200, {"ok": True})
            if url.path == "/api/testload/stop":
                _stop.set()
                return self._send(200, {"ok": True})
            return self._send(404, {"error": "not found"})
        except Exception as e:
            traceback.print_exc()
            return self._send(500, {"error": str(e)})


def _is_mapmover(port):
    try:
        with urllib.request.urlopen(f"http://{HOST}:{port}/api/ping", timeout=1.5) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "mapmover"
    except Exception:
        return False


def start():
    """Bind a port and serve on a background thread. The first scan runs on
    its own thread and /api/state waits for it. Returns (url, ours): ours is
    False when a map mover was already running and url points at that one.
    (None, message) when every port is busy."""
    global PORT
    cfg = load_config()
    first = int(cfg.get("port") or DEFAULT_PORT)
    for port in range(first, first + 10):
        if _is_mapmover(port):
            return f"http://{HOST}:{port}", False
        try:
            srv = ThreadingHTTPServer((HOST, port), Handler)
        except OSError:
            print(f"Port {port} is used by another program, trying {port + 1}.")
            continue
        PORT = port
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        threading.Thread(target=_first_scan, daemon=True).start()
        return f"http://{HOST}:{port}", True
    return None, f'Ports {first} to {first + 9} are all busy. Set "port" in config.json.'


def _first_scan():
    print("Scanning mods, this takes a few seconds...")
    rescan()
    if _state_error:
        print("Scan problem:", _state_error)
    else:
        print(f"Found {len(_state['groups'])} map groups.")


def job_running():
    with _lock:
        return _job["running"]


def main():
    """Server only, for run.bat and testing. It opens a browser tab only
    with --browser, which run.bat passes, so a test server never pops a page
    up next to the desktop window (mapmover.desktop)."""
    browser = "--browser" in sys.argv
    url, ours = start()
    if url is None:
        print(ours)
        return 1
    if not ours:
        print(f"The map mover is already running at {url}. Opening it.")
        if browser:
            webbrowser.open(url)
        return
    _ready.wait()
    print(f"Map mover running at {url}  (close this window to stop it)")
    if browser:
        webbrowser.open(url)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    sys.exit(main())
