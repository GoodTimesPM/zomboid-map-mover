"""Maps switched off: they leave the mod list and maps block at Apply, stay
listed, and their companions go with them."""
import unittest
from unittest import mock

from mapmover import layout
from mapmover.build import make_plan, rewrite_modlist
from tests.test_loadorder import Dir, Mod


def mod(mid, maps, require=()):
    m = Mod("/nowhere/" + mid, mid, maps)
    m.require = list(require)
    m.workshop_id = None
    return m


TOWN = mod("town", [Dir("Town", {(1, 1)})])
CITY = mod("city", [Dir("City", {(5, 5)})])
CITY_SPAWNS = mod("cityspawns", [], require=["city"])  # a companion of city
TILES = mod("tiles", [])


def state(active_ids, active_maps, off_keys=()):
    with mock.patch.object(layout, "scan_mods", return_value=[TOWN, CITY, CITY_SPAWNS, TILES]), \
            mock.patch.object(layout, "scan_vanilla", return_value=[]), \
            mock.patch.object(layout, "read_modlist", return_value=(active_ids, active_maps)):
        return layout.build_state({"mods_dir": "/nowhere"}, (), off_keys)


class SwitchOffTest(unittest.TestCase):
    def test_off_map_leaves_the_lists_with_its_companion(self):
        st = state(["tiles", "town", "city", "cityspawns"], ["Town", "City"], off_keys=["city"])
        self.assertEqual([g.key for g in st["groups"]], ["town"])
        self.assertEqual([g.key for g in st["off_groups"]], ["city"])
        self.assertTrue(st["off_groups"][0].active)
        self.assertEqual(st["off_ids"], ["city", "cityspawns"])
        self.assertEqual(st["off_maps"], ["City"])
        self.assertEqual(st["active_maps"], ["Town"])
        self.assertEqual(st["listed_ids"], ["tiles", "town", "city", "cityspawns"])
        plan = make_plan(st, {})
        self.assertEqual(plan["final_ids"], ["tiles", "town"])

        text = "VERSION = 1,\nmods\n{\n    mod = tiles,\n    mod = town,\n    mod = city,\n    mod = cityspawns,\n}\n" \
               "maps\n{\n    map = Town,\n    map = City,\n}\n"
        out = rewrite_modlist(text, plan["final_ids"], plan["added_maps"], drop_maps=st["off_maps"])
        self.assertNotIn("city", out)
        self.assertNotIn("City", out)
        self.assertIn("map = Town,", out)

    def test_off_map_stays_listed_after_apply_took_it_out(self):
        st = state(["tiles", "town"], ["Town"], off_keys=["city"])
        self.assertEqual([g.key for g in st["off_groups"]], ["city"])
        self.assertFalse(st["off_groups"][0].active)
        self.assertFalse(st["off_groups"][0].added)
        self.assertNotIn("city", [m["id"] for m in st["inactive"]])

    def test_switched_back_on_comes_back_like_an_added_map(self):
        st = state(["tiles", "town"], ["Town"])
        plan = make_plan(st, {}, add_ids=["city"])
        self.assertEqual(plan["final_ids"], ["tiles", "town", "city"])
        self.assertEqual(plan["added_maps"], ["City"])


if __name__ == "__main__":
    unittest.main()
