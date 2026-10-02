'use strict';

/* API client, accounts and views. Loaded after ui.js, before app.js.
 *
 * Auth is a bearer token in the Authorization header (no cookies), kept in
 * localStorage so a reload stays logged in. Any authed call that comes back
 * 401 opens the login dialog over whatever is on screen — the editor is never
 * touched — and retries the call once after a successful login. */

const TOKEN_KEY = 'dxfconv.token';

const API = {
  token: null,
  user: null,                         // { username, is_admin }
  status: { accounts: false, storage: false, signup: false },
};

try { API.token = localStorage.getItem(TOKEN_KEY); } catch (_) { /* private mode */ }

function storeToken(token) {
  API.token = token;
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token); else localStorage.removeItem(TOKEN_KEY);
  } catch (_) { /* the session still works for this tab */ }
}

class ApiError extends Error {
  constructor(status, message) { super(message); this.status = status; }
}

async function api(path, { method = 'GET', json, body, auth = false, raw = false, retry = true } = {}) {
  const headers = {};
  let payload = body;
  if (json !== undefined) {
    headers['Content-Type'] = 'application/json';
    payload = JSON.stringify(json);
  }
  if (auth && API.token) headers.Authorization = `Bearer ${API.token}`;

  let res;
  try {
    res = await fetch(path, { method, headers, body: payload });
  } catch (_) {
    throw new ApiError(0, "Can't reach the server — check the connection and try again.");
  }

  if (res.status === 401 && auth) {
    const hadToken = !!API.token;
    setLoggedOut();
    if (retry && await requireLogin(hadToken
      ? 'Your session expired — log in again to continue.'
      : 'Log in to continue.')) {
      return api(path, { method, json, body, auth, raw, retry: false });
    }
    throw new ApiError(401, 'Not logged in.');
  }
  if (!res.ok) {
    let detail = `${res.status} ${res.statusText}`;
    try {
      const j = await res.json();
      if (typeof j.detail === 'string') detail = j.detail;
      else if (Array.isArray(j.detail)) detail = j.detail.map((d) => d.msg).join('; ');
    } catch (_) { /* not JSON */ }
    throw new ApiError(res.status, detail);
  }
  return raw ? res : res.json();
}

/* ---------- session ---------- */

function setLoggedIn(user, token) {
  if (token) storeToken(token);
  API.user = user;
  renderAccount();
  document.dispatchEvent(new CustomEvent('auth-changed'));
}

function setLoggedOut() {
  storeToken(null);
  API.user = null;
  renderAccount();
  document.dispatchEvent(new CustomEvent('auth-changed'));
}

async function initAuth() {
  try {
    API.status = await api('/api/auth/status');
  } catch (_) {
    API.status = { accounts: false, storage: false, signup: false };
  }
  if (API.status.accounts && API.token) {
    // A stale token on page load just means "logged out" — no dialog.
    try {
      const me = await api('/api/auth/me', { auth: true, retry: false });
      setLoggedIn({ username: me.username, is_admin: me.is_admin });
      return;
    } catch (_) { setLoggedOut(); return; }
  }
  renderAccount();
  document.dispatchEvent(new CustomEvent('auth-changed'));
}

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST', auth: true, retry: false }); } catch (_) { /* gone anyway */ }
  setLoggedOut();
  if (currentView === 'admin') showView('home');
  toast('Logged out');
}

/* ---------- login / sign-up dialog ----------
 * requireLogin(reason) resolves true once someone is logged in, false if the
 * dialog was dismissed. Concurrent callers share one dialog. */

let loginPending = null;

function requireLogin(reason, mode = 'login') {
  if (API.user) return Promise.resolve(true);
  if (!API.status.accounts) return Promise.resolve(false);
  if (!loginPending) {
    loginPending = authDialog(mode, reason).finally(() => { loginPending = null; });
  }
  return loginPending;
}

async function authDialog(mode = 'login', reason = '') {
  let current = mode === 'signup' && API.status.signup ? 'signup' : 'login';
  const body = document.createElement('div');

  const draw = () => {
    const signup = current === 'signup';
    body.innerHTML = `
      ${reason ? `<p class="hint">${escapeHtml(reason)}</p>` : ''}
      <label>Username
        <input name="username" autocomplete="username" autocapitalize="none" spellcheck="false" required></label>
      <label>Password
        <input name="password" type="password" autocomplete="${signup ? 'new-password' : 'current-password'}" required></label>
      ${signup ? `<label>Confirm password
        <input name="confirm" type="password" autocomplete="new-password" required></label>
        <p class="hint">Letters, digits and _ . - for the name; at least 8 characters for the password.</p>` : ''}
      ${API.status.signup ? `<p class="switch">${signup
        ? 'Have an account? <a href="#" data-to="login">Log in</a>'
        : 'No account? <a href="#" data-to="signup">Sign up</a>'}</p>` : ''}`;
    const sw = body.querySelector('[data-to]');
    if (sw) {
      sw.onclick = (e) => {
        e.preventDefault();
        const keep = body.querySelector('[name=username]').value;
        current = sw.dataset.to;
        draw();
        body.querySelector('[name=username]').value = keep;
        const title = body.closest('form')?.querySelector('h3');
        if (title) title.textContent = current === 'signup' ? 'Sign up' : 'Log in';
        const btn = body.closest('form')?.querySelector('footer button.primary');
        if (btn) btn.textContent = current === 'signup' ? 'Create account' : 'Log in';
        body.closest('form')?.setError(null);
        body.querySelector('[name=password]').focus();
      };
    }
  };
  draw();

  const result = await openDialog({
    title: current === 'signup' ? 'Sign up' : 'Log in',
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: current === 'signup' ? 'Create account' : 'Log in', kind: 'primary', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      const username = form.querySelector('[name=username]').value.trim();
      const password = form.querySelector('[name=password]').value;
      if (!username) throw new Error('Enter a username.');
      if (!password) throw new Error('Enter a password.');
      if (current === 'signup') {
        if (password !== form.querySelector('[name=confirm]').value) {
          throw new Error("The passwords don't match.");
        }
      }
      const res = await api(current === 'signup' ? '/api/auth/signup' : '/api/auth/login',
        { method: 'POST', json: { username, password } });
      setLoggedIn(res.user, res.token);
      toast(current === 'signup' ? `Welcome, ${res.user.username}` : `Logged in as ${res.user.username}`);
      return true;
    },
  });
  return result === 'ok';
}

/* ---------- account area (top bar) ---------- */

function renderAccount() {
  const el = $('account');
  if (!el) return;
  el.innerHTML = '';
  if (!API.status.accounts) return;           // accounts off: nothing to show
  if (!API.user) {
    const login = document.createElement('button');
    login.className = 'ghost small'; login.textContent = 'Log in';
    login.onclick = () => authDialog('login');
    el.appendChild(login);
    if (API.status.signup) {
      const up = document.createElement('button');
      up.className = 'primary small'; up.textContent = 'Sign up';
      up.onclick = () => authDialog('signup');
      el.appendChild(up);
    }
    return;
  }
  const btn = document.createElement('button');
  btn.className = 'ghost small user-btn';
  btn.setAttribute('aria-haspopup', 'menu');
  btn.innerHTML = `${escapeHtml(API.user.username)}${API.user.is_admin ? ' <span class="badge">admin</span>' : ''} ▾`;
  const menu = document.createElement('div');
  menu.className = 'menu'; menu.hidden = true; menu.setAttribute('role', 'menu');
  const items = [['Account', () => accountDialog()]];
  if (API.user.is_admin) {
    items.push(['Admin', async () => {
      // Same guard as the Home link: don't drop unsaved editor work silently.
      if (currentView === 'editor' && typeof leaveEditor === 'function' && !(await leaveEditor())) return;
      showView('admin');
    }]);
  }
  items.push(['Log out', logout]);
  for (const [label, fn] of items) {
    const it = document.createElement('button');
    it.setAttribute('role', 'menuitem'); it.textContent = label;
    it.onclick = () => { menu.hidden = true; fn(); };
    menu.appendChild(it);
  }
  btn.onclick = (e) => { e.stopPropagation(); menu.hidden = !menu.hidden; };
  el.append(btn, menu);
}

document.addEventListener('click', () => {
  const m = document.querySelector('#account .menu');
  if (m) m.hidden = true;
});
document.addEventListener('keydown', (e) => {
  const m = document.querySelector('#account .menu');
  if (e.key === 'Escape' && m && !m.hidden) m.hidden = true;
});

/* ---------- account (self-service) ---------- */

async function accountDialog() {
  let me;
  try { me = await api('/api/auth/me', { auth: true }); } catch (_) { return; }
  const body = document.createElement('div');
  body.innerHTML = `
    <p><b>${escapeHtml(me.username)}</b>${me.is_admin ? ' <span class="badge">admin</span>' : ''}</p>
    <p class="hint">Member since ${new Date(me.created_at).toLocaleDateString()}.</p>`;
  const choice = await openDialog({
    title: 'Account',
    body,
    actions: [
      { label: 'Delete account…', kind: 'danger', value: 'delete' },
      { label: 'Change password', kind: 'ghost', value: 'password' },
      { label: 'Close', kind: 'primary', value: null },
    ],
  });
  if (choice === 'password') return changePasswordDialog();
  if (choice === 'delete') return deleteAccountDialog(me);
}

async function changePasswordDialog() {
  const body = document.createElement('div');
  body.innerHTML = `
    <label>Current password
      <input name="current" type="password" autocomplete="current-password"></label>
    <label>New password
      <input name="new" type="password" autocomplete="new-password"></label>
    <label>Confirm new password
      <input name="confirm" type="password" autocomplete="new-password"></label>
    <p class="hint">At least 8 characters. Your other logged-in browsers will be signed out;
      this one stays logged in.</p>`;
  const done = await openDialog({
    title: 'Change password',
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Change password', kind: 'primary', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      const current = form.querySelector('[name=current]').value;
      const next = form.querySelector('[name=new]').value;
      if (!current) throw new Error('Enter your current password.');
      if (next !== form.querySelector('[name=confirm]').value) throw new Error("The new passwords don't match.");
      await api('/api/auth/password', { method: 'POST', auth: true,
        json: { current_password: current, new_password: next } });
      return true;
    },
  });
  if (done) toast('Password changed — your other sessions were signed out');
}

async function deleteAccountDialog(me) {
  const body = document.createElement('div');
  body.innerHTML = `
    <p>This deletes <b>${escapeHtml(me.username)}</b> and every scan and version you've saved.
      It can't be undone.</p>
    <label>Type your username to confirm
      <input name="confirmName" autocomplete="off" autocapitalize="none" spellcheck="false"></label>
    <label>Password
      <input name="password" type="password" autocomplete="current-password"></label>`;
  const done = await openDialog({
    title: 'Delete your account?',
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Delete my account', kind: 'danger solid', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      if (form.querySelector('[name=confirmName]').value.trim().toLowerCase() !== me.username) {
        throw new Error(`Type “${me.username}” to confirm.`);
      }
      await api('/api/auth/me', { method: 'DELETE', auth: true,
        json: { password: form.querySelector('[name=password]').value } });
      return true;
    },
  });
  if (!done) return;
  setLoggedOut();
  if (currentView !== 'home') {
    if (typeof leaveEditor === 'function' && currentView === 'editor') {
      S.dirty = false; await leaveEditor();
    }
    showView('home');
  }
  toast('Your account was deleted');
}

/* ---------- views ---------- */

let currentView = 'home';

function showView(name) {
  currentView = name;
  for (const v of ['home', 'editor', 'admin']) {
    const el = $(v);
    if (el) el.hidden = v !== name;
  }
  document.dispatchEvent(new CustomEvent('view-changed', { detail: name }));
}
