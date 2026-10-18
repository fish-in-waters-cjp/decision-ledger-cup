// Judge sandboxes and public mode (DL-018).
// A sandbox lives at /s/<id>/; the id is the project id. In public mode the showcase is read-only.
import { api } from './api.js';
import { RT } from './runtime.js';
import { esc } from './components.js';
import { t, problemTitle, errorText } from './i18n.js';
import { signInMode, renderAccount } from './account.js';

export const SB = { id: null, project: null, expired: false, busy: false, limit: false };
// Dictionary keys (the backend's problem titles for the same cases), translated when rendered (DL-031).
const EXPIRED = 'problem.sandbox-expired';
const LIMIT = 'problem.sandbox-limit';

export function sandboxIdFromPath() {
  const m = location.pathname.match(/^\/s\/([A-Za-z0-9_-]{16})(?:\/|$)/);
  return m ? m[1] : null;
}

// Resolve the sandbox in the URL; false when it is gone (unknown or expired with its container).
export async function loadSandbox() {
  try {
    const sb = await api.sandbox(SB.id);
    SB.project = { id: sb.id, name: sb.name, repo: null, pending: 0 };
    return true;
  } catch {
    SB.expired = true;
    return false;
  }
}

// Read-only in public mode and whenever Google sign-in is on (DL-032): nobody writes to the showcase.
export const readOnlyShowcase = pid => Boolean((RT.public_mode || signInMode()) && pid && pid === RT.showcase_project);

export function renderSandboxBar(S) {
  const bar = document.getElementById('sandbox-bar');
  const switcher = document.getElementById('switcher');
  if (switcher) switcher.hidden = Boolean(RT.public_mode || SB.id || signInMode());
  if (!bar) return;
  if (signInMode()) { renderAccount(bar); return; }  // DL-032: "Sign in with Google" replaces "Start my sandbox"
  const dis = SB.busy ? 'disabled' : '';
  if (SB.id && !SB.expired) {
    bar.innerHTML = `<span class="sb-id" title="${esc(t('sandbox.id_hint'))}">${esc(t('sandbox.label'))} <code>${esc(SB.id)}</code></span>
      <button class="btn small" type="button" data-reset-sandbox ${dis}>${esc(t('sandbox.reset'))}</button>
      <a class="linkbtn" href="/">${esc(t('sandbox.showcase'))}</a>`;
  } else if (RT.public_mode) {
    bar.innerHTML = `${readOnlyShowcase(S.pid) ? `<span class="sb-note">${esc(t('sandbox.read_only'))}</span>` : ''}
      <button class="btn primary small" type="button" data-start-sandbox ${dis || (SB.limit ? 'disabled' : '')}>${esc(t(SB.limit ? LIMIT : SB.busy ? 'sandbox.starting' : 'sandbox.start'))}</button>`;
  } else {
    bar.innerHTML = '';
  }
}

export function renderExpired() {
  document.getElementById('sandbox-bar').innerHTML = '';
  document.getElementById('switcher').hidden = true;
  document.getElementById('importer').hidden = true;
  document.getElementById('backend').hidden = true;
  document.getElementById('view').innerHTML = `<div class="banner" role="alert"><span>${esc(t('sandbox.sentence', { text: t(EXPIRED) }))}</span>
    <button class="btn primary small" type="button" data-start-sandbox>${esc(t('sandbox.start'))}</button></div>`;
}

async function startSandbox() {
  SB.busy = true;
  try {
    const sb = await api.createSandbox();
    location.assign(sb.url);
  } catch (e) {
    SB.busy = false;
    if (e.status === 429) {
      SB.limit = true;  // the button itself says why
      document.querySelectorAll('[data-start-sandbox]').forEach(b => { b.textContent = t(LIMIT); b.disabled = true; });
      return;
    }
    alert(t('sandbox.start_failed', { error: errorText(e) }));
  }
}

async function resetSandbox() {
  if (!confirm(t('sandbox.reset_confirm'))) return;
  SB.busy = true;
  try {
    await api.resetSandbox(SB.id);
    location.reload();
  } catch (e) {
    SB.busy = false;
    alert(t('sandbox.reset_failed', { error: errorText(e) }));
  }
}

export const sandboxActions = {
  click(btn) {
    if (btn.hasAttribute('data-start-sandbox')) { startSandbox(); return true; }
    if (btn.hasAttribute('data-reset-sandbox')) { resetSandbox(); return true; }
    return false;
  },
};
