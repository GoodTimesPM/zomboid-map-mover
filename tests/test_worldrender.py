"""The config the map mover writes for the isometric render engine."""
import os
import tempfile
import unittest

from mapmover import worldrender

try:
    import yaml
except ImportError:
    yaml = None


def plan():
    return {
        "maps": [
            {"name": "Raven Creek B42", "title": "Raven's Creek", "mod": "RC",
             "path": r"C:\mods\RC_moved\42\media\maps\Raven Creek B42", "cells": 4, "bounds": [1, 2, 2, 2]},
            {"name": "Raven-Creek-B42", "title": "It's another", "mod": "X",
             "path": r"C:\mods\X\common\media\maps\Raven-Creek-B42", "cells": 1, "bounds": [9, 9, 1, 1]},
        ],
        "textures": [{"mod": "RC", "path": r"E:\ws\1\mods\RC\common\media\texturepacks"}],
        "missing": [],
    }


@unittest.skipIf(yaml is None, "pyyaml not installed")
class WorldRenderConfTest(unittest.TestCase):
    def write(self, **kw):
        d = tempfile.mkdtemp()
        cfg = {"world_render_dir": d, "game_dir": r"E:\PZ", "workshop_dir": r"E:\ws",
               "user_dir": r"C:\Users\x\Zomboid"}
        path = worldrender.write_conf(cfg, plan(), **kw)
        conf = yaml.safe_load(open(path, encoding="utf-8"))
        maps = yaml.safe_load(open(os.path.join(d, "engine", "conf", worldrender.MAPS_DIR, "maps.txt"),
                                   encoding="utf-8"))
        return conf, maps

    def test_keys_are_safe_and_unique(self):
        conf, maps = self.write()
        self.assertEqual(conf["mod_maps"], ["Raven_Creek_B42", "Raven_Creek_B42_2"])
        self.assertEqual(maps["Raven_Creek_B42"]["map_path"], plan()["maps"][0]["path"])
        self.assertEqual(maps["Raven_Creek_B42_2"]["display_name"], "It's another")
        self.assertTrue(maps["tex_RC"]["texture"])

    def test_vanilla_only_and_cell_range(self):
        conf, _ = self.write(maps=[], cells=[[30, 30, 2, 2]])
        self.assertEqual(conf["mod_maps"], [])
        self.assertEqual(conf["render_conf"]["render_cell_range"], [[30, 30, 2, 2]])
        self.assertEqual(conf["pz_root"], r"E:\PZ")

    def test_key_never_default(self):
        self.assertEqual(worldrender.key_of("default", set()), "default_2")


class KentuckyTest(unittest.TestCase):
    def test_joined_by_lots_either_way(self):
        maps = {
            "A": {"lots": ["Muldraugh, KY"]},
            "B": {"lots": ["A"]},              # through another map
            "C": {"lots": []},                 # named by D, which is in Kentucky
            "D": {"lots": ["C", "Muldraugh, KY"]},
            "Manhattan": {"lots": []},         # a world of its own
            "Bronx": {"lots": ["Manhattan"]},
        }
        self.assertEqual(worldrender.kentucky(maps), {"A", "B", "C", "D"})


class DrawOnceTest(unittest.TestCase):
    def folder(self, root, name, cells):
        d = os.path.join(root, name)
        os.makedirs(d, exist_ok=True)
        for x, y in cells:
            open(os.path.join(d, f"{x}_{y}.lotheader"), "w").close()
        return d

    def test_rects_cover_exactly(self):
        cells = {(0, 0), (1, 0), (2, 0), (0, 1), (1, 1), (2, 1), (5, 5), (-3, -4)}
        rects = worldrender.rects_of(cells)
        back = {(x + i, y + j) for x, y, w, h in rects for i in range(w) for j in range(h)}
        self.assertEqual(back, cells)
        self.assertIn([0, 0, 3, 2], rects)

    def test_first_map_wins_and_vanilla_fills_the_rest(self):
        root = tempfile.mkdtemp()
        game = os.path.join(root, "game")
        self.folder(os.path.join(game, "media", "maps"), worldrender.VANILLA_MAP, [(0, 0), (1, 0), (2, 0)])
        p = {"maps": [{"name": "A", "path": self.folder(root, "A", [(1, 0), (-5, 2)])},
                      {"name": "B", "path": self.folder(root, "B", [(1, 0), (2, 0)])},
                      {"name": "C", "path": self.folder(root, "C", [(2, 0)])}]}
        own, van = worldrender.winners({"game_dir": game}, p, {"A", "B", "C"})
        self.assertEqual(own, {"A": {(1, 0), (-5, 2)}, "B": {(2, 0)}})
        self.assertEqual(van, {(0, 0)})
        own, van = worldrender.winners({"game_dir": game}, p, {"B"}, cells=[[0, 0, 2, 1]])
        self.assertEqual((own, van), ({}, {(0, 0)}))


if __name__ == "__main__":
    unittest.main()
