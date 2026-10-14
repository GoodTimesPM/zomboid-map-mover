"""The 42.x world load crash check (loadorder.py).

"low" has cells (1,1) and (2,2), so the game marks its 300 block (1,1),
which reaches into cell (2,1). "high" has only (2,1). With "high" on top
the game asks "low" for cell (2,1), which it doesn't have."""

import os
import shutil
import tempfile
import unittest

from mapmover.layout import Group
from mapmover.loadorder import check, crashes, road_no_go, stack

LOW = {(1, 1), (2, 2)}
HIGH = {(2, 1)}


class Dir:
    def __init__(self, name, cells, lots=()):
        self.name, self.cells, self.lots = name, sorted(cells), list(lots)


class Mod:
    def __init__(self, folder, mid, maps):
        self.folder, self.id, self.name, self.maps = folder, mid, mid, maps
        self.version_dir_name, self.manifest = "42", None


class Crashes(unittest.TestCase):
    def test_lower_folder_without_the_cell(self):
        found = crashes([("high", HIGH), ("low", LOW)])
        self.assertEqual(found, [{"top": "high", "asked": "low", "cells": [[2, 1]], "forced": False}])

    def test_other_way_round_is_fine(self):
        self.assertEqual(crashes([("low", LOW), ("high", HIGH)]), [])

    def test_far_apart_is_fine(self):
        self.assertEqual(crashes([("high", {(9, 9)}), ("low", LOW)]), [])


class Order(unittest.TestCase):
    """The same two maps as mods, both joined to Kentucky, with the vanilla
    map far away."""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.mods = {}
        for mid, cells in (("high", HIGH), ("low", LOW)):
            folder = os.path.join(self.tmp, mid)
            os.makedirs(os.path.join(folder, "42", "media", "lua", "shared"))
            m = Mod(folder, mid, [Dir(mid, cells, ["Muldraugh, KY"])])
            g = Group(mid)
            g.originals = {mid: m}
            g.mods = [m]
            g.map_names = {mid}
            g.cells = set(cells)
            g.kentucky = True
            self.mods[mid] = (m, g)
        self.state = {"groups": [g for _, g in self.mods.values()],
                      "originals": {k: m for k, (m, _) in self.mods.items()},
                      "by_id": {k: m for k, (m, _) in self.mods.items()},
                      "vanilla_cells": {(30, 30)}, "active_maps": []}

    def tearDown(self):
        shutil.rmtree(self.tmp)

    def plan(self, ids):
        return {"final_ids": ids, "copies": {}, "added_maps": [], "errors": []}

    def test_maps_list_decides(self):
        self.state["active_maps"] = ["high", "low"]
        self.assertEqual(len(check(self.state, {}, self.plan(["high", "low"]))), 1)
        self.state["active_maps"] = ["low", "high"]
        self.assertEqual(check(self.state, {}, self.plan(["high", "low"])), [])

    def test_layer_puts_low_on_top(self):
        self.state["active_maps"] = ["high", "low"]
        self.assertEqual(check(self.state, {"low": {"layer": 2}}, self.plan(["high", "low"])), [])

    def test_lua_beats_the_maps_list(self):
        with open(os.path.join(self.tmp, "high", "42", "media", "lua", "shared", "biome.lua"), "w") as f:
            f.write("getWorld():setMap('high' .. ';' .. getWorld():getMap())\n")
        self.state["active_maps"] = ["low", "high"]
        found = check(self.state, {"low": {"layer": 2}}, self.plan(["high", "low"]))
        self.assertEqual([(c["top"], c["asked"], c["forced"]) for c in found], [("high", "low", True)])

    def test_move_clears_it(self):
        self.state["active_maps"] = ["high", "low"]
        self.assertEqual(check(self.state, {"high": {"offset": [5, 0]}}, self.plan(["high", "low"])), [])

    def test_stack_order(self):
        folders, forced = stack(self.state, {}, ["high", "low"], ["low", "high"])
        self.assertEqual([n for n, _ in folders], ["low", "high", "Muldraugh, KY"])
        self.assertEqual(forced, set())

    def test_road_no_go(self):
        # The road links mod loads on top, so at (2,1) and (1,2) the game asks
        # "low", whose block reaches into both, whichever map is higher.
        for maps in (["high", "low"], ["low", "high"]):
            self.state["active_maps"] = maps
            self.assertEqual(road_no_go(self.state, {}, self.plan(["high", "low"])), {(2, 1), (1, 2)})


if __name__ == "__main__":
    unittest.main()
