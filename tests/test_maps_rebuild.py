"""A default.txt saved by the game's mod screen has an empty maps { } block.
The map mover rebuilds it from what the last Apply wrote."""
import os
import tempfile
import unittest

from mapmover.layout import APPLIED_NAME, VANILLA_MAP, rebuild_maps
from tests.test_loadorder import Dir, Mod


def mod(mid, names):
    return Mod("/nowhere/" + mid, mid, [Dir(n, {(1, 1)}) for n in names])


MODS = [mod("town", ["Town"]), mod("city", ["City"]), mod("farm", ["Farm"]), mod("tiles", [])]


class MapsRebuildTest(unittest.TestCase):
    def backups(self, *blocks, applied=None):
        d = tempfile.mkdtemp()
        os.makedirs(os.path.join(d, "backups"))

        def put(path, maps):
            body = "".join(f"    map = {m},\n" for m in maps)
            with open(path, "w") as f:
                f.write(f"VERSION = 1,\nmods\n{{\n    mod = town,\n}}\nmaps\n{{\n{body}}}\n")
        for i, maps in enumerate(blocks):
            put(os.path.join(d, "backups", f"default-2026101{i}-000000.txt"), maps)
        if applied is not None:
            put(os.path.join(d, APPLIED_NAME), applied)
        return d

    def test_what_apply_wrote_beats_the_backups(self):
        # A backup is the file before an Apply; the order Apply settled on is newer.
        d = self.backups(["City", "Town", VANILLA_MAP], applied=["Town", "City", VANILLA_MAP])
        self.assertEqual(rebuild_maps(MODS, ["town", "city"], d), ["Town", "City", VANILLA_MAP])

    def test_keeps_the_last_apply_order_and_puts_new_maps_above_vanilla(self):
        d = self.backups(["Town", "City", VANILLA_MAP],              # older
                         ["City", "Gone", "Town", VANILLA_MAP])      # newest
        out = rebuild_maps(MODS, ["tiles", "town", "city", "farm"], d)
        self.assertEqual(out, ["City", "Town", "Farm", VANILLA_MAP])

    def test_skips_backups_with_an_empty_block(self):
        d = self.backups(["Town", "City", VANILLA_MAP], [])
        out = rebuild_maps(MODS, ["town", "city"], d)
        self.assertEqual(out, ["Town", "City", VANILLA_MAP])

    def test_no_backups_uses_mod_order(self):
        out = rebuild_maps(MODS, ["city", "town"], "/no/such/dir")
        self.assertEqual(out, ["City", "Town"])

    def test_no_vanilla_line_unless_there_was_one(self):
        d = self.backups(applied=["City", "Town"])
        self.assertEqual(rebuild_maps(MODS, ["town", "city", "farm"], d), ["City", "Town", "Farm"])

    def test_no_map_mods_leaves_it_empty(self):
        self.assertEqual(rebuild_maps(MODS, ["tiles"], "/no/such/dir"), [])


if __name__ == "__main__":
    unittest.main()
