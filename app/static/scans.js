'use strict';

/* Saved scans: saving, the library and versions. Loaded after app.js, so the
 * editor state (S) and its helpers are available.
 *
 * S.scan is null for anonymous work, or { id, name, current, versions } when
 * the editor holds a saved scan — current is the version number loaded or
 * last saved, which becomes the parent of the next save. */

S.scan = null;

const fileStem = (name) => (name || 'scan').replace(/\.[^.]+$/, '');

/* Editor state that isn't detection settings. page and report are kept so a
 * reopened version lines up with its image without re-running detection. */
function viewState() {
  return { hidden_layers: [...S.hiddenLayers], page: S.page, report: S.report };
}

function setSaveState() {
  const btn = $('saveBtn');
  if (!btn) return;
  btn.hidden = !API.status.accounts;
  btn.disabled = API.status.accounts && !API.status.storage;
  btn.title = btn.disabled ? "Saving isn't available — this server has no image storage."
    : S.scan ? 'Save your changes as a new version' : 'Save this file to your account';
  btn.textContent = S.scan ? 'Save version' : 'Save';
  btn.classList.toggle('attention', !!S.scan && S.dirty);
}

/* ---------- save ---------- */

async function save() {
  if (!(await requireLogin('Log in or sign up to save your work. Your edits stay as they are.'))) return;
  if (S.scan) return saveVersion();        // workflow 6
  return saveNewScan();
}

async function saveNewScan() {
  const body = document.createElement('div');
  body.innerHTML = `
    <label>Name
      <input name="name" maxlength="200" required></label>
    <label>Version label <span class="muted">(optional)</span>
      <input name="label" maxlength="200" placeholder="e.g. squared lettering, star ink picked"></label>
    <label>Note <span class="muted">(optional)</span>
      <textarea name="note" rows="2" maxlength="5000"></textarea></label>`;
  body.querySelector('[name=name]').value = fileStem($('fileName').textContent);

  let saved = null;
  await openDialog({
    title: 'Save scan',
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Save', kind: 'primary', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      const name = form.querySelector('[name=name]').value.trim();
      if (!name) throw new Error('Give the scan a name.');
      try {
        saved = await api('/api/scans', {
          method: 'POST', auth: true,
          json: {
            upload_id: S.id, name,
            label: form.querySelector('[name=label]').value.trim() || null,
            note: form.querySelector('[name=note]').value.trim() || null,
            settings: readSettings(), paths: S.paths, view: viewState(),
          },
        });
      } catch (err) {
        if (err.status === 404) {
          throw new Error('The server restarted and lost the uploaded image, so this '
            + "can't be saved. Download the DXF to keep your edits, then re-upload the image.");
        }
        throw err;
      }
      return true;
    },
  });
  if (!saved) return;

  S.scan = { id: saved.scan.id, name: saved.scan.name, current: saved.version.number,
             versions: [saved.version] };
  S.dirty = false;
  $('editWarn').hidden = true;
  $('fileName').textContent = saved.scan.name;
  setSaveState();
  renderVersions();
  libraryStale = true;
  toast(`Saved “${saved.scan.name}” as v${saved.version.number}`);
}

/* ---------- versions ---------- */

function versionFields(v = {}) {
  const body = document.createElement('div');
  body.innerHTML = `
    <label>Label <span class="muted">(optional)</span>
      <input name="label" maxlength="200" placeholder="e.g. squared lettering, star ink picked"></label>
    <label>Note <span class="muted">(optional)</span>
      <textarea name="note" rows="3" maxlength="5000"></textarea></label>`;
  body.querySelector('[name=label]').value = v.label || '';
  body.querySelector('[name=note]').value = v.note || '';
  return body;
}

async function saveVersion() {
  const parent = S.scan.current;
  const body = versionFields();
  if (parent) {
    const hint = document.createElement('p');
    hint.className = 'hint';
    hint.textContent = `Saves as v${Math.max(...S.scan.versions.map((v) => v.number)) + 1}, `
      + `edited from v${parent}. Earlier versions stay as they are.`;
    body.prepend(hint);
  }
  let saved = null;
  await openDialog({
    title: `Save a new version of “${S.scan.name}”`,
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Save version', kind: 'primary', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      saved = await api(`/api/scans/${S.scan.id}/versions`, {
        method: 'POST', auth: true,
        json: {
          label: form.querySelector('[name=label]').value.trim() || null,
          note: form.querySelector('[name=note]').value.trim() || null,
          parent_number: parent || null,
          settings: readSettings(), paths: S.paths, view: viewState(),
        },
      });
      return true;
    },
  });
  if (!saved) return;
  S.scan.versions.push(saved);
  S.scan.current = saved.number;
  S.dirty = false;
  $('editWarn').hidden = true;
  libraryStale = true;
  setSaveState();
  renderVersions();
  toast(`Saved v${saved.number}`);
}

async function loadVersion(number) {
  if (number === S.scan.current && !S.dirty) return;
  if (S.dirty && !(await confirmDialog({
    title: `Load v${number}?`,
    body: 'Your unsaved changes will be lost. Save a version first if you want to keep them.',
    confirmLabel: `Load v${number}`, danger: true,
  }))) return;
  clearError('versionsErr');
  $('busy').hidden = false;
  try {
    const v = await api(`/api/scans/${S.scan.id}/versions/${number}`, { auth: true });
    loadVersionIntoEditor(v);
    if (!S.page) await recoverPage();
    S.scan.current = number;
    if (S.compare && S.compare.number === number) S.compare = null;
    render();
    setSaveState();
    renderVersions();
    $('status').textContent = `${S.scan.name} · v${number} · ${S.paths.length} paths · ${pointCount(S.paths)} points`;
    toast(`Loaded v${number}`);
  } catch (err) {
    showError('versionsErr', `Couldn't load v${number}: ${err.message}`);
  } finally {
    $('busy').hidden = true;
  }
}

async function editVersion(v) {
  let updated = null;
  await openDialog({
    title: `v${v.number}: label and note`,
    body: versionFields(v),
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Save', kind: 'primary', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      updated = await api(`/api/scans/${S.scan.id}/versions/${v.number}`, {
        method: 'PATCH', auth: true,
        json: { label: form.querySelector('[name=label]').value.trim() || null,
                note: form.querySelector('[name=note]').value.trim() || null },
      });
      return true;
    },
  });
  if (!updated) return;
  Object.assign(v, { label: updated.label, note: updated.note });
  libraryStale = true;
  renderVersions();
  toast(`Updated v${v.number}`);
}

async function deleteVersion(v) {
  const isCurrent = v.number === S.scan.current;
  const ok = await confirmDialog({
    title: `Delete v${v.number}${v.label ? ` (${v.label})` : ''}?`,
    body: `It can't be undone.${isCurrent ? ' This is the version in the editor: what you see '
      + 'stays, but it will be unsaved until you save a version.' : ''}`,
    confirmLabel: `Delete v${v.number}`, danger: true,
  });
  if (!ok) return;
  clearError('versionsErr');
  try {
    await api(`/api/scans/${S.scan.id}/versions/${v.number}`, { method: 'DELETE', auth: true });
  } catch (err) {
    showError('versionsErr', `Couldn't delete v${v.number}: ${err.message}`);
    return;
  }
  S.scan.versions = S.scan.versions.filter((x) => x.number !== v.number);
  if (S.compare && S.compare.number === v.number) { S.compare = null; render(); }
  if (isCurrent) { S.scan.current = null; S.dirty = true; }
  libraryStale = true;
  setSaveState();
  renderVersions();
  toast(`Deleted v${v.number}`);
}

/* ---------- compare ---------- */

S.compare = null;     // { number, label, paths, settings, pageH, pageW, opacity, flip }

async function startCompare(number) {
  clearError('versionsErr');
  try {
    const v = await api(`/api/scans/${S.scan.id}/versions/${number}`, { auth: true });
    const page = (v.view || {}).page || S.page;
    S.compare = { number, label: v.label, paths: v.paths, settings: v.settings || {},
                  pageH: page.height_mm, pageW: page.width_mm,
                  opacity: S.compare ? S.compare.opacity : 0.7, flip: false };
  } catch (err) {
    showError('versionsErr', `Couldn't load v${number} to compare: ${err.message}`);
    return;
  }
  render();
  renderVersions();
}

function stopCompare() {
  S.compare = null;
  render();
  renderVersions();
}

function flipCompare() {
  if (!S.compare) return;
  S.compare.flip = !S.compare.flip;
  render();
}

function geomStats(paths) {
  let lo = [Infinity, Infinity], hi = [-Infinity, -Infinity];
  for (const p of paths) for (const [x, y] of p.points) {
    lo = [Math.min(lo[0], x), Math.min(lo[1], y)];
    hi = [Math.max(hi[0], x), Math.max(hi[1], y)];
  }
  const ok = Number.isFinite(lo[0]);
  return { paths: paths.length, points: pointCount(paths),
           circles: paths.filter((p) => p.kind === 'circle').length,
           w: ok ? hi[0] - lo[0] : 0, h: ok ? hi[1] - lo[1] : 0 };
}

/* Two versions line up only if they were scaled the same way: same
 * Largest-dimension setting (or both from DPI) and the same page size. */
function scaleMismatch() {
  const a = readSettings(), b = S.compare.settings;
  const fitA = a.fit_mm ? Number(a.fit_mm) : null, fitB = b.fit_mm ? Number(b.fit_mm) : null;
  const pageOff = Math.abs(S.compare.pageW - S.page.width_mm) / S.page.width_mm > 0.005;
  if (!pageOff && (fitA === fitB || (fitA && fitB && Math.abs(fitA - fitB) < 0.01))) return null;
  const desc = (fit) => (fit ? `scaled to ${fit.toFixed(2)} mm` : 'scaled from the DPI');
  const here = S.scan.current ? `v${S.scan.current}` : 'The open geometry';
  return `${here} is ${desc(fitA)}; v${S.compare.number} is ${desc(fitB)}. `
    + "The overlay won't line up — compare shapes, not positions.";
}

/* The compare box is built once (renderCompare) and its numbers refreshed on
 * every redraw (updateCompareStats), so the table follows edits, undo and
 * rescales — and rebuilding it never interrupts a drag on the slider. */
function renderCompare() {
  const box = $('compareBox');
  if (!box) return;
  box.hidden = !S.compare;
  if (!S.compare) { box.innerHTML = ''; return; }
  box.innerHTML = `
    <div class="compare-head" id="cmpHead"></div>
    <div class="warn-chip" id="cmpWarn" hidden></div>
    <label>Ghost opacity <span class="val" id="cmpOpacityVal"></span>
      <input type="range" id="cmpOpacity" min="0.1" max="1" step="0.05" value="${S.compare.opacity}"></label>
    <table class="compare-table">
      <thead><tr><th></th><th id="cmpColA"></th><th>v${S.compare.number}</th><th>Δ</th></tr></thead>
      <tbody id="cmpBody"></tbody>
    </table>
    <div class="vactions">
      <button class="ghost small" id="cmpFlip" title="B"></button>
      <button class="ghost small" id="cmpStop">Stop comparing</button>
    </div>`;
  $('cmpOpacity').oninput = (e) => {
    S.compare.opacity = parseFloat(e.target.value);
    render();
  };
  $('cmpFlip').onclick = flipCompare;
  $('cmpStop').onclick = stopCompare;
  updateCompareStats();
}

function updateCompareStats() {
  if (!S.compare || !$('cmpBody')) return;
  const a = geomStats(S.paths), b = geomStats(S.compare.paths);
  const here = S.scan && S.scan.current ? `v${S.scan.current}${S.dirty ? ' (edited)' : ''}` : 'open (unsaved)';
  const delta = (x, y, digits = 0) => {
    const d = x - y;
    if (Math.abs(d) < (digits ? 0.005 : 0.5)) return '<span class="muted">same</span>';
    return `${d > 0 ? '+' : '−'}${Math.abs(d).toFixed(digits)}`;
  };
  const row = (label, x, y, digits = 0, unit = '') => `
    <tr><th>${label}</th><td>${x.toFixed(digits)}${unit}</td><td>${y.toFixed(digits)}${unit}</td>
    <td>${delta(x, y, digits)}</td></tr>`;
  $('cmpHead').innerHTML = `Comparing <b>${escapeHtml(here)}</b> with
    <span class="ghost-key"></span><b>v${S.compare.number}</b>${S.compare.label
      ? ` <span class="muted">${escapeHtml(S.compare.label)}</span>` : ''}`;
  $('cmpColA').textContent = here;
  const warn = scaleMismatch();
  $('cmpWarn').hidden = !warn;
  $('cmpWarn').textContent = warn || '';
  $('cmpOpacityVal').textContent = `${Math.round(S.compare.opacity * 100)}%`;
  $('cmpFlip').textContent = `${S.compare.flip ? 'Show current in front'
    : `Show v${S.compare.number} in front`} (B)`;
  $('cmpBody').innerHTML = row('Paths', a.paths, b.paths) + row('Points', a.points, b.points)
    + row('Circles', a.circles, b.circles) + row('Width', a.w, b.w, 2, ' mm')
    + row('Height', a.h, b.h, 2, ' mm');
}

function renderVersions() {
  const sec = $('versionsSection');
  if (!sec) return;
  sec.hidden = !S.scan;
  if (!S.scan) return;
  const only = S.scan.versions.length === 1;
  const list = $('versionList');
  list.innerHTML = '';
  const rows = [...S.scan.versions].sort((a, b) => b.number - a.number);
  for (const v of rows) {
    const st = v.stats || {};
    const row = document.createElement('div');
    row.className = 'version-row' + (v.number === S.scan.current ? ' current' : '');
    row.innerHTML = `
      <div class="vline">
        <span class="vnum">v${v.number}</span>
        <span class="vlabel" title="${escapeHtml(v.label || '')}">${escapeHtml(v.label || '—')}</span>
        ${v.number === S.scan.current ? `<span class="tag">${S.dirty ? 'edited' : 'open'}</span>` : ''}
      </div>
      <div class="vmeta">${st.paths ?? '?'} paths · ${st.circles ?? 0} circles · ${ago(v.created_at)}${
        v.parent_number ? ` · from v${v.parent_number}` : ''}</div>
      ${v.note ? `<div class="vnote" title="${escapeHtml(v.note)}">${escapeHtml(v.note)}</div>` : ''}
      <div class="vactions">
        ${v.number === S.scan.current && !S.dirty ? '' : '<button class="ghost small" data-act="load">Load</button>'}
        ${v.number === S.scan.current ? '' : S.compare && S.compare.number === v.number
          ? '<button class="ghost small active" data-act="uncompare">Comparing</button>'
          : '<button class="ghost small" data-act="compare">Compare</button>'}
        <button class="ghost small" data-act="edit">Label</button>
        <button class="danger small" data-act="delete" ${only ? 'disabled title="The only version — delete the scan from Home instead"' : ''}>Delete</button>
      </div>`;
    const on = (act, fn) => { const b = row.querySelector(`[data-act=${act}]`); if (b) b.onclick = fn; };
    on('load', () => loadVersion(v.number));
    on('compare', () => startCompare(v.number));
    on('uncompare', stopCompare);
    on('edit', () => editVersion(v));
    on('delete', () => deleteVersion(v));
    list.appendChild(row);
  }
  $('compareHint').hidden = !only;
  renderCompare();
}

/* ---------- library (Home) ---------- */

let libraryScans = [];
let libraryStale = true;
const thumbs = new Map();             // scan id -> object URL of its thumbnail

const SORTS = {
  updated: (a, b) => b.updated_at.localeCompare(a.updated_at),
  name: (a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }),
  created: (a, b) => b.created_at.localeCompare(a.created_at),
};

function ago(iso) {
  const s = (Date.now() - new Date(iso).getTime()) / 1000;
  if (s < 60) return 'just now';
  if (s < 3600) return `${Math.floor(s / 60)} min ago`;
  if (s < 86400) return `${Math.floor(s / 3600)} h ago`;
  if (s < 86400 * 30) return `${Math.floor(s / 86400)} d ago`;
  return new Date(iso).toLocaleDateString();
}

function libraryVisible() {
  return !!(API.status.accounts && API.status.storage && API.user);
}

/* quiet: refresh in place (cards already showing) instead of skeletons. */
async function loadLibrary({ quiet = false } = {}) {
  const sec = $('library');
  sec.hidden = !libraryVisible();
  if (sec.hidden) {
    for (const url of thumbs.values()) URL.revokeObjectURL(url);
    thumbs.clear();
    libraryScans = [];
    return;
  }
  clearError('libraryErr');
  if (!quiet || !libraryScans.length) {
    $('libraryGrid').innerHTML = '<div class="card skeleton"></div>'.repeat(3);
    $('libraryEmpty').hidden = true;
  }
  try {
    libraryScans = (await api('/api/scans', { auth: true })).scans;
    libraryStale = false;
    renderLibrary();
  } catch (err) {
    $('libraryGrid').innerHTML = '';
    showError('libraryErr', `Couldn't load your scans: ${err.message}`);
    const retry = document.createElement('button');
    retry.className = 'ghost small'; retry.textContent = 'Retry';
    retry.onclick = loadLibrary;
    $('libraryErr').appendChild(retry);
  }
}

function renderLibrary() {
  const q = $('libSearch').value.trim().toLowerCase();
  const sort = SORTS[$('libSort').value] || SORTS.updated;
  const rows = libraryScans.filter((sc) => !q || sc.name.toLowerCase().includes(q)).sort(sort);
  $('libCount').textContent = libraryScans.length ? `(${libraryScans.length})` : '';
  const grid = $('libraryGrid');
  grid.innerHTML = '';
  const empty = $('libraryEmpty');
  empty.hidden = rows.length > 0;
  empty.textContent = libraryScans.length
    ? `No scans match “${$('libSearch').value.trim()}”.`
    : 'No saved scans yet — drop a file above, then press Save in the editor.';

  for (const sc of rows) {
    const card = document.createElement('div');
    card.className = 'card';
    const latest = sc.latest || {};
    card.innerHTML = `
      <button class="thumb" title="Open">${thumbs.has(sc.id)
        ? `<img alt="" src="${thumbs.get(sc.id)}">` : '<span class="ph"></span>'}</button>
      <div class="card-body">
        <div class="card-name" title="${escapeHtml(sc.name)}">${escapeHtml(sc.name)}</div>
        <div class="card-meta">${sc.versions} version${sc.versions === 1 ? '' : 's'}${
          latest.label ? ` · v${latest.number}: ${escapeHtml(latest.label)}` : ''}</div>
        <div class="card-meta">updated ${ago(sc.updated_at)}</div>
        <div class="error-strip" hidden></div>
        <div class="card-actions">
          <button class="primary small" data-act="open">Open</button>
          <button class="ghost small" data-act="rename">Rename</button>
          <button class="danger small" data-act="delete">Delete</button>
        </div>
      </div>`;
    card.querySelector('.thumb').onclick = () => openScan(sc.id);
    card.querySelector('[data-act=open]').onclick = () => openScan(sc.id);
    card.querySelector('[data-act=rename]').onclick = () => renameInline(card, sc);
    card.querySelector('[data-act=delete]').onclick = () => deleteScan(sc);
    grid.appendChild(card);
    if (!thumbs.has(sc.id) && sc.thumb_url) loadThumb(sc, card);
  }
}

async function loadThumb(sc, card) {
  try {
    const res = await api(sc.thumb_url, { auth: true, raw: true, retry: false });
    const url = URL.createObjectURL(await res.blob());
    thumbs.set(sc.id, url);
    const slot = card.querySelector('.thumb');
    if (slot) slot.innerHTML = `<img alt="" src="${url}">`;
  } catch (_) { /* placeholder stays */ }
}

function renameInline(card, sc) {
  const nameEl = card.querySelector('.card-name');
  const errEl = card.querySelector('.error-strip');
  const input = document.createElement('input');
  input.className = 'rename'; input.value = sc.name; input.maxLength = 200;
  nameEl.replaceWith(input);
  input.focus(); input.select();
  let done = false;
  const finish = async (commit) => {
    if (done) return;
    const name = input.value.trim();
    if (commit && name && name !== sc.name) {
      try {
        await api(`/api/scans/${sc.id}`, { method: 'PATCH', auth: true, json: { name } });
      } catch (err) {
        errEl.textContent = err.message; errEl.hidden = false;
        input.focus();
        return;
      }
      sc.name = name;
      sc.updated_at = new Date().toISOString();
      if (S.scan && S.scan.id === sc.id) { S.scan.name = name; $('fileName').textContent = name; }
      toast('Renamed');
    } else if (commit && !name) {
      errEl.textContent = 'A scan needs a name.'; errEl.hidden = false;
      input.focus();
      return;
    }
    done = true;
    renderLibrary();
  };
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); finish(true); }
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); finish(false); }
  });
  input.addEventListener('blur', () => finish(true));
}

async function deleteScan(sc) {
  const ok = await confirmDialog({
    title: `Delete “${sc.name}”?`,   // titles are set as text, not HTML
    body: `This deletes the image and ${sc.versions === 1 ? 'its only version'
      : `all ${sc.versions} versions`}. It can't be undone.`,
    confirmLabel: 'Delete scan', danger: true,
  });
  if (!ok) return;
  try {
    await api(`/api/scans/${sc.id}`, { method: 'DELETE', auth: true });
  } catch (err) {
    showError('libraryErr', `Couldn't delete “${sc.name}”: ${err.message}`);
    return;
  }
  libraryScans = libraryScans.filter((x) => x.id !== sc.id);
  if (thumbs.has(sc.id)) { URL.revokeObjectURL(thumbs.get(sc.id)); thumbs.delete(sc.id); }
  renderLibrary();
  toast(`Deleted “${sc.name}”`);
}

/* ---------- opening a saved scan ---------- */

async function openScan(scanId, number = null) {
  clearError('libraryErr');
  showView('editor');
  $('busy').hidden = false;
  $('status').textContent = 'Opening…';
  try {
    const detail = await api(`/api/scans/${scanId}`, { auth: true });
    const n = number || Math.max(...detail.version_list.map((v) => v.number));
    const [version, imgRes] = await Promise.all([
      api(`/api/scans/${scanId}/versions/${n}`, { auth: true }),
      api(`/api/scans/${scanId}/image`, { auth: true, raw: true }),
    ]);
    if (S.imageUrl && S.imageUrl.startsWith('blob:')) URL.revokeObjectURL(S.imageUrl);
    S.imageUrl = URL.createObjectURL(await imgRes.blob());
    S.pixels = null;
    S.id = null;
    S.scan = { id: detail.id, name: detail.name, current: n, versions: detail.version_list };
    S.compare = null;
    loadVersionIntoEditor(version);
    $('fileName').textContent = detail.name;
    $('expName').value = detail.name.replace(/[^A-Za-z0-9_-]+/g, '-').replace(/^-+|-+$/g, '') || 'scan';
    if (!S.page) await recoverPage();
    render();
    zoomFit();
    setSaveState();
    renderVersions();
    $('status').textContent = `${detail.name} · v${n} · ${S.paths.length} paths · ${pointCount(S.paths)} points`;
  } catch (err) {
    showView('home');
    showError('libraryErr', `Couldn't open that scan: ${err.message}`);
  } finally {
    $('busy').hidden = true;
  }
}

function loadVersionIntoEditor(version) {
  const view = version.view || {};
  applySettings(version.settings || {});
  S.paths = version.paths.map((p) => ({ ...p }));
  S.page = view.page || null;
  S.report = view.report || S.report || { source: (version.settings || {}).source || 'scan' };
  S.rejects = [];
  S.hiddenLayers = new Set(view.hidden_layers || []);
  S.sel.clear(); S.selVert = null;
  S.undo.length = 0; S.redo.length = 0;
  S.dirty = false;
  $('editWarn').hidden = true;
  clearError('detectErr'); clearError('exportErr');
}

/* Scans saved before the page size was stored: run detection once on the
 * stored image just to learn the page size; its paths are thrown away. */
async function recoverPage() {
  const data = await api(`/api/scans/${S.scan.id}/convert`,
    { method: 'POST', auth: true, json: { settings: readSettings() } });
  S.page = data.page;
  S.report = data.report;
}

/* ---------- wiring ---------- */

(function initScans() {
  $('saveBtn').addEventListener('click', save);
  document.addEventListener('auth-changed', () => { setSaveState(); loadLibrary(); });
  // Always refresh on Home: scans may have been saved in another tab or device.
  document.addEventListener('view-changed', (e) => {
    if (e.detail === 'home') loadLibrary({ quiet: !libraryStale });
  });
  $('libSearch').addEventListener('input', renderLibrary);
  $('libSort').addEventListener('change', () => {
    try { localStorage.setItem('dxfconv.libSort', $('libSort').value); } catch (_) { /* ok */ }
    renderLibrary();
  });
  try { $('libSort').value = localStorage.getItem('dxfconv.libSort') || 'updated'; } catch (_) { /* ok */ }
  // Edits mark a saved scan as having unsaved changes.
  document.addEventListener('edited', () => { setSaveState(); renderVersions(); });
  document.addEventListener('scan-changed', () => { renderVersions(); setSaveState(); });
  setSaveState();
})();
