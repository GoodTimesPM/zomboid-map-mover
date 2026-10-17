"""The world image's map names and where it saves."""
import os
import tempfile
import unittest

from mapmover import worldpng


def m(name, mod, title, path=None):
    return {"name": name, "mod": mod, "title": title, "path": path or "/maps/" + name}


class Tidy(unittest.TestCase):
    def test_drops_build_tags_and_move_notes(self):
        self.assertEqual(worldpng.tidy("Raven Creek (moved 3,-2)"), "Raven Creek")
        self.assertEqual(worldpng.tidy("Louisville Mall Base (Build 42)"), "Louisville Mall Base")
        self.assertEqual(worldpng.tidy("Kingsmouth B42"), "Kingsmouth")
        self.assertEqual(worldpng.tidy("Little Township [B42]"), "Little Township")
        self.assertEqual(worldpng.tidy("Chinatown Expansion B42 version"), "Chinatown Expansion")

    def test_spawn_suffix_and_state_comma(self):
        self.assertEqual(worldpng.tidy("HartburgSpawn,KY"), "Hartburg, KY")

    def test_placeholder_titles(self):
        self.assertFalse(worldpng.good_title("MyMap"))
        self.assertFalse(worldpng.good_title("See media/maps"))
        self.assertFalse(worldpng.good_title(""))
        self.assertTrue(worldpng.good_title("Ekron"))


class Labels(unittest.TestCase):
    def test_touching_folders_share_one_name(self):
        plan = {"maps": [m("a", "mod1", "Town"), m("b", "mod1", "Town")]}
        cells = {"/maps/a": {(0, 0), (1, 0)}, "/maps/b": {(2, 1)}}
        out = worldpng.labels(plan, cells)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0][0], "Town")
        self.assertEqual(out[0][1], {(0, 0), (1, 0), (2, 1)})

    def test_far_apart_folders_get_their_own(self):
        plan = {"maps": [m("a", "mod1", "North"), m("b", "mod1", "South")]}
        cells = {"/maps/a": {(0, 0)}, "/maps/b": {(0, 10)}}
        self.assertEqual(sorted(t for t, _ in worldpng.labels(plan, cells)), ["North", "South"])

    def test_roads_and_empty_maps_get_none(self):
        road = next(iter(worldpng.NO_LABEL))
        plan = {"maps": [m(road, "roads", "Roads"), m("e", "mod2", "Empty")]}
        cells = {"/maps/" + road: {(0, 0)}, "/maps/e": set()}
        self.assertEqual(worldpng.labels(plan, cells), [])

    def test_rename_wins_over_title(self):
        plan = {"maps": [m("PIElots", "pie", "MyMap")]}
        out = worldpng.labels(plan, {"/maps/PIElots": {(5, 5)}})
        self.assertEqual(out[0][0], "Indiana (PIE)")


class DefaultPath(unittest.TestCase):
    def test_config_wins(self):
        with tempfile.TemporaryDirectory() as d:
            want = os.path.join(d, "w.png")
            self.assertEqual(worldpng.default_path({"world_png": want}), os.path.normpath(want))

    def test_falls_back_to_a_png(self):
        self.assertTrue(worldpng.default_path({}).endswith("zomboid_world_map.png"))


if __name__ == "__main__":
    unittest.main()
