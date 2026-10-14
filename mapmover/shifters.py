"""Shift each kind of map file by a whole number of 256-tile cells.

dx, dy here are always in CELLS. Tile offsets are dx * 256, dy * 256.
Every function takes the source bytes and returns new bytes, so nothing
here ever writes to the source mod.

What lives where (checked against the B42 game code, build 42.21):

  X_Y.lotheader, world_X_Y.lotpack, chunkdata_X_Y.bin
      The cell position comes only from the file name. Room and building
      rects inside are relative to the cell. Renaming is the whole job.
  maps/biomemap_X_Y.png       one image per cell, named by cell. Rename.
  worldmap.xml, worldmap-forest.xml
      <cell x= y=> in cells; points inside are relative to the cell.
  worldmap.xml.bin, worldmap-forest.xml.bin   ("IGMB" v2)
      Same data, binary. The game prefers the .bin when it exists.
  streets.xml                 <point x= y=> in absolute tiles (floats).
  pyramid.zip, forest.pyramid.zip
      pyramid.txt "bounds=minX minY maxX maxY" in absolute tiles. Tiles are
      indexed relative to minX/minY, so only the bounds line changes.
  map.info                    zoomX / zoomY in absolute tiles.
  *.lua                       see luashift.py.
"""

import io
import re
import struct
import zipfile

from .luashift import shift_lua

CELL = 256

LOT_NAME = re.compile(r"^(-?\d+)_(-?\d+)\.lotheader$")
LOTPACK_NAME = re.compile(r"^world_(-?\d+)_(-?\d+)\.lotpack$")
CHUNKDATA_NAME = re.compile(r"^chunkdata_(-?\d+)_(-?\d+)\.bin$")
BIOME_NAME = re.compile(r"^biomemap_(-?\d+)_(-?\d+)\.png$", re.IGNORECASE)


def shifted_cell_filename(name, dx, dy, in_maps_subdir=False):
    """New name for a per-cell file, or None if the name isn't one."""
    if in_maps_subdir:
        m = BIOME_NAME.match(name)
        if m:
            return f"biomemap_{int(m.group(1)) + dx}_{int(m.group(2)) + dy}.png"
        return None
    m = LOT_NAME.match(name)
    if m:
        return f"{int(m.group(1)) + dx}_{int(m.group(2)) + dy}.lotheader"
    m = LOTPACK_NAME.match(name)
    if m:
        return f"world_{int(m.group(1)) + dx}_{int(m.group(2)) + dy}.lotpack"
    m = CHUNKDATA_NAME.match(name)
    if m:
        return f"chunkdata_{int(m.group(1)) + dx}_{int(m.group(2)) + dy}.bin"
    return None


# ---------------------------------------------------------------- XML


_CELL_TAG = re.compile(rb"<cell\b[^>]*>")
_POINT_TAG = re.compile(rb"<point\b[^>]*>")
_ATTR_X = re.compile(rb'(\sx\s*=\s*)(["\'])([^"\']*)\2')
_ATTR_Y = re.compile(rb'(\sy\s*=\s*)(["\'])([^"\']*)\2')


def _fmt_num(original, value):
    s = original.decode("ascii").strip()
    m = re.fullmatch(r"-?\d*\.(\d*)", s)
    if m:
        return f"{value:.{len(m.group(1))}f}".encode("ascii")
    return str(int(round(value))).encode("ascii")


def _shift_attrs(tag, dx, dy, counter):
    def sub_attr(regex, delta, text):
        def repl(m):
            raw = m.group(3)
            try:
                v = float(raw)
            except ValueError:
                return m.group(0)
            counter[0] += 1
            return m.group(1) + m.group(2) + _fmt_num(raw, v + delta) + m.group(2)
        return regex.sub(repl, text, count=1)

    tag = sub_attr(_ATTR_X, dx, tag)
    tag = sub_attr(_ATTR_Y, dy, tag)
    return tag


def shift_worldmap_xml(data, dx, dy):
    """<cell x= y=> attributes are cell numbers. Returns (bytes, cells)."""
    counter = [0]
    out = _CELL_TAG.sub(lambda m: _shift_attrs(m.group(0), dx, dy, counter), data)
    return out, counter[0] // 2


def shift_streets_xml(data, dx, dy):
    """<point x= y=> are absolute tiles. Returns (bytes, points)."""
    counter = [0]
    tdx, tdy = dx * CELL, dy * CELL
    out = _POINT_TAG.sub(lambda m: _shift_attrs(m.group(0), tdx, tdy, counter), data)
    return out, counter[0] // 2


# ---------------------------------------------------------------- IGMB


class WorldMapBinError(Exception):
    pass


def parse_worldmap_bin(data):
    """Walk an IGMB v2 file the way WorldMapBinary.read does.

    Returns a list of (offset_of_x, x, y) for every non-empty cell. Raises
    WorldMapBinError if the layout doesn't match or bytes are left over.
    """
    if data[:4] != b"IGMB":
        raise WorldMapBinError("magic is not IGMB")
    pos = 4

    def rint():
        nonlocal pos
        if pos + 4 > len(data):
            raise WorldMapBinError("unexpected end of file")
        v = struct.unpack_from("<i", data, pos)[0]
        pos += 4
        return v

    def rshort():
        nonlocal pos
        if pos + 2 > len(data):
            raise WorldMapBinError("unexpected end of file")
        v = struct.unpack_from("<h", data, pos)[0]
        pos += 2
        return v

    def rbyte():
        nonlocal pos
        if pos + 1 > len(data):
            raise WorldMapBinError("unexpected end of file")
        v = data[pos]
        pos += 1
        return v

    version = rint()
    if version != 2:
        raise WorldMapBinError(f"version {version}, only 2 is supported")
    cell_size = rint()
    if cell_size != CELL:
        raise WorldMapBinError(f"cell size {cell_size}")
    width = rint()
    height = rint()
    nstrings = rint()
    if nstrings < 0:
        raise WorldMapBinError("bad string table size")
    for _ in range(nstrings):
        length = rshort()
        if length < 0:
            raise WorldMapBinError("bad string length")
        pos += length
    cells = []
    for _ in range(max(0, width) * max(0, height)):
        at = pos
        x = rint()
        if x == -1:
            continue
        y = rint()
        nfeatures = rint()
        if nfeatures < 0:
            raise WorldMapBinError("bad feature count")
        for _ in range(nfeatures):
            rshort()  # geometry type, string index
            ncoords = rbyte()
            for _ in range(ncoords):
                npoints = rshort()
                if npoints < 0:
                    raise WorldMapBinError("bad point count")
                pos += npoints * 4
            nprops = rbyte()
            pos += nprops * 4
        cells.append((at, x, y))
    if pos != len(data):
        raise WorldMapBinError(f"{len(data) - pos} bytes left after the last cell")
    return cells


def shift_worldmap_bin(data, dx, dy):
    """Returns (bytes, cells). Only the 8 bytes of each cell's x,y change."""
    cells = parse_worldmap_bin(data)
    if any(x + dx == -1 for _, x, _ in cells):
        # The file marks an empty slot with x = -1, so the game would misread it.
        raise WorldMapBinError("a cell would land on x = -1, which this file can't hold")
    out = bytearray(data)
    for at, x, y in cells:
        struct.pack_into("<ii", out, at, x + dx, y + dy)
    return bytes(out), len(cells)


# ---------------------------------------------------------------- pyramid


_BOUNDS = re.compile(r"^bounds=(-?\d+) (-?\d+) (-?\d+) (-?\d+)[ \t]*(?=\r?$)", re.MULTILINE)


def shift_pyramid_zip(data, dx, dy):
    """Rewrite pyramid.txt's bounds. Returns (bytes, old_bounds, new_bounds)."""
    tdx, tdy = dx * CELL, dy * CELL
    src = zipfile.ZipFile(io.BytesIO(data))
    names = src.namelist()
    if "pyramid.txt" not in names:
        raise ValueError("pyramid.txt missing from zip")
    text = src.read("pyramid.txt").decode("utf-8")
    m = _BOUNDS.search(text)
    if not m:
        raise ValueError("no bounds= line in pyramid.txt")
    old = tuple(int(g) for g in m.groups())
    new = (old[0] + tdx, old[1] + tdy, old[2] + tdx, old[3] + tdy)
    new_text = text[:m.start()] + "bounds=%d %d %d %d" % new + text[m.end():]
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as dst:
        for info in src.infolist():
            payload = new_text.encode("utf-8") if info.filename == "pyramid.txt" else src.read(info.filename)
            dst.writestr(info, payload, compress_type=info.compress_type)
    return buf.getvalue(), old, new


def read_pyramid_bounds(data):
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        m = _BOUNDS.search(z.read("pyramid.txt").decode("utf-8"))
        return tuple(int(g) for g in m.groups()) if m else None


# ---------------------------------------------------------------- map.info


def shift_map_info(text, dx, dy):
    tdx, tdy = dx * CELL, dy * CELL
    out = []
    for line in text.splitlines(keepends=True):
        body = line.rstrip("\r\n")
        end = line[len(body):]
        for key, delta in (("zoomX=", tdx), ("zoomY=", tdy)):
            if body.startswith(key):
                raw = body[len(key):].strip()
                try:
                    v = float(raw)
                except ValueError:
                    break
                val = str(int(v + delta)) if re.fullmatch(r"-?\d+", raw) else f"{v + delta}"
                body = key + val
                break
        out.append(body + end)
    return "".join(out)


# ---------------------------------------------------------------- lua


def decode_text(data):
    """Decode a text file without losing any byte, keeping a BOM if present."""
    bom = data.startswith(b"\xef\xbb\xbf")
    body = data[3:] if bom else data
    try:
        return body.decode("utf-8"), bom, "utf-8"
    except UnicodeDecodeError:
        return body.decode("latin-1"), bom, "latin-1"


def encode_text(text, bom, enc):
    return (b"\xef\xbb\xbf" if bom else b"") + text.encode(enc)


def shift_lua_bytes(data, dx, dy):
    text, bom, enc = decode_text(data)
    out, stats, warnings = shift_lua(text, dx * CELL, dy * CELL)
    return encode_text(out, bom, enc), stats, warnings
