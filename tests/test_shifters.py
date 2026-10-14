import io
import struct
import unittest
import zipfile

from mapmover import shifters as sh
from mapmover.build import rewrite_mod_info, rewrite_modlist
from mapmover.luashift import shift_lua


class LuaTests(unittest.TestCase):
    def test_object_zone_xy(self):
        src = '{ name = "", type = "DeepForest", x = 13200, y = 8700, z = 0, width = 50, height = 50 },'
        out, stats, _ = shift_lua(src, 256, 512)
        self.assertEqual(out, '{ name = "", type = "DeepForest", x = 13456, y = 9212, z = 0, width = 50, height = 50 },')
        self.assertEqual(stats["xy"], 1)

    def test_polygon_points(self):
        src = '{ type = "Nav", geometry = "polygon", points = { 10, 20, 30, 40 }, x = 1, y = 2 }'
        out, stats, _ = shift_lua(src, 100, 1000)
        self.assertIn("points = { 110, 1020, 130, 1040 }", out)
        self.assertIn("x = 101, y = 1002", out)
        self.assertEqual(stats["points"], 2)

    def test_spawn_legacy_cells(self):
        src = "{ worldX = 58, worldY = 43, posX = 259, posY = 168, posZ = 0 }"
        out, stats, _ = shift_lua(src, 256 * 30, 0)
        self.assertEqual(out, "{ worldX = 0, worldY = 0, posX = %d, posY = %d, posZ = 0 }"
                         % (58 * 300 + 259 + 7680, 43 * 300 + 168))
        self.assertEqual(stats["spawn_cell"], 1)

    def test_spawn_absolute(self):
        out, stats, _ = shift_lua("{posX=14848,posY=11264,posZ=0}", 256, -256)
        self.assertEqual(out, "{posX=15104,posY=11008,posZ=0}")
        self.assertEqual(stats["spawn_abs"], 1)

    def test_relative_fields_untouched(self):
        src = "{ width = 3, height = 4, stairx = 1, stairy = 2, StairX = 7 }"
        out, _, _ = shift_lua(src, 999, 999)
        self.assertEqual(out, src)

    def test_strings_and_comments_untouched(self):
        src = '-- x = 5, y = 6\nlocal s = "{ x = 1, y = 2 }"\n--[[ { x = 3, y = 4 } ]]\nt = { x = 10, y = 20 }'
        out, _, _ = shift_lua(src, 1, 1)
        self.assertEqual(out, src.replace("x = 10, y = 20", "x = 11, y = 21"))

    def test_negative_and_decimal(self):
        out, _, _ = shift_lua("{ x = -5, y = 10.5 }", 256, 256)
        self.assertEqual(out, "{ x = 251, y = 266.5 }")

    def test_annotation_call(self):
        src = 'symbolsAPI:addUntranslatedText("CAMDEN", "Annotation", 16200, 11456)\nsym:setScale(0.6)'
        out, stats, _ = shift_lua(src, 256, 0)
        self.assertIn('"Annotation", 16456, 11456)', out)
        self.assertIn("setScale(0.6)", out)
        self.assertEqual(stats["annotation"], 1)

    def test_nested_tables(self):
        src = "{ x = 1, y = 2, choices = { { name = 'a', x = 5 } } }"
        out, _, warns = shift_lua(src, 10, 10)
        self.assertTrue(out.startswith("{ x = 11, y = 12,"))
        self.assertIn("x = 5", out)  # lone x in the inner table is left alone
        self.assertTrue(warns)


class LuaStrictTests(unittest.TestCase):
    def test_expression_left_alone(self):
        src = "{ x = 5 * 2, y = 10 }"
        out, _, warns = shift_lua(src, 256, 256)
        self.assertEqual(out, src)
        self.assertTrue(warns)

    def test_points_with_expression_left_alone(self):
        src = "{ points = { 10 - 5, 20, 30, 40 } }"
        out, _, warns = shift_lua(src, 256, 256)
        self.assertEqual(out, src)
        self.assertTrue(warns)

    def test_trailing_separator(self):
        out, _, _ = shift_lua("{ x = 1; y = 2; }", 10, 20)
        self.assertEqual(out, "{ x = 11; y = 22; }")


class XmlTests(unittest.TestCase):
    def test_worldmap_cells(self):
        data = b'<world><cell x="59" y="33"><feature><point x="10" y="20"/></feature></cell></world>'
        out, n = sh.shift_worldmap_xml(data, 30, -2)
        self.assertEqual(out, b'<world><cell x="89" y="31"><feature><point x="10" y="20"/></feature></cell></world>')
        self.assertEqual(n, 1)

    def test_streets(self):
        data = b'<street name="Main" width="2"><points><point x="12895.5" y="11204.5"/><point x="7" y="8"/></points></street>'
        out, n = sh.shift_streets_xml(data, 1, 2)
        self.assertEqual(out, b'<street name="Main" width="2"><points><point x="13151.5" y="11716.5"/><point x="263" y="520"/></points></street>')
        self.assertEqual(n, 2)


def make_igmb(cells):
    """cells: list of (x, y) or None for an empty slot."""
    b = bytearray(b"IGMB")
    b += struct.pack("<iiii", 2, 256, len(cells), 1)
    strings = [b"Polygon", b"highway"]
    b += struct.pack("<i", len(strings))
    for s in strings:
        b += struct.pack("<h", len(s)) + s
    for c in cells:
        if c is None:
            b += struct.pack("<i", -1)
            continue
        b += struct.pack("<iii", c[0], c[1], 1)
        b += struct.pack("<h", 0) + bytes([1])           # type, 1 coord list
        b += struct.pack("<h", 2) + struct.pack("<hhhh", 1, 2, 3, 4)
        b += bytes([1]) + struct.pack("<hh", 1, 1)        # one property
    return bytes(b)


class BinTests(unittest.TestCase):
    def test_roundtrip(self):
        data = make_igmb([(50, 33), None, (51, 33)])
        out, n = sh.shift_worldmap_bin(data, 30, 1)
        self.assertEqual(n, 2)
        self.assertEqual(len(out), len(data))
        cells = [(x, y) for _, x, y in sh.parse_worldmap_bin(out)]
        self.assertEqual(cells, [(80, 34), (81, 34)])

    def test_cell_on_x_minus_one_rejected(self):
        # -1 marks an empty slot, so a cell moved there can't be written.
        with self.assertRaises(sh.WorldMapBinError):
            sh.shift_worldmap_bin(make_igmb([(64, 0)]), -65, 0)

    def test_trailing_bytes_rejected(self):
        with self.assertRaises(sh.WorldMapBinError):
            sh.parse_worldmap_bin(make_igmb([(1, 1)]) + b"\0")


class PyramidTests(unittest.TestCase):
    def test_bounds(self):
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as z:
            z.writestr("0/tile0x0.png", b"png")
            z.writestr("pyramid.txt", "VERSION=1\r\nbounds=12900 8700 19200 14700\r\nimageSize=256\r\n")
        out, old, new = sh.shift_pyramid_zip(buf.getvalue(), 30, 0)
        self.assertEqual(old, (12900, 8700, 19200, 14700))
        self.assertEqual(new, (20580, 8700, 26880, 14700))
        z = zipfile.ZipFile(io.BytesIO(out))
        self.assertEqual(z.read("pyramid.txt").decode(),
                         "VERSION=1\r\nbounds=20580 8700 26880 14700\r\nimageSize=256\r\n")
        self.assertEqual(z.read("0/tile0x0.png"), b"png")


class InfoTests(unittest.TestCase):
    def test_map_info(self):
        text = "title=Camden\r\nlots=Muldraugh, KY\r\nzoomX=14848\r\nzoomY=11264\r\n"
        out = sh.shift_map_info(text, 30, -1)
        self.assertEqual(out, "title=Camden\r\nlots=Muldraugh, KY\r\nzoomX=22528\r\nzoomY=11008\r\n")

    def test_cell_names(self):
        self.assertEqual(sh.shifted_cell_filename("50_33.lotheader", 30, 0), "80_33.lotheader")
        self.assertEqual(sh.shifted_cell_filename("world_50_33.lotpack", 30, 0), "world_80_33.lotpack")
        self.assertEqual(sh.shifted_cell_filename("chunkdata_50_33.bin", 30, 0), "chunkdata_80_33.bin")
        self.assertEqual(sh.shifted_cell_filename("biomemap_50_33.png", 30, 0, True), "biomemap_80_33.png")
        self.assertIsNone(sh.shifted_cell_filename("worldmap.xml.bin", 30, 0))
        self.assertIsNone(sh.shifted_cell_filename("50_33.lotheader", 30, 0, True))

    def test_mod_info(self):
        text = "name=Camden County\nid=CamdenCountyB42\nrequire=\\SpoonTiles,\\tikitown\npack=SpoonTiles\n"
        out = rewrite_mod_info(text, "CamdenCountyB42_moved", "Camden County (moved +30,+0)",
                               {"tikitown": "tikitown_moved", "CamdenCountyB42": "CamdenCountyB42_moved"})
        self.assertEqual(out, "name=Camden County (moved +30,+0)\nid=CamdenCountyB42_moved\n"
                              "require=\\SpoonTiles,\\tikitown_moved\npack=SpoonTiles\n")

    def test_modlist(self):
        text = "VERSION = 1,\n\nmods\n{\n    mod = A,\n    mod = B,\n}\n\nmaps\n{\n    map = X,\n}\n"
        out = rewrite_modlist(text, ["A", "B_moved", "C"], ["Y"])
        self.assertEqual(out, "VERSION = 1,\n\nmods\n{\n    mod = A,\n    mod = B_moved,\n    mod = C,\n}\n\n"
                              "maps\n{\n    map = X,\n    map = Y,\n}\n")


if __name__ == "__main__":
    unittest.main()
