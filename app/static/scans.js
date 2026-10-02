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

/* Filled in by workflow 6. */
async function saveVersion() {
  toast('Saving new versions comes in a later step.');
}

function renderVersions() {
  const sec = $('versionsSection');
  if (!sec) return;
  sec.hidden = !S.scan;
  if (!S.scan) return;
  $('versionList').innerHTML = S.scan.versions.map((v) => `
    <div class="version-row${v.number === S.scan.current ? ' current' : ''}">
      <span class="vnum">v${v.number}</span>
      <span class="vlabel">${escapeHtml(v.label || '')}</span>
    </div>`).join('');
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
  document.addEventListener('edited', setSaveState);
  document.addEventListener('scan-changed', () => { renderVersions(); setSaveState(); });
  setSaveState();
})();
