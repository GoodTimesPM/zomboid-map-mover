"use strict";

// ------------------------------------------------------------------ state

let S = null;            // /api/state
let groups = [];         // S.groups plus runtime fields
let byKey = new Map();
let vanilla = new Set(); // "x,y"
let plans = {};          // key -> {offset:[dx,dy], join:bool, roads:bool, layer:int}
let addIds = [];
let off = [];            // keys of maps switched off: Apply takes them out of the mod list
let offGroups = [];      // S.off_groups
let selected = null;     // group key
let westShown = "";      // keys of the maps west of x 0 the banner shows
let hits = new Map();    // key -> {conflict:Set, vanilla:Set, oob:number}
let conflictCells = new Set();   // two maps on one cell, same layer
let layeredCells = new Map();    // two maps on one cell, a higher layer on top: cell -> top group key
const undoStack = [];
let OV = null;           // /api/overlay: vanilla towns, water, roads
let ovPaths = null;      // Path2D per layer, in cell units
let ovCells = new Map(); // "x,y" -> {b, t, w, wn, hw, rd, s}
const roadCache = new Map(); // group key -> Path2D per layer, or "loading"
let PYR = null;          // /api/pyramids: terrain images for vanilla and some maps
const tileImgs = new Map(); // tile URL -> Image (complete or loading)
const linkCache = new Map(); // autoKey(g) -> /api/links answer, or "loading", or {error}
const endsCache = new Map(); // endsKey(g) -> road ends of that map, or "loading"
const looseMaps = new Set(); // maps whose road ends are looked for harder
const handEnds = new Map();  // map key -> road ends made by hand (Alt+click), in home squares
let links = [];              // road links drawn by hand, saved in the layout
let highlighted = new Set(); // maps whose roads and road ends are on show (Ctrl+click)
let pick = null;             // the road end a new link starts from
let selLink = null;          // id of the picked hand link
let hoverEnd = null;         // road end under the mouse
let mouse = null;            // [x, y] of the mouse on the canvas
let routing = null;          // {t0, est} while a road is being routed
let hintMsg = null;          // {text, err} shown over the map
let choices = null;          // {links, hover, replace}: roads routed for a new link, to pick one
let crashList = [];          // where the game would crash loading a new world (loadorder.py)

const view = { x: 40, y: 40, scale: 8 };
const canvas = document.getElementById("map");
const ctx = canvas.getContext("2d");
const tip = document.getElementById("tip");

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const ck = (x, y) => x + "," + y;

async function api(path, body) {
  const opt = body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) };
  const r = await fetch(path, opt);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || r.statusText);
  return j;
}

// ------------------------------------------------------------------ load

async function load(keepView) {
  S = await api("/api/state");
  crashList = S.crashes || [];
  plans = {};
  for (const [k, p] of Object.entries(S.layout.plans || {})) plans[k] = { offset: [p.offset[0], p.offset[1]], join: !!p.join, roads: !!p.roads, layer: p.layer || 1 };
  addIds = S.layout.add_ids || [];
  off = S.layout.off || [];
  offGroups = S.off_groups || [];
  links = S.layout.links || [];
  roadCache.clear();
  endsCache.clear();
  pick = null;
  vanilla = new Set(S.vanilla.map(([x, y]) => ck(x, y)));
  groups = S.groups.map((g, i) => prepGroup(g, i));
  byKey = new Map(groups.map((g) => [g.key, g]));
  loadPyramids();
  if (selected && !byKey.has(selected)) selected = null;
  highlighted = new Set([...highlighted].filter((k) => byKey.has(k)));
  if (!links.some((l) => l.id === selLink)) selLink = null;
  banners();
  $("version").textContent = `B${S.game_version}${S.version_guessed ? " (guessed)" : ""} · ${groups.length} maps`
    + `${offGroups.length ? ` (${offGroups.length} off)` : ""} · mods in ${S.mods_dir}`;
  fillInactive();
  recompute();
  if (!keepView) fit();
  renderList();
  renderDetail();
  renderManual();
  draw();
}

function prepGroup(g, i) {
  const hue = (i * 137.508) % 360;
  g.color = `hsl(${hue} 70% 58%)`;
  g.fill = `hsla(${hue}, 70%, 58%, 0.62)`;
  g.fillOwn = `hsla(${hue}, 70%, 58%, 0.16)`;
  g.cellSet = new Set(g.cells.map(([x, y]) => ck(x, y)));
  g.cellPath = new Path2D();
  for (const [x, y] of g.cells) g.cellPath.rect(x, y, 1, 1);
  // Outline segments in original coordinates.
  const segs = [];
  for (const [x, y] of g.cells) {
    if (!g.cellSet.has(ck(x, y - 1))) segs.push([x, y, x + 1, y]);
    if (!g.cellSet.has(ck(x, y + 1))) segs.push([x, y + 1, x + 1, y + 1]);
    if (!g.cellSet.has(ck(x - 1, y))) segs.push([x, y, x, y + 1]);
    if (!g.cellSet.has(ck(x + 1, y))) segs.push([x + 1, y, x + 1, y + 1]);
  }
  g.segs = segs;
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  for (const [x, y] of g.cells) { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + 1); y1 = Math.max(y1, y + 1); }
  g.box = [x0, y0, x1, y1];
  return g;
}

function plan(g) {
  const p = plans[g.key];
  return { offset: p ? p.offset : g.offset, join: p ? p.join : g.joined, roads: !!(p && p.roads), layer: (p && p.layer) || 1 };
}
// Load order where maps overlap: higher layer first, then mod list order.
const above = (a, b) => (plan(b).layer - plan(a).layer) || (a.order - b.order);
const inKentucky = (g) => g.kentucky || plan(g).join;
const pending = (g) => {
  const p = plan(g);
  return g.added || g.mixed || p.offset[0] !== g.offset[0] || p.offset[1] !== g.offset[1] || (!g.kentucky && p.join !== g.joined)
    || (hits.get(g.key) && hits.get(g.key).reorder);
};

function setPlan(g, offset, join, roads) {
  const old = plans[g.key] || {};
  plans[g.key] = { offset: [offset[0], offset[1]], join: !!join };
  if (roads === undefined ? old.roads : roads) plans[g.key].roads = true;
  if (old.layer > 1) plans[g.key].layer = old.layer;
}

function setLayer(g, layer) {
  layer = Math.max(1, Math.min(99, layer));
  if (layer === plan(g).layer) return;
  pushUndo();
  const p = plan(g);
  setPlan(g, p.offset, p.join);
  if (layer > 1) plans[g.key].layer = layer;
  else delete plans[g.key].layer;
  changed();
}

// Road links: only maps moved inside the Kentucky world get them.
const canLink = (g) => { const p = plan(g); return inKentucky(g) && (p.offset[0] || p.offset[1]); };
// The links depend on where every Kentucky map sits, not just this one.
function linkKey(g) {
  const all = groups.filter(inKentucky).map((o) => o.key + ":" + plan(o).offset.join(",")).join("|");
  return g.key + "@" + all;
}
const autoKey = (g) => linkKey(g) + "#" + links.map((l) => l.id).join(",");
const layoutBody = () => ({ plans, add_ids: addIds, off, links });
// Automatic links are drawn while "Pave road links" is ticked, or after Show
// links until Hide links. Unticking the box hides them and Apply skips them.
const autoPreview = new Set();
const showAuto = (g) => canLink(g) && (plan(g).roads || autoPreview.has(g.key));

function groupLinks(g, fetch) {
  const k = autoKey(g);
  const c = linkCache.get(k);
  if (c || !fetch) return c && c !== "loading" ? c : null;
  linkCache.set(k, "loading");
  updateDetail();
  api("/api/links", { key: g.key, layout: layoutBody() }).then((r) => {
    linkCache.set(k, r);
  }).catch((err) => linkCache.set(k, { error: err.message })).finally(() => { redraw(); updateDetail(); });
  return null;
}

// ------------------------------------------------------------------ vanilla overlay

async function loadOverlay() {
  try { OV = await api("/api/overlay"); } catch (err) { OV = { error: err.message }; }
  if (OV.error) { banners(); return; }
  ovPaths = {};
  for (const [k, polys] of Object.entries(OV.layers || {})) ovPaths[k] = polyPath(polys, k === "water");
  ovCells = new Map(Object.entries(OV.cells || {}));
  // Town areas: one tinted path plus an outline, like the map outlines.
  const town = new Path2D(), edge = new Path2D();
  for (const a of OV.towns || []) {
    const set = new Set(a.cells.map(([x, y]) => ck(x, y)));
    for (const [x, y] of a.cells) {
      town.rect(x, y, 1, 1);
      if (!set.has(ck(x, y - 1))) { edge.moveTo(x, y); edge.lineTo(x + 1, y); }
      if (!set.has(ck(x, y + 1))) { edge.moveTo(x, y + 1); edge.lineTo(x + 1, y + 1); }
      if (!set.has(ck(x - 1, y))) { edge.moveTo(x, y); edge.lineTo(x, y + 1); }
      if (!set.has(ck(x + 1, y))) { edge.moveTo(x + 1, y); edge.lineTo(x + 1, y + 1); }
    }
  }
  ovPaths.town = town;
  ovPaths.townEdge = edge;
  recompute();
  renderList();
  updateDetail();
  redraw();
}

// ------------------------------------------------------------------ terrain images

async function loadPyramids() {
  try { PYR = await api("/api/pyramids"); } catch { PYR = null; }
  for (const g of groups) {
    g.imgs = (PYR && PYR.groups[g.key]) || [];
  }
  redraw();
}

function tileImg(p, z, x, y) {
  const url = p.lots !== undefined
    ? `/api/ltile?g=${PYR.gen}&id=${p.lots}&z=${z}&x=${x}&y=${y}`
    : `/api/ptile?g=${PYR.gen}&id=${p.id}&z=${z}&x=${x}&y=${y}`;
  let im = tileImgs.get(url);
  if (!im) {
    if (tileImgs.size > 3000) tileImgs.clear();
    im = new Image();
    im.onload = redraw;
    im.src = url;
    tileImgs.set(url, im);
  }
  return im.complete && im.naturalWidth ? im : null;
}

// Draw one pyramid in cell units (call inside inCells). Coarsest level first
// as a backdrop, then the level that suits the zoom over it.
function drawPyramid(p, dx, dy) {
  const [bx0, by0, bx1, by1] = p.bounds;
  const tpp = (bx1 - bx0) / p.image[0];           // world tiles per image pixel
  const screenPxPerImgPx = view.scale / 256 * tpp * (window.devicePixelRatio || 1);   // device pixels
  const want = Math.max(0, Math.min(p.levels - 1, Math.floor(Math.log2(1 / screenPxPerImgPx))));
  const [vx0, vy0, vx1, vy1] = visibleRange();
  // Edge tiles are padded out to 256 px, often with opaque black. The game
  // stops at the bounds, so clip there and let the land below show through.
  ctx.save();
  ctx.beginPath();
  ctx.rect(bx0 / 256, by0 / 256, (bx1 - bx0) / 256, (by1 - by0) / 256);
  ctx.clip();
  // Blown up past one image pixel per screen pixel, keep the squares crisp.
  ctx.imageSmoothingEnabled = screenPxPerImgPx * 2 ** want < 1;
  for (const z of want === p.levels - 1 ? [want] : [p.levels - 1, want]) {
    const span = 256 * 2 ** z * tpp;             // world tiles per pyramid tile
    const nx = Math.ceil(p.image[0] / (256 * 2 ** z)), ny = Math.ceil(p.image[1] / (256 * 2 ** z));
    const ix0 = Math.max(0, Math.floor(((vx0 - dx) * 256 - bx0) / span)), ix1 = Math.min(nx - 1, Math.floor(((vx1 - dx) * 256 - bx0) / span));
    const iy0 = Math.max(0, Math.floor(((vy0 - dy) * 256 - by0) / span)), iy1 = Math.min(ny - 1, Math.floor(((vy1 - dy) * 256 - by0) / span));
    for (let iy = iy0; iy <= iy1; iy++) for (let ix = ix0; ix <= ix1; ix++) {
      const im = tileImg(p, z, ix, iy);
      if (!im) continue;
      const k = 2 ** z * tpp / 256;              // cells per tile-image pixel
      ctx.drawImage(im, (bx0 + ix * span) / 256, (by0 + iy * span) / 256, im.naturalWidth * k, im.naturalHeight * k);
    }
  }
  ctx.restore();
}

// Cells drawn by the server from a map's lot files (maps with no image).
// Each cell is one image; 16, 64 or 256 px (one per square) picked by zoom.
const LOT_MIN_SCALE = 6;   // below this many screen px per cell, use the flat fill
function drawLotCells(p, dx, dy) {
  const d = view.scale * (window.devicePixelRatio || 1);
  const z = d >= 128 ? 0 : d >= 32 ? 2 : 4;
  const [vx0, vy0, vx1, vy1] = visibleRange();
  ctx.imageSmoothingEnabled = d < 256 >> z;
  for (const [x, y] of p.cells) {
    if (x + dx < vx0 - 1 || x + dx > vx1 || y + dy < vy0 - 1 || y + dy > vy1) continue;
    const im = tileImg(p, z, x, y);
    if (im) ctx.drawImage(im, x, y, 1, 1);
  }
  ctx.imageSmoothingEnabled = true;
}

function hasTerrain(g) {
  return g.imgs && g.imgs.some((p) => p.lots === undefined || view.scale >= LOT_MIN_SCALE);
}

// Polygons in tiles -> one Path2D in cells. Water is filled, roads stroked.
function polyPath(polys, closed) {
  const p = new Path2D();
  for (const poly of polys) for (const r of poly) {
    p.moveTo(r[0] / 256, r[1] / 256);
    for (let i = 2; i < r.length; i += 2) p.lineTo(r[i] / 256, r[i + 1] / 256);
    if (closed) p.closePath();
  }
  return p;
}

function groupRoads(g) {
  const c = roadCache.get(g.key);
  if (c) return c === "loading" ? null : c;
  roadCache.set(g.key, "loading");
  api("/api/roads?key=" + encodeURIComponent(g.key)).then((r) => {
    const out = {};
    for (const [k, polys] of Object.entries(r)) out[k] = polyPath(polys, k === "water");
    out.empty = !Object.values(r).some((v) => v.length);
    roadCache.set(g.key, out);
    redraw();
    updateDetail();
  }).catch(() => roadCache.delete(g.key));
  return null;
}

// What vanilla has under a set of cells: towns, water, highways.
function vanillaUnder(cells) {
  const towns = new Map(), water = new Map(), roads = new Set();
  let hw = 0;
  for (const c of cells) {
    const v = ovCells.get(c);
    if (!v) continue;
    if (v.t !== undefined) { const n = OV.towns[v.t].name; towns.set(n, (towns.get(n) || 0) + 1); }
    if (v.wn) water.set(v.wn, (water.get(v.wn) || 0) + 1);
    if (v.hw) hw++;
    for (const s of v.s || []) roads.add(s);
  }
  return { towns, water, hw, roads };
}

// ------------------------------------------------------------------ overlaps

function recompute() {
  hits = new Map();
  conflictCells = new Set();
  layeredCells = new Map();
  const owner = new Map();
  const stacks = new Map();   // cell -> every Kentucky group on it
  const min = S.min_cell, max = S.max_cell;
  for (const g of groups) hits.set(g.key, { conflict: new Set(), layered: new Set(), top: 0, vanilla: new Set(), oob: 0, west: 0, under: null, reorder: false });
  for (const g of groups) {
    const [dx, dy] = plan(g).offset;
    const h = hits.get(g.key);
    if (dx || dy) for (const [x, y] of g.cells) {
      const nx = x + dx, ny = y + dy;
      if (nx < min || ny < min || nx > max || ny > max) h.oob++;
    }
    if (!inKentucky(g)) continue;
    // The game can't load map cells a move pushes west of x 0 (see layout.west_cells).
    for (const [x] of g.cells) if (x >= 0 && x + dx < 0) h.west++;
    for (const [x, y] of g.cells) {
      const c = ck(x + dx, y + dy);
      if (vanilla.has(c)) h.vanilla.add(c);
      const o = owner.get(c);
      if (o && o !== g.key) {
        const st = stacks.get(c);
        if (st) { if (!st.includes(g)) st.push(g); } else stacks.set(c, [byKey.get(o), g]);
      } else owner.set(c, g.key);
    }
    if (OV) h.under = vanillaUnder(h.vanilla);
  }
  // A higher layer settles who is on top. On the same layer the mod list
  // order decides, which is shown as a clash so it gets looked at.
  for (const [c, st] of stacks) {
    st.sort(above);
    const tie = plan(st[0]).layer === plan(st[1]).layer;
    if (tie) conflictCells.add(c); else layeredCells.set(c, st[0].key);
    const nowTop = st.reduce((a, b) => (b.order < a.order ? b : a));
    for (const g of st) {
      const h = hits.get(g.key);
      (tie ? h.conflict : h.layered).add(c);
      if (!tie && g === st[0]) h.top++;
      if (nowTop !== st[0]) h.reorder = true;
    }
  }
  // The banner names the maps west of x 0, so redraw it when that list changes.
  const west = groups.filter((g) => hits.get(g.key).west).map((g) => g.key).join("|");
  if (west !== westShown) { westShown = west; banners(); }
}

// One short list tag for what vanilla loses under a map, biggest first.
function underTag(u) {
  if (!u) return "";
  const top = (m) => [...m.entries()].sort((a, b) => b[1] - a[1])[0][0];
  if (u.towns.size) return `over ${top(u.towns)}${u.towns.size > 1 ? " +" + (u.towns.size - 1) : ""}`;
  if (u.water.size) return `on ${top(u.water)}`;
  if (u.hw) return "cuts highway";
  return "";
}

function underLines(u) {
  if (!u) return [];
  const list = (m) => [...m.entries()].sort((a, b) => b[1] - a[1]).map(([n, c]) => `${esc(n)} (${c} cell${c > 1 ? "s" : ""})`).join(", ");
  const out = [];
  if (u.towns.size) out.push(`<b style="color:#ffc98a">Covers vanilla towns:</b> ${list(u.towns)}. Those cells of the town are replaced by this map.`);
  if (u.water.size) out.push(`<b style="color:#8cc8ff">Covers water:</b> ${list(u.water)}.`);
  if (u.hw || u.roads.size) out.push(`<b style="color:#f0b44c">Cuts vanilla roads</b>${u.hw ? ` in ${u.hw} highway cell${u.hw > 1 ? "s" : ""}` : ""}${u.roads.size ? `: ${[...u.roads].map(esc).join(", ")}` : ""}. Those roads end at this map's edge unless the map's own roads line up with them.`);
  return out;
}

// ------------------------------------------------------------------ drawing

function resize() {
  const r = canvas.getBoundingClientRect();
  const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.round(r.width * dpr);
  canvas.height = Math.round(r.height * dpr);
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  draw();
}

const W = () => canvas.clientWidth;
const H = () => canvas.clientHeight;
const sx = (x) => (x - view.x) * view.scale + W() / 2;
const sy = (y) => (y - view.y) * view.scale + H() / 2;
const wx = (px) => (px - W() / 2) / view.scale + view.x;
const wy = (py) => (py - H() / 2) / view.scale + view.y;

function fit() {
  let x0 = 1e9, y0 = 1e9, x1 = -1e9, y1 = -1e9;
  const take = (x, y) => { x0 = Math.min(x0, x); y0 = Math.min(y0, y); x1 = Math.max(x1, x + 1); y1 = Math.max(y1, y + 1); };
  for (const c of S.vanilla) take(c[0], c[1]);
  // Kentucky-world cells near the vanilla map. A few maps keep far-off
  // pockets (teleport rooms) that would zoom everything out.
  const [vx0, vy0, vx1, vy1] = [x0 - 60, y0 - 60, x1 + 60, y1 + 60];
  for (const g of groups) {
    if (!inKentucky(g)) continue;
    const [dx, dy] = plan(g).offset;
    for (const [x, y] of g.cells) {
      const nx = x + dx, ny = y + dy;
      if (nx >= vx0 && nx <= vx1 && ny >= vy0 && ny <= vy1) take(nx, ny);
    }
  }
  if (x0 > x1) { x0 = 0; y0 = 0; x1 = 100; y1 = 100; }
  view.x = (x0 + x1) / 2;
  view.y = (y0 + y1) / 2;
  view.scale = Math.max(1, Math.min(60, Math.min((W() - 40) / (x1 - x0 + 4), (H() - 80) / (y1 - y0 + 4))));
}

function cellRect(x, y, inset = 0) {
  const a = sx(x), b = sy(y);
  const s = view.scale;
  return [Math.floor(a) + inset, Math.floor(b) + inset, Math.ceil(a + s) - Math.floor(a) - 2 * inset, Math.ceil(b + s) - Math.floor(b) - 2 * inset];
}

function visibleRange() {
  return [Math.floor(wx(0)) - 1, Math.floor(wy(0)) - 1, Math.ceil(wx(W())) + 1, Math.ceil(wy(H())) + 1];
}

function draw() {
  if (!S) return;
  const w = W(), h = H();
  ctx.fillStyle = "#111317";
  ctx.fillRect(0, 0, w, h);
  const [vx0, vy0, vx1, vy1] = visibleRange();
  const vis = (x, y) => x >= vx0 && x <= vx1 && y >= vy0 && y <= vy1;
  const min = S.min_cell, max = S.max_cell, span = (max + 1 - min) * view.scale;

  // Playable area.
  ctx.fillStyle = "#171a1f";
  ctx.fillRect(sx(min), sy(min), span, span);

  // Grid.
  const step = view.scale >= 9 ? 1 : view.scale >= 3 ? 5 : 10;
  ctx.lineWidth = 1;
  for (let x = Math.max(min, Math.ceil(vx0 / step) * step); x <= Math.min(max + 1, vx1); x += step) {
    ctx.strokeStyle = x % 10 === 0 ? "#272b33" : "#1e2127";
    ctx.beginPath(); ctx.moveTo(Math.round(sx(x)) + .5, sy(min)); ctx.lineTo(Math.round(sx(x)) + .5, sy(max + 1)); ctx.stroke();
  }
  for (let y = Math.max(min, Math.ceil(vy0 / step) * step); y <= Math.min(max + 1, vy1); y += step) {
    ctx.strokeStyle = y % 10 === 0 ? "#272b33" : "#1e2127";
    ctx.beginPath(); ctx.moveTo(sx(min), Math.round(sy(y)) + .5); ctx.lineTo(sx(max + 1), Math.round(sy(y)) + .5); ctx.stroke();
  }
  ctx.strokeStyle = "#5a2a2a";
  ctx.strokeRect(sx(min) - .5, sy(min) - .5, span + 1, span + 1);

  // Vanilla.
  ctx.fillStyle = "#4a505c";
  for (const [x, y] of S.vanilla) if (vis(x, y)) ctx.fillRect(...cellRect(x, y));

  // Vanilla terrain image.
  const terrain = $("showTerrain").checked && PYR;
  if (terrain && PYR.vanilla) inCells(0, 0, () => drawPyramid(PYR.vanilla, 0, 0));

  // Vanilla towns, water and roads under the maps. The terrain image already
  // shows them, so only the road lines stay on over it.
  const showTowns = $("showTowns").checked, showRoads = $("showRoads").checked;
  if (ovPaths) inCells(0, 0, () => {
    if (showTowns && !terrain) {
      ctx.fillStyle = "rgba(214,160,98,.42)";
      ctx.fill(ovPaths.town);
      ctx.fillStyle = "#2f6fa8";
      ctx.fill(ovPaths.water);
    }
    if (showRoads && !terrain) {
      line(ovPaths.road, "rgba(205,211,222,.5)", 0.8);
      line(ovPaths.rail, "rgba(184,150,220,.7)", 1, [4, 3]);
      ctx.fillStyle = "#f0b44c";
      ctx.fill(ovPaths.highway);
      line(ovPaths.highway, "#f0b44c", 2.2);
    }
  });

  const showOwn = $("showOwn").checked;
  // Lowest layer first, so the map that wins an overlap is drawn on top.
  const order = groups.slice().sort((a, b) => above(b, a));

  // Own-world maps first (faint), then Kentucky maps.
  for (const pass of [false, true]) {
    for (const g of order) {
      const kt = inKentucky(g);
      if (kt !== pass) continue;
      if (!kt && !showOwn && g.key !== selected) continue;
      const [dx, dy] = plan(g).offset;
      if (terrain && hasTerrain(g) && (kt || g.key === selected)) {
        // The map's own image, cut to its cells, with a light tint of its colour.
        inCells(dx, dy, () => {
          ctx.save();
          ctx.clip(g.cellPath);
          for (const p of g.imgs) p.lots === undefined ? drawPyramid(p, dx, dy) : drawLotCells(p, dx, dy);
          ctx.fillStyle = g.fillOwn;
          ctx.fill(g.cellPath);
          ctx.restore();
        });
      } else {
        ctx.fillStyle = kt ? g.fill : g.fillOwn;
        for (const [x, y] of g.cells) if (vis(x + dx, y + dy)) ctx.fillRect(...cellRect(x + dx, y + dy));
      }
      outline(g, dx, dy, g.color, g.key === selected ? 2.5 : 1.2, kt ? [] : [5, 4]);
    }
  }
  const selMap = selected && byKey.get(selected);
  if (selMap && (inKentucky(selMap) || showOwn)) outline(selMap, ...plan(selMap).offset, selMap.color, 2.5, inKentucky(selMap) ? [] : [5, 4]);

  // Vanilla on top again, faint: what a map would cover.
  if (ovPaths) inCells(0, 0, () => {
    if (showTowns) {
      ctx.fillStyle = terrain ? "rgba(70,150,235,.3)" : "rgba(70,150,235,.5)";
      ctx.fill(ovPaths.water);
      line(ovPaths.townEdge, "rgba(255,201,138,.75)", 1.2, [5, 3]);
    }
    if (showRoads) line(ovPaths.highway, "rgba(240,180,76,.6)", 1.4);
  });

  // Own roads of the selected and highlighted maps, to see where they run off their edges.
  const selG = selected && byKey.get(selected);
  if ($("showRoads").checked) for (const g of shownMaps()) {
    const r = groupRoads(g);
    const [dx, dy] = plan(g).offset;
    if (r) inCells(dx, dy, () => {
      line(r.minor, "rgba(255,255,255,.55)", 0.7);
      line(r.road, "rgba(255,255,255,.85)", 1.1);
      ctx.fillStyle = "#fff";
      ctx.fill(r.highway);
      line(r.highway, "#fff", 2);
    });
  }

  // Road links of the selected map, as wide as the road they pave.
  const lk = selG && showAuto(selG) && linkCache.get(autoKey(selG));
  if (lk && lk.routes) inCells(0, 0, () => {
    for (const r of lk.routes) {
      const p = new Path2D();
      r.path.forEach(([x, y], i) => (i ? p.lineTo : p.moveTo).call(p, (x + .5) / 256, (y + .5) / 256));
      ctx.lineCap = "butt";
      line(p, "rgba(0,0,0,.6)", Math.max(4, r.width * view.scale / 256) + 2);
      line(p, r.like ? "#ff8a3d" : "#ffd23d", Math.max(3, r.width * view.scale / 256));
    }
    ctx.fillStyle = "#ff5050";
    const rad = Math.max(4, 10) / view.scale;
    for (const [x, y] of lk.missed || []) { ctx.beginPath(); ctx.arc((x + .5) / 256, (y + .5) / 256, rad, 0, 7); ctx.fill(); }
  });

  drawManual();

  // Maps over vanilla.
  if ($("showOverV").checked && view.scale >= 3) {
    ctx.strokeStyle = "#ffb340";
    ctx.lineWidth = 1.5;
    for (const g of groups) for (const c of hits.get(g.key).vanilla) {
      const [x, y] = c.split(",").map(Number);
      if (vis(x, y)) { const r = cellRect(x, y, 2); ctx.strokeRect(r[0] + .5, r[1] + .5, r[2] - 1, r[3] - 1); }
    }
  }

  // Clashes.
  ctx.fillStyle = "rgba(255,60,60,0.85)";
  for (const c of conflictCells) {
    const [x, y] = c.split(",").map(Number);
    if (vis(x, y)) ctx.fillRect(...cellRect(x, y, view.scale >= 6 ? 1 : 0));
  }
  // Overlaps settled by layer: a dashed edge, the top map shows through.
  if (view.scale >= 3) {
    ctx.strokeStyle = "#c9a0ff";
    ctx.lineWidth = 1.5;
    ctx.setLineDash([4, 3]);
    for (const c of layeredCells.keys()) {
      const [x, y] = c.split(",").map(Number);
      if (vis(x, y)) { const r = cellRect(x, y, 1); ctx.strokeRect(r[0] + .5, r[1] + .5, r[2] - 1, r[3] - 1); }
    }
    ctx.setLineDash([]);
  }

  // Selected: ghost at the original spot.
  const sg = selected && byKey.get(selected);
  if (sg) {
    const [dx, dy] = plan(sg).offset;
    if (dx || dy) outline(sg, 0, 0, "rgba(255,255,255,.55)", 1.5, [3, 3]);
  }

  // Square grid, zoomed far in.
  if (view.scale / 256 >= 6) {
    ctx.strokeStyle = "rgba(0,0,0,.22)";
    ctx.lineWidth = 1;
    ctx.beginPath();
    for (let x = Math.floor(wx(0) * 256); x <= Math.ceil(wx(w) * 256); x++) {
      const px = Math.round(sx(x / 256)) + .5;
      ctx.moveTo(px, 0); ctx.lineTo(px, h);
    }
    for (let y = Math.floor(wy(0) * 256); y <= Math.ceil(wy(h) * 256); y++) {
      const py = Math.round(sy(y / 256)) + .5;
      ctx.moveTo(0, py); ctx.lineTo(w, py);
    }
    ctx.stroke();
  }

  // Labels, with each Kentucky map's layer in a badge left of its name.
  const badges = $("showLayers").checked;
  if (view.scale >= 3) {
    const fs = Math.max(11, Math.min(15, view.scale * 1.3));
    ctx.textBaseline = "middle";
    for (const g of order) {
      if (!inKentucky(g) && !showOwn && g.key !== selected) continue;
      const [dx, dy] = plan(g).offset;
      const cx = sx((g.box[0] + g.box[2]) / 2 + dx), cy = sy((g.box[1] + g.box[3]) / 2 + dy);
      const wpx = (g.box[2] - g.box[0]) * view.scale;
      const badge = badges && inKentucky(g) && wpx >= 24;
      if (wpx < 70 && g.key !== selected) {
        if (badge) layerBadge(plan(g).layer, cx, cy, false, false);
        continue;
      }
      let t = g.title;
      if (t.length > 34) t = t.slice(0, 32) + "...";
      ctx.font = `${fs}px system-ui, sans-serif`;
      const tw = ctx.measureText(t).width;
      const bw = badge ? layerBadge(plan(g).layer, 0, 0, view.scale >= 60, false, true) + 6 : 0;
      const left = cx - (bw + tw) / 2;
      if (badge) layerBadge(plan(g).layer, left + (bw - 6) / 2, cy, view.scale >= 60, g.key === selected);
      ctx.font = `${fs}px system-ui, sans-serif`;
      ctx.textAlign = "left";
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,.8)";
      ctx.strokeText(t, left + bw, cy);
      ctx.fillStyle = g.key === selected ? "#fff" : "#e8eaef";
      ctx.fillText(t, left + bw, cy);
    }
  }

  // Vanilla town and river names.
  if (OV && OV.labels && $("showTowns").checked && view.scale >= 2) {
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    for (const l of OV.labels) {
      const town = l.style === "text-town", water = l.style.startsWith("text-water");
      if (!town && !(water && l.scale >= 1.5) && view.scale < 30) continue;
      const px = sx(l.x / 256), py = sy(l.y / 256);
      if (px < -100 || py < -20 || px > W() + 100 || py > H() + 20) continue;
      const size = town ? Math.max(11, Math.min(17, view.scale * 1.1)) : 11;
      ctx.font = `${town ? "700 " : water ? "italic " : ""}${size}px system-ui, sans-serif`;
      const t = town ? l.name.toUpperCase() : l.name;
      ctx.lineWidth = 3;
      ctx.strokeStyle = "rgba(0,0,0,.85)";
      ctx.strokeText(t, px, py);
      ctx.fillStyle = town ? "#ffd9a8" : water ? "#a9d4ff" : "#c9ccd4";
      ctx.fillText(t, px, py);
    }
  }

  // Axis numbers.
  ctx.font = "11px system-ui, sans-serif";
  ctx.fillStyle = "#8d94a3";
  const lab = view.scale >= 9 ? 5 : view.scale >= 3 ? 10 : 50;
  ctx.textAlign = "center"; ctx.textBaseline = "top";
  for (let x = Math.max(min, Math.ceil(vx0 / lab) * lab); x <= Math.min(max, vx1); x += lab) ctx.fillText(x, sx(x + .5), 4);
  ctx.textAlign = "left"; ctx.textBaseline = "middle";
  for (let y = Math.max(min, Math.ceil(vy0 / lab) * lab); y <= Math.min(max, vy1); y += lab) ctx.fillText(y, 4, sy(y + .5));
  updateHint();
}

// ------------------------------------------------------------------ links by hand

// Maps whose roads and road ends are on show: the selected one and the
// Ctrl+clicked ones.
function shownMaps() {
  const out = [...highlighted].filter((k) => byKey.has(k)).map((k) => byKey.get(k));
  const s = selected && byKey.get(selected);
  if (s && !highlighted.has(s.key)) out.push(s);
  return out;
}

function toggleHighlight(key) {
  if (highlighted.has(key)) highlighted.delete(key);
  else highlighted.add(key);
  renderList();
  renderManual();
  redraw();
}

// Road ends of one map, in home squares (where they are with the map at
// offset 0). While a map is dragged the last answer moves with it; the new
// one is asked for once it is dropped.
function groupEnds(g) {
  if (!inKentucky(g)) return [];
  const loose = looseMaps.has(g.key);
  const k = endsKey(g);
  const c = endsCache.get(k);
  const hand = handEnds.get(g.key) || [];
  if (c && c !== "loading") return [...(g.lastEnds = c), ...hand];
  if (!c && !drag) {
    endsCache.set(k, "loading");
    api("/api/ends", { keys: [g.key], layout: layoutBody(), loose }).then((r) => {
      const m = r.maps && r.maps[g.key];
      const ends = (m && m.ends) || [];
      endsCache.set(k, ends);
      if (loose) {
        const n = ends.filter((e) => e.loose).length;
        hintMsg = { text: n ? `Found ${n} more road end${n === 1 ? "" : "s"} on ${g.title}, the teal dots.`
          : `No more road ends on ${g.title}. Alt+click right on its road where one should be.` };
      }
    }).catch(() => endsCache.set(k, [])).finally(redraw);
  }
  return [...(g.lastEnds || []), ...hand];
}
function endsKey(g) { return linkKey(g) + (looseMaps.has(g.key) ? "~loose" : ""); }

// Alt+click: make a road end on map g's own road near world square [x, y],
// then start a link from it, or end the one being drawn there.
async function makeEnd(g, [x, y]) {
  if (routing) return;
  const [dx, dy] = plan(g).offset;
  hintMsg = { text: `Looking at ${g.title}'s road there.` };
  redraw();
  let r;
  try {
    r = await api("/api/end", { key: g.key, layout: layoutBody(), hx: x - dx * 256, hy: y - dy * 256 });
  } catch (err) {
    r = { error: "Couldn't make a road end: " + err.message };
  }
  if (r.error) { hintMsg = { text: r.error, err: true }; return redraw(); }
  const e = r.end;
  const list = handEnds.get(g.key) || [];
  if (!list.some((o) => sameEnd(o, e))) handEnds.set(g.key, [...list, e]);
  if (!highlighted.has(g.key)) { highlighted.add(g.key); renderList(); renderManual(); }
  if (pick && !sameEnd(pick, e)) return joinTo(pick, e);
  pick = e;
  hintMsg = { text: `Road end made, ${e.width} wide.` };
  redraw();
}

// World square of a link end or road end now, or null when its map is gone.
function pointPos(p) {
  if (p.key === null || p.key === undefined) return [p.hx, p.hy];
  const g = byKey.get(p.key);
  if (!g) return null;
  const [dx, dy] = plan(g).offset;
  return [p.hx + dx * 256, p.hy + dy * 256];
}
const sameEnd = (a, b) => a && b && a.key === b.key && a.hx === b.hx && a.hy === b.hy;
const scr = ([x, y]) => [sx((x + .5) / 256), sy((y + .5) / 256)];

function shownEnds() {
  const out = [];
  for (const g of shownMaps()) for (const e of groupEnds(g)) {
    const [px, py] = scr(pointPos(e));
    if (px > -20 && py > -20 && px < W() + 20 && py < H() + 20) out.push({ ...e, px, py });
  }
  return out;
}

function endAt(ox, oy) {
  let best = null, bd = 11 * 11;
  for (const e of shownEnds()) {
    const d = (e.px - ox) ** 2 + (e.py - oy) ** 2;
    if (d < bd) { bd = d; best = e; }
  }
  return best;
}

// A hand link drawn while a map at one of its ends has moved since it was
// routed: Apply routes it again.
const linkStale = (l) => Object.entries(l.at || {}).some(([k, v]) => {
  const g = byKey.get(k);
  return !g || plan(g).offset[0] !== v[0] || plan(g).offset[1] !== v[1];
});

function linkPts(l) {
  if (!linkStale(l)) return l.path;
  const a = pointPos(l.a), b = pointPos(l.b);
  return a && b ? [a, b] : null;
}

function linkAt(ox, oy, list = links) {
  let best = null, bd = 8;
  for (const l of list) {
    const pts = (linkPts(l) || []).map(scr);
    for (let i = 1; i < pts.length; i++) {
      const [x1, y1] = pts[i - 1], [x2, y2] = pts[i];
      const len = (x2 - x1) ** 2 + (y2 - y1) ** 2;
      const t = len ? Math.max(0, Math.min(1, ((ox - x1) * (x2 - x1) + (oy - y1) * (y2 - y1)) / len)) : 0;
      const d = Math.hypot(ox - (x1 + t * (x2 - x1)), oy - (y1 + t * (y2 - y1)));
      if (d < bd) { bd = d; best = l; }
    }
  }
  return best;
}

function drawManual() {
  ctx.lineJoin = "miter";   // the paving turns with a square block too
  ctx.lineCap = "butt";
  for (const l of links) {
    const pts = linkPts(l);
    if (!pts) continue;
    const stale = linkStale(l);
    ctx.beginPath();
    pts.map(scr).forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    const wpx = Math.max(3, l.width * view.scale / 256);
    if (l.id === selLink) { ctx.strokeStyle = "#fff"; ctx.lineWidth = wpx + 6; ctx.stroke(); }
    ctx.strokeStyle = "rgba(0,0,0,.65)"; ctx.lineWidth = wpx + 2; ctx.stroke();
    ctx.setLineDash(stale ? [8, 6] : []);
    ctx.strokeStyle = stale ? "#9aa3b2" : "#3dd6ff"; ctx.lineWidth = wpx; ctx.stroke();
    ctx.setLineDash([]);
  }
  drawChoices();
  // Road ends: a green dot just past the end, with a tick the way the road runs out.
  for (const e of shownEnds()) {
    const len = Math.max(10, e.width * view.scale / 256 / 2 + 6);
    ctx.beginPath();
    ctx.moveTo(e.px, e.py);
    ctx.lineTo(e.px + e.dir[0] * len, e.py + e.dir[1] * len);
    ctx.strokeStyle = "rgba(0,0,0,.85)"; ctx.lineWidth = 5; ctx.stroke();
    // Paler: a road that stops inside the map. Yellow: a road under the map that it cuts off.
    // Teal: found by looking harder. Lime: made by hand.
    const green = e.cut ? "#f2d14b" : e.hand ? "#d4ff4f" : e.loose ? "#5fe0d0" : e.inner ? "#b6f0a0" : "#4be08a";
    ctx.strokeStyle = green; ctx.lineWidth = 2.5; ctx.stroke();
    const on = sameEnd(e, pick), hov = sameEnd(e, hoverEnd);
    ctx.beginPath();
    ctx.arc(e.px, e.py, hov || on ? 7 : 5, 0, 7);
    ctx.fillStyle = on ? "#3dd6ff" : green;
    ctx.fill();
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5; ctx.stroke();
    if (on || hov) { ctx.beginPath(); ctx.arc(e.px, e.py, 11, 0, 7); ctx.strokeStyle = on ? "#3dd6ff" : "#fff"; ctx.lineWidth = 2; ctx.stroke(); }
  }
  // The road being drawn: from the picked end to the mouse.
  const a = pick && pointPos(pick);
  if (a && mouse) {
    const [px, py] = scr(a);
    ctx.beginPath();
    ctx.moveTo(px, py);
    ctx.lineTo(mouse[0], mouse[1]);
    ctx.setLineDash([6, 5]);
    ctx.strokeStyle = "#3dd6ff"; ctx.lineWidth = 2; ctx.stroke();
    ctx.setLineDash([]);
  }
}

// The roads to pick from: ghosts, the one under the mouse drawn bright and on top.
const CHOICE_COLORS = ["#ffd166", "#ef8cff", "#ff8a4c", "#a8b8ff", "#ff6b8a"];   // not green: road ends are green
function drawChoices() {
  if (!choices) return;
  const order = choices.links.map((_, i) => i).filter((i) => i !== choices.hover);
  if (choices.hover !== null) order.push(choices.hover);
  for (const i of order) {
    const l = choices.links[i], on = i === choices.hover;
    const pts = l.path.map(scr);
    ctx.beginPath();
    pts.forEach(([x, y], k) => (k ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    const wpx = Math.max(3, l.width * view.scale / 256);
    ctx.globalAlpha = on ? 1 : 0.55;
    if (on) { ctx.strokeStyle = "#fff"; ctx.lineWidth = wpx + 6; ctx.stroke(); }
    ctx.strokeStyle = "rgba(0,0,0,.65)"; ctx.lineWidth = wpx + 2; ctx.stroke();
    ctx.setLineDash(on ? [] : [10, 6]);
    ctx.strokeStyle = CHOICE_COLORS[i % CHOICE_COLORS.length]; ctx.lineWidth = wpx; ctx.stroke();
    ctx.setLineDash([]);
    // Its number, halfway along, to match the list.
    const [mx, my] = midPoint(pts);
    ctx.beginPath();
    ctx.arc(mx, my, 10, 0, 7);
    ctx.fillStyle = CHOICE_COLORS[i % CHOICE_COLORS.length]; ctx.fill();
    ctx.strokeStyle = "#000"; ctx.lineWidth = 1.5; ctx.stroke();
    ctx.fillStyle = "#000"; ctx.font = "bold 12px system-ui, sans-serif";
    ctx.textAlign = "center"; ctx.textBaseline = "middle";
    ctx.fillText(String(i + 1), mx, my + 0.5);
    ctx.globalAlpha = 1;
  }
}

// The point halfway along a polyline, in screen pixels.
function midPoint(pts) {
  let total = 0;
  for (let i = 1; i < pts.length; i++) total += Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
  let left = total / 2;
  for (let i = 1; i < pts.length; i++) {
    const d = Math.hypot(pts[i][0] - pts[i - 1][0], pts[i][1] - pts[i - 1][1]);
    if (d >= left && d > 0) {
      const t = left / d;
      return [pts[i - 1][0] + t * (pts[i][0] - pts[i - 1][0]), pts[i - 1][1] + t * (pts[i][1] - pts[i - 1][1])];
    }
    left -= d;
  }
  return pts[0];
}

function choiceText(l, i) {
  const best = choices.links.find((o) => o.kind === "best");
  const name = {
    best: "Shortest way",
    other: best ? `Another way, ${l.length - best.length} squares longer` : "Another way",
    narrow: `Narrower road, ${l.width} wide`,
    cross: "Across the maps' own ground",
    through: "Straight through what's in the way",
  }[l.kind] || "A way";
  const notes = [`${l.length} squares`];
  if (l.bridge) notes.push(`a bridge over ${l.bridge} squares of water`);
  if (l.cross) notes.push(`runs ${l.cross} squares across a map's own ground and paves it`);
  if (l.gaps) notes.push(`${l.gaps} steps under walls or buildings stay unpaved, so the road has gaps there`);
  return `<span class="num" style="background:${CHOICE_COLORS[i % CHOICE_COLORS.length]}">${i + 1}</span>
    <span><b>${esc(name)}</b><br><span class="muted">${esc(notes.join(", "))}</span></span>`;
}

function renderChoices() {
  const box = $("choices");
  if (!choices) { box.hidden = true; box.innerHTML = ""; return; }
  const clean = choices.links.some((l) => l.kind === "best");
  const head = clean
    ? "Pick a road. Hover one to see it, click to keep it."
    : "No road that wide fits the plain way. These get through anyway. Hover one to see it, click to keep it.";
  box.innerHTML = `<div class="head">${esc(head)}</div>
    ${choices.links.map((l, i) => `<button data-i="${i}" class="${i === choices.hover ? "on" : ""}">${choiceText(l, i)}</button>`).join("")}
    <div class="foot muted">Keys 1 to ${choices.links.length} pick, Esc cancels. <button data-act="cancel">Cancel</button></div>`;
  box.hidden = false;
}

// Bring all the roads to pick from into view, clear of the list in the corner.
function frameChoices() {
  const xs = [], ys = [];
  for (const l of choices.links) for (const [x, y] of l.path) { xs.push(x / 256); ys.push(y / 256); }
  const x0 = Math.min(...xs), x1 = Math.max(...xs), y0 = Math.min(...ys), y1 = Math.max(...ys);
  const side = W() > 900 ? $("choices").offsetWidth + 30 : 0;
  view.scale = Math.max(0.8, Math.min(4096, (W() - side - 60) / Math.max(x1 - x0, 0.05), (H() - 120) / Math.max(y1 - y0, 0.05)));
  view.x = (x0 + x1) / 2 - side / 2 / view.scale;
  view.y = (y0 + y1) / 2;
}

function hoverChoice(i) {
  if (!choices || choices.hover === i) return;
  choices.hover = i;
  for (const b of $("choices").querySelectorAll("button[data-i]")) b.classList.toggle("on", Number(b.dataset.i) === i);
  redraw();
}

function choose(i) {
  if (!choices || !choices.links[i]) return;
  const { links: opts, replace } = choices;
  choices = null;
  renderChoices();
  keepLink(opts[i], replace);
  redraw();
}

function cancelChoices() {
  choices = null;
  renderChoices();
  hintMsg = { text: "No road link added." };
  redraw();
}

$("choices").addEventListener("mouseover", (e) => {
  const b = e.target.closest("button[data-i]");
  if (b) hoverChoice(Number(b.dataset.i));
});
$("choices").addEventListener("click", (e) => {
  const b = e.target.closest("button");
  if (!b) return;
  if (b.dataset.act === "cancel") cancelChoices();
  else choose(Number(b.dataset.i));
});

// What the shown maps' road ends are up to, for the hint line.
function endsHint() {
  const maps = shownMaps();
  if (!maps.length) return "";
  if (shownEnds().length) return "Click a green road end to start a road link. Ctrl+click maps to show theirs too. Alt+click a map's road to make a road end there.";
  const own = maps.filter((g) => !inKentucky(g));
  const loading = maps.filter((g) => inKentucky(g) && endsCache.get(endsKey(g)) === "loading");
  if (loading.length) return `<span class="spin small"></span> Looking for road ends of ${esc(loading.map((g) => g.title).join(", "))}.`;
  const n = maps.reduce((a, g) => a + (inKentucky(g) ? (g.lastEnds || []).length : 0), 0);
  if (n) return `${n} road end${n === 1 ? "" : "s"} on the shown maps, none in view. Zoom out or move to see the green dots.`;
  if (own.length) return `${esc(own.map((g) => g.title).join(", "))} ${own.length === 1 ? "is its" : "are their"} own world. Tick "Put it in the Kentucky world" to link roads to ${own.length === 1 ? "it" : "them"}.`;
  return `No road ends found on ${esc(maps.map((g) => g.title).join(", "))}. Select a map and press "Look harder for road ends", or Alt+click right on its road to make one.`;
}

let hintHtml = "";
function updateHint() {
  let t = "";
  if (routing || choices) t = "";
  else {
    if (hintMsg) t = `<span class="${hintMsg.err ? "err" : ""}">${esc(hintMsg.text)}</span>`;
    if (pick) t += (t ? "<br>" : "") + "Now click another green road end, Alt+click a map's road, or click any spot to run the road there. Esc cancels.";
    else if (!hintMsg) t = endsHint();
  }
  if (t !== hintHtml) { hintHtml = t; $("hint").innerHTML = t; }
  canvas.classList.toggle("picking", !!(pick || hoverEnd));
}

const endBody = (e) => ({ key: e.key, hx: e.hx, hy: e.hy, dir: e.dir, width: e.width, tile: e.tile, tiles: e.tiles, inner: !!e.inner });

// Link road end a to road end e, or to a spot when e is null. A road under a
// map that the map cuts off is a place to arrive at from any side, so when
// it was picked first and a map's road end second, the road runs the other way.
function joinTo(a, e, spot) {
  if (e && a.cut && !e.cut) [a, e] = [e, a];
  if (!e) return connect(a, { key: null, hx: spot[0], hy: spot[1] });
  if (e.cut) return connect(a, { key: null, hx: e.hx, hy: e.hy, cut: true });
  connect(a, { key: e.key, hx: e.hx, hy: e.hy, dir: e.dir, width: e.width, inner: !!e.inner });
}

// How long a route takes, in seconds per square of distance between the ends.
// Learned from past routes in this browser, so the estimate gets better with use.
const RATE_KEY = "mapmover.routeRate";
let routeRate = 0.012;
try { routeRate = Number(localStorage.getItem(RATE_KEY)) || routeRate; } catch {}

function guessSecs(a, b) {
  const pa = pointPos(a), pb = pointPos(b);
  const d = pa && pb ? Math.abs(pa[0] - pb[0]) + Math.abs(pa[1] - pb[1]) : 2000;
  return { d, est: 1.5 + routeRate * d };
}

let busyTimer = null;
function showBusy() {
  const box = $("busy");
  if (!routing) { box.hidden = true; clearInterval(busyTimer); busyTimer = null; return; }
  const secs = (performance.now() - routing.t0) / 1000;
  const left = routing.est - secs;
  const when = left > 0.5 ? `about ${Math.ceil(left)}s left` : "taking longer than usual, still working";
  box.querySelector(".what").textContent = `Processing. Building the road, ${Math.floor(secs)}s so far, ${when}.`;
  box.hidden = false;
  if (!busyTimer) busyTimer = setInterval(showBusy, 250);
}

// Route a road from road end a to b and keep it. replace: the id of a link it replaces.
async function connect(a, b, replace) {
  if (routing) return;
  const { d, est } = guessSecs(a, b);
  routing = { t0: performance.now(), est };
  hintMsg = null;
  choices = null;
  renderChoices();
  showBusy();
  redraw();
  try {
    const r = await api("/api/connect", { layout: layoutBody(), a: endBody(a), b });
    if (r.error) { hintMsg = { text: r.error, err: true }; return; }
    const opts = r.links || [r.link];
    pick = null;
    // One plain road: keep it. More, or only ones with a catch: let the user pick.
    if (opts.length === 1 && opts[0].kind === "best") keepLink(opts[0], replace);
    else { choices = { links: opts, hover: 0, replace }; renderChoices(); frameChoices(); }
  } catch (err) {
    hintMsg = { text: "Couldn't route it: " + err.message, err: true };
  } finally {
    // Learn from this route: a slow average so one odd route doesn't swing it.
    const secs = (performance.now() - routing.t0) / 1000;
    if (d > 50) {
      routeRate = routeRate * 0.6 + Math.max(0.001, (secs - 1.5) / d) * 0.4;
      try { localStorage.setItem(RATE_KEY, String(routeRate)); } catch {}
    }
    routing = null;
    showBusy();
    redraw();
  }
}

function keepLink(link, replace) {
  pushUndo();
  if (replace) {
    link.id = replace;
    links = links.map((l) => (l.id === replace ? link : l));
  } else links = [...links, link];
  selLink = link.id;
  const br = link.bridge ? `, with a bridge over ${link.bridge} squares of water` : "";
  const gaps = link.gaps ? ` It has gaps at ${link.gaps} steps under walls or buildings.` : "";
  hintMsg = { text: `Road link added, ${link.length} squares long${br}. Apply paves it.${gaps}` };
  changed();
}

function removeLink(id) {
  pushUndo();
  links = links.filter((l) => l.id !== id);
  if (selLink === id) selLink = null;
  changed();
}

const mapName = (k) => (k && byKey.has(k) ? byKey.get(k).title : k ? k : "a spot");
const linkName = (l) => `${l.a.key ? mapName(l.a.key) : "A cut-off road"} to ${l.b.dir ? mapName(l.b.key) + " road end" : l.b.cut ? "a cut-off road" : l.b.key ? "a spot in " + mapName(l.b.key) : "a spot"}`;

function renderManual() {
  const box = $("manual");
  if (!links.length && !highlighted.size) { box.innerHTML = ""; return; }
  let html = `<h3>Road links drawn by hand</h3>`;
  if (highlighted.size) html += `<div class="row muted">Showing road ends of ${highlighted.size} map${highlighted.size === 1 ? "" : "s"}. <button data-act="clear">Hide them</button></div>`;
  if (!links.length) html += `<p class="muted">Click a green road end on the map, then another road end or any spot. The road runs between them, as wide as the first one.</p>`;
  else {
    html += `<ul>${links.map((l) => `<li data-id="${esc(l.id)}" class="${l.id === selLink ? "sel" : ""}">
      <span class="what" title="${esc(linkName(l))}">${esc(linkName(l))}</span>
      ${linkStale(l) ? `<span class="tag amber" title="A map moved since it was routed. Apply routes it again, or press Route again now.">moved</span><button data-act="again">Route again</button>` : `<span class="muted">${l.length} sq</span>`}
      ${l.bridge ? `<span class="tag blue" title="Crosses ${l.bridge} squares of water on a road paved over it">bridge</span>` : ""}
      ${l.cross ? `<span class="tag amber" title="Runs ${l.cross} squares across a map's own ground and paves it">over a map</span>` : ""}
      ${l.gaps ? `<span class="tag amber" title="${l.gaps} steps run under walls or buildings, left unpaved">gaps</span>` : ""}
      <button data-act="show">Show</button><button data-act="del" class="danger" title="Remove (Delete key)">Remove</button></li>`).join("")}</ul>
      <p class="muted">Apply paves these into the road links mod. Road ends linked here are left out of the automatic links.</p>`;
  }
  box.innerHTML = html;
}

$("manual").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-act]");
  if (!b) return;
  if (b.dataset.act === "clear") { highlighted.clear(); renderList(); renderManual(); redraw(); return; }
  const l = links.find((x) => x.id === b.closest("li").dataset.id);
  if (!l) return;
  if (b.dataset.act === "del") return removeLink(l.id);
  if (b.dataset.act === "again") return connect(l.a, l.b, l.id);
  // Show: centre on it and zoom to fit.
  selLink = l.id;
  const pts = linkPts(l) || [];
  if (pts.length) {
    const xs = pts.map((q) => q[0]), ys = pts.map((q) => q[1]);
    view.x = (Math.min(...xs) + Math.max(...xs) + 1) / 2 / 256;
    view.y = (Math.min(...ys) + Math.max(...ys) + 1) / 2 / 256;
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), 40) / 256;
    view.scale = Math.max(0.8, Math.min(4096, 0.7 * Math.min(W(), H()) / span));
  }
  renderManual();
  redraw();
});

// Run fn with the canvas in cell units, shifted by (dx, dy) cells.
function inCells(dx, dy, fn) {
  const dpr = window.devicePixelRatio || 1, s = view.scale;
  ctx.save();
  ctx.setTransform(dpr * s, 0, 0, dpr * s, dpr * (W() / 2 + (dx - view.x) * s), dpr * (H() / 2 + (dy - view.y) * s));
  fn();
  ctx.restore();
}

// Stroke a cell-unit path with a width and dash in screen pixels.
function line(path, color, px, dash) {
  if (!path) return;
  ctx.strokeStyle = color;
  ctx.lineWidth = px / view.scale;
  ctx.lineJoin = "round";
  ctx.setLineDash(dash ? dash.map((d) => d / view.scale) : []);
  ctx.stroke(path);
  ctx.setLineDash([]);
}

function outline(g, dx, dy, color, width, dash) {
  ctx.strokeStyle = color;
  ctx.lineWidth = width;
  ctx.setLineDash(dash);
  ctx.beginPath();
  for (const [x1, y1, x2, y2] of g.segs) {
    ctx.moveTo(sx(x1 + dx), sy(y1 + dy));
    ctx.lineTo(sx(x2 + dx), sy(y2 + dy));
  }
  ctx.stroke();
  ctx.setLineDash([]);
}

// Layer badge colours: layer 1 is plain, each layer above it its own colour.
const LAYER_COLORS = ["#5b6475", "#8a5cf6", "#e0892b", "#1fa38a", "#d9457a", "#3b82f6"];
const layerColor = (n) => LAYER_COLORS[Math.min(n, LAYER_COLORS.length) - 1];

// A pill centred on (cx, cy): "Layer 2" zoomed in, else just "2". Returns
// its width; measure=true only measures.
function layerBadge(layer, cx, cy, wide, sel, measure) {
  const size = Math.round(Math.max(12, Math.min(18, view.scale * 0.5)));
  const t = wide ? `Layer ${layer}` : String(layer);
  ctx.font = `700 ${size}px system-ui, sans-serif`;
  const h = size + 8, w = Math.max(ctx.measureText(t).width + 12, h);
  if (measure) return w;
  ctx.beginPath();
  ctx.roundRect(cx - w / 2, cy - h / 2, w, h, h / 2);
  ctx.fillStyle = layerColor(layer);
  ctx.fill();
  ctx.lineWidth = sel ? 2.5 : 1.5;
  ctx.strokeStyle = sel ? "#fff" : "rgba(0,0,0,.75)";
  ctx.stroke();
  ctx.fillStyle = "#fff";
  ctx.textAlign = "center";
  ctx.fillText(t, cx, cy + 1);
  return w;
}

let drawQueued = false;
function redraw() {
  if (drawQueued) return;
  drawQueued = true;
  requestAnimationFrame(() => { drawQueued = false; draw(); });
}

// ------------------------------------------------------------------ hit test

function groupsAt(cx, cy) {
  const out = [];
  for (const g of groups) {
    const [dx, dy] = plan(g).offset;
    if (g.cellSet.has(ck(cx - dx, cy - dy))) out.push(g);
  }
  return out;
}

function pickAt(cx, cy) {
  const showOwn = $("showOwn").checked;
  const at = groupsAt(cx, cy).filter((g) => inKentucky(g) || showOwn || g.key === selected);
  if (!at.length) return null;
  const sel = at.find((g) => g.key === selected);
  if (sel) return sel;
  return at.find(inKentucky) || at[0];
}

// ------------------------------------------------------------------ mouse

let drag = null;
canvas.addEventListener("contextmenu", (e) => e.preventDefault());

canvas.addEventListener("mousedown", (e) => {
  canvas.focus();
  const cx = Math.floor(wx(e.offsetX)), cy = Math.floor(wy(e.offsetY));
  if (e.button === 0 && !routing && choices) {
    const l = linkAt(e.offsetX, e.offsetY, choices.links);
    if (l) { choose(choices.links.indexOf(l)); return; }
  }
  if (e.button === 0 && !routing && e.altKey) {
    e.preventDefault();
    const g = pickAt(cx, cy);
    if (g && inKentucky(g)) makeEnd(g, [Math.floor(wx(e.offsetX) * 256), Math.floor(wy(e.offsetY) * 256)]);
    else { hintMsg = { text: "Alt+click right on a road of a map in the Kentucky world to make a road end there.", err: true }; redraw(); }
    return;
  }
  if (e.button === 0 && !routing) {
    if (e.ctrlKey || e.metaKey) {
      const g = pickAt(cx, cy);
      if (g) toggleHighlight(g.key);
      return;
    }
    const end = endAt(e.offsetX, e.offsetY);
    if (end) {
      hintMsg = null;
      if (!pick) {
        // Click, then click the other end. Or hold and drag to it and let go.
        pick = end;
        drag = { kind: "link", px: e.clientX, py: e.clientY, moved: false };
        canvas.classList.add("picking");
      } else if (sameEnd(end, pick)) pick = null;
      else joinTo(pick, end);
      redraw();
      return;
    }
    if (pick) {
      // A click routes to this spot, a drag pans.
      drag = { kind: "pan", px: e.clientX, py: e.clientY, vx: view.x, vy: view.y, moved: false,
               spot: [Math.floor(wx(e.offsetX) * 256), Math.floor(wy(e.offsetY) * 256)] };
      canvas.classList.add("dragging");
      return;
    }
    const l = linkAt(e.offsetX, e.offsetY);
    if (l) {
      selLink = l.id;
      renderManual();
      redraw();
      return;
    }
    const g = pickAt(cx, cy);
    if (g) {
      select(g.key, false);
      drag = { kind: "move", g, start: [cx, cy], from: [...plan(g).offset], moved: false };
      canvas.classList.add("dragging");
      return;
    }
  }
  drag = { kind: "pan", px: e.clientX, py: e.clientY, vx: view.x, vy: view.y, moved: false };
  canvas.classList.add("dragging");
});

window.addEventListener("mousemove", (e) => {
  if (drag && drag.kind === "pan") {
    view.x = drag.vx - (e.clientX - drag.px) / view.scale;
    view.y = drag.vy - (e.clientY - drag.py) / view.scale;
    if (Math.abs(e.clientX - drag.px) + Math.abs(e.clientY - drag.py) > 3) drag.moved = true;
    redraw();
    return;
  }
  const r = canvas.getBoundingClientRect();
  const ox = e.clientX - r.left, oy = e.clientY - r.top;
  if (drag && drag.kind === "link") {
    if (Math.abs(e.clientX - drag.px) + Math.abs(e.clientY - drag.py) > 5) drag.moved = true;
    mouse = [ox, oy];
    const h = endAt(ox, oy);
    hoverEnd = sameEnd(h, pick) ? null : h;
    hideTip();
    redraw();
    return;
  }
  if (drag && drag.kind === "move") {
    const cx = Math.floor(wx(ox)), cy = Math.floor(wy(oy));
    const off = [drag.from[0] + cx - drag.start[0], drag.from[1] + cy - drag.start[1]];
    const cur = plan(drag.g).offset;
    if (off[0] !== cur[0] || off[1] !== cur[1]) {
      if (!drag.moved) pushUndo();
      drag.moved = true;
      setPlan(drag.g, off, plan(drag.g).join);
      recompute();
      redraw();
      updateDetail();
    }
    hideTip();
    return;
  }
  if (e.target === canvas && choices) {
    const l = linkAt(ox, oy, choices.links);
    if (l) hoverChoice(choices.links.indexOf(l));
  }
  if (e.target === canvas) {
    showTip(ox, oy);
    mouse = [ox, oy];
    const h = endAt(ox, oy);
    if (!sameEnd(h, hoverEnd) || pick) { hoverEnd = h; redraw(); }
  } else hideTip();
});

window.addEventListener("mouseup", (e) => {
  if (!drag) return;
  const d = drag;
  drag = null;
  canvas.classList.remove("dragging");
  if (d.kind === "link") {
    // Let go after a drag: route to the end or spot under the mouse. Let go
    // where it started: it was a click, so wait for the second click.
    if (!d.moved || !pick) return redraw();
    const r = canvas.getBoundingClientRect();
    const ox = e.clientX - r.left, oy = e.clientY - r.top;
    const end = endAt(ox, oy);
    if (end && sameEnd(end, pick)) return redraw();
    if (ox < 0 || oy < 0 || ox > r.width || oy > r.height) { pick = null; return redraw(); }
    joinTo(pick, end, [Math.floor(wx(ox) * 256), Math.floor(wy(oy) * 256)]);
    return;
  }
  if (d.kind === "pan" && !d.moved && d.spot) joinTo(pick, null, d.spot);
  else if (d.kind === "pan" && !d.moved) { selected = null; selLink = null; renderList(); renderDetail(); renderManual(); redraw(); }
  if (d.kind === "move" && d.moved) changed();
});

canvas.addEventListener("mouseleave", () => { hideTip(); mouse = null; redraw(); });

canvas.addEventListener("wheel", (e) => {
  e.preventDefault();
  const bx = wx(e.offsetX), by = wy(e.offsetY);
  const f = Math.exp(-e.deltaY * 0.0015);
  view.scale = Math.max(0.8, Math.min(4096, view.scale * f));   // up to 16 px per square
  view.x = bx - (e.offsetX - W() / 2) / view.scale;
  view.y = by - (e.offsetY - H() / 2) / view.scale;
  redraw();
}, { passive: false });

function showTip(ox, oy) {
  const cx = Math.floor(wx(ox)), cy = Math.floor(wy(oy));
  if (cx < S.min_cell - 5 || cy < S.min_cell - 5 || cx > S.max_cell + 5 || cy > S.max_cell + 5) return hideTip();
  const c = ck(cx, cy);
  const at = groupsAt(cx, cy);
  let html = view.scale >= 512
    ? `<b>Cell ${cx}, ${cy}</b> <span class="muted">· square ${Math.floor(wx(ox) * 256)}, ${Math.floor(wy(oy) * 256)}</span>`
    : `<b>Cell ${cx}, ${cy}</b> <span class="muted">· tiles ${cx * 256}, ${cy * 256}</span>`;
  if (vanilla.has(c)) html += `<br><span class="muted">Vanilla Kentucky cell</span>`;
  const v = ovCells.get(c);
  if (v) {
    if (v.t !== undefined) html += `<br><b style="color:#ffc98a">${esc(OV.towns[v.t].name)}</b> <span class="muted">(${v.b} buildings)</span>`;
    else if (v.b) html += `<br><span class="muted">${v.b} vanilla buildings</span>`;
    if (v.wn) html += `<br><span style="color:#8cc8ff">${esc(v.wn)}</span> <span class="muted">(${Math.round(v.w * 100)}% water)</span>`;
    if (v.s && v.s.length) html += `<br><span style="color:#f0b44c">${v.s.map(esc).join(", ")}</span>`;
    else if (v.hw) html += `<br><span style="color:#f0b44c">Highway</span>`;
  }
  at.sort(above);
  const stacked = conflictCells.has(c) || layeredCells.has(c);
  let first = true;
  for (const g of at) {
    const kt = inKentucky(g);
    const note = !kt ? ' <span class="muted">(own world)</span>'
      : stacked ? ` <span class="muted">(layer ${plan(g).layer}${first ? ", on top" : ""})</span>` : "";
    if (kt) first = false;
    html += `<br><i class="sw" style="background:${g.color}"></i>${esc(g.title)}${note}`;
  }
  if (conflictCells.has(c)) html += `<br><b style="color:#ff8080">Clash: same layer, so the mod list order picks the top map</b>`;
  else if (layeredCells.has(c)) html += `<br><span style="color:#c9a0ff">Overlap: the top map wins each square it has something on</span>`;
  if (cx < S.min_cell || cy < S.min_cell || cx > S.max_cell || cy > S.max_cell) html += `<br><b style="color:#ff8080">Outside the world</b>`;
  tip.innerHTML = html;
  tip.hidden = false;
  const tw = tip.offsetWidth, th = tip.offsetHeight;
  tip.style.left = Math.min(ox + 16, W() - tw - 6) + "px";
  tip.style.top = Math.min(oy + 16, H() - th - 6) + "px";
}
function hideTip() { tip.hidden = true; }

// ------------------------------------------------------------------ keys

window.addEventListener("keydown", (e) => {
  const tag = (document.activeElement && document.activeElement.tagName) || "";
  const typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === "z" && !typing) { e.preventDefault(); undo(); return; }
  if (typing || $("applyDlg").open) return;
  const g = selected && byKey.get(selected);
  if (choices && /^[1-9]$/.test(e.key)) { choose(Number(e.key) - 1); return; }
  if (e.key === "Escape") {
    if (choices) { cancelChoices(); return; }
    if (pick || hintMsg) { pick = null; hintMsg = null; }
    else if (selLink) { selLink = null; renderManual(); }
    else { selected = null; renderList(); renderDetail(); }
    redraw();
    return;
  }
  if ((e.key === "Delete" || e.key === "Backspace") && selLink) { e.preventDefault(); removeLink(selLink); return; }
  if (!g || routing) return;
  const n = e.shiftKey ? 10 : 1;
  const d = { ArrowLeft: [-n, 0], ArrowRight: [n, 0], ArrowUp: [0, -n], ArrowDown: [0, n] }[e.key];
  if (d) {
    e.preventDefault();
    pushUndo();
    const [ox, oy] = plan(g).offset;
    setPlan(g, [ox + d[0], oy + d[1]], plan(g).join);
    changed();
  }
});

// ------------------------------------------------------------------ undo + save

function pushUndo() {
  undoStack.push(JSON.stringify({ plans, addIds, off, links }));
  if (undoStack.length > 200) undoStack.shift();
  $("undo").disabled = false;
}

async function undo() {
  const s = undoStack.pop();
  $("undo").disabled = !undoStack.length;
  if (!s) return;
  const o = JSON.parse(s);
  const addChanged = JSON.stringify(o.addIds) !== JSON.stringify(addIds)
    || JSON.stringify(o.off || []) !== JSON.stringify(off);
  plans = o.plans;
  addIds = o.addIds;
  off = o.off || [];
  links = o.links || [];
  if (addChanged) { if (await save(true)) await load(true); return; }
  changed();
}

function changed() {
  recompute();
  renderList();
  renderManual();
  updateDetail();
  redraw();
  save(false);
}

// Saving. A failed save keeps the changes in this tab and in localStorage,
// retries every 3 s, and shows a banner until the server answers again.
let saveTimer = null, retryTimer = null, dirty = false, offline = false, restore = null;
const BACKUP = "mapmover.unsaved";

function backup(v) {
  try {
    if (v) localStorage.setItem(BACKUP, JSON.stringify(v));
    else localStorage.removeItem(BACKUP);
  } catch {}
}

function save(now) {
  clearTimeout(saveTimer);
  clearTimeout(retryTimer);
  dirty = true;
  backup({ plans, add_ids: addIds, off, links, when: new Date().toLocaleString() });
  $("saved").textContent = "Saving...";
  const go = async () => {
    try {
      const r = await api("/api/layout", layoutBody());
      dirty = false;
      if (r.crashes && JSON.stringify(r.crashes) !== JSON.stringify(crashList)) { crashList = r.crashes; banners(); }
      backup(null);
      $("saved").textContent = "Layout saved";
      if (offline) { offline = false; banners(); }
      return true;
    } catch (err) {
      $("saved").textContent = "Not saved yet, retrying...";
      if (!offline) { offline = true; banners(); }
      retryTimer = setTimeout(() => save(true), 3000);
      return false;
    }
  };
  if (now) return go();
  saveTimer = setTimeout(go, 400);
  return Promise.resolve(null);
}

window.addEventListener("beforeunload", (e) => {
  if (dirty) { e.preventDefault(); e.returnValue = ""; }
});

// Offer changes a closed tab never got to save, if they differ from the server.
function checkBackup() {
  let b = null;
  try { b = JSON.parse(localStorage.getItem(BACKUP) || "null"); } catch {}
  if (!b || !b.plans) return;
  const norm = (pl, ids) => JSON.stringify([Object.entries(pl).filter(([, p]) => p.offset[0] || p.offset[1] || p.join || p.layer > 1)
    .map(([k, p]) => [k, p.offset[0], p.offset[1], !!p.join, p.layer || 1]).sort(), [...ids].sort()]);
  const ids = (ls) => (ls || []).map((l) => l.id).sort().join(",");
  if (norm(b.plans, [...(b.add_ids || []), ...(b.off || off)]) === norm(plans, [...addIds, ...off]) && ids(b.links) === ids(links)) { backup(null); return; }
  restore = { plans: b.plans, add_ids: b.add_ids || [], off: b.off || off, links: b.links || links, when: b.when || "earlier" };
  banners();
}

// ------------------------------------------------------------------ sidebar

function select(key, center) {
  selected = key;
  renderList();
  renderDetail();
  if (center) {
    const g = byKey.get(key);
    const [dx, dy] = plan(g).offset;
    view.x = (g.box[0] + g.box[2]) / 2 + dx;
    view.y = (g.box[1] + g.box[3]) / 2 + dy;
    const span = Math.max(g.box[2] - g.box[0], g.box[3] - g.box[1]) + 10;
    view.scale = Math.max(view.scale, Math.min(16, Math.min(W(), H()) / span));
  }
  redraw();
  const li = document.querySelector(`#groups li[data-key="${CSS.escape(key)}"]`);
  if (li) li.scrollIntoView({ block: "nearest" });
}

function renderList() {
  const q = $("search").value.trim().toLowerCase();
  const only = $("onlyConflicts").checked;
  const ul = $("groups");
  const rows = [];
  const all = [...groups, ...offGroups.map((g) => ({ ...g, isOff: true }))]
    .sort((a, b) => a.title.toLowerCase().localeCompare(b.title.toLowerCase()));
  for (const g of all) {
    if (g.isOff) {
      if (only) continue;
      if (q && !(g.title.toLowerCase().includes(q) || g.mods.join(" ").toLowerCase().includes(q) || g.maps.join(" ").toLowerCase().includes(q))) continue;
      rows.push(`<li data-key="${esc(g.key)}" class="off" title="Switched off. Tick it to put it back."><input type="checkbox" class="onoff" title="Switch this map on"><span class="name" title="${esc(g.title)}">${esc(g.title)}</span><span class="tag">off</span>${g.active ? `<span class="tag amber">to apply</span>` : ""}</li>`);
      continue;
    }
    const h = hits.get(g.key);
    if (only && !h.conflict.size && !h.oob && !h.west) continue;
    if (q && !(g.title.toLowerCase().includes(q) || g.mods.join(" ").toLowerCase().includes(q) || g.maps.join(" ").toLowerCase().includes(q))) continue;
    const p = plan(g);
    const tags = [];
    if (h.oob) tags.push(`<span class="tag red">off map</span>`);
    if (h.west) tags.push(`<span class="tag red" title="The game crashes loading a new world with map cells west of x 0">west of 0</span>`);
    if (h.conflict.size) tags.push(`<span class="tag red">${h.conflict.size} clash</span>`);
    if (inKentucky(g) && (p.layer > 1 || h.layered.size)) tags.push(`<span class="tag purple" title="Where maps overlap, the higher layer wins">layer ${p.layer}</span>`);
    if (!inKentucky(g)) tags.push(`<span class="tag">own world</span>`);
    else if (!g.kentucky) tags.push(`<span class="tag blue">joining</span>`);
    const ut = inKentucky(g) && underTag(h.under);
    if (ut) tags.push(`<span class="tag${ut.startsWith("cuts") ? "" : " orange"}" title="Vanilla under this map">${esc(ut)}</span>`);
    if (p.offset[0] || p.offset[1]) tags.push(`<span class="tag green">${fmtOff(p.offset)}</span>`);
    if (g.added) tags.push(`<span class="tag blue">new</span>`);
    if (pending(g)) tags.push(`<span class="tag amber">to apply</span>`);
    rows.push(`<li data-key="${esc(g.key)}" class="${g.key === selected ? "sel" : ""}${highlighted.has(g.key) ? " hl" : ""}" title="Ctrl+click to show its road ends"><input type="checkbox" class="onoff" checked title="Switch this map off"><span class="swatch" style="background:${g.color}"></span><span class="name" title="${esc(g.title)}">${esc(g.title)}</span>${tags.join("")}</li>`);
  }
  ul.innerHTML = rows.join("") || `<li class="muted">No maps match.</li>`;
  const n = groups.filter(pending).length + offGroups.filter((g) => g.active).length;
  $("apply").textContent = n ? `Apply (${n})` : "Apply";
}

$("groups").addEventListener("click", (e) => {
  const li = e.target.closest("li[data-key]");
  if (li && e.target.classList.contains("onoff")) switchMap(li.dataset.key, e.target.checked);
  else if (li && li.classList.contains("off")) return;
  else if (li && (e.ctrlKey || e.metaKey)) toggleHighlight(li.dataset.key);
  else if (li) select(li.dataset.key, true);
});
$("search").addEventListener("input", renderList);
$("onlyConflicts").addEventListener("change", renderList);
$("showOwn").addEventListener("change", redraw);
$("showOverV").addEventListener("change", redraw);
$("showTowns").addEventListener("change", redraw);
$("showRoads").addEventListener("change", redraw);
$("showTerrain").addEventListener("change", redraw);
$("showLayers").addEventListener("change", redraw);

const fmtOff = (o) => `${o[0] >= 0 ? "+" : ""}${o[0]}, ${o[1] >= 0 ? "+" : ""}${o[1]}`;

function renderDetail() {
  const box = $("detail");
  const g = selected && byKey.get(selected);
  if (!g) {
    const clash = groups.filter((x) => hits.get(x.key).conflict.size).length;
    box.innerHTML = `<p class="muted">Click a map to select it, then drag it on the grid.</p>
      <p>${clash ? `<b style="color:#ff9b9b">${clash} maps clash</b> with another map right now.` : "No clashes."} Red cells are where two maps want the same spot. The game would only load one of them there.</p>
      <p class="muted">Moving a map makes a shifted copy in your mods folder and swaps it into your mod list. The workshop files are never touched.</p>
      <p class="muted">The tick box on each map in the list switches it on or off. An off map leaves your mod list at the next Apply.</p>
      <p class="muted">To join roads between maps, Ctrl+click the maps to show their road ends (green dots), click one end, then click another end or any spot. Apply paves the road.</p>
      <p class="muted">A road end missing? Select the map and press "Look harder for road ends", or Alt+click right on its road to make one there.</p>`;
    return;
  }
  const p = plan(g);
  const own = !g.kentucky;
  box.innerHTML = `
    <h2>${esc(g.title)}</h2>
    ${g.thumb ? `<img src="/api/thumb?key=${encodeURIComponent(g.key)}" alt="">` : ""}
    <div class="kv muted">Mods: ${g.mods.map(esc).join(", ")}</div>
    <div class="kv muted">Map folders: ${g.maps.map(esc).join(", ")}</div>
    <div class="kv muted">${g.cells.length} cells. Original spot x ${g.box[0]} to ${g.box[2] - 1}, y ${g.box[1]} to ${g.box[3] - 1}.</div>
    <div class="row">Offset in cells
      <input type="number" id="offX" step="1"> <input type="number" id="offY" step="1">
      <button id="resetBtn" title="Back to the original spot">Reset</button>
    </div>
    <div class="kv muted" id="newSpot"></div>
    <div class="row" id="layerRow">Layer
      <button id="layerDown" title="Load under maps it overlaps">&minus;</button><b id="layerNum" class="layer-num">1</b><button id="layerUp" title="Load over maps it overlaps">+</button>
      <span class="muted">Higher wins where maps overlap.</span></div>
    ${own ? `<div class="row"><label><input type="checkbox" id="joinBox"> Put it in the Kentucky world</label></div>
      <div class="kv muted">This map is its own world now, picked at New Game. Joining adds it to the normal Kentucky map, so move it somewhere free first.</div>` : ""}
    <div class="row" id="linkRow"><label><input type="checkbox" id="roadsBox"> Pave road links to the nearest roads</label>
      <button id="linksBtn">Show links</button></div>
    <div class="row" id="endsRow"><button id="looseBtn">Look harder for road ends</button></div>
    <div class="kv muted" id="endsNote">Still missing one? Alt+click right on its road where it should end.</div>
    <div id="detailStatus"></div>
    ${g.notes && g.notes.length ? `<details class="kv"><summary style="color:#ffd68a">${g.notes.length} notes from the last build</summary>
      <p class="muted">Scripts listed here hold their own coordinates and were copied unchanged. Features they drive (item spawns, paper maps, teleports) may still point at the old spot.</p>
      <ul class="muted">${g.notes.map((n) => `<li>${esc(n)}</li>`).join("")}</ul></details>` : ""}
    ${g.companions.length ? `<div class="kv muted">Also copied, because they require this map: ${g.companions.map(esc).join(", ")}</div>` : ""}
    ${g.mixed ? `<div class="kv" style="color:#ffd68a">Its copies were built at different offsets. Apply rebuilds them to match.</div>` : ""}
    ${g.added ? `<div class="row"><span class="tag blue">Not enabled yet</span><button id="removeAdd" class="danger">Don't add</button></div>` : ""}
  `;
  $("offX").addEventListener("change", offsetInput);
  $("offY").addEventListener("change", offsetInput);
  $("resetBtn").addEventListener("click", () => { pushUndo(); setPlan(g, [0, 0], false); changed(); });
  $("layerUp").addEventListener("click", () => setLayer(g, plan(g).layer + 1));
  $("layerDown").addEventListener("click", () => setLayer(g, plan(g).layer - 1));
  if (own) $("joinBox").addEventListener("change", (e) => { pushUndo(); setPlan(g, plan(g).offset, e.target.checked); changed(); });
  if (g.added) $("removeAdd").addEventListener("click", () => removeAdded(g));
  $("roadsBox").addEventListener("change", (e) => {
    pushUndo();
    setPlan(g, plan(g).offset, plan(g).join, e.target.checked);
    if (e.target.checked) groupLinks(g, true);
    else autoPreview.delete(g.key);
    changed();
    redraw();
  });
  $("looseBtn").addEventListener("click", () => {
    if (looseMaps.has(g.key)) { looseMaps.delete(g.key); hintMsg = null; }
    else { looseMaps.add(g.key); highlighted.add(g.key); renderList(); renderManual(); }
    updateDetail();
    redraw();
  });
  $("linksBtn").addEventListener("click", () => {
    if (!plan(g).roads && autoPreview.has(g.key)) autoPreview.delete(g.key);
    else { autoPreview.add(g.key); groupLinks(g, true); }
    updateDetail();
    redraw();
  });
  updateDetail();
}

function offsetInput() {
  const g = byKey.get(selected);
  const x = parseInt($("offX").value, 10), y = parseInt($("offY").value, 10);
  if (Number.isNaN(x) || Number.isNaN(y)) return updateDetail();
  pushUndo();
  setPlan(g, [x, y], plan(g).join);
  changed();
}

function updateDetail() {
  const g = selected && byKey.get(selected);
  if (!g || !$("offX")) return;
  const p = plan(g);
  if (document.activeElement !== $("offX")) $("offX").value = p.offset[0];
  if (document.activeElement !== $("offY")) $("offY").value = p.offset[1];
  if ($("joinBox")) $("joinBox").checked = p.join;
  $("linkRow").hidden = !canLink(g);
  $("endsRow").hidden = $("endsNote").hidden = !inKentucky(g);
  $("looseBtn").textContent = looseMaps.has(g.key) ? "Back to the usual road ends" : "Look harder for road ends";
  $("layerRow").hidden = !inKentucky(g);
  $("layerNum").textContent = p.layer;
  $("layerDown").disabled = p.layer <= 1;
  $("roadsBox").checked = p.roads;
  $("linksBtn").textContent = !p.roads && autoPreview.has(g.key) ? "Hide links" : "Show links";
  $("linksBtn").hidden = !!p.roads && !!linkCache.get(autoKey(g));
  $("newSpot").textContent = `Now at x ${g.box[0] + p.offset[0]} to ${g.box[2] - 1 + p.offset[0]}, y ${g.box[1] + p.offset[1]} to ${g.box[3] - 1 + p.offset[1]}.`;
  const h = hits.get(g.key);
  const lines = [];
  if (h.oob) lines.push(`<b style="color:#ff9b9b">${h.oob} cells are outside the world (${S.min_cell} to ${S.max_cell}). Apply will refuse.</b>`);
  if (h.west) lines.push(`<b style="color:#ff9b9b">${h.west} cells are west of x 0. The game can't load map cells there and crashes making a new world, so Apply will refuse. Move it right.</b>`);
  if (!inKentucky(g)) lines.push(`<span class="muted">Own world, so it can't clash with Kentucky maps.</span>`);
  else {
    const othersOn = (cells) => {
      const others = new Map();
      for (const c of cells) {
        const [x, y] = c.split(",").map(Number);
        for (const o of groupsAt(x, y)) if (o.key !== g.key && inKentucky(o)) others.set(o.key, o);
      }
      return [...others.values()];
    };
    if (h.conflict.size) {
      const others = othersOn(h.conflict);
      const top = [g, ...others].sort(above)[0];
      lines.push(`<b style="color:#ff9b9b">${h.conflict.size} cells clash</b> with ${others.map((o) => esc(o.title)).join(", ")}. They're on the same layer, so the mod list order puts ${top === g ? "this map" : esc(top.title)} on top. Raise the layer of the one that should win.`);
    }
    if (h.layered.size) {
      const others = othersOn(h.layered);
      const under = h.layered.size - h.top;
      lines.push(`<span style="color:#c9a0ff">${h.layered.size} cells overlap</span> ${others.map((o) => `${esc(o.title)} (layer ${plan(o).layer})`).join(", ")}.`
        + `${h.top ? ` This map is on top in ${h.top === h.layered.size ? "all of them" : h.top}.` : ""}${under ? ` It's under another map in ${under === h.layered.size ? "all of them" : under}.` : ""}`
        + ` <span class="muted">The top map wins every square it has anything on, grass too. Only squares it leaves empty show the map below.</span>`);
    }
    if (!h.conflict.size && !h.layered.size) lines.push(`<span style="color:#9be8b4">No clashes.</span>`);
    if (h.vanilla.size) lines.push(`<span class="muted">${h.vanilla.size} cells sit on vanilla Kentucky and replace it there. Most map mods do this on purpose.</span>`);
    lines.push(...underLines(h.under));
    const r = roadCache.get(g.key);
    if ($("showRoads").checked && r && r !== "loading") lines.push(r.empty
      ? `<span class="muted">This map has no roads in its world map data, so there's nothing to line up.</span>`
      : `<span class="muted">White lines are this map's own roads. Where they reach its edge is where a road into the rest of the world has to join.</span>`);
  }
  if (canLink(g) && showAuto(g)) {
    const c = linkCache.get(autoKey(g));
    if (c === "loading") lines.push(`<span class="muted">Looking for roads to join, this takes a few seconds...</span>`);
    else if (c && c.error) lines.push(`<span class="err">Couldn't work out road links: ${esc(c.error)}</span>`);
    else if (c && c.routes) {
      const sq = c.routes.reduce((a, r) => a + r.squares, 0);
      lines.push(c.routes.length || c.missed.length
        ? `<span>${c.routes.length} road link${c.routes.length === 1 ? "" : "s"}, ${sq} squares of road.</span> <span class="muted">Orange joins a road of the same surface, yellow the nearest other street.${c.missed.length ? ` Red dots are ${c.missed.length} road end${c.missed.length === 1 ? "" : "s"} with no road within 2 cells.` : ""}</span>`
        : `<span class="muted">No road of this map stops at its edge, so there is nothing to link.</span>`);
    } else if (p.roads) lines.push(`<span class="muted">Press Show links to see where the roads go.</span>`);
  }
  if (pending(g)) lines.push(`<span class="tag amber">Changed, press Apply to build it</span>`);
  $("detailStatus").innerHTML = lines.map((l) => `<div class="kv">${l}</div>`).join("");
}

// ------------------------------------------------------------------ add maps

function fillInactive() {
  const sel = $("inactive");
  const list = S.inactive || [];
  sel.innerHTML = list.length
    ? list.map((m) => `<option value="${esc(m.id)}">${esc(m.name)} (${m.cells} cells)</option>`).join("")
    : `<option value="">No other map mods installed</option>`;
  $("addBtn").disabled = !list.length;
}

$("addBtn").addEventListener("click", async () => {
  const id = $("inactive").value;
  if (!id || addIds.includes(id)) return;
  pushUndo();
  addIds = [...addIds, id];
  $("saved").textContent = "Adding...";
  if (!(await save(true))) {
    addIds = addIds.filter((x) => x !== id);
    $("saved").textContent = "Can't add a map while the map mover is closed";
    return;
  }
  await load(true);
  const g = groups.find((x) => x.mods.includes(id));
  if (g) select(g.key, true);
});

// Switch a map off or back on. Off maps leave the mod list at the next
// Apply and keep their spot, layer and moved copy for when they come back.
async function switchMap(key, on) {
  const g = on ? offGroups.find((x) => x.key === key) : byKey.get(key);
  if (!g) return;
  pushUndo();
  if (on) {
    off = off.filter((k) => k !== key);
    // Already out of the mod list: it goes back in like an added map.
    if (!g.active) addIds = [...addIds, ...g.mods.filter((i) => !addIds.includes(i))];
  } else {
    off = [...off, key];
    addIds = addIds.filter((i) => !g.mods.includes(i));
    if (selected === key) selected = null;
    highlighted.delete(key);
  }
  $("saved").textContent = on ? "Switching on..." : "Switching off...";
  if (await save(true)) await load(true);
}

async function removeAdded(g) {
  pushUndo();
  addIds = addIds.filter((i) => !g.mods.includes(i));
  delete plans[g.key];
  if (!(await save(true))) return;
  selected = null;
  await load(true);
}

// ------------------------------------------------------------------ banners

// What to do about one crash spot, in words.
function crashFix(c) {
  const top = esc(c.top_title), asked = esc(c.asked_title);
  if (c.forced) return `${top} always loads on top (its own Lua puts it there), so layers can't fix this. Move ${top} or ${asked} so they don't touch here.`;
  if (!c.asked_key) return `The vanilla map can't go on a higher layer. Move ${top} so it doesn't touch the vanilla map here.`;
  return `Put ${asked} on a higher layer than ${top}, or move one of them.`;
}

function crashHtml(list, buttons) {
  const cells = (c) => c.cells.map(([x, y]) => `${x},${y}`).join(" and ");
  return `<b>The game will crash loading a new world with this layout.</b> It's a bug in build 42: where a map is on top of a cell, the game also reads the next map down that has a nearby block, and crashes if that map doesn't have this exact cell.<ul>` +
    list.map((c, i) => `<li>${esc(c.top_title)} over ${esc(c.asked_title)} at cell ${cells(c)}. ${crashFix(c)}` +
      (buttons ? ` <button data-crash="${i}">Show</button>` : "") + `</li>`).join("") + `</ul>`;
}

function banners() {
  const b = [];
  if (S.error) b.push(`<div class="err">${esc(S.error)}</div>`);
  if (crashList.length) b.push(`<div class="err">${crashHtml(crashList, true)}</div>`);
  const west = groups.filter((g) => hits.get(g.key)?.west);
  if (west.length) b.push(`<div class="err"><b>The game will crash loading a new world with this layout.</b> ${west.map((g) => esc(g.title)).join(", ")} ${west.length > 1 ? "have" : "has"} cells west of x 0, and the game can't read map cells there. Move ${west.length > 1 ? "them" : "it"} right so every cell is at x 0 or more.</div>`);
  if (S.maps_rebuilt) b.push(`<div class="warn">Your mod list has no maps block, which happens when the game's mod screen saves it. The map order here comes from your last Apply. Press Apply to write it back.</div>`);
  if (S.game_running) b.push(`<div class="warn">Project Zomboid is running. You can plan, but close the game before Apply.</div>`);
  if (OV && OV.error) b.push(`<div class="warn">${esc(OV.error)} Towns and roads aren't shown.</div>`);
  if (offline) b.push(`<div class="err">Can't reach the map mover, so your last changes aren't saved yet. They're kept in this tab and saved as soon as it's back. Start it from the desktop shortcut.</div>`);
  if (restore) b.push(`<div class="warn">This browser has unsaved changes from an earlier visit (${esc(restore.when)}). <button id="restoreBtn">Restore them</button> <button id="dropBtn">Throw away</button></div>`);
  if (S.version_guessed) b.push(`<div class="warn">Couldn't read the game version from its logs, so the newest mod folders are assumed. Start the game once to fix this.</div>`);
  $("banners").innerHTML = b.join("");
  for (const btn of $("banners").querySelectorAll("[data-crash]")) {
    btn.addEventListener("click", () => {
      const c = crashList[+btn.dataset.crash];
      const key = c && (byKey.has(c.top_key) ? c.top_key : c.asked_key);
      if (key && byKey.has(key)) select(key, true);
    });
  }
  if (restore) {
    $("restoreBtn").addEventListener("click", async () => {
      const r = restore;
      restore = null;
      pushUndo();
      plans = r.plans;
      addIds = r.add_ids;
      off = r.off;
      links = r.links;
      if (await save(true)) await load(true);
      else { recompute(); renderList(); renderDetail(); redraw(); banners(); }
    });
    $("dropBtn").addEventListener("click", () => { restore = null; backup(null); banners(); });
  }
}

// ------------------------------------------------------------------ apply

$("apply").addEventListener("click", async () => {
  clearTimeout(saveTimer);
  if (!(await save(true))) return;
  const dlg = $("applyDlg");
  $("progressWrap").hidden = true;
  $("applyGo").hidden = false;
  $("applyGo").disabled = true;
  $("applyCancel").disabled = false;
  $("applyBody").innerHTML = "<p class='muted'>Working out what changes...</p>";
  dlg.showModal();
  let pv;
  try {
    pv = await api("/api/preview", layoutBody());
  } catch (err) {
    $("applyBody").innerHTML = `<p class="err">${esc(err.message)}</p>`;
    return;
  }
  const clashes = groups.filter((g) => hits.get(g.key).conflict.size).length;
  let html = "";
  if (pv.errors.length) html += `<p class="err"><b>Can't apply yet:</b></p><ul class="err">${pv.errors.map((e) => `<li>${esc(e)}</li>`).join("")}</ul>`;
  if (pv.crashes && pv.crashes.length) html += `<div class="err">${crashHtml(pv.crashes, false)}<p>You can still Apply, but fix these first if you want to play this layout.</p></div>`;
  if (pv.copies.length) {
    html += `<p>These mods get a shifted copy in your mods folder (named <code>&lt;id&gt;_moved</code>):</p><ul>`;
    for (const c of pv.copies) {
      const parts = [];
      if (c.offset[0] || c.offset[1]) parts.push(`moved ${fmtOff(c.offset)} cells`);
      if (c.join) parts.push("joined to Kentucky");
      if (!parts.length) parts.push("not moved, copied because it requires a moved mod");
      html += `<li>${esc(c.name)} <span class="muted">(${esc(c.id)}): ${parts.join(", ")}</span></li>`;
    }
    html += `</ul>`;
  } else html += `<p>No map is moved, so no copies are needed. Any old copies get removed and the originals go back in the mod list.</p>`;
  if (pv.added.length || pv.removed.length) {
    html += `<p>Mod list changes:</p><ul>`;
    for (const i of pv.added) html += `<li>+ ${esc(i)}</li>`;
    for (const i of pv.removed) html += `<li class="muted">- ${esc(i)}</li>`;
    html += `</ul>`;
  } else html += `<p class="muted">The mod list stays the same.</p>`;
  if (pv.off && pv.off.length) html += `<p>Switched off, so they leave the mod list: ${pv.off.map(esc).join(", ")}. Their spot and copies stay for when you switch them back on.</p>`;
  if (pv.links && pv.links.length) html += `<p>Road links get paved for ${pv.links.map(esc).join(", ")}. They go in one mod, <code>mapmover_roads</code>, rebuilt on every Apply.</p>`;
  if (pv.manual) html += `<p>${pv.manual} road link${pv.manual === 1 ? "" : "s"} drawn by hand get paved too, in <code>mapmover_roads</code>.</p>`;
  if (pv.layers && pv.layers.length) html += `<p>Layers: ${pv.layers.map(([t, n]) => `${esc(t)} on ${n}`).join(", ")}. Everything else is on layer 1.${pv.reorder ? " The <code>maps</code> list in your mod list gets sorted so higher layers load first." : " The <code>maps</code> list is already in that order."}</p>`;
  else if (pv.reorder) html += `<p>The <code>maps</code> list in your mod list gets sorted back to mod list order.</p>`;
  if (clashes) html += `<p style="color:#ffd68a">${clashes} maps still clash on the same layer. Apply will still work, and the mod list order picks which one is on top there.</p>`;
  html += `<p class="muted">Your current mod list is backed up first. Start a new save afterwards. Existing saves keep their maps where they were.</p>`;
  $("applyBody").innerHTML = html;
  $("applyGo").disabled = pv.errors.length > 0;
});

$("applyCancel").addEventListener("click", () => $("applyDlg").close());
// Esc must not hide the dialog while an Apply is running.
$("applyDlg").addEventListener("cancel", (e) => { if ($("applyCancel").disabled) e.preventDefault(); });

$("applyGo").addEventListener("click", async () => {
  $("applyGo").disabled = true;
  $("applyCancel").disabled = true;
  $("progressWrap").hidden = false;
  $("log").textContent = "";
  let r;
  try {
    r = await api("/api/apply", layoutBody());
  } catch (err) {
    $("log").textContent = "Couldn't start: " + err.message;
    $("applyCancel").disabled = false;
    return;
  }
  jobId = r.id;
  lastSeen = Date.now();
  poll();
});

let jobId = null, lastSeen = 0;

// The server stopped or restarted in the middle (closed, crashed, killed):
// say so instead of waiting forever.
function applyLost(why) {
  $("log").textContent += `\n\n${why} The Apply didn't finish. Your mod list wasn't changed (it's written last) and your layout is saved. Open the map mover again and press Apply; it clears the half-built copies first.`;
  $("log").scrollTop = $("log").scrollHeight;
  $("progress").max = 1; $("progress").value = 0;
  $("applyCancel").disabled = false;
  $("applyGo").hidden = true;
}

async function poll() {
  let j;
  try { j = await api("/api/progress"); } catch (err) {
    if (Date.now() - lastSeen > 15000) { applyLost("Lost contact with the map mover."); return; }
    setTimeout(poll, 1000);
    return;
  }
  lastSeen = Date.now();
  if (jobId && j.id !== jobId) { applyLost("The map mover restarted."); return; }
  $("log").textContent = j.log.join("\n");
  $("log").scrollTop = $("log").scrollHeight;
  const pr = $("progress");
  if (j.total) { pr.max = j.total; pr.value = j.done; } else pr.removeAttribute("value");
  if (j.running || !j.finished) { setTimeout(poll, 400); return; }
  pr.max = 1; pr.value = 1;
  $("applyCancel").disabled = false;
  $("applyGo").hidden = true;
  undoStack.length = 0;
  $("undo").disabled = true;
  await load(true);
}

// ------------------------------------------------------------------ test load

// Loads a new world from the mod list as it is now (after the last Apply) in
// the game's dedicated server, hidden, and shows whether it got there.
let testId = null, testT0 = 0, testSeen = 0;

$("testBtn").addEventListener("click", () => {
  $("testResult").innerHTML = "";
  $("testLog").textContent = "";
  $("testGo").hidden = false;
  $("testGo").disabled = false;
  $("testStop").hidden = true;
  $("testClose").disabled = false;
  $("testIntro").hidden = false;
  $("testDlg").showModal();
});

$("testGo").addEventListener("click", async () => {
  let r;
  try { r = await api("/api/testload", {}); } catch (err) {
    $("testResult").innerHTML = `<p class="err">Couldn't start: ${esc(err.message)}</p>`;
    return;
  }
  testId = r.id;
  testT0 = testSeen = Date.now();
  $("testGo").hidden = true;
  $("testStop").hidden = false;
  $("testStop").disabled = false;
  $("testClose").disabled = true;
  $("testIntro").hidden = true;
  pollTest();
});

$("testStop").addEventListener("click", async () => {
  $("testStop").disabled = true;
  try { await api("/api/testload/stop", {}); } catch {}
});
$("testClose").addEventListener("click", () => $("testDlg").close());
$("testDlg").addEventListener("cancel", (e) => { if ($("testClose").disabled) e.preventDefault(); });

async function pollTest() {
  let j;
  try { j = await api("/api/progress"); } catch {
    if (Date.now() - testSeen > 15000) return testDone(`<p class="err">Lost contact with the map mover, so the test stopped with it.</p>`);
    setTimeout(pollTest, 1000);
    return;
  }
  testSeen = Date.now();
  if (j.id !== testId) return testDone(`<p class="err">The map mover restarted, so the test stopped with it.</p>`);
  $("testLog").textContent = j.log.join("\n");
  $("testLog").scrollTop = $("testLog").scrollHeight;
  if (j.running || !j.finished) {
    $("testResult").innerHTML = `<p class="muted"><span class="spin small"></span> Working, ${Math.round((Date.now() - testT0) / 1000)} s so far. A big mod list takes a minute or two.</p>`;
    setTimeout(pollTest, 1000);
    return;
  }
  const r = j.result;
  if (j.error || !r) return testDone(`<p class="err">The test itself broke: ${esc(j.error || "no result")}. The log above says where.</p>`);
  if (r.ok) testDone(`<p class="ok"><b>The world loads.</b> A new game with this mod list gets past the loading screen.</p>`
    + (r.lines.length ? `<p class="muted">${r.lines.length} error line${r.lines.length === 1 ? "" : "s"} in the log, listed above. They didn't stop the load, most come from mod scripts.</p>` : ""));
  else if (r.stopped) testDone(`<p class="muted">Stopped before the world finished loading.</p>`);
  else testDone(`<p class="err"><b>The world load failed.</b> A new game would crash or hang the same way. The errors above are where to look, the last ones first.</p>`);
}

function testDone(html) {
  $("testResult").innerHTML = html;
  $("testStop").hidden = true;
  $("testClose").disabled = false;
}

// ------------------------------------------------------------------ world image

// The whole world as one PNG with each map's name over it (mapmover/worldpng.py).
let pngId = null, pngT0 = 0, pngSeen = 0;

$("pngBtn").addEventListener("click", () => {
  $("pngResult").innerHTML = "";
  $("pngLog").textContent = "";
  $("pngWrap").hidden = true;
  $("pngIntro").hidden = false;
  $("pngGo").hidden = false;
  $("pngGo").disabled = false;
  for (const id of ["pngStop", "pngOpen", "pngFolder"]) $(id).hidden = true;
  $("pngClose").disabled = false;
  $("pngDlg").showModal();
});

$("pngGo").addEventListener("click", async () => {
  let r;
  $("pngGo").disabled = true;
  try { r = await api("/api/worldpng", { scale: Number($("pngScale").value), names: $("pngNames").checked }); }
  catch (err) {
    $("pngResult").innerHTML = `<p class="err">Couldn't start: ${esc(err.message)}</p>`;
    $("pngGo").disabled = false;
    return;
  }
  pngId = r.id;
  pngT0 = pngSeen = Date.now();
  $("pngGo").hidden = true;
  $("pngStop").hidden = false;
  $("pngStop").disabled = false;
  $("pngClose").disabled = true;
  $("pngIntro").hidden = true;
  $("pngWrap").hidden = false;
  $("pngProgress").value = 0;
  pollPng();
});

$("pngStop").addEventListener("click", async () => {
  $("pngStop").disabled = true;
  try { await api("/api/testload/stop", {}); } catch {}
});
$("pngOpen").addEventListener("click", () => api("/api/worldpng/show", {}).catch(() => {}));
$("pngFolder").addEventListener("click", () => api("/api/worldpng/show", { folder: true }).catch(() => {}));
$("pngClose").addEventListener("click", () => $("pngDlg").close());
$("pngDlg").addEventListener("cancel", (e) => { if ($("pngClose").disabled) e.preventDefault(); });

async function pollPng() {
  let j;
  try { j = await api("/api/progress"); } catch {
    if (Date.now() - pngSeen > 15000) return pngDone(`<p class="err">Lost contact with the map mover, so the image stopped with it.</p>`);
    setTimeout(pollPng, 1000);
    return;
  }
  pngSeen = Date.now();
  if (j.id !== pngId) return pngDone(`<p class="err">The map mover restarted, so the image stopped with it.</p>`);
  $("pngLog").textContent = j.log.join("\n");
  $("pngLog").scrollTop = $("pngLog").scrollHeight;
  if (j.total) $("pngProgress").value = j.done / j.total;
  if (j.running || !j.finished) {
    const pct = j.total ? `${Math.round(100 * j.done / j.total)}% of the cells, ` : "";
    $("pngResult").innerHTML = `<p class="muted"><span class="spin small"></span> Drawing, ${pct}${Math.round((Date.now() - pngT0) / 1000)} s so far.</p>`;
    setTimeout(pollPng, 1000);
    return;
  }
  if (j.error === "stopped") return pngDone(`<p class="muted">Stopped. Nothing was saved.</p>`);
  const r = j.result;
  if (j.error || !r) return pngDone(`<p class="err">The image broke: ${esc(j.error || "no result")}. The log above says where.</p>`);
  $("pngProgress").value = 1;
  $("pngOpen").hidden = $("pngFolder").hidden = false;
  pngDone(`<p class="ok"><b>Saved.</b> ${r.w.toLocaleString()} x ${r.h.toLocaleString()} pixels, ${r.mb} MB${r.names ? `, ${r.names} map names` : ""}.</p>`
    + `<p class="muted">${esc(r.path)}</p>`);
}

function pngDone(html) {
  $("pngResult").innerHTML = html;
  $("pngStop").hidden = true;
  $("pngClose").disabled = false;
}

// ------------------------------------------------------------------ header

$("undo").addEventListener("click", undo);
$("undo").disabled = true;
$("rescan").addEventListener("click", async () => {
  $("rescan").disabled = true;
  $("saved").textContent = "Rescanning...";
  try { await api("/api/rescan", {}); await load(true); $("saved").textContent = "Rescanned"; }
  catch (err) { $("saved").textContent = "Rescan failed: " + err.message; }
  $("rescan").disabled = false;
});

window.addEventListener("resize", resize);
resize();
load(false).then(() => {
  // #<group key> in the address selects that map (handy for bookmarks).
  const k = decodeURIComponent(location.hash.slice(1));
  if (k && byKey.has(k)) select(k, true);
  checkBackup();
  loadOverlay();
}).catch((err) => {
  $("banners").innerHTML = `<div class="err">Couldn't load: ${esc(err.message)}. Is the map mover window still open?</div>`;
});

// World view: the isometric render served under /world/ (mapmover/worldrender.py).
$("worldBtn").addEventListener("click", async () => {
  $("worldView").hidden = false;
  let ok = false;
  try { ok = (await fetch("/world/pzmap_config.json", {cache: "no-store"})).ok; } catch (e) {}
  $("worldNone").hidden = ok;
  $("worldFrame").hidden = !ok;
  if (ok && !$("worldFrame").src) $("worldFrame").src = "/world/pzmap.html";
});
$("worldClose").addEventListener("click", () => { $("worldView").hidden = true; });
