"""Connect a moved map's roads to the nearest road of the same kind.

find_exits looks along the edge of a group's footprint for roads that run
into the edge and stop there. route() searches outward from one exit for the
closest road with the same tile sheet and returns the squares to pave, as
wide as the exit. Paths move in 4 directions with a cost for each turn, so
they come out as straight runs with few corners, like the roads in the game.

Squares inside rooms or under walls are never paved. The whole width of
the road has to fit, not just its centre line. Water stops the automatic
links. A link drawn by hand may cross it: the road is paved over the water,
a flat bridge, and each water square costs a lot so it crosses where the
water is narrowest.
"""

import heapq
import time
from array import array
from collections import Counter

from .lots import CELL
from .render import sheet_of
from .world import BLOCKED, EMPTY, FENCE, PAVED, ROAD, TREE, WATER

MIN_WIDTH = 3
MAX_WIDTH = 14
LOOSE_MAX_WIDTH = 32  # a looser look for road ends takes wider ones too (an airstrip)
RADIUS = 512          # how far from the exit to look, in squares
MAX_STEPS = 700_000   # search nodes before giving up
TURN_COST = 40
STEP_COST = {EMPTY: 2, 1: 2, PAVED: 2, TREE: 5, FENCE: 12, WATER: 40}
DIRS = ((1, 0), (0, 1), (-1, 0), (0, -1))


def road_kind(tile):
    """blends_street_01_85 -> blends_street_01:5. Each row of 16 tiles on a
    road sheet is one surface (a full tile and its worn variants)."""
    sheet = sheet_of(tile)
    try:
        return f"{sheet}:{int(tile[len(sheet) + 1:]) // 16}"
    except ValueError:
        return sheet


INNER_DEPTH = 16  # a dead end inside a map has to run straight back this far
INNER_AHEAD = 8   # and have nothing built this far ahead of it
DEAD_END_INTO = (1, TREE, WATER)   # what an inner dead end may stop at: open ground, trees, water


def find_exits(sampler, home, key, offset, inner=False, loose=False):
    """Places where a group's own road stops and the road should go on.

    That is a road square of the group next to a square the group leaves
    empty, where either the empty square is outside the group's cells (the
    road runs to the edge of the map) or, at home, a road from another map
    showed through there (a map built into Kentucky joining a vanilla road).

    inner adds roads that just stop inside the map, at open ground, trees or
    water (a road up to a river bank). Those need the full width to run
    straight back INNER_DEPTH squares with no road beside it, so the stepped
    edge of a diagonal road isn't taken for a dead end, and nothing built
    just ahead, so a driveway up to a house doesn't count. They come with
    "inner": True. Only links drawn by hand use them.

    `home` samples the world with the group at its original spot, `sampler`
    with it at the planned spot. Returns [{"x", "y", "dir": (dx, dy),
    "width", "tile", "tiles"}]; x and y are the first square past the end of
    the road, at the planned spot, in the middle of its width.

    loose takes roads up to LOOSE_MAX_WIDTH wide (their "width" is capped at
    MAX_WIDTH, the road that runs on from them) and inner dead ends with
    anything ahead of them, a house or a wall too.
    """
    wide = LOOSE_MAX_WIDTH if loose else MAX_WIDTH
    own = home.group_only(key)
    cells = home.group_cells.get(key, set())
    home.preload(cells)
    rows = {}   # (dx, dy, line) -> {position across the road}
    dead = {}   # same, for roads that stop inside the map
    for (cx, cy) in cells:
        wc = own.cell(cx, cy)
        codes, hcodes = wc.codes, home.cell(cx, cy).codes
        bx, by = cx * CELL, cy * CELL
        for i in wc.road:
            lx, ly = i % CELL, i // CELL
            for dx, dy in DIRS:
                nlx, nly = lx + dx, ly + dy
                if 0 <= nlx < CELL and 0 <= nly < CELL:
                    j = nly * CELL + nlx
                    c = codes[j]
                    if c == ROAD:
                        continue
                    if inner and c == EMPTY and hcodes[j] == EMPTY:
                        pass  # the map's ground stops here though its cell goes on: its real edge
                    elif c != EMPTY or hcodes[j] != ROAD:
                        if inner and c in DEAD_END_INTO:
                            x, y = bx + lx, by + ly
                            line, pos = (x, y) if dx else (y, x)
                            dead.setdefault((dx, dy, line), set()).add(pos)
                        continue  # a road that just stops inside the map
                else:
                    nx, ny = bx + nlx, by + nly
                    c = own.code(nx, ny)
                    if c != EMPTY:
                        if inner and c in DEAD_END_INTO:
                            x, y = bx + lx, by + ly
                            line, pos = (x, y) if dx else (y, x)
                            dead.setdefault((dx, dy, line), set()).add(pos)
                        continue
                    if (nx // CELL, ny // CELL) in cells and home.code(nx, ny) != ROAD                             and not (inner and home.code(nx, ny) == EMPTY):
                        continue
                x, y = bx + lx, by + ly
                line, pos = (x, y) if dx else (y, x)
                rows.setdefault((dx, dy, line), set()).add(pos)
    ox, oy = offset[0] * CELL, offset[1] * CELL
    exits = []
    seen = set()
    for group, is_inner in ((rows, False), (dead, True)):
        for (dx, dy, line), positions in group.items():
            def at(along, depth):
                # depth 0 is the last road square, 1 the first one past it, -n further back.
                if dx:
                    return line + dx * depth, along
                return along, line + dy * depth

            runs = _runs(sorted(positions))
            if inner and not is_inner:
                # A road along the map's edge: dots where side roads join it.
                runs = [r for s0, n in runs for r in ([(s0, n)] if n <= wide else _joins(own, at, s0, n, wide))]
            for start, length in runs:
                if is_inner:
                    # Worn corners make a street end ragged: take the road's
                    # full width a few squares back, where it is whole.
                    span = _widen(own, at, start, length, wide)
                    if not span or (dx, dy, span) in seen:
                        continue
                    start, length = span
                if not MIN_WIDTH <= length <= wide:
                    continue  # a long run is a road alongside, not one that ends here
                mid = start + length // 2
                if own.code(*at(mid, -2)) != ROAD:
                    continue  # doesn't lead back into the map
                if is_inner:
                    if not _clean_end(own, at, start, length, loose):
                        continue
                    seen.add((dx, dy, span))   # one dot for each street end
                past = {p: next(k for k in range(1, 5) if own.code(*at(p, k)) != ROAD)
                        for p in range(start, start + length)}
                x, y = at(mid, past[mid])
                if any(sampler.code(at(p, k)[0] + ox, at(p, k)[1] + oy) == ROAD for p, k in past.items()):
                    continue  # still meets a road at the planned spot
                tiles = Counter(own.road_tile(*at(p, -k)) for p in range(start, start + length)
                                for k in range(4))
                tiles.pop(None, None)
                if not tiles:
                    continue
                top = tiles.most_common(1)[0][0]
                kind = road_kind(top)
                mix = {t: n for t, n in tiles.items() if road_kind(t) == kind}
                e = {"x": x + ox, "y": y + oy, "dir": (dx, dy), "width": min(length, MAX_WIDTH),
                     "tile": top, "tiles": mix}
                if is_inner:
                    e["inner"] = True
                exits.append(e)
    exits.sort(key=lambda e: (e["x"], e["y"]))
    return exits


HAND_REACH = 10       # how far from a click to look for the road
HAND_RUN = 600        # how far along a road to look for its end


def end_at(sampler, home, key, offset, x, y):
    """A road end made by hand from the group's own road near square x, y
    (at the planned spot): the end of that road nearest the click, the way
    it runs out. Returns (end, None), the end like find_exits' with "inner"
    and "hand", or (None, a reason for the user)."""
    own = home.group_only(key)
    ox, oy = offset[0] * CELL, offset[1] * CELL
    hx, hy = x - ox, y - oy
    home.preload({((hx + dx) // CELL, (hy + dy) // CELL) for dx in (-HAND_RUN, 0, HAND_RUN)
                  for dy in (-HAND_RUN, 0, HAND_RUN)} & home.group_cells.get(key, set()))
    near = [(abs(i) + abs(j), hx + i, hy + j) for i in range(-HAND_REACH, HAND_REACH + 1)
            for j in range(-HAND_REACH, HAND_REACH + 1) if own.code(hx + i, hy + j) == ROAD]
    if not near:
        return None, "No road of this map there. Alt+click right on one of its roads."
    _, rx, ry = min(near)
    cands = []
    for dx, dy in DIRS:
        k = 0
        while k < HAND_RUN and own.code(rx + dx * (k + 1), ry + dy * (k + 1)) == ROAD:
            k += 1
        line = (rx + dx * k) if dx else (ry + dy * k)
        along0 = ry if dx else rx

        def at(along, depth, dx=dx, dy=dy, line=line):
            if dx:
                return line + dx * depth, along
            return along, line + dy * depth

        # How wide the road is a few squares back from that end, where it is whole.
        back = min(RAGGED, k)
        if own.code(*at(along0, -back)) != ROAD:
            continue
        lo = hi = along0
        while own.code(*at(lo - 1, -back)) == ROAD and hi - lo < LOOSE_MAX_WIDTH:
            lo -= 1
        while own.code(*at(hi + 1, -back)) == ROAD and hi - lo < LOOSE_MAX_WIDTH:
            hi += 1
        width = hi - lo + 1
        if width > LOOSE_MAX_WIDTH:
            continue   # that way is the side of a long road, not its end
        cands.append((k, (dx, dy), at, lo, width))
    # Across its end a road is as wide as it is; across its side, as long as
    # it is. So of the ways about as narrow as the narrowest, the nearest end.
    ok = [c for c in cands if c[4] >= MIN_WIDTH] or cands
    if not ok:
        return None, "Couldn't tell which way that road runs out. Alt+click nearer the end of it."
    narrow = min(c[4] for c in ok)
    _, (dx, dy), at, lo, width = min((c for c in ok if c[4] <= narrow * 1.5 + 1), key=lambda c: c[0])
    mid = lo + width // 2
    tiles = Counter(own.road_tile(*at(p, -k)) for p in range(lo, lo + width) for k in range(4))
    tiles.pop(None, None)
    if not tiles:
        return None, "No road of this map there. Alt+click right on one of its roads."
    top = tiles.most_common(1)[0][0]
    kind = road_kind(top)
    ex, ey = at(mid, 1)
    return {"x": ex + ox, "y": ey + oy, "dir": (dx, dy), "width": max(MIN_WIDTH, min(width, MAX_WIDTH)),
            "tile": top, "tiles": {t: n for t, n in tiles.items() if road_kind(t) == kind},
            "inner": True, "hand": True}, None


def find_cut_roads(sampler, key, cells):
    """Roads from under a map (vanilla, or a map on a lower layer) that stop
    where this map's own squares begin, at its planned spot. cells: the
    map's planned cells. Returns ends like find_exits', with "cut": True;
    x and y are the last road square and dir points into the map."""
    own = sampler.group_only(key)
    ring = {(cx + dx, cy + dy) for cx, cy in cells for dx in (-1, 0, 1) for dy in (-1, 0, 1)}
    sampler.preload(ring)
    own.preload([c for c in ring if c in own.sources])
    edge = {}   # road square -> the way into the map
    for cx, cy in ring:
        bx, by = cx * CELL, cy * CELL
        for i in sampler.cell(cx, cy).road:
            x, y = bx + i % CELL, by + i // CELL
            if own.code(x, y) != EMPTY:
                continue  # the map's own road, or the map covers it
            for dx, dy in DIRS:
                c = own.code(x + dx, y + dy)
                if c != EMPTY and c != ROAD and sampler.code(x + dx, y + dy) != ROAD:
                    edge[(x, y)] = (dx, dy)
    # Squares touching corner to corner are one road: a diagonal road meets
    # the map's edge in steps, not in one straight row.
    ends = []
    left = set(edge)
    while left:
        group = [left.pop()]
        for x, y in group:
            for nx in (x - 1, x, x + 1):
                for ny in (y - 1, y, y + 1):
                    if (nx, ny) in left:
                        left.remove((nx, ny))
                        group.append((nx, ny))
        dirs = Counter(edge[q] for q in group)
        (dx, dy), across = dirs.most_common(1)[0]
        # A diagonal road meets a straight edge along more squares than it is wide.
        if not MIN_WIDTH <= across <= 3 * MAX_WIDTH or len(group) > 3 * MAX_WIDTH:
            continue  # a scrap of road, or a road running along the map's side
        gx = sum(q[0] for q in group) / len(group)
        gy = sum(q[1] for q in group) / len(group)
        x, y = min(group, key=lambda q: (q[0] - gx) ** 2 + (q[1] - gy) ** 2)
        if not any(sampler.code(x - dx * k, y - dy * k) == ROAD for k in (2, 3)):
            continue  # a corner of road, not one running into the map
        tiles = Counter(sampler.road_tile(qx - dx * k, qy - dy * k) for qx, qy in group for k in range(4))
        tiles.pop(None, None)
        if not tiles:
            continue
        top = tiles.most_common(1)[0][0]
        kind = road_kind(top)
        ends.append({"x": x, "y": y, "dir": (dx, dy), "width": min(across, MAX_WIDTH), "tile": top,
                     "tiles": {t: n for t, n in tiles.items() if road_kind(t) == kind}, "cut": True})
    ends.sort(key=lambda e: (e["x"], e["y"]))
    return ends


RAGGED = 3     # how far the corners of a street end may stick out past the rest
DRIVEWAY = 8   # how wide a driveway or side street off the end may be
CLEAR_SIDE = 3  # squares either side of a street's tip that have to be free of buildings


def _widen(own, at, start, length, wide=MAX_WIDTH):
    """The road's whole width RAGGED squares back from a dead end run, as
    (start, length), or None when there is no road there."""
    mid = start + length // 2
    if own.code(*at(mid, -RAGGED)) != ROAD:
        return None
    lo = hi = mid
    while own.code(*at(lo - 1, -RAGGED)) == ROAD and hi - lo < wide:
        lo -= 1
    while own.code(*at(hi + 1, -RAGGED)) == ROAD and hi - lo < wide:
        hi += 1
    if lo > start + length - 1 or hi < start:
        return None
    return lo, hi - lo + 1


def _clean_end(own, at, start, length, loose=False):
    """A real dead end: road across the whole width, but for a few worn
    squares, for INNER_DEPTH squares back, nothing but non-road on both
    sides of it beyond a driveway or side street, and mostly open ground,
    trees or water for INNER_AHEAD squares ahead. The last RAGGED squares
    may be worn away at the corners. Gravel, the odd fence post or a mailbox
    ahead is fine; a wall across the way, a yard or another road is not,
    and nor is a building close by (a driveway up to a house). loose lets
    all that ahead go: only the road going on, or one alongside, rule it out."""
    worn = max(1, length // 3)
    run = [0, 0]   # road squares in a row along each side
    for k in range(RAGGED, RAGGED + INNER_DEPTH):
        if sum(own.code(*at(p, -k)) != ROAD for p in range(start, start + length)) > worn:
            return False
        for i, p in enumerate((start - 1, start + length)):
            run[i] = run[i] + 1 if own.code(*at(p, -k)) == ROAD else 0
            if run[i] > DRIVEWAY:
                return False   # a road alongside, not a driveway off the side
    tip = {}
    for p in range(start - 1, start + length + 1):
        k = 1
        while own.code(*at(p, k)) == ROAD:
            if k > RAGGED or p in (start - 1, start + length):
                return False   # the road goes on, or turns
            k += 1
        tip[p] = k
    if loose:
        return True
    other = 0
    for j in range(INNER_AHEAD):
        across = [own.code(*at(p, k + j)) for p, k in tip.items()]
        if ROAD in across:
            return False
        if sum(c not in DEAD_END_INTO for c in across) * 2 > len(across):
            return False   # a wall or a fence across the way
        other += sum(c not in DEAD_END_INTO for c in across)
    if other * 4 > len(tip) * INNER_AHEAD:
        return False
    # A driveway ends at a house; a street that runs out has nothing built
    # just past its tip, give or take a mailbox or a sign.
    built = 0
    for p in range(start - CLEAR_SIDE, start + length + CLEAR_SIDE):
        for k in range(1, INNER_AHEAD + 4):
            built += own.code(*at(p, k)) == BLOCKED
    return built <= 6


def _joins(own, at, start, length, wide=MAX_WIDTH):
    """Side roads that join a long road running along the map's edge, as
    (start, length) runs across them. The road along the edge is as deep as
    most of it; a side road runs on back well past that."""
    reach = 4 * INNER_DEPTH
    depth = {}
    for p in range(start, start + length):
        k = 0
        while k < reach and own.code(*at(p, -k)) == ROAD:
            k += 1
        depth[p] = k
    along = sorted(depth.values())[len(depth) // 2]
    deep = [p for p, k in depth.items() if k >= along + INNER_DEPTH // 2]
    return [r for r in _runs(deep) if MIN_WIDTH <= r[1] <= wide]


def _runs(sorted_positions):
    out = []
    start = prev = None
    for p in sorted_positions:
        if prev is not None and p == prev + 1:
            prev = p
            continue
        if start is not None:
            out.append((start, prev - start + 1))
        start = prev = p
    if start is not None:
        out.append((start, prev - start + 1))
    return out


class _Window:
    """Square codes for a box of the world, plus prefix sums of obstacles so
    "is this whole cross-section clear" costs two lookups.

    owns: samplers of groups whose own squares the road must not cross.
    bridges: water can be paved over."""

    def __init__(self, sampler, owns, x0, y0, w, h, bridges=False):
        self.x0, self.y0, self.w, self.h = x0, y0, w, h
        self.stop = (BLOCKED,) if bridges else (BLOCKED, WATER)
        codes = bytearray(w * h)
        mines = [bytearray(w * h) for _ in owns]
        cx0, cy0 = x0 // CELL, y0 // CELL
        cx1, cy1 = (x0 + w - 1) // CELL, (y0 + h - 1) // CELL
        span = [(cx, cy) for cx in range(cx0, cx1 + 1) for cy in range(cy0, cy1 + 1)]
        sampler.preload(span)
        for o in owns:
            o.preload([c for c in span if c in o.sources])
        for cx in range(cx0, cx1 + 1):
            for cy in range(cy0, cy1 + 1):
                ax, bx = max(x0, cx * CELL), min(x0 + w, (cx + 1) * CELL)
                pairs = [(sampler.cell(cx, cy).codes, codes)]
                pairs += [(o.cell(cx, cy).codes, m) for o, m in zip(owns, mines) if (cx, cy) in o.sources]
                for src, dst in pairs:
                    for wy in range(max(y0, cy * CELL), min(y0 + h, (cy + 1) * CELL)):
                        s = (wy - cy * CELL) * CELL + (ax - cx * CELL)
                        d = (wy - y0) * w + (ax - x0)
                        dst[d:d + bx - ax] = src[s:s + bx - ax]
        # Cells where the road links mod would crash the world load count as
        # forbidden squares too: never crossed, never paved.
        nogo = [c for c in span if c in sampler.no_go]
        if nogo:
            m = bytearray(w * h)
            for cx, cy in nogo:
                ax, bx = max(x0, cx * CELL), min(x0 + w, (cx + 1) * CELL)
                for wy in range(max(y0, cy * CELL), min(y0 + h, (cy + 1) * CELL)):
                    d = (wy - y0) * w + (ax - x0)
                    m[d:d + bx - ax] = b"\x01" * (bx - ax)
            mines.append(m)
        # Those groups' squares are off limits: the road leaves the map.
        nonzero = bytes([0] + [1] * 255)
        own = 0
        for m in mines:
            own |= int.from_bytes(m.translate(nonzero), "little")
        self.codes = codes
        self.own = own.to_bytes(w * h, "little")
        bad = bytes(1 if c in self.stop else 0 for c in range(256))
        obst = (int.from_bytes(codes.translate(bad), "little") | own).to_bytes(w * h, "little")
        # rowsum[y*(w+1)+x] = obstacles in row y left of x; colsum likewise down a column.
        rows = array("i", bytes(4 * h * (w + 1)))
        for y in range(h):
            acc = 0
            base, ob = y * (w + 1), y * w
            for x in range(w):
                acc += obst[ob + x]
                rows[base + x + 1] = acc
        cols = array("i", bytes(4 * w * (h + 1)))
        for x in range(w):
            acc = 0
            for y in range(h):
                acc += obst[y * w + x]
                cols[x * (h + 1) + y + 1] = acc
        self.rows, self.cols = rows, cols

    def inside(self, x, y):
        return 0 <= x - self.x0 < self.w and 0 <= y - self.y0 < self.h

    def code(self, x, y):
        return self.codes[(y - self.y0) * self.w + (x - self.x0)]

    def mine(self, x, y):
        return self.own[(y - self.y0) * self.w + (x - self.x0)]

    def clear_row(self, x, y, a, b):
        """No obstacles from x+a to x+b on row y."""
        lx, ly = x - self.x0, y - self.y0
        if lx + a < 0 or lx + b >= self.w:
            return False
        base = ly * (self.w + 1)
        return self.rows[base + lx + b + 1] == self.rows[base + lx + a]

    def clear_col(self, x, y, a, b):
        lx, ly = x - self.x0, y - self.y0
        if ly + a < 0 or ly + b >= self.h:
            return False
        base = lx * (self.h + 1)
        return self.cols[base + ly + b + 1] == self.cols[base + ly + a]


def _cross(width):
    """Offsets across a road of this width, matching how exits are centred."""
    a = -(width // 2)
    return a, a + width - 1


def _fitter(win, width):
    a, b = _cross(width)

    def fits(x, y, d):
        if not win.inside(x, y):
            return False
        if d % 2 == 0:   # moving along x: the road crosses a column
            return win.clear_col(x, y, a, b)
        return win.clear_row(x, y, a, b)
    return fits


def _search(win, fits, start, goal, heuristic=None, max_steps=MAX_STEPS, soft=0, avoid=None, out=None):
    """Cheapest path of (x, y, dir) nodes from start to a node where goal()
    is true, or None. With a heuristic it is A*: much faster over open
    ground, and the path can come out a little longer than the best.

    soft: a step where the road doesn't fit (a wall in the way) costs this
    much more instead of being refused. avoid: extra cost a square, a
    bytearray over the window, to push the road away from earlier routes.
    out: a dict that gets "capped" set when it gave up after max_steps."""
    hf = heuristic or (lambda x, y: 0)
    dist = {start: 0}
    prev = {}
    heap = [(hf(start[0], start[1]), 0, start)]
    steps = 0
    end = None
    while heap:
        _, cost, node = heapq.heappop(heap)
        if dist.get(node) != cost:
            continue
        x, y, d = node
        if goal(x, y, d):
            end = node
            break
        steps += 1
        if steps > max_steps:
            if out is not None:
                out["capped"] = True
            break
        for nd in (d, (d + 1) % 4, (d + 3) % 4):
            dx, dy = DIRS[nd]
            nx, ny = x + dx, y + dy
            if not win.inside(nx, ny) or win.mine(nx, ny):
                continue
            # The corner square block has to be clear too.
            if not fits(nx, ny, nd) or (nd != d and not fits(x, y, nd)):
                if not soft:
                    continue
                step = soft
            else:
                step = 0
            c = win.code(nx, ny)
            step += 0 if c == ROAD else STEP_COST.get(c, 2)
            if avoid is not None:
                step += avoid[(ny - win.y0) * win.w + (nx - win.x0)]
            nc = cost + step + (TURN_COST if nd != d else 0)
            k = (nx, ny, nd)
            if nc < dist.get(k, 1 << 60):
                dist[k] = nc
                prev[k] = node
                heapq.heappush(heap, (nc + hf(nx, ny), nc, k))
    if end is None:
        return None
    nodes = []
    k = end
    while k != start:
        nodes.append(k)
        k = prev[k]
    nodes.append(start)
    nodes.reverse()
    return nodes


def _pave(win, nodes, width, mix):
    """{(x, y): tile} for a road of this width along nodes. The last node is
    where the road arrives; each lane carries on from there until it meets
    a road (within 2 widths), so a wide road meeting a curve or a narrower
    road leaves no gap. Walls and the forbidden maps' squares are never
    paved over, water only when the window allows bridges."""
    a, b = _cross(width)
    mix = sorted(mix.items())
    total = sum(n for _, n in mix)
    squares = {}

    def pave(x, y):
        if not win.inside(x, y) or win.mine(x, y):
            return
        c = win.code(x, y)
        if c == ROAD or c in win.stop:
            return
        # Spread the worn variants like the map does, the same way every run.
        r = ((x * 73856093) ^ (y * 19349663)) % total
        for t, n in mix:
            r -= n
            if r < 0:
                squares[(x, y)] = t
                break

    last_d = None
    for x, y, d in nodes[:-1]:
        if last_d is not None and d != last_d:
            for oy in range(a, b + 1):
                for ox in range(a, b + 1):
                    pave(x + ox, y + oy)
        for o in range(a, b + 1):
            if d % 2 == 0:
                pave(x, y + o)
            else:
                pave(x + o, y)
        last_d = d
    x, y, d = nodes[-1]
    dx, dy = DIRS[d]
    for o in range(a, b + 1):
        px, py = (x, y + o) if d % 2 == 0 else (x + o, y)
        lane = []
        met = False
        for _ in range(2 * width):
            if not win.inside(px, py) or win.mine(px, py):
                break
            c = win.code(px, py)
            if c == ROAD:
                met = True
                break
            if c in win.stop:
                break
            lane.append((px, py))
            px, py = px + dx, py + dy
        # No road ahead: the road just ends at the last node.
        for q in (lane if met else lane[:1]):
            pave(*q)
    return squares


def _mix(exit):
    return exit.get("tiles") or {exit["tile"]: 1}


def route(sampler, key, exit, radius=RADIUS, max_steps=MAX_STEPS, like=True):
    """Squares to pave from one exit to the nearest road of its kind (any
    street when like is False).

    Returns {"target": (x, y), "path": [(x, y)], "squares": {(x, y): tile}}
    or None when nothing is in reach.
    """
    width, tile = exit["width"], exit["tile"]
    kind = road_kind(tile)
    own = sampler.group_only(key)
    sx, sy = exit["x"], exit["y"]
    win = _Window(sampler, [own], sx - radius, sy - radius, 2 * radius + 1, 2 * radius + 1)
    fits = _fitter(win, width)

    def is_target(x, y, d):
        if win.code(x, y) != ROAD or win.mine(x, y):
            return False
        t = sampler.road_tile(x, y)
        if not t or (road_kind(t) != kind if like else not t.startswith("blends_street")):
            return False
        n = 0
        for yy in range(y - 3, y + 4):
            for xx in range(x - 3, x + 4):
                if win.inside(xx, yy) and win.code(xx, yy) == ROAD and not win.mine(xx, yy):
                    n += 1
        return n >= 15  # a real road, not a few stray tiles

    d0 = DIRS.index(tuple(exit["dir"]))
    if not fits(sx, sy, d0):
        return None
    nodes = _search(win, fits, (sx, sy, d0), is_target, max_steps=max_steps)
    if nodes is None:
        return None
    path = [(x, y) for x, y, _ in nodes]
    return {"target": path[-1], "path": _corners(path), "squares": _pave(win, nodes, width, _mix(exit)),
            "length": len(path) - 1}


MANUAL_MARGIN = 96           # squares around the two ends the road may swing out
MANUAL_MAX_AREA = 6_000_000  # squares in the search box, about 2400 by 2400
MANUAL_STEPS = 2_000_000


GUESS = 3            # what a square still to go is guessed to cost: above the 2 of
                     # open ground, so a long link through woods is found in seconds
SOFT_COST = 200      # a step through a wall, for the road of last resort
AVOID_COST = 3       # more a square near an earlier option, so the next goes another way
OPTIONS = 3          # roads to offer for one link
OPTIONS_SECONDS = 25 # once there's one, stop looking for more after this


def route_between(sampler, a, b, forbid, max_steps=MANUAL_STEPS):
    """The best road from road end a to b, as wide as a, that keeps off
    walls and the forbidden maps. Returns (result, None) with a result like
    route()'s, or (None, a reason for the user)."""
    opts, err = route_options(sampler, a, b, forbid, max_steps, want=1, relax=False)
    return (opts[0], None) if opts else (None, err)


def route_options(sampler, a, b, forbid, max_steps=MANUAL_STEPS, want=OPTIONS, relax=True):
    """Up to want different roads from road end a to b, best first.

    b is another road end (the road arrives head on, running into it, or
    from the side when there's no room for that) or any square (no "dir").
    forbid: group keys whose own squares the road must not cross.

    The first is the cheapest. The rest are pushed away from the ones before
    so they go another way. When no road as wide as a gets through and relax
    is true, there is still something to pick: a narrower road, one paved
    across the forbidden maps' ground ("cross": steps on it), and if
    nothing else, one straight through what's in the way ("gaps": steps of
    road under walls, left as they are).

    Each option is like route()'s result plus "kind" (best, other, narrow,
    cross, through), "width", "cross" and "gaps". Returns (options, None) or
    ([], a reason for the user).
    """
    width = a["width"]
    ax, ay, bx, by = a["x"], a["y"], b["x"], b["y"]
    m = MANUAL_MARGIN + width
    x0, y0 = min(ax, bx) - m, min(ay, by) - m
    x1, y1 = max(ax, bx) + m + 1, max(ay, by) + m + 1
    if (x1 - x0) * (y1 - y0) > MANUAL_MAX_AREA:
        return [], "Those two points are too far apart. Keep a link under about 2000 squares (8 cells) each way."
    owns = [sampler.group_only(k) for k in forbid]
    # Room to go around the maps in the way: their cells near the two points,
    # plus the margin, as long as the box stays small enough to search.
    reach = 4 * CELL
    for o in owns:
        for cx, cy in o.sources:
            if x0 - reach <= cx * CELL < x1 + reach and y0 - reach <= cy * CELL < y1 + reach:
                nx0, ny0 = min(x0, cx * CELL - m), min(y0, cy * CELL - m)
                nx1, ny1 = max(x1, (cx + 1) * CELL + m), max(y1, (cy + 1) * CELL + m)
                if (nx1 - nx0) * (ny1 - ny0) <= MANUAL_MAX_AREA:
                    x0, y0, x1, y1 = nx0, ny0, nx1, ny1
    for (px, py), which in (((ax, ay), "first road end"), ((bx, by), "second end")):
        if px < 0:
            return [], (f"The {which} is west of x 0, where the game can't load map cells. "
                        "Pick a spot further east.")
        if (px // CELL, py // CELL) in sampler.no_go:
            return [], (f"The {which} is in cell {px // CELL},{py // CELL}, where a road would crash the "
                        "game's world load (a 42.x bug with the maps stacked there). Pick another spot.")
    w, h = x1 - x0, y1 - y0
    strict = _Window(sampler, owns, x0, y0, w, h, bridges=True)
    if not b.get("dir") and strict.mine(bx, by):
        return [], "That spot is inside the map the road starts from. Pick a spot outside it."
    da = DIRS.index(tuple(a["dir"]))
    head = DIRS.index((-b["dir"][0], -b["dir"][1])) if b.get("dir") else None
    # Open ground costs 2 a square, so guess 2 a square: far fewer squares searched.
    guess = lambda x, y: GUESS * (abs(x - bx) + abs(y - by))
    why = set()
    stats = {}

    def run(win, wd, soft=0, avoid=None):
        fits = _fitter(win, wd)
        if not soft and not fits(ax, ay, da):
            why.add("start")
            return None
        # Run into b head on when there's room, else arrive from the side.
        for bd in ([head] if head is not None and fits(bx, by, head) else []) + [None]:
            nodes = _search(win, fits, (ax, ay, da),
                            lambda x, y, d: x == bx and y == by and (bd is None or d == bd),
                            guess, max_steps, soft, avoid, stats)
            if nodes:
                return nodes
        why.add("way")
        return None

    opts = []
    t0 = time.monotonic()

    def late():
        return bool(opts) and time.monotonic() - t0 > OPTIONS_SECONDS

    def add(win, wd, nodes, kind, soft=0):
        path = [(x, y) for x, y, _ in nodes]
        # Two roads within a few squares of each other most of the way are the same choice.
        near = {(x // 8, y // 8) for x, y in path}
        for o in opts:
            if len(near & o["_near"]) > 0.75 * min(len(near), len(o["_near"])):
                return
        squares = _pave(win, nodes, wd, _mix(a))
        gaps = 0
        if soft:
            fits = _fitter(strict, wd)
            gaps = sum(1 for x, y, d in nodes[1:] if not fits(x, y, d))
        cross = sum(1 for x, y, _ in nodes if strict.mine(x, y)) if win is not strict else 0
        opts.append({"target": path[-1], "path": _corners(path), "squares": squares, "length": len(path) - 1,
                     "bridge": sum(win.code(x, y) == WATER for x, y in squares), "kind": kind,
                     "width": wd, "cross": cross, "gaps": gaps, "_near": near})

    avoid = bytearray(w * h)

    def spread(win, wd, nodes, kind, soft=0):
        """Keep this road, then look for others like it: the ground near
        each road found so far costs more, so the next one goes another way."""
        add(win, wd, nodes, kind, soft)
        r = max(12, 2 * wd)
        fill = bytes([AVOID_COST]) * (2 * r + 1)
        for _ in range(2 * want):
            if len(opts) >= want or late():
                return
            for x, y, _d in nodes:
                lx, ly = x - x0, y - y0
                ax0, ax1 = max(0, lx - r), min(w, lx + r + 1)
                for yy in range(max(0, ly - r), min(h, ly + r + 1)):
                    avoid[yy * w + ax0:yy * w + ax1] = fill[:ax1 - ax0]
            nodes = run(win, wd, soft, avoid)
            if not nodes:
                return
            add(win, wd, nodes, "other" if kind == "best" else kind, soft)

    nodes = run(strict, width)
    if nodes:
        spread(strict, width, nodes, "best")
    if not opts and not relax:
        if stats.get("capped"):
            return [], ("Gave up looking: the way between those two points winds too much to search. "
                        "Try a spot closer by.")
        if "start" in why:
            return [], "Something is in the way right at the first road end (a wall or another map)."
        return [], ("No way through for a road this wide. Walls and the maps' own squares "
                    "block it. Try a spot further out.")
    if not opts:
        # A search that ran out of steps would only run out again narrower.
        for wd in sorted({*range(width - 2, MIN_WIDTH, -2), MIN_WIDTH}, reverse=True):
            if wd < width and not stats.get("capped"):
                nodes = run(strict, wd, avoid=avoid)
                if nodes:
                    spread(strict, wd, nodes, "narrow")
                    break
        free = strict
        if forbid and len(opts) < want and not late():
            # The maps' own squares are the trouble: pave across them.
            free = _Window(sampler, [], x0, y0, w, h, bridges=True)
            nodes = run(free, width, avoid=avoid)
            if nodes:
                spread(free, width, nodes, "cross")
        if len(opts) < want and not late():
            # Always something to pick: straight through what's in the way.
            nodes = run(free, width, SOFT_COST, avoid)
            if nodes:
                spread(free, width, nodes, "through", SOFT_COST)
    if not opts and stats.get("capped"):
        return [], ("Gave up looking: the way between those two points winds too much to search. "
                    "Try a spot closer by, or link it in two steps through a spot halfway.")
    if not opts:
        return [], "No way at all between those two points in the search box. Try a spot closer by."
    for o in opts:
        del o["_near"]
    return opts, None


def _expand(corners, start_dir):
    """Corner points back to the full list of (x, y, dir) nodes."""
    x, y = corners[0]
    nodes = [(x, y, DIRS.index(tuple(start_dir)))]
    for (px, py), (qx, qy) in zip(corners, corners[1:]):
        if px != qx and py != qy:
            raise ValueError("path has a diagonal step")
        dx, dy = (qx > px) - (qx < px), (qy > py) - (qy < py)
        if (dx, dy) == (0, 0):
            continue
        d = DIRS.index((dx, dy))
        while (x, y) != (qx, qy):
            x, y = x + dx, y + dy
            nodes.append((x, y, d))
    return nodes


def pave_path(sampler, corners, start_dir, width, mix, forbid, check=True):
    """Squares for a road routed earlier, or None when the world changed
    under it and something is in the way now. check False: a road picked
    to run through what's in the way, paved where it can be."""
    try:
        nodes = _expand(corners, start_dir)
    except ValueError:
        return None
    m = 3 * width + 2
    xs, ys = [n[0] for n in nodes], [n[1] for n in nodes]
    x0, y0 = min(xs) - m, min(ys) - m
    w, h = max(xs) - x0 + m + 1, max(ys) - y0 + m + 1
    if w * h > MANUAL_MAX_AREA:
        return None
    win = _Window(sampler, [sampler.group_only(k) for k in forbid], x0, y0, w, h, bridges=True)
    fits = _fitter(win, width)
    for i, (x, y, d) in enumerate(nodes if check else ()):
        if not fits(x, y, d) or (i and win.mine(x, y)):
            return None
        if i and d != nodes[i - 1][2] and not fits(nodes[i - 1][0], nodes[i - 1][1], d):
            return None
    return _pave(win, nodes, width, mix)


def _corners(path):
    """Just the points where the path turns, plus both ends."""
    if len(path) < 3:
        return list(path)
    out = [path[0]]
    for p, q, r in zip(path, path[1:], path[2:]):
        if (q[0] - p[0], q[1] - p[1]) != (r[0] - q[0], r[1] - q[1]):
            out.append(q)
    out.append(path[-1])
    return out


def connect_group(sampler, home, key, offset, log=print, skip=()):
    """Routes for every exit of one group, nearest like road first, then any
    street. Exits at a square in skip (ones linked by hand) are left alone.
    Returns (routes, exits that found nothing)."""
    out, missed = [], []
    for e in find_exits(sampler, home, key, offset):
        if (e["x"], e["y"]) in skip:
            continue
        r = route(sampler, key, e)
        like = r is not None
        if r is None:
            r = route(sampler, key, e, like=False)
        if r is None:
            log(f"no road within {RADIUS} squares of the road end at {e['x']},{e['y']}")
            missed.append([e["x"], e["y"]])
            continue
        out.append({"exit": [e["x"], e["y"]], "dir": list(e["dir"]), "width": e["width"],
                    "tile": e["tile"], "like": like, "target": list(r["target"]),
                    "length": r["length"], "path": [list(p) for p in r["path"]],
                    "squares": r["squares"]})
    return out, missed
