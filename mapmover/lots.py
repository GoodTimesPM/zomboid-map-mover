"""Read and write the game's map cell files: X_Y.lotheader and world_X_Y.lotpack.

Formats follow the B42 game code (IsoMetaGrid.loadCell, IsoLot.load and the
zombie.pot writers). Every number is a little-endian int32.

lotheader
  ["LOTH" int version]          no magic means version 0
  int tile count, then that many tile names, each ending in "\\n"
  [one byte]                    version 0 only
  int width, int height         chunk size in squares, always 8
  int minLevel, int maxLevel    version 0: one int, the level count
  int room count, each room:
      name "\\n", int level, int rect count, rects (x y w h, cell-relative),
      int object count, objects (type x y)
  int building count, each building: int room count, then room indexes
  1024 bytes zombie intensity, byte [wx * 32 + wy] for chunk (wx, wy)

lotpack
  ["LOTP" int version]          no magic means version 0
  int chunk size (8)
  1024 table entries of 8 bytes, entry [wx * 32 + wy] holds the chunk's offset
  each chunk: levels minLevel..maxLevel, then x 0..7, then y 0..7 per square:
      int n = -1, then int k     k squares are empty, this one included
      int n > 1                  int room, then n - 1 indexes into tile names
      int n = 0 or 1             nothing more (the game never writes these)

A cell is 256 x 256 squares, 32 x 32 chunks of 8 x 8.
"""

import struct
from array import array

CELL = 256
CHUNKS = 32
CHUNK = 8
HEADER_MAGIC = b"LOTH"
PACK_MAGIC = b"LOTP"


class LotError(Exception):
    pass


class _Reader:
    def __init__(self, data):
        self.data = data
        self.pos = 0

    def int(self):
        if self.pos + 4 > len(self.data):
            raise LotError("file ends early")
        v = struct.unpack_from("<i", self.data, self.pos)[0]
        self.pos += 4
        return v

    def line(self):
        end = self.data.find(b"\n", self.pos)
        if end < 0:
            raise LotError("file ends inside a name")
        s = self.data[self.pos:end].decode("utf-8", "replace").strip()
        self.pos = end + 1
        return s

    def bytes(self, n):
        if self.pos + n > len(self.data):
            raise LotError("file ends early")
        b = self.data[self.pos:self.pos + n]
        self.pos += n
        return b


class LotHeader:
    """A parsed lotheader. Rooms keep their raw cell-relative numbers."""

    def __init__(self, tiles=None, min_level=0, max_level=0, rooms=None,
                 buildings=None, zombies=None):
        self.version = 1
        self.tiles = tiles or []
        self.min_level = min_level
        self.max_level = max_level
        # {"name", "level", "rects": [(x, y, w, h)], "objects": [(type, x, y)]}
        self.rooms = rooms or []
        self.buildings = buildings or []        # [[room index, ...]]
        self.zombies = zombies or bytes(CHUNKS * CHUNKS)


def unreadable(path):
    """Why the game can't load this lotheader, or None when it can. Some maps
    ship a leftover B41 cell (no magic, 10x10 chunks): B42 logs "invalid
    chunk width", leaves the cell's header null, and a new world then
    crashes in LotHeader.getZombieIntensityForChunk."""
    try:
        with open(path, "rb") as f:
            data = f.read()
    except OSError as e:
        return str(e)
    if data[:4] == HEADER_MAGIC:
        return None  # B42 writes the magic, always with 8x8 chunks
    try:
        read_header(data)
    except LotError as e:
        return str(e)
    except (IndexError, struct.error, UnicodeDecodeError):
        return "cut short"
    return None


def read_header(data):
    r = _Reader(data)
    if data[:4] == HEADER_MAGIC:
        r.pos = 4
    h = LotHeader()
    h.version = r.int()
    if h.version not in (0, 1):
        raise LotError(f"unknown lotheader version {h.version}")
    n = r.int()
    if n < 0 or n > len(data):
        raise LotError(f"bad tile count {n}")
    h.tiles = [r.line() for _ in range(n)]
    if h.version == 0:
        r.bytes(1)
    w, hh = r.int(), r.int()
    if (w, hh) != (CHUNK, CHUNK):
        raise LotError(f"chunk size {w}x{hh}, expected 8x8")
    if h.version == 0:
        h.min_level, h.max_level = 0, r.int() - 1
    else:
        h.min_level, h.max_level = r.int(), r.int()
    for _ in range(r.int()):
        room = {"name": r.line(), "level": r.int()}
        room["rects"] = [(r.int(), r.int(), r.int(), r.int()) for _ in range(r.int())]
        room["objects"] = [(r.int(), r.int(), r.int()) for _ in range(r.int())]
        h.rooms.append(room)
    for _ in range(r.int()):
        h.buildings.append([r.int() for _ in range(r.int())])
    h.zombies = r.bytes(CHUNKS * CHUNKS)
    return h


def write_header(h):
    out = bytearray(HEADER_MAGIC)
    out += struct.pack("<ii", 1, len(h.tiles))
    for t in h.tiles:
        out += t.encode("utf-8") + b"\n"
    out += struct.pack("<iiiii", CHUNK, CHUNK, h.min_level, h.max_level, len(h.rooms))
    for room in h.rooms:
        out += room["name"].encode("utf-8") + b"\n"
        out += struct.pack("<ii", room["level"], len(room["rects"]))
        for rect in room["rects"]:
            out += struct.pack("<4i", *rect)
        out += struct.pack("<i", len(room["objects"]))
        for obj in room["objects"]:
            out += struct.pack("<3i", *obj)
    out += struct.pack("<i", len(h.buildings))
    for b in h.buildings:
        out += struct.pack(f"<i{len(b)}i", len(b), *b)
    if len(h.zombies) != CHUNKS * CHUNKS:
        raise LotError("zombie intensity must be 1024 bytes")
    out += h.zombies
    return bytes(out)


class LotPack:
    """A lotpack held as an int array, read chunk by chunk on demand."""

    def __init__(self, data, header):
        if len(data) % 4:
            data = data[:len(data) - len(data) % 4]
        self.ints = array("i")
        self.ints.frombytes(data)
        if array("i", [1]).tobytes() != struct.pack("<i", 1):
            self.ints.byteswap()
        self.header = header
        # Table offsets in ints: version 1 has magic + version before the chunk size.
        self.table = 3 if data[:4] == PACK_MAGIC else 1
        if len(self.ints) < self.table + CHUNKS * CHUNKS * 2:
            raise LotError("lotpack too short for its chunk table")

    def chunk(self, cx, cy, levels=None):
        """{(x, y, z): [tile index, ...]} for chunk (cx, cy) of the cell.

        x and y are 0..7 inside the chunk. `levels` limits which z are kept;
        reading stops after the highest wanted level.
        """
        h = self.header
        ints = self.ints
        pos = ints[self.table + (cx * CHUNKS + cy) * 2]
        if pos % 4 or pos < 0 or pos // 4 >= len(ints):
            raise LotError(f"bad chunk offset {pos} for chunk {cx},{cy}")
        i = pos // 4
        top = h.max_level if levels is None else min(h.max_level, max(levels))
        out = {}
        skip = 0
        n_ints = len(ints)
        for z in range(h.min_level, top + 1):
            keep = levels is None or z in levels
            for x in range(CHUNK):
                for y in range(CHUNK):
                    if skip > 0:
                        skip -= 1
                        continue
                    if i >= n_ints:
                        raise LotError(f"chunk {cx},{cy} runs past the end")
                    count = ints[i]
                    i += 1
                    if count == -1:
                        skip = ints[i] - 1
                        i += 1
                        continue
                    if count > 1:
                        if keep:
                            out[(x, y, z)] = ints[i + 1:i + count].tolist()
                        i += count
        return out

    def level0(self):
        """{(x, y): [tile index, ...]} for every non-empty level 0 square of the cell."""
        out = {}
        for cx in range(CHUNKS):
            for cy in range(CHUNKS):
                ox, oy = cx * CHUNK, cy * CHUNK
                for (x, y, _), tiles in self.chunk(cx, cy, levels=(0,)).items():
                    out[(ox + x, oy + y)] = tiles
        return out


def write_pack(squares, min_level=0, max_level=0):
    """Lotpack bytes from {(x, y, z): [tile index, ...]}, x and y cell-relative 0..255."""
    by_chunk = {}
    for (x, y, z), tiles in squares.items():
        if not (0 <= x < CELL and 0 <= y < CELL and min_level <= z <= max_level):
            raise LotError(f"square {x},{y},{z} is outside the cell")
        if tiles:
            by_chunk.setdefault((x // CHUNK, y // CHUNK), {})[(x % CHUNK, y % CHUNK, z)] = tiles
    out = array("i", [0, 1, CHUNK])
    out[0] = struct.unpack("<i", PACK_MAGIC)[0]
    table = len(out)
    out.extend([0] * (CHUNKS * CHUNKS * 2))
    for cx in range(CHUNKS):
        for cy in range(CHUNKS):
            out[table + (cx * CHUNKS + cy) * 2] = len(out) * 4
            sq = by_chunk.get((cx, cy), {})
            empty = 0
            for z in range(min_level, max_level + 1):
                for x in range(CHUNK):
                    for y in range(CHUNK):
                        tiles = sq.get((x, y, z))
                        if not tiles:
                            empty += 1
                            continue
                        if empty:
                            out.extend((-1, empty))
                            empty = 0
                        out.append(len(tiles) + 1)
                        out.append(-1)
                        out.extend(tiles)
            if empty:
                out.extend((-1, empty))
    if array("i", [1]).tobytes() != struct.pack("<i", 1):
        out.byteswap()
    return out.tobytes()
