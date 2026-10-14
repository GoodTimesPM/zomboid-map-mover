import os
import struct
import unittest


os.environ["MAPMOVER_CELL_CACHE"] = "0"   # keep test cells out of the real cache
from mapmover.lots import LotError, LotHeader, LotPack, read_header, write_header, write_pack
from mapmover.render import feature_of, png_rgba, render_cell, shrink, sheet_of


def header(**kw):
    h = LotHeader(tiles=["blends_natural_01_16", "blends_street_01_48", "walls_exterior_01_0"],
                  min_level=0, max_level=1, **kw)
    return h


class Header(unittest.TestCase):
    def test_round_trip(self):
        h = header(rooms=[{"name": "kitchen", "level": 0, "rects": [(3, 4, 5, 6)],
                           "objects": [(1, 3, 4)]}],
                   buildings=[[0]], zombies=bytes(range(256)) * 4)
        back = read_header(write_header(h))
        self.assertEqual(back.version, 1)
        self.assertEqual(back.tiles, h.tiles)
        self.assertEqual((back.min_level, back.max_level), (0, 1))
        self.assertEqual(back.rooms, h.rooms)
        self.assertEqual(back.buildings, [[0]])
        self.assertEqual(back.zombies, h.zombies)

    def test_version0(self):
        # No magic, version 0, a stray byte after the names, a level count.
        data = struct.pack("<ii", 0, 1) + b"grass\n" + b"\0" + struct.pack("<iiiii", 8, 8, 3, 0, 0) + bytes(1024)
        h = read_header(data)
        self.assertEqual((h.version, h.tiles, h.min_level, h.max_level), (0, ["grass"], 0, 2))

    def test_truncated(self):
        with self.assertRaises(LotError):
            read_header(write_header(header())[:-10])

    def test_bad_chunk_size(self):
        data = b"LOTH" + struct.pack("<iiiiii", 1, 0, 10, 10, 0, 0)
        with self.assertRaises(LotError):
            read_header(data)


class Pack(unittest.TestCase):
    def test_round_trip(self):
        h = header()
        squares = {(0, 0, 0): [0], (7, 7, 0): [1, 2], (8, 0, 0): [1], (255, 255, 1): [0, 2]}
        pack = LotPack(write_pack(squares, 0, 1), h)
        self.assertEqual(pack.chunk(0, 0), {(0, 0, 0): [0], (7, 7, 0): [1, 2]})
        self.assertEqual(pack.chunk(1, 0), {(0, 0, 0): [1]})
        self.assertEqual(pack.chunk(31, 31), {(7, 7, 1): [0, 2]})
        self.assertEqual(pack.chunk(31, 31, levels=(0,)), {})
        self.assertEqual(pack.level0(), {(0, 0): [0], (7, 7): [1, 2], (8, 0): [1]})

    def test_outside_cell(self):
        with self.assertRaises(LotError):
            write_pack({(256, 0, 0): [0]})

    def test_skip_zero_consumes_square(self):
        # The game reads "-1, 0" as one empty square.
        h = LotHeader(tiles=["a"], min_level=0, max_level=0)
        ints = [8] + [0] * 2048            # no magic: chunk size, then the table
        ints[1] = len(ints) * 4
        ints += [-1, 0, 2, -1, 0, -1, 62]
        data = struct.pack(f"<{len(ints)}i", *ints)
        self.assertEqual(LotPack(data, h).chunk(0, 0), {(0, 1, 0): [0]})


class Render(unittest.TestCase):
    def test_names(self):
        self.assertEqual(sheet_of("blends_natural_01_16"), "blends_natural_01")
        self.assertEqual(feature_of(["a", "jumbo_tree_01_3"], False), "tree")
        self.assertEqual(feature_of(["a", "walls_exterior_01_0", "jumbo_tree_01_3"], False), "wall")
        self.assertEqual(feature_of(["a"], True), "room")
        self.assertEqual(feature_of(["a"], False), "")

    def test_cell(self):
        h = header(rooms=[{"name": "r", "level": 0, "rects": [(10, 10, 2, 2)], "objects": []}])
        rgba = render_cell(write_header(h), write_pack({(0, 0, 0): [0], (10, 10, 0): [0], (5, 5, 0): [1]}))
        self.assertEqual(len(rgba), 256 * 256 * 4)
        px = lambda x, y: rgba[(y * 256 + x) * 4:(y * 256 + x) * 4 + 4]
        self.assertEqual(px(1, 1)[3], 0)                 # empty square
        self.assertEqual(px(0, 0)[3], 255)
        self.assertNotEqual(px(0, 0), px(5, 5))          # grass vs street
        self.assertNotEqual(px(0, 0), px(10, 10))        # room
        self.assertEqual(len(shrink(rgba, 16)), 16 * 16 * 4)
        self.assertTrue(png_rgba(16, 16, shrink(rgba, 16)).startswith(b"\x89PNG"))


if __name__ == "__main__":
    unittest.main()
