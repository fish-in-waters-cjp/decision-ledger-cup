// Google sign-in UI (DL-032): AccountArea, AccountMenu, ShowcaseNotice and WorkspaceSettings.
// Signed out, the read-only showcase is shown; signing in opens the user's own workspace (GET /api/me).
// app.js passes in the hooks it owns (project switching, refresh, render) so this module never imports it.
import { api, setTokenSource } from './api.js';
import { RT } from './runtime.js';
import { esc } from './components.js';
import { t, errorText } from './i18n.js';
import { AUTH, initAuth, onAuthChange, signInAvailable, signIn, signOut, idToken } from './auth.js';

export const ACC = {
  workspace: null,      // the signed-in user's project, from /api/me
  menu: false,
  signing: false,
  error: null,          // { key, error? }: shown under the sign-in button
  resetting: false,
  settings: { open: false, repo: '', saving: false, saved: false, error: null },
};

const hooks = { switchTo: async () => {}, refresh: async () => {}, render: () => {}, busy: () => false, pid: () => null };
let lastUid;

export const signInMode = () => signInAvailable();

// Start sign-in: read /api/runtime, wire the bearer token into every API call, then follow auth changes.
export async function startAccount(appHooks) {
  Object.assign(hooks, appHooks);
  await initAuth();
  if (!signInAvailable()) return false;
  setTokenSource({ token: idToken, onExpired: expired });
  lastUid = AUTH.user?.uid ?? null;
  onAuthChange(user => {
    const uid = user?.uid ?? null;
    if (uid === lastUid) { hooks.render(); return; }  // a refreshed token for the same account
    lastUid = uid;
    followAuth();
  });
  if (AUTH.user) await loadWorkspace();
  return true;
}

// The user's workspace id (creating it on the first visit); null when signed out or refused.
async function loadWorkspace() {
  try {
    const me = await api.me();
    ACC.workspace = me.project;
    ACC.error = null;
  } catch (e) {
    ACC.workspace = null;
    ACC.error = e.status === 429 ? { key: 'problem.user-limit' } : { key: 'auth.failed', error: e };
  }
  return ACC.workspace?.id ?? null;
}

// After sign-in or sign-out: wait for an import in flight to finish, then show the workspace or the showcase.
async function followAuth() {
  while (hooks.busy()) await new Promise(resolve => setTimeout(resolve, 300));
  ACC.menu = false;
  ACC.settings = { ...ACC.settings, open: false, error: null, saved: false };
  const pid = AUTH.user ? await loadWorkspace() : null;
  if (!AUTH.user) ACC.workspace = null;
  await hooks.switchTo(pid || RT.showcase_project);
}

async function expired() {
  ACC.error = { key: 'auth.expired' };
  await signOut();
}

// The project to open at start: a remembered project the account can see, else its workspace, else the showcase.
export function initialProject(projects, remembered) {
  if (!signInAvailable()) return remembered;
  if (remembered && projects.some(p => p.id === remembered)) return remembered;
  return ACC.workspace?.id || RT.showcase_project || remembered;
}

// Why a linked project is not shown: signed out, or it belongs to another account.
export const deniedKey = () => (AUTH.user ? 'problem.forbidden' : 'problem.unauthenticated');

export const isShowcase = pid => Boolean(pid && pid === RT.showcase_project);

// ---- AccountArea + AccountMenu (in the TopBar, where "Start my sandbox" used to be) -------------------

function avatar(user) {
  if (user.photo) return `<img class="avatar" src="${esc(user.photo)}" alt="" referrerpolicy="no-referrer">`;
  return `<span class="avatar" aria-hidden="true">${esc((user.name || user.uid || '?').slice(0, 1).toUpperCase())}</span>`;
}

export function renderAccount(bar) {
  const user = AUTH.user;
  const err = ACC.error ? `<div class="errline" role="alert">${esc(ACC.error.error ? t(ACC.error.key, { error: errorText(ACC.error.error) }) : t(ACC.error.key))}</div>` : '';
  if (!user) {
    bar.innerHTML = `<div class="account"><button class="btn primary small" type="button" data-sign-in ${ACC.signing ? 'disabled' : ''}>${esc(t(ACC.signing ? 'auth.signing_in' : 'auth.sign_in'))}</button>${err}</div>`;
    return;
  }
  const pid = hooks.pid();
  const ws = ACC.workspace;
  const items = [
    user.email ? `<div class="who">${esc(user.email)}</div>` : '',
    ws ? `<button class="item${pid === ws.id ? ' current' : ''}" type="button" role="menuitem" data-account-go="${esc(ws.id)}">${esc(t('auth.my_workspace'))}</button>` : '',
    RT.showcase_project ? `<button class="item${isShowcase(pid) ? ' current' : ''}" type="button" role="menuitem" data-account-go="${esc(RT.showcase_project)}">${esc(t('auth.showcase'))}</button>` : '',
    '<hr>',
    ws ? `<button class="item" type="button" role="menuitem" data-account-settings>${esc(t('auth.settings'))}</button>` : '',
    ws ? `<button class="item" type="button" role="menuitem" data-account-reset ${ACC.resetting ? 'disabled' : ''}>${esc(t(ACC.resetting ? 'auth.resetting' : 'auth.reset'))}</button>` : '',
    `<button class="item" type="button" role="menuitem" data-sign-out>${esc(t('auth.sign_out'))}</button>`,
  ].join('');
  bar.innerHTML = `<div class="account switcher">
    <button class="account-btn" type="button" data-account-toggle aria-haspopup="true" aria-expanded="${ACC.menu}" aria-label="${esc(t('auth.account_menu'))}">${avatar(user)}<span class="account-name">${esc(user.name || user.uid)}</span> ▾</button>
    ${ACC.menu ? `<div class="menu account-menu" role="menu">${items}</div>` : ''}${err}</div>`;
}

// ---- ShowcaseNotice / WorkspaceSettings (above the importer) ------------------------------------------

export function renderAccountPanel(pid) {
  const el = document.getElementById('account-panel');
  if (!el) return;
  if (!signInAvailable()) { el.innerHTML = ''; return; }
  const s = ACC.settings;
  if (AUTH.user && s.open && ACC.workspace && pid === ACC.workspace.id) {
    const dis = s.saving ? 'disabled' : '';
    el.innerHTML = `<form class="prform ws-settings" id="workspace-settings" novalidate>
      <label for="ws-repo">${esc(t('settings.repo'))}</label>
      <input class="field mono" id="ws-repo" name="repo" placeholder="${esc(t('project.repo_placeholder'))}" value="${esc(s.repo)}" autocomplete="off" ${dis}>
      <p class="help">${esc(t('settings.repo_help'))}</p>
      <div class="actions"><button class="btn primary small" type="submit" ${dis}>${esc(t(s.saving ? 'settings.saving' : 'settings.save'))}</button>
        <button class="btn small" type="button" data-settings-close ${dis}>${esc(t('settings.close'))}</button>
        ${s.saved ? `<span class="saved" role="status">${esc(t('settings.saved'))}</span>` : ''}</div>
      ${s.error ? `<div class="errline" role="alert">${esc(s.error.status === 422 ? t('settings.repo_invalid') : errorText(s.error))}</div>` : ''}</form>`;
    return;
  }
  if (!AUTH.user && isShowcase(pid)) {
    el.innerHTML = `<div class="showcase-notice"><span>${esc(t('auth.showcase_notice'))}</span>
      <button class="btn primary small" type="button" data-sign-in ${ACC.signing ? 'disabled' : ''}>${esc(t(ACC.signing ? 'auth.signing_in' : 'auth.sign_in'))}</button></div>`;
    return;
  }
  el.innerHTML = '';
}

// ---- actions ------------------------------------------------------------------------------------------

async function doSignIn() {
  if (ACC.signing) return;
  let testUid;
  if (AUTH.mode === 'fake') {
    testUid = prompt(t('auth.test_prompt'));
    if (!testUid) return;
  }
  ACC.signing = true;
  ACC.error = null;
  hooks.render();
  try {
    await signIn(testUid);  // the auth listener opens the workspace
  } catch (e) {
    ACC.error = { key: 'auth.failed', error: e };
  } finally {
    ACC.signing = false;
    hooks.render();
  }
}

async function doReset() {
  if (!confirm(t('auth.reset_confirm'))) return;
  ACC.resetting = true;
  hooks.render();
  try {
    await api.resetMe();
    ACC.resetting = false;
    ACC.menu = false;
    await hooks.switchTo(ACC.workspace.id);
  } catch (e) {
    ACC.resetting = false;
    hooks.render();
    alert(t('auth.reset_failed', { error: errorText(e) }));
  }
}

function openSettings() {
  ACC.menu = false;
  ACC.settings = { open: true, repo: ACC.workspace?.repo || '', saving: false, saved: false, error: null };
  if (hooks.pid() !== ACC.workspace.id) { hooks.switchTo(ACC.workspace.id); return; }
  hooks.render();
  document.getElementById('ws-repo')?.focus();
}

async function saveSettings(form) {
  const s = ACC.settings;
  s.repo = form.repo.value;
  s.saving = true;
  s.saved = false;
  s.error = null;
  hooks.render();
  try {
    ACC.workspace = await api.updateProject(ACC.workspace.id, s.repo.trim());
    s.repo = ACC.workspace.repo || '';
    s.saved = true;
    setTimeout(() => { if (ACC.settings === s) { s.saved = false; hooks.render(); } }, 2000);
  } catch (e) {
    s.error = e;
  } finally {
    s.saving = false;
    hooks.render();
  }
}

export function closeAccountMenu(focus = false) {
  if (!ACC.menu) return false;
  ACC.menu = false;
  hooks.render();
  if (focus) document.querySelector('[data-account-toggle]')?.focus();
  return true;
}

export const accountActions = {
  click(btn) {
    if (btn.hasAttribute('data-sign-in')) { doSignIn(); return true; }
    if (btn.hasAttribute('data-account-toggle')) { ACC.menu = !ACC.menu; hooks.render(); return true; }
    if (btn.dataset.accountGo) { ACC.menu = false; hooks.switchTo(btn.dataset.accountGo); return true; }
    if (btn.hasAttribute('data-account-settings')) { openSettings(); return true; }
    if (btn.hasAttribute('data-account-reset')) { doReset(); return true; }
    if (btn.hasAttribute('data-sign-out')) { ACC.menu = false; ACC.error = null; signOut(); return true; }
    if (btn.hasAttribute('data-settings-close')) { ACC.settings = { ...ACC.settings, open: false, error: null }; hooks.render(); return true; }
    return false;
  },
  submit(e) {
    if (e.target.id !== 'workspace-settings') return false;
    e.preventDefault();
    if (!ACC.settings.saving) saveSettings(e.target);
    return true;
  },
  input(e) {
    if (e.target.id === 'ws-repo') ACC.settings.repo = e.target.value;
  },
};
