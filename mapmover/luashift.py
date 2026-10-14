"""Shift the absolute tile coordinates inside a map folder's Lua files.

The game reads these Lua shapes from a map folder, and each one stores
absolute tile positions:

  objects.lua, regions.lua, roomtones.lua, worldobjects.lua
      { type = "...", x = 13200, y = 8700, width = 50, ... }
      { type = "...", geometry = "polygon", points = { x1, y1, x2, y2, ... } }
  basements.lua
      { x = 10044, y = 7325, stairDir = "W", choices = {...} }
  spawnpoints.lua (and any file with a SpawnPoints() function)
      { worldX = 58, worldY = 43, posX = 259, posY = 168, posZ = 0 }
          worldX/worldY are legacy 300-tile cells: tile = worldX * 300 + posX
      { posX = 14848, posY = 11264, posZ = 0 }
  worldmap-annotations.lua
      symbolsAPI:addUntranslatedText("TEXT", "layer", 16200, 11456)

Everything else in these files is either relative (stairx, StairX, width,
height) or not positional at all, and is left byte-for-byte alone. The file
is tokenized so that comments and strings are never touched, and the output
is the original text with only the shifted number tokens replaced.
"""

import re

# Methods on the world-map symbols API that take (..., x, y, ...).
# The first two plain number arguments are the position.
ANNOTATION_METHODS = {
    "addTranslatedText",
    "addUntranslatedText",
    "addTexture",
    "addText",
}

_POSITION_FIELDS = {"x", "y", "posX", "posY", "worldX", "worldY"}

_NUMBER = re.compile(
    r"0[xX][0-9a-fA-F]+|(?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?"
)
_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_LONG_OPEN = re.compile(r"\[(=*)\[")
_WS = re.compile(r"\s+")
_OPS3 = ("...",)
_OPS2 = ("==", "~=", "<=", ">=", "..", "::", "//", ">>", "<<")


class LuaTokenError(Exception):
    pass


class Tok:
    __slots__ = ("kind", "text")

    def __init__(self, kind, text):
        self.kind = kind  # ws, comment, string, number, name, op
        self.text = text

    def __repr__(self):
        return f"Tok({self.kind},{self.text!r})"


def tokenize(src):
    toks = []
    i = 0
    n = len(src)
    while i < n:
        c = src[i]
        m = _WS.match(src, i)
        if m:
            toks.append(Tok("ws", m.group()))
            i = m.end()
            continue
        if src.startswith("--", i):
            m = _LONG_OPEN.match(src, i + 2)
            if m:
                close = "]" + m.group(1) + "]"
                j = src.find(close, m.end())
                if j < 0:
                    raise LuaTokenError(f"unclosed long comment at offset {i}")
                j += len(close)
            else:
                j = src.find("\n", i)
                if j < 0:
                    j = n
            toks.append(Tok("comment", src[i:j]))
            i = j
            continue
        if c == "[":
            m = _LONG_OPEN.match(src, i)
            if m:
                close = "]" + m.group(1) + "]"
                j = src.find(close, m.end())
                if j < 0:
                    raise LuaTokenError(f"unclosed long string at offset {i}")
                j += len(close)
                toks.append(Tok("string", src[i:j]))
                i = j
                continue
        if c in "\"'":
            j = i + 1
            while j < n:
                d = src[j]
                if d == "\\":
                    j += 2
                    continue
                if d == c:
                    j += 1
                    break
                if d == "\n":
                    raise LuaTokenError(f"unfinished string at offset {i}")
                j += 1
            else:
                raise LuaTokenError(f"unfinished string at offset {i}")
            toks.append(Tok("string", src[i:j]))
            i = j
            continue
        if c.isdigit() or (c == "." and i + 1 < n and src[i + 1].isdigit()):
            m = _NUMBER.match(src, i)
            toks.append(Tok("number", m.group()))
            i = m.end()
            continue
        m = _NAME.match(src, i)
        if m:
            toks.append(Tok("name", m.group()))
            i = m.end()
            continue
        for op in _OPS3 + _OPS2:
            if src.startswith(op, i):
                toks.append(Tok("op", op))
                i += len(op)
                break
        else:
            toks.append(Tok("op", c))
            i += 1
    return toks


def _num_value(text):
    if text[:2] in ("0x", "0X"):
        return int(text, 16)
    if re.fullmatch(r"\d+", text):
        return int(text)
    return float(text)


def _format_like(original, value):
    """Format value the way the original literal was written."""
    if isinstance(value, int) or re.fullmatch(r"\d+", original):
        return str(int(round(value)))
    m = re.fullmatch(r"\d*\.(\d*)", original)
    if m:
        places = len(m.group(1))
        return f"{value:.{places}f}"
    return repr(float(value))


class _NumRef:
    """A number literal in the token list, with an optional leading minus."""

    __slots__ = ("minus_idx", "num_idx")

    def __init__(self, minus_idx, num_idx):
        self.minus_idx = minus_idx
        self.num_idx = num_idx


class LuaShifter:
    def __init__(self, src, dx, dy):
        self.toks = tokenize(src)
        self.dx = dx
        self.dy = dy
        self.sig = [i for i, t in enumerate(self.toks) if t.kind not in ("ws", "comment")]
        self.stats = {"xy": 0, "points": 0, "spawn_abs": 0, "spawn_cell": 0, "annotation": 0}
        self.warnings = []
        self._done = set()

    # Helpers over the significant-token list (indexes into self.sig).

    def _t(self, s):
        if 0 <= s < len(self.sig):
            return self.toks[self.sig[s]]
        return None

    def _numref_at(self, s):
        """If a (possibly negated) number literal starts at sig s, return
        (NumRef, next_s). Otherwise (None, s)."""
        t = self._t(s)
        if t is None:
            return None, s
        if t.kind == "op" and t.text == "-":
            t2 = self._t(s + 1)
            if t2 is not None and t2.kind == "number":
                return _NumRef(self.sig[s], self.sig[s + 1]), s + 2
            return None, s
        if t.kind == "number":
            return _NumRef(None, self.sig[s]), s + 1
        return None, s

    def _plain_numref_at(self, s):
        """Like _numref_at, but only when the number is the whole value: the
        next token must be ',', ';' or '}'. So "x = 5 * 2" is never touched."""
        ref, s2 = self._numref_at(s)
        if ref is None:
            return None, s
        nt = self._t(s2)
        if nt is None or not (nt.kind == "op" and nt.text in (",", ";", "}")):
            return None, s
        return ref, s2

    def _value(self, ref):
        v = _num_value(self.toks[ref.num_idx].text)
        if ref.minus_idx is not None:
            v = -v
        return v

    def _set(self, ref, value, original_text=None):
        key = ref.num_idx
        if key in self._done:
            return
        self._done.add(key)
        orig = original_text or self.toks[ref.num_idx].text
        text = _format_like(orig, value)
        if ref.minus_idx is not None:
            self.toks[ref.minus_idx].text = ""
        self.toks[ref.num_idx].text = text

    def _shift(self, ref, delta):
        self._set(ref, self._value(ref) + delta)

    def run(self):
        self._tables()
        self._annotations()
        return "".join(t.text for t in self.toks)

    # Table constructors: fields x, y, points, and spawn points.

    def _tables(self):
        stack = []  # each entry: dict of field name -> NumRef, plus "_points"
        s = 0
        count = len(self.sig)
        while s < count:
            t = self._t(s)
            if t.kind == "op" and t.text == "{":
                stack.append({})
                s += 1
                continue
            if t.kind == "op" and t.text == "}":
                if stack:
                    self._close_table(stack.pop())
                s += 1
                continue
            if stack and t.kind == "name":
                prev = self._t(s - 1)
                nxt = self._t(s + 1)
                at_field = prev is not None and prev.kind == "op" and prev.text in ("{", ",", ";")
                if at_field and nxt is not None and nxt.kind == "op" and nxt.text == "=":
                    if t.text == "points":
                        o = self._t(s + 2)
                        if o is not None and o.kind == "op" and o.text == "{":
                            end = self._points(s + 3)
                            if end is not None:
                                # The points list is consumed here, so the
                                # outer loop must not treat its braces as a table.
                                s = end + 1
                                continue
                    if t.text in _POSITION_FIELDS:
                        ref, _ = self._plain_numref_at(s + 2)
                        if ref is not None:
                            stack[-1][t.text] = ref
                        else:
                            stack[-1].setdefault("_unplain", []).append(t.text)
            s += 1

    def _points(self, s):
        """Shift a flat {x1, y1, x2, y2, ...} list starting after its '{'.
        Returns the sig index of the closing '}' or None if it isn't a plain
        number list (then nothing is changed)."""
        refs = []
        while True:
            t = self._t(s)
            if t is None:
                return None
            if t.kind == "op" and t.text == "}":
                break
            ref, s2 = self._plain_numref_at(s)
            if ref is None:
                self.warnings.append("points list holds something other than numbers; left unchanged")
                return None
            refs.append(ref)
            s = s2
            t = self._t(s)
            if t is not None and t.kind == "op" and t.text in (",", ";"):
                s += 1
        if len(refs) % 2:
            self.warnings.append("points list has an odd count of numbers; left unchanged")
            return s
        for k, ref in enumerate(refs):
            self._shift(ref, self.dx if k % 2 == 0 else self.dy)
        self.stats["points"] += len(refs) // 2
        return s

    def _close_table(self, fields):
        unplain = fields.pop("_unplain", None)
        if unplain:
            self.warnings.append(f"{'/'.join(unplain)} set to an expression, not a number; table left unchanged")
            return
        if "posX" in fields and "posY" in fields:
            if "worldX" in fields and "worldY" in fields:
                wx = self._value(fields["worldX"])
                wy = self._value(fields["worldY"])
                px = self._value(fields["posX"])
                py = self._value(fields["posY"])
                self._set(fields["worldX"], 0, "0")
                self._set(fields["worldY"], 0, "0")
                self._set(fields["posX"], int(wx) * 300 + px + self.dx)
                self._set(fields["posY"], int(wy) * 300 + py + self.dy)
                self.stats["spawn_cell"] += 1
            else:
                self._shift(fields["posX"], self.dx)
                self._shift(fields["posY"], self.dy)
                self.stats["spawn_abs"] += 1
            return
        if "x" in fields and "y" in fields:
            self._shift(fields["x"], self.dx)
            self._shift(fields["y"], self.dy)
            self.stats["xy"] += 1
        elif "x" in fields or "y" in fields:
            self.warnings.append("table with only one of x / y; left unchanged")

    # symbolsAPI:addUntranslatedText(text, layer, x, y) and friends.

    def _annotations(self):
        count = len(self.sig)
        for s in range(count - 3):
            a, b, c, d = self._t(s), self._t(s + 1), self._t(s + 2), self._t(s + 3)
            if not (a.kind == "op" and a.text == ":" and b.kind == "name"
                    and b.text in ANNOTATION_METHODS and c.kind == "op" and c.text == "("):
                continue
            nums = []
            depth = 0
            k = s + 3
            arg_start = True
            plain = []
            while k < count:
                t = self._t(k)
                if t.kind == "op" and t.text in "({[":
                    depth += 1
                elif t.kind == "op" and t.text in ")}]":
                    if depth == 0:
                        break
                    depth -= 1
                if depth == 0 and arg_start:
                    ref, k2 = self._numref_at(k)
                    nt = self._t(k2)
                    if ref is not None and nt is not None and nt.kind == "op" and nt.text in (",", ")"):
                        plain.append(ref)
                        k = k2
                        arg_start = False
                        continue
                arg_start = depth == 0 and t.kind == "op" and t.text == ","
                k += 1
            if len(plain) >= 2:
                self._shift(plain[0], self.dx)
                self._shift(plain[1], self.dy)
                self.stats["annotation"] += 1
            else:
                self.warnings.append(f"{b.text}() call without two plain number arguments; left unchanged")


def shift_lua(src, dx, dy):
    """Return (new_source, stats, warnings). dx, dy are in tiles."""
    sh = LuaShifter(src, dx, dy)
    out = sh.run()
    return out, sh.stats, sh.warnings
