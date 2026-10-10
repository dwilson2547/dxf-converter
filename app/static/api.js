'use strict';

/* API client, accounts and views. Loaded after ui.js, before app.js.
 *
 * Auth is a bearer token in the Authorization header (no cookies), kept in
 * localStorage so a reload stays signed in. Signing in is through authentik,
 * in a popup (/api/auth/oidc/login): its result page puts the token in
 * localStorage under SIGNIN_KEY, which this window hears as a `storage`
 * event, so the editor is never touched. Any authed call that comes back 401
 * asks to sign in over whatever is on screen and retries the call once
 * after a successful sign-in. */

const TOKEN_KEY = 'dxfconv.token';
const SIGNIN_KEY = 'dxfconv.signin';

const API = {
  token: null,
  user: null,                         // { username, is_admin }
  status: { accounts: false, storage: false, signin: false },
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

/* A 429 comes from the ingress rate limit, not the app, so it has no JSON detail. */
function tooManyMessage(res) {
  const s = parseInt(res.headers.get('Retry-After') || '', 10);
  return `Too many requests from your connection — wait ${s > 0 ? `${s} s` : 'a moment'} and try again.`;
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
      ? 'Your session expired — sign in again to continue.'
      : 'Sign in to continue.')) {
      return api(path, { method, json, body, auth, raw, retry: false });
    }
    throw new ApiError(401, 'Not signed in.');
  }
  if (res.status === 429) throw new ApiError(429, tooManyMessage(res));
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
  const pending = takeSignin();
  if (pending && pending.token) storeToken(pending.token);
  try {
    API.status = await api('/api/auth/status');
  } catch (_) {
    API.status = { accounts: false, storage: false, signin: false };
  }
  if (API.status.accounts && API.token) {
    // A stale token on page load just means "logged out" — no dialog.
    try {
      const me = await api('/api/auth/me', { auth: true, retry: false });
      setLoggedIn({ username: me.username, is_admin: me.is_admin });
      if (pending && pending.token) toast(`Signed in as ${me.username}`);
      return;
    } catch (_) { setLoggedOut(); return; }
  }
  renderAccount();
  document.dispatchEvent(new CustomEvent('auth-changed'));
  if (pending && pending.error) toast(`Sign-in failed: ${pending.error}`);
}

async function logout() {
  try { await api('/api/auth/logout', { method: 'POST', auth: true, retry: false }); } catch (_) { /* gone anyway */ }
  setLoggedOut();
  if (currentView === 'admin') showView('home');
  toast('Signed out');
}

/* ---------- sign-in (authentik, in a popup) ----------
 * requireLogin(reason) resolves true once someone is signed in, false if the
 * dialog was dismissed. Concurrent callers share one dialog. */

/* The result page of the popup leaves {token, username} or {error} under
 * SIGNIN_KEY. Read it once and remove it, so it is never left lying there. */
function takeSignin(raw) {
  let value = raw;
  try {
    if (value === undefined) value = localStorage.getItem(SIGNIN_KEY);
    if (value) localStorage.removeItem(SIGNIN_KEY);
  } catch (_) { return null; }
  if (!value) return null;
  try {
    const r = JSON.parse(value);
    return Date.now() - (r.at || 0) < 10 * 60 * 1000 ? r : null;   // stale: ignore
  } catch (_) { return null; }
}

let signinWaiters = [];

async function finishSignin(r) {
  if (!r) return;
  const waiters = signinWaiters; signinWaiters = [];
  if (r.error) { waiters.forEach((w) => w.fail(r.error)); if (!waiters.length) toast(`Sign-in failed: ${r.error}`); return; }
  storeToken(r.token);
  try {
    const me = await api('/api/auth/me', { auth: true, retry: false });
    setLoggedIn({ username: me.username, is_admin: me.is_admin });
    toast(`Signed in as ${me.username}`);
    waiters.forEach((w) => w.ok());
  } catch (err) {
    setLoggedOut();
    waiters.forEach((w) => w.fail(err.message));
  }
}

window.addEventListener('storage', (e) => {
  if (e.key === SIGNIN_KEY && e.newValue) finishSignin(takeSignin(e.newValue));
});

/* Open the authentik popup. Must run from a click, or the browser blocks
 * it; if it is blocked anyway, fall back to signing in in this tab. */
function openSignin() {
  const w = 520, h = 680;
  const left = window.screenX + Math.max(0, (window.outerWidth - w) / 2);
  const top = window.screenY + Math.max(0, (window.outerHeight - h) / 2);
  const popup = window.open('/api/auth/oidc/login?popup=1', 'dxfconv-signin',
    `popup,width=${w},height=${h},left=${left},top=${top}`);
  if (!popup) {
    if (currentView === 'editor' && typeof S !== 'undefined' && S.dirty
      && !confirm('Your browser blocked the sign-in window. Sign in in this tab instead? Unsaved edits will be lost.')) {
      return false;
    }
    location.href = '/api/auth/oidc/login';
  }
  return true;
}

let loginPending = null;

function requireLogin(reason) {
  if (API.user) return Promise.resolve(true);
  if (!API.status.accounts || !API.status.signin) return Promise.resolve(false);
  if (!loginPending) {
    loginPending = signinDialog(reason).finally(() => { loginPending = null; });
  }
  return loginPending;
}

async function signinDialog(reason = '') {
  const body = document.createElement('div');
  body.innerHTML = `
    ${reason ? `<p class="hint">${escapeHtml(reason)}</p>` : ''}
    <p class="signin-msg">Sign in with your danwils account. A sign-in window opens; this page
      stays as it is.</p>`;
  // Not a busy submit: the user may close the popup without finishing, so
  // the dialog stays cancellable and closes itself when the sign-in lands.
  let form = null;
  const waiter = {
    ok: () => { if (dialogOpen && dialogOpen.form === form) dialogOpen.close('ok'); },
    fail: (m) => {
      signinWaiters.push(waiter);               // still open: allow another try
      if (form) form.setError(`Sign-in failed: ${m}`);
    },
  };
  signinWaiters.push(waiter);
  const shown = openDialog({
    title: 'Sign in',
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Sign in', kind: 'primary', value: 'ok', submit: true },
    ],
    onSubmit: async (f) => {
      f.setError(null);
      if (!openSignin()) return false;
      body.querySelector('.signin-msg').textContent =
        'Waiting for you to finish signing in in the other window…';
      return false;
    },
  });
  form = dialogOpen && dialogOpen.form;        // set synchronously by openDialog
  const result = await shown.finally(() => { signinWaiters = signinWaiters.filter((w) => w !== waiter); });
  return result === 'ok' && !!API.user;
}

/* ---------- account area (top bar) ---------- */

function renderAccount() {
  const el = $('account');
  if (!el) return;
  el.innerHTML = '';
  if (!API.status.accounts) return;           // accounts off: nothing to show
  if (!API.user) {
    if (!API.status.signin) return;           // no authentik configured
    const login = document.createElement('button');
    login.className = 'primary small'; login.textContent = 'Sign in';
    login.onclick = () => openSignin();
    el.appendChild(login);
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
  items.push(['Sign out', logout]);
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
    <p class="hint">Member since ${new Date(me.created_at).toLocaleDateString()}. You sign in with
      your danwils account; manage it at <a href="https://auth.danwils.com/if/user/" target="_blank" rel="noopener">auth.danwils.com</a>.</p>`;
  const choice = await openDialog({
    title: 'Account',
    body,
    actions: [
      { label: 'Delete account…', kind: 'danger', value: 'delete' },
      { label: 'Sign out everywhere', kind: 'ghost', value: 'all' },
      { label: 'Close', kind: 'primary', value: null },
    ],
  });
  if (choice === 'all') return signOutEverywhere();
  if (choice === 'delete') return deleteAccountDialog(me);
}

async function signOutEverywhere() {
  if (!(await confirmDialog({
    title: 'Sign out everywhere?',
    body: 'Every browser signed in to this account is signed out, this one included.',
    confirmLabel: 'Sign out everywhere',
  }))) return;
  try { await api('/api/auth/logout-all', { method: 'POST', auth: true, retry: false }); } catch (_) { /* gone anyway */ }
  setLoggedOut();
  if (currentView === 'admin') showView('home');
  toast('Signed out everywhere');
}

async function deleteAccountDialog(me) {
  const body = document.createElement('div');
  body.innerHTML = `
    <p>This deletes <b>${escapeHtml(me.username)}</b> here and every scan and version you've saved.
      It can't be undone. Your danwils sign-in account is not affected.</p>
    <label>Type your username to confirm
      <input name="confirmName" autocomplete="off" autocapitalize="none" spellcheck="false"></label>`;
  const done = await openDialog({
    title: 'Delete your account?',
    body,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: null },
      { label: 'Delete my account', kind: 'danger solid', value: 'ok', submit: true },
    ],
    onSubmit: async (form) => {
      const typed = form.querySelector('[name=confirmName]').value.trim().toLowerCase();
      if (typed !== me.username) throw new Error(`Type “${me.username}” to confirm.`);
      await api('/api/auth/me', { method: 'DELETE', auth: true, json: { username: typed } });
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
