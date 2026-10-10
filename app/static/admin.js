'use strict';

/* Admin panel: users with their saved content, and the actions an admin
 * needs. Loaded last; reached only from the account menu. */

let adminUsers = [];

function adminShell() {
  const root = $('admin');
  if (root.dataset.built) return;
  root.dataset.built = '1';
  root.innerHTML = `
    <div class="admin-wrap">
      <div class="library-head">
        <h2>Users <span class="muted" id="adminCount"></span></h2>
        <span class="spacer"></span>
        <input type="search" id="adminSearch" placeholder="Filter by name" aria-label="Filter users">
      </div>
      <div class="error-strip" id="adminErr" hidden></div>
      <table class="admin-table">
        <thead><tr><th>User</th><th>Role</th><th>Joined</th><th class="num">Scans</th>
          <th class="num">Versions</th><th>Actions</th></tr></thead>
        <tbody id="adminBody"></tbody>
      </table>
      <p class="empty" id="adminEmpty" hidden></p>
      <p class="hint">People sign in with their danwils account (authentik); an account appears
        here at its first sign-in. Admins are the members of the <code>dxf-converter-admins</code>
        group in authentik, read at each sign-in — after changing someone's groups, use
        <b>Sign out</b> so it applies now rather than when their session lapses.</p>
    </div>`;
  $('adminSearch').addEventListener('input', renderAdmin);
}

async function loadAdmin() {
  adminShell();
  clearError('adminErr');
  $('adminBody').innerHTML = '<tr><td colspan="6" class="loading-row"><span class="spinner"></span> Loading users…</td></tr>';
  $('adminEmpty').hidden = true;
  try {
    adminUsers = (await api('/api/admin/users', { auth: true })).users;
  } catch (err) {
    $('adminBody').innerHTML = '';
    showError('adminErr', err.status === 403 ? 'Only admins can see this page.'
      : `Couldn't load users: ${err.message}`);
    const retry = document.createElement('button');
    retry.className = 'ghost small'; retry.textContent = 'Retry'; retry.onclick = loadAdmin;
    $('adminErr').appendChild(retry);
    return;
  }
  renderAdmin();
}

function renderAdmin() {
  const q = $('adminSearch').value.trim().toLowerCase();
  const rows = adminUsers.filter((u) => !q || u.username.includes(q));
  $('adminCount').textContent = `(${adminUsers.length})`;
  $('adminEmpty').hidden = rows.length > 0;
  $('adminEmpty').textContent = `No users match “${$('adminSearch').value.trim()}”.`;
  const body = $('adminBody');
  body.innerHTML = '';
  for (const u of rows) {
    const tr = document.createElement('tr');
    tr.innerHTML = `
      <td><b>${escapeHtml(u.username)}</b>${u.you ? ' <span class="tag">you</span>' : ''}${u.unclaimed
        ? ' <span class="tag" title="From before sign-in moved to authentik; claimed by the authentik user with this username at their first sign-in">not yet claimed</span>' : ''}</td>
      <td>${u.is_admin ? '<span class="badge">admin</span>' : '<span class="muted">user</span>'}</td>
      <td>${new Date(u.created_at).toLocaleDateString()}</td>
      <td class="num">${u.scans}</td>
      <td class="num">${u.versions}</td>
      <td class="actions">
        <button class="ghost small" data-act="signout" ${u.you ? 'disabled title="Use Account → Sign out everywhere"' : ''}>Sign out</button>
        <button class="danger small" data-act="content" ${u.scans ? '' : 'disabled title="Nothing saved"'}>Delete content</button>
        <button class="danger small" data-act="delete" ${u.you ? 'disabled title="Use Account → Delete account"' : ''}>Delete user</button>
        <div class="error-strip" hidden></div>
      </td>`;
    const err = tr.querySelector('.error-strip');
    const fail = (e) => { err.textContent = e.message; err.hidden = false; };
    const on = (act, fn) => { tr.querySelector(`[data-act=${act}]`).onclick = () => { err.hidden = true; fn().catch(fail); }; };
    on('signout', () => signOutUser(u));
    on('content', () => deleteContent(u));
    on('delete', () => deleteUser(u));
    body.appendChild(tr);
  }
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

async function signOutUser(u) {
  if (!(await confirmDialog({
    title: `Sign ${u.username} out everywhere?`,
    body: `Every session ${escapeHtml(u.username)} has ends now. Signing in again goes through `
      + 'authentik, which also applies any change to their groups.',
    confirmLabel: 'Sign out',
  }))) return;
  const res = await api(`/api/admin/users/${u.id}/logout`, { method: 'POST', auth: true });
  toast(`Signed ${res.username} out (${plural(res.sessions, 'session')})`);
}

async function deleteContent(u) {
  if (!(await confirmDialog({
    title: `Delete ${u.username}'s scans?`,
    body: `This deletes ${plural(u.scans, 'scan')} and ${plural(u.versions, 'version')}, with their images. `
      + `${escapeHtml(u.username)} keeps the account. It can't be undone.`,
    confirmLabel: 'Delete scans', danger: true,
  }))) return;
  await api(`/api/admin/users/${u.id}/content`, { method: 'DELETE', auth: true });
  u.scans = 0; u.versions = 0;
  renderAdmin();
  toast(`Deleted ${u.username}'s scans`);
}

async function deleteUser(u) {
  if (!(await confirmDialog({
    title: `Delete ${u.username}?`,
    body: `This deletes the account and ${plural(u.scans, 'scan')} (${plural(u.versions, 'version')}). `
      + 'They are signed out immediately. It can\'t be undone.',
    confirmLabel: 'Delete user', danger: true,
  }))) return;
  await api(`/api/admin/users/${u.id}`, { method: 'DELETE', auth: true });
  adminUsers = adminUsers.filter((x) => x.id !== u.id);
  renderAdmin();
  toast(`Deleted ${u.username}`);
}

document.addEventListener('view-changed', (e) => { if (e.detail === 'admin') loadAdmin(); });
// Logging out (or losing admin) while on the panel leaves it.
document.addEventListener('auth-changed', () => {
  if (currentView === 'admin' && !(API.user && API.user.is_admin)) showView('home');
});
