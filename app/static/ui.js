'use strict';

/* Shared UI pieces: one dialog component used the same way everywhere, and
 * inline error strips. Loaded before app.js; everything here is global. */

const $ = (id) => document.getElementById(id);

function escapeHtml(s) {
  return String(s ?? '').replace(/[&<>"']/g, (c) => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/* ---------- inline errors ----------
 * An error strip sits under the control whose action failed. It stays until
 * the next success there (or the user dismisses it), unlike a toast. */

function showError(stripId, msg) {
  const el = $(stripId);
  if (!el) return;
  el.innerHTML = `<span>${escapeHtml(msg)}</span><button class="x" title="Dismiss">✕</button>`;
  el.hidden = false;
  el.querySelector('.x').onclick = () => clearError(stripId);
}

function clearError(stripId) {
  const el = $(stripId);
  if (el) { el.hidden = true; el.innerHTML = ''; }
}

/* ---------- dialog ----------
 * openDialog({ title, body (HTML string or Node), actions: [{label, kind,
 * value, submit}] , onSubmit(form) }) -> Promise resolving to the clicked
 * action's value, or null when dismissed (Escape, backdrop, ✕).
 *
 * onSubmit may return false or throw to keep the dialog open (its error goes
 * into the dialog's own error strip). Focus moves into the dialog and back to
 * whatever had it when the dialog closes. */

let dialogOpen = null;

function openDialog({ title, body, actions = [], onSubmit, wide = false }) {
  if (dialogOpen) dialogOpen.close(null);
  const returnFocus = document.activeElement;

  const back = document.createElement('div');
  back.className = 'modal-back';
  back.innerHTML = `
    <form class="modal${wide ? ' wide' : ''}" role="dialog" aria-modal="true" novalidate>
      <header><h3></h3><button type="button" class="x" title="Close (Esc)">✕</button></header>
      <div class="modal-body"></div>
      <div class="error-strip" hidden></div>
      <footer></footer>
    </form>`;
  const form = back.querySelector('form');
  form.querySelector('h3').textContent = title;
  const bodyEl = form.querySelector('.modal-body');
  if (typeof body === 'string') bodyEl.innerHTML = body; else if (body) bodyEl.appendChild(body);
  const errEl = form.querySelector('.error-strip');
  const footer = form.querySelector('footer');

  return new Promise((resolve) => {
    let busy = false;
    const close = (value) => {
      document.removeEventListener('keydown', onKey, true);
      back.remove();
      dialogOpen = null;
      if (returnFocus && returnFocus.focus) returnFocus.focus();
      resolve(value);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); if (!busy) close(null); }
    };
    dialogOpen = { close, form };

    const setError = (msg) => {
      if (msg) { errEl.textContent = msg; errEl.hidden = false; } else { errEl.hidden = true; }
    };
    form.setError = setError;

    const run = async (action) => {
      if (busy) return;
      if (action.submit && onSubmit) {
        busy = true;
        footer.querySelectorAll('button').forEach((b) => { b.disabled = true; });
        setError(null);
        try {
          const ok = await onSubmit(form, action.value);
          if (ok === false) return;
          close(action.value);
        } catch (err) {
          setError(err.message || String(err));
        } finally {
          busy = false;
          footer.querySelectorAll('button').forEach((b) => { b.disabled = false; });
        }
      } else {
        close(action.value);
      }
    };

    for (const a of actions) {
      const b = document.createElement('button');
      b.type = a.submit ? 'submit' : 'button';
      b.textContent = a.label;
      b.className = a.kind || 'ghost';
      b.onclick = (e) => { e.preventDefault(); run(a); };
      footer.appendChild(b);
    }
    // Enter in any field triggers the first submit action.
    form.addEventListener('submit', (e) => {
      e.preventDefault();
      const first = actions.find((a) => a.submit);
      if (first) run(first);
    });
    form.querySelector('.x').onclick = () => { if (!busy) close(null); };
    back.addEventListener('mousedown', (e) => { if (e.target === back && !busy) close(null); });
    document.addEventListener('keydown', onKey, true);

    document.body.appendChild(back);
    const firstInput = form.querySelector('input, textarea, select');
    (firstInput || footer.querySelector('button.primary, button.danger') || form).focus();
  });
}

/* A yes/no question. Resolves true only on the confirm button. */
async function confirmDialog({ title, body, confirmLabel = 'Continue', danger = false }) {
  const v = await openDialog({
    title, body: `<p>${body}</p>`,
    actions: [
      { label: 'Cancel', kind: 'ghost', value: false },
      { label: confirmLabel, kind: danger ? 'danger solid' : 'primary', value: true },
    ],
  });
  return v === true;
}
