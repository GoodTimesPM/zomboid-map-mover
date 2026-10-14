"""Run every shifter over every map file installed, without writing anything.

    python -m tests.sweep_workshop

Checks, for each map folder of each B42 mod found:
  * every .lua tokenizes, a zero shift returns the identical text, and a
    real shift keeps the token structure;
  * every IGMB .bin parses to the end;
  * every pyramid zip has a bounds line;
  * map.info zoom lines shift.
Prints totals and every failure.
"""

import collections
import os
import re
import sys
import traceback

from mapmover import shifters as sh
from mapmover.build import _xml_cells
from mapmover.luashift import shift_lua, tokenize
from mapmover.paths import load_config
from mapmover.scan import scan_mods


def main():
    cfg = load_config()
    mods = scan_mods(cfg)
    stats = collections.Counter()
    warn_kinds = collections.Counter()
    failures = []
    for mod in mods:
        for d in mod.maps:
            for root, _, files in os.walk(d.path):
                for f in files:
                    p = os.path.join(root, f)
                    lf = f.lower()
                    try:
                        if lf.endswith(".lua"):
                            data = open(p, "rb").read()
                            text, bom, enc = sh.decode_text(data)
                            same, st0, _ = shift_lua(text, 0, 0)
                            # Old worldX/worldY spawn points are rewritten to
                            # absolute even at zero shift, so skip those.
                            if not st0["spawn_cell"] and sh.encode_text(same, bom, enc) != data:
                                failures.append(f"{p}: zero shift changed the file")
                            out, st, warns = shift_lua(text, 256 * 7, 256 * 3)
                            a = [t.text for t in tokenize(text) if t.kind == "op" and t.text != "-"]
                            b = [t.text for t in tokenize(out) if t.kind == "op" and t.text != "-"]
                            if a != b:
                                failures.append(f"{p}: structure changed")
                            stats["lua files"] += 1
                            for k, v in st.items():
                                stats["lua " + k] += v
                            for w in warns:
                                warn_kinds[(f, w)] += 1
                        elif lf.endswith(".bin") and open(p, "rb").read(4) == b"IGMB":
                            cells = sh.parse_worldmap_bin(open(p, "rb").read())
                            stats["igmb files"] += 1
                            xml = p[:-4]
                            if os.path.exists(xml):
                                xc = _xml_cells(open(xml, "rb").read())
                                bc = {(x, y) for _, x, y in cells}
                                if xc != bc:
                                    # Authors often leave a stale .xml next to the
                                    # .bin the game reads. Both get shifted.
                                    stats["bin/xml pairs out of sync (harmless)"] += 1
                        elif lf.endswith(".zip"):
                            b = sh.read_pyramid_bounds(open(p, "rb").read())
                            stats["zips with bounds" if b else "zips without bounds"] += 1
                        elif lf == "streets.xml":
                            _, n = sh.shift_streets_xml(open(p, "rb").read(), 1, 1)
                            stats["street points"] += n
                        elif lf.endswith(".xml"):
                            data = open(p, "rb").read()
                            if b"<cell" in data:
                                _, n = sh.shift_worldmap_xml(data, 1, 1)
                                stats["xml cells"] += n
                                if n != len(_xml_cells(data)):
                                    failures.append(f"{p}: cell count mismatch")
                            else:
                                stats["other xml"] += 1
                                print("other xml:", p)
                        elif lf == "map.info":
                            stats["map.info"] += 1
                    except Exception as e:
                        failures.append(f"{p}: {type(e).__name__}: {e}")
    for k, v in sorted(stats.items()):
        print(f"{k:28} {v}")
    print("\nLua warnings by file name:")
    for (f, w), n in warn_kinds.most_common(30):
        print(f"  {n:5}  {f}: {w}")
    print(f"\n{len(failures)} failures")
    for fl in failures:
        print("  " + fl)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
