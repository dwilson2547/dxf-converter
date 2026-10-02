'use strict';

/* Geometry is held in millimetres with the page origin and Y pointing up, the
 * same convention the DXF uses. The SVG draws in "local" space, which is the
 * same millimetres but with Y pointing down, so the scan image lines up. Only
 * two helpers cross between them; everything else stays in model space. */

const SVGNS = 'http://www.w3.org/2000/svg';
const $ = (id) => document.getElementById(id);

const S = {
  id: null,
  page: null,
  paths: [],
  rejects: [],
  report: null,
  sel: new Set(),
  selVert: null,
  view: { k: 1, tx: 0, ty: 0 },
  undo: [],
  redo: [],
  dirty: false,
  inks: [],               // picked ink colours (photo mode); empty = auto
  hiddenLayers: new Set(),
  picking: false,
  pixels: null,           // the photo, drawn to a canvas for the eyedropper
};

const SETTINGS = ['mode', 'min_length_mm', 'border_margin_mm', 'simplify_mm',
  'smooth_mm', 'threshold', 'flatten_mm', 'close_gaps_mm', 'min_area_mm2',
  'prune_spur_mm'];

const PHOTO_SETTINGS = ['colors'];
const PHOTO_FLAGS = ['circles', 'square'];

const DEFAULTS = {
  colors: 4, circles: true, square: false,
  mode: 'outline', min_length_mm: 6, border_margin_mm: 3, simplify_mm: 0.05,
  smooth_mm: 0.6, threshold: 'otsu', flatten_mm: 3, close_gaps_mm: 0,
  min_area_mm2: 0.3, prune_spur_mm: 1.5,
};

const MODE_HINT = {
  outline: 'Both edges of every stroke. Tracing rides the pen against the '
    + 'object, so the inner edge is the true boundary — measure against that.',
  centerline: 'One curve per stroke, down its middle. Tidier, but sits about '
    + 'half a stroke-width outside the true edge.',
};

/* ---------- coordinate helpers ---------- */

const toLocal = (p) => [p[0], S.page.height_mm - p[1]];
const toModel = (x, y) => [x, S.page.height_mm - y];

function screenToLocal(evt) {
  const g = $('gRoot');
  const pt = new DOMPoint(evt.clientX, evt.clientY);
  const m = g.getScreenCTM();
  if (!m) return [0, 0];
  const p = pt.matrixTransform(m.inverse());
  return [p.x, p.y];
}

/* ---------- toast ---------- */

let toastTimer = null;
function toast(msg, isError) {
  const el = $('toast');
  el.textContent = msg;
  el.className = 'toast' + (isError ? ' err' : '');
  el.hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 2800);
}

/* ---------- undo ---------- */

function snapshot() {
  S.undo.push(JSON.stringify(S.paths));
  if (S.undo.length > 60) S.undo.shift();
  S.redo.length = 0;
  S.dirty = true;
  $('editWarn').hidden = false;
}

function undo() {
  if (!S.undo.length) return;
  S.redo.push(JSON.stringify(S.paths));
  S.paths = JSON.parse(S.undo.pop());
  S.sel.clear(); S.selVert = null;
  render();
}

function redo() {
  if (!S.redo.length) return;
  S.undo.push(JSON.stringify(S.paths));
  S.paths = JSON.parse(S.redo.pop());
  S.sel.clear(); S.selVert = null;
  render();
}

/* ---------- upload / convert ---------- */

async function upload(file) {
  const fd = new FormData();
  fd.append('file', file);
  const res = await fetch('/api/upload', { method: 'POST', body: fd });
  if (!res.ok) throw new Error((await res.json()).detail || 'upload failed');
  const data = await res.json();
  S.id = data.id;
  S.inks = []; S.hiddenLayers.clear(); S.pixels = null; S.report = null;
  $('source').value = data.suggested_source || 'scan';
  $('fit_mm').value = '';
  syncLabels();
  renderInks();
  $('fileName').textContent = data.name;
  $('editor').hidden = false;
  $('drop').style.display = 'none';
  return data;
}

function readSettings() {
  const s = {};
  for (const key of SETTINGS) {
    const el = $(key);
    s[key] = el.type === 'range' ? parseFloat(el.value) : el.value;
  }
  const dpi = $('dpi').value.trim();
  if (dpi) s.dpi = parseFloat(dpi);
  s.source = $('source').value;
  const fit = $('fit_mm').value.trim();
  if (fit) s.fit_mm = parseFloat(fit);
  for (const key of PHOTO_SETTINGS) s[key] = parseInt($(key).value, 10);
  for (const key of PHOTO_FLAGS) s[key] = $(key).checked;
  s.inks = S.inks;
  return s;
}

async function detect() {
  $('status').textContent = 'Detecting…';
  try {
    const res = await fetch(`/api/convert/${S.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(readSettings()),
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'conversion failed');
    const data = await res.json();

    S.page = data.page;
    S.paths = data.paths.map((p) => ({
      points: p.points, closed: p.closed, layer: p.layer, color: p.color, kind: p.kind,
    }));
    S.rejects = data.rejects;
    S.report = data.report;
    S.sel.clear(); S.selVert = null;
    S.undo.length = 0; S.redo.length = 0;
    S.dirty = false;
    $('editWarn').hidden = true;

    render();
    zoomFit();
    renderInks();
    $('status').textContent =
      `${data.report.paths} paths · ${data.report.vertices} points`;
  } catch (err) {
    toast(err.message, true);
    $('status').textContent = 'Detection failed';
  }
}

/* ---------- layers ---------- */

const visible = (p) => !S.hiddenLayers.has(p.layer);

function layers() {
  const seen = new Map();
  for (const p of S.paths) {
    const key = p.layer || 'PROFILE';
    if (!seen.has(key)) seen.set(key, { name: key, color: p.color, n: 0 });
    seen.get(key).n += 1;
  }
  return [...seen.values()];
}

function renderLayers() {
  const ls = layers();
  $('layersSection').hidden = ls.length < 2;
  const list = $('layerList');
  list.innerHTML = '';
  for (const l of ls) {
    const row = document.createElement('label');
    row.className = 'layer-row';
    row.innerHTML = `<input type="checkbox" ${S.hiddenLayers.has(l.name) ? '' : 'checked'}>
      <span class="swatch" style="background:${l.color || '#2fbf71'}"></span>
      <span>${l.name}</span><span class="count">${l.n}</span>`;
    row.querySelector('input').addEventListener('change', (e) => {
      if (e.target.checked) S.hiddenLayers.delete(l.name);
      else S.hiddenLayers.add(l.name);
      S.sel.clear(); S.selVert = null;
      render();
    });
    list.appendChild(row);
  }
}

/* ---------- eyedropper ---------- */

function renderInks() {
  const box = $('inkChips');
  box.innerHTML = '';
  const auto = !S.inks.length;
  const shown = auto ? ((S.report && S.report.palette && S.report.palette.inks) || []) : S.inks;
  for (const hex of shown) {
    const chip = document.createElement('span');
    chip.className = 'chip' + (auto ? ' auto' : '');
    chip.innerHTML = `<span class="swatch" style="background:${hex}"></span>${hex}`;
    if (!auto) {
      chip.title = 'Remove';
      chip.onclick = () => {
        S.inks = S.inks.filter((h) => h !== hex);
        renderInks();
        redetect();
      };
    }
    box.appendChild(chip);
  }
  if (auto && !shown.length) box.innerHTML = '<span class="muted">auto</span>';
  $('inkHint').hidden = !auto;
}

function loadPixels() {
  if (S.pixels) return Promise.resolve(S.pixels);
  return new Promise((resolve, reject) => {
    const img = new Image();
    img.onload = () => {
      const c = document.createElement('canvas');
      c.width = img.naturalWidth; c.height = img.naturalHeight;
      const ctx = c.getContext('2d', { willReadFrequently: true });
      ctx.drawImage(img, 0, 0);
      S.pixels = { ctx, w: c.width, h: c.height };
      resolve(S.pixels);
    };
    img.onerror = () => reject(new Error('could not read the image for picking'));
    img.src = `/api/scan/${S.id}`;
  });
}

async function pickAt(lx, ly) {
  const px = await loadPixels();
  const x = Math.round(lx / S.page.width_mm * px.w);
  const y = Math.round(ly / S.page.height_mm * px.h);
  // Median of a small patch, so one noisy pixel doesn't become the ink.
  const r = 2;
  const d = px.ctx.getImageData(Math.max(0, x - r), Math.max(0, y - r), 2 * r + 1, 2 * r + 1).data;
  const ch = [[], [], []];
  for (let i = 0; i < d.length; i += 4) for (let k = 0; k < 3; k++) ch[k].push(d[i + k]);
  const med = ch.map((v) => v.sort((a, b) => a - b)[v.length >> 1]);
  const hex = '#' + med.map((v) => v.toString(16).padStart(2, '0')).join('');
  // The first pick adds to what auto found rather than replacing it.
  if (!S.inks.length && S.report && S.report.palette) S.inks = [...S.report.palette.inks];
  if (!S.inks.includes(hex)) S.inks.push(hex);
  setPicking(false);
  renderInks();
  redetect();
}

function setPicking(on) {
  S.picking = on;
  $('pickInk').classList.toggle('active', on);
  $('svg').classList.toggle('picking', on);
  $('status').textContent = on ? 'Click an ink colour on the image…' : $('status').textContent;
}

function redetect() {
  if (S.dirty && !confirm('Re-detecting discards your edits. Continue?')) return;
  detect();
}

/* ---------- geometry ---------- */

function pathLength(p) {
  let total = 0;
  for (let i = 1; i < p.points.length; i++) {
    total += Math.hypot(p.points[i][0] - p.points[i - 1][0],
                        p.points[i][1] - p.points[i - 1][1]);
  }
  if (p.closed && p.points.length > 2) {
    const a = p.points[0], b = p.points[p.points.length - 1];
    total += Math.hypot(a[0] - b[0], a[1] - b[1]);
  }
  return total;
}

function extents() {
  let lo = [Infinity, Infinity], hi = [-Infinity, -Infinity];
  for (const p of S.paths) {
    for (const q of p.points) {
      lo = [Math.min(lo[0], q[0]), Math.min(lo[1], q[1])];
      hi = [Math.max(hi[0], q[0]), Math.max(hi[1], q[1])];
    }
  }
  return Number.isFinite(lo[0]) ? { lo, hi } : null;
}

function dParam(p) {
  const pts = p.points.map(toLocal);
  if (!pts.length) return '';
  let d = `M ${pts[0][0].toFixed(3)} ${pts[0][1].toFixed(3)}`;
  for (let i = 1; i < pts.length; i++) {
    d += ` L ${pts[i][0].toFixed(3)} ${pts[i][1].toFixed(3)}`;
  }
  return d + (p.closed ? ' Z' : '');
}

/* ---------- render ---------- */

function el(name, attrs, cls) {
  const node = document.createElementNS(SVGNS, name);
  for (const k in attrs) node.setAttribute(k, attrs[k]);
  if (cls) node.setAttribute('class', cls);
  return node;
}

function render() {
  const svg = $('svg');
  svg.innerHTML = '';
  if (!S.page) return;

  const { k, tx, ty } = S.view;
  const g = el('g', { id: 'gRoot', transform: `translate(${tx},${ty}) scale(${k})` });
  svg.appendChild(g);

  // Scan underlay.
  if ($('showScan').checked) {
    g.appendChild(el('image', {
      href: `/api/scan/${S.id}`, x: 0, y: 0,
      width: S.page.width_mm, height: S.page.height_mm,
      opacity: 0.35, preserveAspectRatio: 'none',
    }));
  }

  // What the filters threw away.
  if ($('showRejects').checked) {
    for (const r of S.rejects) {
      g.appendChild(el('rect', {
        x: r.x_mm - 1, y: S.page.height_mm - r.y_mm - r.h_mm - 1,
        width: r.w_mm + 2, height: r.h_mm + 2,
        'stroke-width': 1.2 / k,
      }, 'reject'));
    }
  }

  // Geometry: a fat invisible hit line under a thin visible one.
  S.paths.forEach((p, i) => {
    if (!visible(p)) return;
    const d = dParam(p);
    const hit = el('path', { d, 'stroke-width': 8 / k }, 'geom-hit');
    hit.dataset.path = i;
    g.appendChild(hit);
    const line = el('path', { d, 'stroke-width': 1.6 / k },
                    'geom' + (S.sel.has(i) ? ' sel' : ''));
    if (p.color && !S.sel.has(i)) line.style.stroke = p.color;
    g.appendChild(line);
  });

  // Vertices of the selection only — all of them at once is unreadable.
  if ($('showVerts').checked) {
    for (const i of S.sel) {
      const p = S.paths[i];
      if (!p) continue;
      p.points.forEach((q, j) => {
        const [lx, ly] = toLocal(q);
        const isSel = S.selVert && S.selVert.path === i && S.selVert.index === j;
        const c = el('circle', {
          cx: lx, cy: ly, r: 3.2 / k, 'stroke-width': 1.2 / k,
        }, 'vert' + (isSel ? ' sel' : ''));
        c.dataset.path = i;
        c.dataset.index = j;
        g.appendChild(c);
      });
    }
  }

  renderStats();
  renderLayers();
  renderPathList();
  $('delSelected').disabled = S.sel.size === 0;
  $('undo').disabled = !S.undo.length;
  $('redo').disabled = !S.redo.length;
}

function renderStats() {
  const r = S.report;
  if (!r) return;
  const ext = extents();
  const size = ext
    ? `${(ext.hi[0] - ext.lo[0]).toFixed(2)} × ${(ext.hi[1] - ext.lo[1]).toFixed(2)} mm`
    : '—';
  const verts = S.paths.reduce((n, p) => n + p.points.length, 0);

  if (r.source === 'photo') {
    $('stats').innerHTML = `
      <span class="k">Size</span><span class="v big">${size}</span>
      <span class="k">Scale</span><span class="v">${r.dpi_source}</span>
      <span class="k">Paths</span><span class="v">${S.paths.length}</span>
      <span class="k">Circles</span><span class="v">${S.paths.filter((p) => p.kind === 'circle').length}</span>
      <span class="k">Points</span><span class="v">${verts}</span>
      <span class="k">Specks</span><span class="v">${r.dropped_small}</span>
      <span class="k">Halo slivers</span><span class="v">${r.dropped_halo}</span>
      <span class="k">Low contrast</span><span class="v">${r.dropped_contrast}</span>`;
    return;
  }
  $('stats').innerHTML = `
    <span class="k">Size</span><span class="v big">${size}</span>
    <span class="k">Scan</span><span class="v">${r.dpi} DPI</span>
    <span class="k">Paths</span><span class="v">${S.paths.length}</span>
    <span class="k">Points</span><span class="v">${verts}</span>
    <span class="k">Border artifacts</span><span class="v">${r.dropped_border}</span>
    <span class="k">Specks</span><span class="v">${r.dropped_small}</span>
    <span class="k">Too short</span><span class="v">${r.dropped_short}</span>`;
}

function renderPathList() {
  const list = $('pathList');
  list.innerHTML = '';
  $('pathCount').textContent = S.paths.length ? `(${S.paths.length})` : '';

  const rows = S.paths
    .map((p, i) => ({ i, p, len: pathLength(p), n: p.points.length, closed: p.closed }))
    .filter((row) => visible(row.p))
    .sort((a, b) => a.len - b.len);

  for (const row of rows) {
    const div = document.createElement('div');
    div.className = 'path-row' + (S.sel.has(row.i) ? ' sel' : '');
    div.innerHTML = `
      <span>#${row.i}</span>
      ${row.p.color ? `<span class="swatch" style="background:${row.p.color}"></span>` : ''}
      <span class="tag">${row.p.kind === 'circle' ? 'circle' : (row.closed ? 'closed' : 'open')}</span>
      <span class="tag">${row.n} pts</span>
      <span class="len">${row.len.toFixed(1)} mm</span>
      <span class="del" title="Delete">✕</span>`;
    div.onclick = (e) => {
      if (e.target.classList.contains('del')) {
        snapshot();
        S.paths.splice(row.i, 1);
        S.sel.clear(); S.selVert = null;
        render();
        return;
      }
      if (!e.shiftKey) S.sel.clear();
      S.sel.add(row.i);
      S.selVert = null;
      render();
    };
    list.appendChild(div);
  }
}

/* ---------- view ---------- */

function zoomFit() {
  const svg = $('svg');
  const rect = svg.getBoundingClientRect();
  const ext = extents();
  let w, h, cx, cy;
  if (ext) {
    const pad = 8;
    w = ext.hi[0] - ext.lo[0] + pad * 2;
    h = ext.hi[1] - ext.lo[1] + pad * 2;
    cx = ext.lo[0] - pad;
    cy = S.page.height_mm - ext.hi[1] - pad;
  } else {
    w = S.page.width_mm; h = S.page.height_mm; cx = 0; cy = 0;
  }
  const k = Math.min(rect.width / w, rect.height / h) || 1;
  S.view = {
    k,
    tx: -cx * k + (rect.width - w * k) / 2,
    ty: -cy * k + (rect.height - h * k) / 2,
  };
  render();
}

/* ---------- interaction ---------- */

function setupCanvas() {
  const svg = $('svg');
  let mode = null;      // 'pan' | 'vert' | 'band'
  let start = null;
  let bandEl = null;

  svg.addEventListener('wheel', (e) => {
    e.preventDefault();
    if (!S.page) return;
    const rect = svg.getBoundingClientRect();
    const mx = e.clientX - rect.left, my = e.clientY - rect.top;
    const factor = Math.exp(-e.deltaY * 0.0015);
    const k = Math.max(0.05, Math.min(400, S.view.k * factor));
    const scale = k / S.view.k;
    S.view.tx = mx - (mx - S.view.tx) * scale;
    S.view.ty = my - (my - S.view.ty) * scale;
    S.view.k = k;
    render();
  }, { passive: false });

  svg.addEventListener('pointerdown', (e) => {
    if (!S.page) return;
    if (S.picking) {
      const [lx, ly] = screenToLocal(e);
      pickAt(lx, ly).catch((err) => toast(err.message, true));
      return;
    }
    svg.setPointerCapture(e.pointerId);
    const target = e.target;

    if (target.classList.contains('vert')) {
      const pi = +target.dataset.path, vi = +target.dataset.index;
      S.selVert = { path: pi, index: vi };
      snapshot();
      S.paths[pi].kind = 'poly';   // a hand-moved point means it's no longer a circle
      mode = 'vert';
      start = { pi, vi };
      render();
      return;
    }

    if (target.classList.contains('geom-hit')) {
      const pi = +target.dataset.path;
      if (e.altKey) {
        insertPoint(pi, screenToLocal(e));
        return;
      }
      if (!e.shiftKey) S.sel.clear();
      S.sel.has(pi) ? S.sel.delete(pi) : S.sel.add(pi);
      S.selVert = null;
      render();
      return;
    }

    if (e.shiftKey) {
      mode = 'band';
      start = screenToLocal(e);
      bandEl = el('rect', { 'stroke-width': 1 / S.view.k }, 'band');
      $('gRoot').appendChild(bandEl);
      return;
    }

    mode = 'pan';
    start = { x: e.clientX, y: e.clientY, tx: S.view.tx, ty: S.view.ty };
    svg.classList.add('panning');
  });

  svg.addEventListener('pointermove', (e) => {
    if (S.page) {
      const [lx, ly] = screenToLocal(e);
      const m = toModel(lx, ly);
      $('coords').textContent = `${m[0].toFixed(2)}, ${m[1].toFixed(2)} mm`;
    }
    if (!mode) return;

    if (mode === 'pan') {
      S.view.tx = start.tx + (e.clientX - start.x);
      S.view.ty = start.ty + (e.clientY - start.y);
      render();
    } else if (mode === 'vert') {
      const [lx, ly] = screenToLocal(e);
      S.paths[start.pi].points[start.vi] = toModel(lx, ly);
      render();
    } else if (mode === 'band') {
      const [lx, ly] = screenToLocal(e);
      bandEl.setAttribute('x', Math.min(start[0], lx));
      bandEl.setAttribute('y', Math.min(start[1], ly));
      bandEl.setAttribute('width', Math.abs(lx - start[0]));
      bandEl.setAttribute('height', Math.abs(ly - start[1]));
    }
  });

  svg.addEventListener('pointerup', (e) => {
    if (mode === 'band') {
      const [lx, ly] = screenToLocal(e);
      selectInBand(start[0], start[1], lx, ly, e.ctrlKey || e.metaKey);
      bandEl = null;
    }
    mode = null;
    svg.classList.remove('panning');
    render();
  });
}

function selectInBand(x0, y0, x1, y1, additive) {
  const lo = [Math.min(x0, x1), Math.min(y0, y1)];
  const hi = [Math.max(x0, x1), Math.max(y0, y1)];
  if (!additive) S.sel.clear();
  S.paths.forEach((p, i) => {
    if (!visible(p)) return;
    // A path counts as selected when every point of it is inside the band,
    // so brushing past a big curve doesn't sweep it up with the dirt.
    const inside = p.points.every(([mx, my]) => {
      const [lx, ly] = toLocal([mx, my]);
      return lx >= lo[0] && lx <= hi[0] && ly >= lo[1] && ly <= hi[1];
    });
    if (inside) S.sel.add(i);
  });
  S.selVert = null;
}

function insertPoint(pi, [lx, ly]) {
  const p = S.paths[pi];
  const target = toModel(lx, ly);
  let best = { d: Infinity, at: 1 };

  const n = p.points.length;
  const last = p.closed ? n : n - 1;
  for (let i = 0; i < last; i++) {
    const a = p.points[i], b = p.points[(i + 1) % n];
    const vx = b[0] - a[0], vy = b[1] - a[1];
    const len2 = vx * vx + vy * vy;
    let t = len2 ? ((target[0] - a[0]) * vx + (target[1] - a[1]) * vy) / len2 : 0;
    t = Math.max(0, Math.min(1, t));
    const d = Math.hypot(a[0] + t * vx - target[0], a[1] + t * vy - target[1]);
    if (d < best.d) best = { d, at: i + 1 };
  }

  snapshot();
  p.kind = 'poly';
  p.points.splice(best.at, 0, target);
  S.sel.clear(); S.sel.add(pi);
  S.selVert = { path: pi, index: best.at };
  render();
}

function deleteSelection() {
  if (S.selVert) {
    const p = S.paths[S.selVert.path];
    if (p && p.points.length > 2) {
      snapshot();
      p.kind = 'poly';
      p.points.splice(S.selVert.index, 1);
      S.selVert = null;
      render();
    } else {
      toast('A path needs at least two points — delete the whole path instead.');
    }
    return;
  }
  if (!S.sel.size) return;
  snapshot();
  const doomed = [...S.sel].sort((a, b) => b - a);
  for (const i of doomed) S.paths.splice(i, 1);
  S.sel.clear();
  render();
}

/* ---------- export ---------- */

async function exportDxf() {
  const out = S.paths.filter(visible);
  if (!out.length) { toast('Nothing to export', true); return; }
  $('export').disabled = true;
  try {
    const res = await fetch(`/api/export/${S.id}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        paths: out,
        origin: $('expOrigin').value,
        entity: $('expEntity').value,
        scale: parseFloat($('expScale').value) || 1,
        filename: $('expName').value || 'profile',
      }),
    });
    if (!res.ok) throw new Error((await res.json()).detail || 'export failed');

    const blob = await res.blob();
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = ($('expName').value || 'profile') + '.dxf';
    a.click();
    URL.revokeObjectURL(url);
    toast('DXF downloaded');
  } catch (err) {
    toast(err.message, true);
  } finally {
    $('export').disabled = false;
  }
}

/* ---------- wiring ---------- */

function syncLabels() {
  const pairs = {
    min_length_mm: 'minLengthVal', border_margin_mm: 'borderVal',
    simplify_mm: 'simplifyVal', smooth_mm: 'smoothVal',
    flatten_mm: 'flattenVal', close_gaps_mm: 'gapsVal',
    min_area_mm2: 'areaVal', prune_spur_mm: 'spurVal',
  };
  for (const key in pairs) {
    const unit = key === 'min_area_mm2' ? ' mm²' : ' mm';
    $(pairs[key]).textContent = parseFloat($(key).value).toFixed(2) + unit;
  }
  $('modeHint').textContent = MODE_HINT[$('mode').value];
  $('colorsVal').textContent = $('colors').value;
  const photo = $('source').value === 'photo';
  $('photoOpts').hidden = !photo;
  $('scanOpts').hidden = photo;
  $('fitHint').textContent = photo
    ? 'A photo has no real scale: measure the object (e.g. a badge\'s diameter) and enter it.'
    : 'Optional — overrides the scan DPI.';
}

function init() {
  for (const key in DEFAULTS) {
    if (PHOTO_FLAGS.includes(key)) $(key).checked = DEFAULTS[key];
    else $(key).value = DEFAULTS[key];
  }
  syncLabels();

  for (const key of [...SETTINGS, ...PHOTO_SETTINGS, 'source']) {
    $(key).addEventListener('input', syncLabels);
  }
  $('pickInk').addEventListener('click', () => setPicking(!S.picking));

  const drop = $('drop');
  const handle = async (file) => {
    if (!file) return;
    try {
      await upload(file);
      await detect();
    } catch (err) { toast(err.message, true); }
  };

  ['dragenter', 'dragover'].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add('over'); }));
  ['dragleave', 'drop'].forEach((ev) =>
    drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove('over'); }));
  drop.addEventListener('drop', (e) => handle(e.dataTransfer.files[0]));
  $('file').addEventListener('change', (e) => handle(e.target.files[0]));

  $('reconvert').addEventListener('click', detect);
  $('export').addEventListener('click', exportDxf);
  $('delSelected').addEventListener('click', deleteSelection);
  $('zoomFit').addEventListener('click', zoomFit);
  $('undo').addEventListener('click', undo);
  $('redo').addEventListener('click', redo);
  $('newFile').addEventListener('click', () => {
    if (S.dirty && !confirm('Discard your edits and start a new scan?')) return;
    S.id = null; S.paths = []; S.page = null;
    $('editor').hidden = true;
    $('drop').style.display = '';
    $('file').value = '';
  });

  for (const id of ['showScan', 'showRejects', 'showVerts']) {
    $(id).addEventListener('change', render);
  }

  document.addEventListener('keydown', (e) => {
    if (/^(INPUT|SELECT|TEXTAREA)$/.test(e.target.tagName)) return;
    if (e.key === 'Delete' || e.key === 'Backspace') {
      e.preventDefault(); deleteSelection();
    } else if (e.key === 'Escape') {
      if (S.picking) setPicking(false);
      S.sel.clear(); S.selVert = null; render();
    } else if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'z') {
      e.preventDefault();
      e.shiftKey ? redo() : undo();
    }
  });

  window.addEventListener('resize', () => { if (S.page) render(); });
  setupCanvas();
}

init();
