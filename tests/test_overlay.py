import struct
import unittest

from mapmover.overlay import layer_of, read_labels, read_worldmap_bin, town_areas
from mapmover.shifters import WorldMapBinError


def make_igmb(cells, strings):
    """cells: {(x, y): [(gtype, [ring, ...], {key: value})]} on a 2x1 grid."""
    idx = {s: i for i, s in enumerate(strings)}
    b = bytearray(b"IGMB") + struct.pack("<5i", 2, 256, 2, 1, len(strings))
    for s in strings:
        e = s.encode()
        b += struct.pack("<h", len(e)) + e
    for x in range(2):
        feats = cells.get((x, 0))
        if feats is None:
            b += struct.pack("<i", -1)
            continue
        b += struct.pack("<iii", x, 0, len(feats))
        for gtype, rings, props in feats:
            b += struct.pack("<h", idx[gtype]) + bytes([len(rings)])
            for r in rings:
                b += struct.pack("<h", len(r) // 2) + struct.pack(f"<{len(r)}h", *r)
            b += bytes([len(props)])
            for k, v in props.items():
                b += struct.pack("<hh", idx[k], idx[v])
    return bytes(b)


class ReadBin(unittest.TestCase):
    def test_features_and_props(self):
        strings = ["Polygon", "water", "river", "highway", "primary"]
        data = make_igmb({(1, 0): [
            ("Polygon", [(0, 0, 10, 0, 10, 10)], {"water": "river"}),
            ("Polygon", [(0, 0, 256, 0, 256, 4, 0, 4)], {"highway": "primary"}),
        ]}, strings)
        feats = list(read_worldmap_bin(data))
        self.assertEqual(len(feats), 2)
        x, y, gtype, rings, props = feats[0]
        self.assertEqual((x, y, gtype), (1, 0, "Polygon"))
        self.assertEqual(rings, [(0, 0, 10, 0, 10, 10)])
        self.assertEqual(layer_of(props), "water")
        self.assertEqual(layer_of(feats[1][4]), "highway")

    def test_bad_magic(self):
        with self.assertRaises(WorldMapBinError):
            list(read_worldmap_bin(b"NOPE" + bytes(20)))

    def test_truncated(self):
        data = make_igmb({(0, 0): [("Polygon", [(0, 0, 1, 1)], {})]}, ["Polygon"])
        with self.assertRaises(WorldMapBinError):
            list(read_worldmap_bin(data[:-3]))


class Labels(unittest.TestCase):
    def test_names_scale_and_caps(self):
        text = '''
symbolsAPI:addUntranslatedText("Muldraugh","text-town",10880,9900):setScale(2.5)
symbolsAPI:addUntranslatedText("OhioRiver","text-water-river",500,300):setScale(3)
symbolsAPI:addUntranslatedText("Pond","text-water",1,2)
'''
        got = read_labels(text, {"Muldraugh": "MULDRAUGH", "OhioRiver": "Ohio River"})
        self.assertEqual([l["name"] for l in got], ["Muldraugh", "Ohio River", "Pond"])
        self.assertEqual(got[0]["style"], "text-town")
        self.assertEqual((got[1]["x"], got[1]["scale"]), (500.0, 3.0))
        self.assertEqual(got[2]["scale"], 1.0)


class Towns(unittest.TestCase):
    def test_dense_cells_take_nearest_town_name(self):
        buildings = {(10, 10): 20, (11, 10): 9, (12, 11): 30, (30, 30): 3, (40, 40): 12}
        labels = [{"name": "Muldraugh", "style": "text-town", "x": 11 * 256, "y": 10 * 256}]
        areas, owner = town_areas(buildings, labels)
        names = sorted(a["name"] for a in areas)
        self.assertEqual(names, ["Buildings", "Muldraugh"])
        mul = next(a for a in areas if a["name"] == "Muldraugh")
        self.assertEqual(mul["cells"], [(10, 10), (11, 10), (12, 11)])
        self.assertNotIn((30, 30), owner)


if __name__ == "__main__":
    unittest.main()
