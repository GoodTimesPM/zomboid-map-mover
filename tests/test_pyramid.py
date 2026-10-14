import os
import tempfile
import unittest
import zipfile

from mapmover.pyramid import Pyramid, PyramidError, parse_pyramid_txt


def make_zip(path, txt, tiles):
    with zipfile.ZipFile(path, "w") as z:
        if txt is not None:
            z.writestr("pyramid.txt", txt)
        for name in tiles:
            z.writestr(name, b"png:" + name.encode())


class ParseTxt(unittest.TestCase):
    def test_bounds_and_image(self):
        got = parse_pyramid_txt("VERSION=1\nbounds=12900 8700 19200 14700\nimageSize=6300 6000\n")
        self.assertEqual(got, {"bounds": [12900, 8700, 19200, 14700], "image": [6300, 6000]})

    def test_image_defaults_to_bounds_size(self):
        got = parse_pyramid_txt("bounds=0 0 19800 7500")
        self.assertEqual(got["image"], [19800, 7500])

    def test_bad_bounds(self):
        for txt in ("VERSION=1", "bounds=10 10 5 20", "bounds=a b c d"):
            with self.assertRaises(PyramidError):
                parse_pyramid_txt(txt)


class Zip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "pyramid.zip")

    def tearDown(self):
        self.dir.cleanup()

    def test_tiles_and_levels(self):
        make_zip(self.path, "bounds=0 0 300 300", ["0/", "0/tile0x0.png", "0/tile1x1.png", "1/tile0x0.png"])
        p = Pyramid(self.path)
        self.assertEqual(p.levels, 2)
        self.assertEqual(p.tile(0, 1, 1), b"png:0/tile1x1.png")
        self.assertIsNone(p.tile(0, 5, 5))
        self.assertIsNone(p.tile(-1, 0, 0))

    def test_missing_txt(self):
        make_zip(self.path, None, ["0/tile0x0.png"])
        with self.assertRaises(PyramidError):
            Pyramid(self.path)


if __name__ == "__main__":
    unittest.main()
