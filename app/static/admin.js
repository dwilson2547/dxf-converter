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
      <p class="hint">Sign-up is ${API.status.signup ? 'on' : 'off'} — set <code>ALLOW_SIGNUP</code>
        in the deployment to change it. New admins: make an existing user an admin here.</p>
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
      <td><b>${escapeHtml(u.username)}</b>${u.you ? ' <span class="tag">you</span>' : ''}</td>
      <td>${u.is_admin ? '<span class="badge">admin</span>' : '<span class="muted">user</span>'}</td>
      <td>${new Date(u.created_at).toLocaleDateString()}</td>
      <td class="num">${u.scans}</td>
      <td class="num">${u.versions}</td>
      <td class="actions">
        <button class="ghost small" data-act="role" ${u.you ? 'disabled title="You can\'t change your own role"' : ''}>
          ${u.is_admin ? 'Remove admin' : 'Make admin'}</button>
        <button class="ghost small" data-act="reset" ${u.you ? 'disabled title="Use Account → Change password"' : ''}>Reset password</button>
        <button class="danger small" data-act="content" ${u.scans ? '' : 'disabled title="Nothing saved"'}>Delete content</button>
        <button class="danger small" data-act="delete" ${u.you ? 'disabled title="Use Account → Delete account"' : ''}>Delete user</button>
        <div class="error-strip" hidden></div>
      </td>`;
    const err = tr.querySelector('.error-strip');
    const fail = (e) => { err.textContent = e.message; err.hidden = false; };
    const on = (act, fn) => { tr.querySelector(`[data-act=${act}]`).onclick = () => { err.hidden = true; fn().catch(fail); }; };
    on('role', () => toggleRole(u));
    on('reset', () => resetPassword(u));
    on('content', () => deleteContent(u));
    on('delete', () => deleteUser(u));
    body.appendChild(tr);
  }
}

const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

async function toggleRole(u) {
  const make = !u.is_admin;
  if (!(await confirmDialog({
    title: make ? `Make ${u.username} an admin?` : `Remove ${u.username}'s admin role?`,
    body: make ? 'Admins can see every user, reset passwords and delete accounts and content.'
      : `${escapeHtml(u.username)} keeps their account and scans but loses the admin panel.`,
    confirmLabel: make ? 'Make admin' : 'Remove admin',
  }))) return;
  const res = await api(`/api/admin/users/${u.id}`, { method: 'PATCH', auth: true, json: { is_admin: make } });
  u.is_admin = res.is_admin;
  renderAdmin();
  toast(make ? `${u.username} is now an admin` : `${u.username} is no longer an admin`);
}

async function resetPassword(u) {
  if (!(await confirmDialog({
    title: `Reset ${u.username}'s password?`,
    body: `${escapeHtml(u.username)} gets a new generated password and is signed out everywhere. `
      + "You'll see the password once, to pass on.",
    confirmLabel: 'Reset password',
  }))) return;
  const res = await api(`/api/admin/users/${u.id}/password`, { method: 'POST', auth: true });
  const body = document.createElement('div');
  body.innerHTML = `
    <p>New password for <b>${escapeHtml(res.username)}</b>. It isn't stored anywhere you can
      see it again — copy it now.</p>
    <div class="secret"><code id="newPw"></code><button class="ghost small" type="button" id="copyPw">Copy</button></div>`;
  body.querySelector('#newPw').textContent = res.password;
  body.querySelector('#copyPw').onclick = async () => {
    try {
      await navigator.clipboard.writeText(res.password);
      body.querySelector('#copyPw').textContent = 'Copied';
    } catch (_) {
      const range = document.createRange();
      range.selectNodeContents(body.querySelector('#newPw'));
      const sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range);
      body.querySelector('#copyPw').textContent = 'Selected — press Ctrl+C';
    }
  };
  await openDialog({ title: 'Password reset', body,
    actions: [{ label: 'Done', kind: 'primary', value: true }] });
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
