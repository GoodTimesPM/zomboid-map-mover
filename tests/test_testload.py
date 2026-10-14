import os
import tempfile
import unittest

from mapmover.testload import _mirror, errors, server_maps
from tests.test_loadorder import Dir, Mod


class ServerMapsTest(unittest.TestCase):
    def test_kentucky_world_only_with_vanilla_last(self):
        by_id = {
            "town": Mod("/nowhere/town", "town", [Dir("Town", {(1, 1)}, ["Muldraugh, KY"])]),
            "island": Mod("/nowhere/island", "island", [Dir("Island", {(1, 1)})]),
        }
        maps = ["Island", "Town", "Town"]
        self.assertEqual(server_maps(by_id, ["island", "town"], maps), ["Town", "Muldraugh, KY"])


class ErrorsTest(unittest.TestCase):
    def test_joins_the_exception_and_skips_noise(self):
        log = "\n".join([
            "WARN : Sprite       f:0 st:1 at IsoSpriteManager.AddSprite          > duplicate texture x_1",
            "ERROR: General      f:0 st:2 at X.visitFileFailed> Exception thrown",
            "\tjava.nio.file.NoSuchFileException: E:\\m\\media\\AnimSets",
            "ERROR: General      f:0 st:3> GameServer.main> Exception thrown",
            "\tjava.lang.NullPointerException: lotHeader2 is null",
            "\t\tjava.base/sun.nio.fs.WindowsException.translateToIOException(Unknown Source)",
            "ERROR: General      f:0 st:4> GameServer.main> Exception thrown",
            "\tjava.lang.NullPointerException: lotHeader2 is null",
        ])
        self.assertEqual(errors(log), ["GameServer.main> Exception thrown: java.lang.NullPointerException: lotHeader2 is null"])


class MirrorTest(unittest.TestCase):
    def test_copies_then_only_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            src, dst = os.path.join(tmp, "src"), os.path.join(tmp, "dst")
            os.makedirs(os.path.join(src, "media"))
            for n in ("a.txt", os.path.join("media", "b.txt")):
                with open(os.path.join(src, n), "w") as f:
                    f.write(n)
            self.assertEqual(_mirror(src, dst, link=True), 2)
            self.assertEqual(_mirror(src, dst, link=True), 0)
            os.remove(os.path.join(src, "a.txt"))
            self.assertEqual(_mirror(src, dst, link=False), 1)
            self.assertFalse(os.path.exists(os.path.join(dst, "a.txt")))
            with open(os.path.join(dst, "media", "b.txt")) as f:
                self.assertEqual(f.read(), os.path.join("media", "b.txt"))


if __name__ == "__main__":
    unittest.main()
