'use strict';

/* Saved scans: saving, the library and versions. Loaded after app.js, so the
 * editor state (S) and its helpers are available.
 *
 * S.scan is null for anonymous work, or { id, name, current, versions } when
 * the editor holds a saved scan — current is the version number loaded or
 * last saved, which becomes the parent of the next save. */

S.scan = null;

const fileStem = (name) => (name || 'scan').replace(/\.[^.]+$/, '');

function viewState() {
  return { hidden_layers: [...S.hiddenLayers] };
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

/* ---------- wiring ---------- */

(function initScans() {
  $('saveBtn').addEventListener('click', save);
  document.addEventListener('auth-changed', setSaveState);
  // Edits mark a saved scan as having unsaved changes.
  document.addEventListener('edited', setSaveState);
  document.addEventListener('scan-changed', () => { renderVersions(); setSaveState(); });
  setSaveState();
})();
