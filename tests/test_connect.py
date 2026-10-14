import os
import shutil
import tempfile
import unittest


os.environ["MAPMOVER_CELL_CACHE"] = "0"   # keep test cells out of the real cache
from mapmover.build import rewrite_modlist
from mapmover.connect import find_exits, pave_path, road_kind, route, route_between
from mapmover.layout import Group
from mapmover.lots import LotHeader, LotPack, read_header, write_header, write_pack
from mapmover.roads import MAP_NAMES, cells_of, make_link, manual_routes, plan_links, road_ends, write_mod
from mapmover.world import BLOCKED, OPEN, ROAD, TREE, WATER, WorldSampler, classify

GRASS, STREET, WALL = "blends_natural_01_16", "blends_street_01_48", "walls_exterior_01_0"


def write_cell(folder, cx, cy, squares, rooms=()):
    """squares: {(x, y): [tile names]}, cell-relative."""
    names = sorted({n for t in squares.values() for n in t})
    idx = {n: i for i, n in enumerate(names)}
    h = LotHeader(tiles=names, rooms=list(rooms))
    with open(os.path.join(folder, f"{cx}_{cy}.lotheader"), "wb") as f:
        f.write(write_header(h))
    with open(os.path.join(folder, f"world_{cx}_{cy}.lotpack"), "wb") as f:
        f.write(write_pack({(x, y, 0): [idx[n] for n in t] for (x, y), t in squares.items()}))


def read(path, mode="rb"):
    with open(path, mode) as f:
        return f.read()


class FakeDir:
    def __init__(self, name, path, cells):
        self.name, self.path, self.cells, self.lots = name, path, cells, []


class FakeMod:
    def __init__(self, mid, maps):
        self.id, self.name, self.maps = mid, mid, maps


class World(unittest.TestCase):
    """Vanilla: cells (0,0) and (1,0), grass with a north-south road at x 300..304.
    A town map at cell (5,5) with an east-west road at y 100..104 across the
    whole cell. Moved by (-5,-5) its east end stops 44 squares short of it."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        v = os.path.join(cls.tmp, "vanilla")
        t = os.path.join(cls.tmp, "town")
        os.makedirs(v)
        os.makedirs(t)
        for cx in (0, 1):
            sq = {(x, y): [GRASS] for x in range(256) for y in range(256)}
            if cx == 1:
                for x in range(44, 49):
                    for y in range(256):
                        sq[(x, y)] = [STREET]
            write_cell(v, cx, 0, sq)
        sq = {}
        for x in range(256):
            for y in range(100, 105):
                sq[(x, y)] = [STREET]
        for x in range(10, 20):         # a house beside the road
            for y in range(110, 120):
                sq[(x, y)] = [GRASS, WALL]
        write_cell(t, 5, 5, sq)
        g = Group("town")
        g.originals = {"town": FakeMod("town", [FakeDir("town", t, [(5, 5)])])}
        g.cells = {(5, 5)}
        cls.state = {"groups": [g], "active_maps": ["town"], "vanilla_cells": {(0, 0), (1, 0)}}
        cls.vdir = v
        plans = {"town": {"offset": [-5, -5], "join": True}}
        cls.sampler = WorldSampler(cls.state, plans, v)
        cls.home = WorldSampler(cls.state, {"town": {"offset": [0, 0], "join": True}}, None, only="town")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def test_classify(self):
        self.assertEqual(classify([STREET], False), ROAD)
        self.assertEqual(classify([GRASS, WALL], False), BLOCKED)
        self.assertEqual(classify([GRASS, "e_americanholly_1_tree"], False), TREE)
        self.assertEqual(classify(["blends_natural_02_0"], False), WATER)
        self.assertEqual(classify([GRASS], True), BLOCKED)
        self.assertEqual(classify([GRASS], False), OPEN)

    def test_priority(self):
        self.assertEqual(self.sampler.owner(0, 0), "town")
        self.assertEqual(self.sampler.code(5, 102), ROAD)      # the town's road
        self.assertEqual(self.sampler.code(5, 50), OPEN)       # vanilla grass shows through
        self.assertEqual(self.sampler.code(15, 115), BLOCKED)

    def test_road_kind(self):
        self.assertEqual(road_kind("blends_street_01_85"), "blends_street_01:5")
        self.assertEqual(road_kind("blends_street_01_48"), road_kind("blends_street_01_55"))

    def test_exits_and_route(self):
        exits = find_exits(self.sampler, self.home, "town", (-5, -5))
        east = [e for e in exits if e["dir"] == (1, 0)]
        self.assertEqual(len(east), 1)
        e = east[0]
        self.assertEqual((e["x"], e["y"], e["width"], e["tile"]), (256, 102, 5, STREET))
        r = route(self.sampler, "town", e, radius=96)
        self.assertIsNotNone(r)
        self.assertEqual(r["target"], (300, 102))
        self.assertEqual(r["path"], [(256, 102), (300, 102)])
        self.assertEqual(set(r["squares"]), {(x, y) for x in range(256, 300) for y in range(100, 105)})
        self.assertEqual(set(r["squares"].values()), {STREET})

    def test_no_road_in_reach(self):
        west = [e for e in find_exits(self.sampler, self.home, "town", (-5, -5)) if e["dir"] == (-1, 0)]
        self.assertEqual(len(west), 1)
        self.assertIsNone(route(self.sampler, "town", west[0], radius=40))

    def test_still_connected_is_no_exit(self):
        # At its home spot over nothing, the road runs off both edges: two exits.
        # Shifted so the east end lands on the vanilla road, that one is joined already.
        plans = {"town": {"offset": [-4, -5], "join": True}}
        s = WorldSampler(self.state, plans, self.vdir)
        # x offset -4 puts the town at cell 1: its road crosses x 256..511,
        # vanilla's road at 300..304 sits under it, the ends at 255 and 512.
        exits = find_exits(s, self.home, "town", (-4, -5))
        self.assertEqual(sorted(e["dir"] for e in exits), [(-1, 0), (1, 0)])

    def test_write_mod(self):
        r = route(self.sampler, "town", [e for e in find_exits(self.sampler, self.home, "town", (-5, -5))
                                         if e["dir"] == (1, 0)][0], radius=96)
        links = {"town": {"routes": [{"squares": r["squares"]}], "missed": []}}
        cells = cells_of(links)
        self.assertEqual(set(cells), {(1, 0)})
        out = os.path.join(self.tmp, "mod")
        write_mod(out, cells, self.sampler)
        d = os.path.join(out, "42", "media", "maps", MAP_NAMES[1])     # odd cell x
        self.assertTrue(os.path.isfile(os.path.join(out, "42", "mod.info")))
        self.assertIn("lots=Muldraugh, KY", read(os.path.join(d, "map.info"), "r"))
        h = read_header(read(os.path.join(d, "1_0.lotheader")))
        self.assertEqual(h.tiles, [STREET])
        sq = LotPack(read(os.path.join(d, "world_1_0.lotpack")), h).level0()
        self.assertEqual(set(sq), {(x, y) for x in range(0, 44) for y in range(100, 105)})
        self.assertEqual(os.listdir(os.path.join(out, "42", "media", "maps", MAP_NAMES[0])), ["map.info"])

    def east(self):
        return [e for e in find_exits(self.sampler, self.home, "town", (-5, -5)) if e["dir"] == (1, 0)][0]

    def test_route_to_a_spot(self):
        r, err = route_between(self.sampler, self.east(), {"x": 280, "y": 150}, ["town"])
        self.assertIsNone(err)
        self.assertEqual(r["path"][0], (256, 102))
        self.assertEqual(r["path"][-1], (280, 150))
        self.assertEqual(len(r["path"]), 3)      # one turn
        self.assertNotIn((255, 102), r["squares"])
        again = pave_path(self.sampler, r["path"], (1, 0), 5, {STREET: 1}, ["town"])
        self.assertEqual(again, r["squares"])

    def test_route_head_on(self):
        # b is a road end facing west, so the road has to arrive heading east.
        r, err = route_between(self.sampler, self.east(), {"x": 280, "y": 130, "dir": (-1, 0)}, ["town"])
        self.assertIsNone(err)
        self.assertEqual(r["path"][-1], (280, 130))
        self.assertEqual(r["path"][-2][1], 130)   # the last leg runs along x
        self.assertLess(r["path"][-2][0], 280)

    def test_route_into_own_map(self):
        r, err = route_between(self.sampler, self.east(), {"x": 100, "y": 103}, ["town"])
        self.assertIsNone(r)
        self.assertIn("inside the map", err)

    def test_no_go_cell(self):
        r, _ = route_between(self.sampler, self.east(), {"x": 280, "y": 150}, ["town"])
        self.sampler.no_go = {(280 // 256, 150 // 256)}
        try:
            self.assertIsNone(pave_path(self.sampler, r["path"], (1, 0), 5, {STREET: 1}, ["town"]))
            r2, err = route_between(self.sampler, self.east(), {"x": 280, "y": 150}, ["town"])
            self.assertIsNone(r2)
            self.assertIn("crash", err)
        finally:
            self.sampler.no_go = set()

    def test_pave_path_blocked(self):
        # A path straight through the house is refused.
        self.assertIsNone(pave_path(self.sampler, [(0, 115), (30, 115)], (1, 0), 3, {STREET: 1}, []))

    def test_make_link_and_apply(self):
        plans = {"town": {"offset": [-5, -5], "join": True}}
        ends = road_ends(self.sampler, self.state, plans, "town", self.vdir)
        a = [e for e in ends if e["dir"] == [1, 0]][0]
        self.assertEqual((a["hx"], a["hy"]), (256 + 5 * 256, 102 + 5 * 256))
        link, err = make_link(self.sampler, self.state, plans, a, {"key": None, "hx": 280, "hy": 150})
        self.assertIsNone(err)
        self.assertEqual(link["at"], {"town": [-5, -5]})
        self.assertEqual(link["b"]["key"], None)            # vanilla ground, not a map
        routes, kept = manual_routes(self.sampler, self.state, plans, [link], log=lambda m: None)
        self.assertEqual(kept[0]["path"], link["path"])
        self.assertEqual(len(routes[0]["squares"]), link["squares"])
        # The automatic links leave that road end alone now.
        _, auto = plan_links(self.state, {"town": dict(plans["town"], roads=True)}, self.vdir,
                             log=lambda m: None, manual=[link])
        self.assertFalse(any(r["path"][0] == (256, 102) for r in auto["town"]["routes"]))

    def test_moved_map_routes_again(self):
        plans = {"town": {"offset": [-5, -5], "join": True}}
        a = [e for e in road_ends(self.sampler, self.state, plans, "town", self.vdir) if e["dir"] == [1, 0]][0]
        link, _ = make_link(self.sampler, self.state, plans, a, {"key": None, "hx": 280, "hy": 150})
        moved = {"town": {"offset": [-5, -6], "join": True}}   # the town sits 256 squares further north
        s = WorldSampler(self.state, moved, self.vdir)
        logs = []
        routes, kept = manual_routes(s, self.state, moved, [link], log=logs.append)
        self.assertEqual(kept[0]["at"], {"town": [-5, -6]})
        self.assertEqual(kept[0]["path"][0], [256, 102 - 256])
        self.assertEqual(kept[0]["path"][-1], [280, 150])
        self.assertTrue(any("routed again" in m for m in logs))


RIVER = "blends_natural_02_0"


class River(unittest.TestCase):
    """A map on its own at cell (5,5): a road north to south at x 100..104
    broken by a river at y 110..139, a driveway up to a house, and a
    diagonal road."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        t = os.path.join(cls.tmp, "river")
        os.makedirs(t)
        sq = {(x, y): [GRASS] for x in range(256) for y in range(256)}
        for x in range(256):
            for y in range(110, 140):
                sq[(x, y)] = [RIVER]
        for x in range(100, 105):
            for y in list(range(0, 110)) + list(range(140, 256)):
                sq[(x, y)] = [STREET]
        for x in range(30, 33):         # a driveway from a side road up to a house
            for y in range(20, 40):
                sq[(x, y)] = [STREET]
        for x in range(0, 100):
            for y in range(40, 45):
                sq[(x, y)] = [STREET]
        for x in range(26, 37):
            for y in range(12, 20):
                sq[(x, y)] = [GRASS, WALL]
        for k in range(60):             # a diagonal road, five wide
            for j in range(5):
                sq[(150 + k + j, 20 + k)] = [STREET]
        write_cell(t, 5, 5, sq)
        g = Group("river")
        g.originals = {"river": FakeMod("river", [FakeDir("river", t, [(5, 5)])])}
        g.cells = {(5, 5)}
        cls.state = {"groups": [g], "active_maps": ["river"], "vanilla_cells": set()}
        cls.sampler = WorldSampler(cls.state, {"river": {"offset": [0, 0], "join": True}}, None, only="river")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def inner(self):
        return [e for e in find_exits(self.sampler, self.sampler, "river", [0, 0], inner=True) if e.get("inner")]

    def test_dead_ends_at_the_river_only(self):
        ends = sorted((e["x"] - 1280, e["y"] - 1280, e["dir"], e["width"]) for e in self.inner())
        self.assertEqual(ends, [(102, 110, (0, 1), 5), (102, 139, (0, -1), 5)])

    def test_no_dead_ends_without_inner(self):
        self.assertFalse(any(e.get("inner") for e in find_exits(self.sampler, self.sampler, "river", [0, 0])))

    def test_bridge(self):
        a, b = sorted(self.inner(), key=lambda e: e["y"])
        r, why = route_between(self.sampler, a, b, [])
        self.assertIsNone(why)
        self.assertGreaterEqual(r["bridge"], 5 * 28)
        self.assertLess(r["length"], 40)    # straight across, not around
        again = pave_path(self.sampler, r["path"], a["dir"], a["width"], a["tiles"], [])
        self.assertEqual(sorted(again), sorted(r["squares"]))


class Cut(unittest.TestCase):
    """Vanilla cell (0,0) has a road north to south at x 100..104 that runs
    off its south edge. A map at cell (0,1) is grass there, so it cuts that
    road off. The map has its own street at x 100..104, y 120..200, with a
    worn corner at the north tip and a driveway off its east side."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        v, t = os.path.join(cls.tmp, "vanilla"), os.path.join(cls.tmp, "plot")
        os.makedirs(v)
        os.makedirs(t)
        sq = {(x, y): [STREET if 100 <= x < 105 else GRASS] for x in range(256) for y in range(256)}
        write_cell(v, 0, 0, sq)
        sq = {(x, y): [GRASS] for x in range(256) for y in range(256)}
        for x in range(100, 105):
            for y in range(122 if x == 100 else 120, 201):
                sq[(x, y)] = [STREET]
        for x in range(105, 112):       # the driveway
            for y in range(150, 153):
                sq[(x, y)] = [STREET]
        for x in range(112, 120):
            for y in range(146, 156):
                sq[(x, y)] = [GRASS, WALL]
        write_cell(t, 0, 1, sq)
        g = Group("plot")
        g.originals = {"plot": FakeMod("plot", [FakeDir("plot", t, [(0, 1)])])}
        g.cells = {(0, 1)}
        cls.state = {"groups": [g], "active_maps": ["plot"], "vanilla_cells": {(0, 0)}}
        cls.vdir = v
        cls.plans = {"plot": {"offset": [0, 0], "join": True}}
        cls.sampler = WorldSampler(cls.state, cls.plans, v)
        cls.ends = road_ends(cls.sampler, cls.state, cls.plans, "plot", v)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_cut_vanilla_road(self):
        cut = [e for e in self.ends if e.get("cut")]
        self.assertEqual([(e["key"], e["hx"], e["hy"], e["dir"], e["width"]) for e in cut],
                         [(None, 102, 255, [0, 1], 5)])

    def test_worn_street_end(self):
        inner = sorted((e["hx"], e["hy"], tuple(e["dir"])) for e in self.ends if e.get("inner"))
        self.assertIn((0, -1), [d for _, _, d in inner])
        self.assertTrue(all(100 <= x <= 104 for x, _, _ in inner))

    def test_inner_end_routes_through_its_own_map(self):
        cut = [e for e in self.ends if e.get("cut")][0]
        b = {"key": None, "hx": cut["hx"], "hy": cut["hy"], "cut": True}
        for a in (e for e in self.ends if e.get("inner")):
            link, err = make_link(self.sampler, self.state, self.plans, a, b)
            self.assertIsNone(err, a)
            self.assertEqual(link["b"], b)
        # A cut-off road can start a link too, out to a spot on the map.
        link, err = make_link(self.sampler, self.state, self.plans, cut, {"key": None, "hx": 160, "hy": 300})
        self.assertIsNone(err)


class EmptyEdge(unittest.TestCase):
    """A map at cell (5,5) whose ground stops at y 200 though the cell goes
    on, like Dirkerdam's south coast. A road runs along that edge at y
    197..199 and a street at x 100..107 runs south into it, a T."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        t = os.path.join(cls.tmp, "isle")
        os.makedirs(t)
        sq = {(x, y): [GRASS] for x in range(256) for y in range(200)}
        for x in range(256):
            for y in range(197, 200):
                sq[(x, y)] = [STREET]
        for x in range(100, 108):
            for y in range(0, 197):
                sq[(x, y)] = [STREET]
        write_cell(t, 5, 5, sq)
        g = Group("isle")
        g.originals = {"isle": FakeMod("isle", [FakeDir("isle", t, [(5, 5)])])}
        g.cells = {(5, 5)}
        cls.state = {"groups": [g], "active_maps": ["isle"], "vanilla_cells": set()}
        cls.sampler = WorldSampler(cls.state, {"isle": {"offset": [0, 0], "join": True}}, None, only="isle")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_dot_where_the_street_meets_the_edge_road(self):
        ends = [(e["x"] - 1280, e["y"] - 1280, e["dir"], e["width"])
                for e in find_exits(self.sampler, self.sampler, "isle", [0, 0], inner=True)]
        self.assertIn((104, 200, (0, 1), 8), ends)

    def test_not_for_automatic_links(self):
        ends = find_exits(self.sampler, self.sampler, "isle", [0, 0])
        self.assertFalse(any(e["y"] == 1280 + 200 for e in ends))


class CellCache(unittest.TestCase):
    def test_pack_round_trip(self):
        from mapmover.cells import Classes, _pack, _unpack
        cl = Classes(zombies=b"")
        cl.codes[5] = ROAD
        cl.codes[700] = WATER
        cl.road[5] = STREET
        back = _unpack(_pack(cl))
        self.assertEqual(back.codes, cl.codes)
        self.assertEqual(back.road, {5: STREET})
        self.assertEqual(back.zombies, b"")
        with self.assertRaises(ValueError):
            _unpack(b"nope" + bytes(20))


class Layers(unittest.TestCase):
    """Two maps on one cell. "a" is grass with a house, "b" is grass with a
    road, and "b" leaves one corner empty. Mod list order puts "a" first."""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        a, b = os.path.join(cls.tmp, "a"), os.path.join(cls.tmp, "b")
        os.makedirs(a)
        os.makedirs(b)
        sq = {(x, y): [GRASS] for x in range(256) for y in range(256)}
        for x in range(10, 20):
            for y in range(10, 20):
                sq[(x, y)] = [GRASS, WALL]
        write_cell(a, 0, 0, sq)
        sq = {(x, y): [STREET if 100 <= y < 105 else GRASS] for x in range(256) for y in range(256)
              if not (x < 30 and y < 30)}
        write_cell(b, 0, 0, sq)
        groups = []
        for name, path in (("a", a), ("b", b)):
            g = Group(name)
            g.originals = {name: FakeMod(name, [FakeDir(name, path, [(0, 0)])])}
            g.cells = {(0, 0)}
            g.map_names = {name}
            groups.append(g)
        cls.state = {"groups": groups, "active_maps": ["a", "b"], "vanilla_cells": set()}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp)

    def sampler(self, layer_b):
        plans = {"a": {"offset": [0, 0], "join": True},
                 "b": {"offset": [0, 0], "join": True, "layer": layer_b}}
        return WorldSampler(self.state, plans, None)

    def test_same_layer_keeps_mod_list_order(self):
        s = self.sampler(1)
        self.assertEqual(s.owner(0, 0), "a")
        self.assertEqual(s.code(50, 102), OPEN)       # a's grass covers b's road

    def test_higher_layer_wins(self):
        s = self.sampler(2)
        self.assertEqual(s.owner(0, 0), "b")
        self.assertEqual(s.code(50, 102), ROAD)
        self.assertEqual(s.code(50, 50), OPEN)
        self.assertEqual(s.code(15, 15), BLOCKED)     # b is empty there, so a's house shows

    def test_modlist_order(self):
        text = "mods\n{\n    mod = a,\n}\nmaps\n{\n    map = a,\n    -- note\n    map = b,\n    map = c,\n}\n"
        out = rewrite_modlist(text, ["a"], ["d"], front_maps=MAP_NAMES, layers={"c": 3, "d": 2})
        self.assertEqual(out[out.index("maps"):],
                         "maps\n{\n    map = mapmover_roads_a,\n    map = mapmover_roads_b,\n"
                         "    map = c,\n    -- note\n    map = d,\n    map = a,\n    map = b,\n}\n")
        self.assertEqual(rewrite_modlist(text, ["a"], [], layers={}), text)


class ModList(unittest.TestCase):
    TEXT = "VERSION = 1,\nmods\n{\n    mod = a,\n}\nmaps\n{\n    map = Town,\n    map = mapmover_roads_b,\n}\n"

    def test_front_maps(self):
        out = rewrite_modlist(self.TEXT, ["a", "mapmover_roads"], [], front_maps=MAP_NAMES, drop_maps=MAP_NAMES)
        maps = out[out.index("maps"):]
        self.assertEqual(maps, "maps\n{\n    map = mapmover_roads_a,\n    map = mapmover_roads_b,\n    map = Town,\n}\n")
        self.assertIn("    mod = mapmover_roads,\n", out)

    def test_drop_maps(self):
        out = rewrite_modlist(self.TEXT, ["a"], [], drop_maps=MAP_NAMES)
        self.assertNotIn("mapmover_roads", out)
        self.assertIn("    map = Town,\n", out)


if __name__ == "__main__":
    unittest.main()
