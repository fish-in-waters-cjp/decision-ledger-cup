// App shell: state, TopBar (ProjectSwitcher + tabs), ImportPanel (upload + demo script + pipeline), routing.
import { api, setProject } from './api.js';
import { $, esc, button, openDrawer, openRawDrawer, closeDrawer } from './components.js';
import { renderOverview, overviewActions } from './overview.js';
import { renderTopics, topicActions, loadTopic, openDecision } from './topics.js';
import { renderPullRequests, renderAsk, pageActions } from './pages.js';
import { loadRuntime, renderRecorded } from './runtime.js';
import { t, tn, problemTitle, errorText, initLocale, setLocale, loadZh, hasDictionary, getLocale, applyStatic, htmlLang, readStoredLocale } from './i18n.js';
import { SB, sandboxIdFromPath, loadSandbox, renderSandboxBar, renderExpired, readOnlyShowcase, sandboxActions } from './sandbox.js';
import { readOnlyKey } from './readonly.js';
import { startAccount, initialProject, signInMode, deniedKey, renderAccountPanel, accountActions, closeAccountMenu, ACC } from './account.js';

const REDUCED = matchMedia('(prefers-reduced-motion: reduce)').matches;
const sleep = ms => new Promise(r => setTimeout(r, REDUCED ? 0 : ms));

// Stage names are dictionary keys, translated when rendered (DL-031); `p.stages === SOURCE_STAGES` compares the key arrays.
const SOURCE_STAGES = ['pipeline.stage.ingestion', 'pipeline.stage.extractor', 'pipeline.stage.reconciler', 'pipeline.stage.assumption_watcher'];
const PR_STAGES = ['pipeline.stage.diff_summary', 'pipeline.stage.search_decisions', 'pipeline.stage.compliance', 'pipeline.stage.pr_comment'];
const PROJECT_KEY = 'decision-ledger.project';

const freshProjectState = () => ({
  overview: null, topics: [], prChecks: [], topicId: null, topic: null, topicError: null, topicQuery: '',
  renaming: false, renameBusy: false, renameError: null, merging: false, mergeInto: '', mergeBusy: false, mergeError: null,
  selected: [], moveInto: '', splitName: '', moveBusy: false, moveError: null,
  prSel: null, replayBusy: null, replayError: null, focusDecision: null, focusScroll: false, chat: [], exceptionFor: null, exceptionNotes: {}, pending: null, cardError: {}, resolvedHere: {},
});

export const S = {
  view: 'overview',
  projects: [],
  pid: null,
  loadError: null,
  manifest: null,
  busy: false,
  importOpen: null,  // null = automatic (open while the project is empty or busy)
  pipe: { src: '', stages: SOURCE_STAGES, at: -1, log: [], failed: false, retry: null },
  switcher: { open: false, creating: false, submitting: false, error: null, name: '', repo: '' },
  ...freshProjectState(),
};

// ---- project selection ------------------------------------------------------------

function rememberedProject() {
  const m = location.hash.match(/(?:^#|&)p=([^&]+)/);
  if (m) return decodeURIComponent(m[1]);
  try { return localStorage.getItem(PROJECT_KEY); } catch { return null; }
}

function linkedDecision() {
  const m = location.hash.match(/(?:^#|&)d=([^&]+)/);
  return m ? decodeURIComponent(m[1]) : null;
}

// Deep link #p=<project>&d=<decision>: switch project when needed, then open the decision's topic.
async function followDeepLink() {
  const decisionId = linkedDecision();
  if (!decisionId) return;
  const pid = rememberedProject();
  history.replaceState(null, '', `#p=${encodeURIComponent(S.pid)}`);
  // Decision numbers are per project: an unknown or missing project must not open another project's decision.
  if (!pid || !S.projects.some(p => p.id === pid)) { goTo('overview'); return; }
  if (pid !== S.pid) await switchProject(pid);
  if (!openDecision(S, decisionId)) goTo('overview');
}

// A remembered project that no longer exists (e.g. the database was deleted) falls back to the first one.
function useProject(pid) {
  const known = S.projects.some(p => p.id === pid);
  S.pid = known ? pid : S.projects[0]?.id ?? null;
  setProject(S.pid);
  if (!S.pid) return;
  history.replaceState(null, '', `#p=${encodeURIComponent(S.pid)}`);
  try { localStorage.setItem(PROJECT_KEY, S.pid); } catch { /* private mode: the URL still carries it */ }
}

async function switchProject(pid, { force = false } = {}) {
  if (S.busy) return;
  // Signing in or out changes which projects this browser may see: reload the list first (DL-032).
  if (force) {
    S.notice = null;  // a notice about the previous account no longer applies
    try { S.projects = await api.projects(); } catch (e) { S.loadError = e; }
  }  // an import in flight writes to the project it started in; switch after it finishes
  Object.assign(S, freshProjectState());
  // The pipeline log describes the previous project's last import; a new project starts with none.
  S.pipe = { src: '', stages: SOURCE_STAGES, at: -1, log: [], failed: false, retry: null };
  S.switcher = { ...S.switcher, open: false, creating: false, error: null };
  useProject(pid);
  if (S.view === 'ask' && readOnlyShowcase(S.pid)) S.view = 'overview';  // the showcase has no Ask page (DL-032 F1.3)
  await refresh();
}

// ---- data ---------------------------------------------------------------------

export async function refresh() {
  try {
    S.projects = SB.id ? [SB.project] : await api.projects();
    if (!S.projects.some(p => p.id === S.pid)) useProject(S.pid);
    const pid = S.pid;
    const [overview, prChecks, topics] = await Promise.all([api.overview(), api.prChecks(), api.topics()]);
    if (pid !== S.pid) return;  // the user switched projects meanwhile: a late answer must not paint the new one
    Object.assign(S, { overview, prChecks, topics, loadError: null });
    if (S.view === 'topics' && S.topicId) await loadTopic(S, S.topicId, false);
    if (pid !== S.pid) return;
  } catch (e) {
    // DL-032: a project this account may not open (e.g. after signing out elsewhere) goes back to its own workspace.
    if (e.status === 403 && signInMode() && ACC.workspace && S.pid !== ACC.workspace.id && !S.busy) {
      await switchProject(ACC.workspace.id, { force: true });
      S.notice = { key: deniedKey(), alert: true };
      render();
      return;
    }
    S.loadError = e;  // the error object; its title is translated when rendered
  }
  render();
}

// A stored message ({key, n?, params, error?}) as text in the current language; `error` fills {error}
// and `n` selects the plural form.
export function message(m) {
  if (!m) return '';
  const params = { ...(m.params || {}), ...(m.error ? { error: problemTitle(m.error) } : {}) };
  return m.n === undefined ? t(m.key, params) : tn(m.key, m.n, params);
}

// ---- TopBar -------------------------------------------------------------------

// Interface language (DL-031). English needs no download; Chinese is fetched on the first switch.
const LOCALE = { busy: false };

function browserStorage() {
  try { return window.localStorage; } catch { return null; }
}

function renderLangToggle() {
  const b = $('#lang-toggle');
  if (!b) return;
  b.disabled = LOCALE.busy;
  b.textContent = LOCALE.busy ? t('locale.loading') : t(getLocale() === 'en' ? 'locale.switch_to_zh' : 'locale.switch_to_en');
}

async function switchLocale() {
  if (LOCALE.busy) return;
  const next = getLocale() === 'en' ? 'zh-TW' : 'en';
  if (next === 'zh-TW' && !hasDictionary('zh-TW')) {
    LOCALE.busy = true;
    renderLangToggle();
    const ok = await loadZh(url => fetch(url));
    LOCALE.busy = false;
    if (!ok) {
      if (SB.expired) { renderLangToggle(); return; }  // keep the expired page; it has no notice area
      S.notice = { key: 'notice.locale_load_failed' };
      render();
      return;
    }
  }
  setLocale(next, { storage: browserStorage() });
  document.documentElement.lang = htmlLang();
  applyStatic(document);
  if (SB.expired) { renderExpired(); renderLangToggle(); return; }  // the expired page has no app view to redraw
  render();
}

function renderBackend() {
  const b = $('#backend');
  const name = S.overview?.backend;
  if (!name) { b.textContent = t(S.loadError ? 'status.offline' : 'status.connecting'); return; }
  const live = name !== 'fake';
  b.className = `proto${live ? ' live' : ''}`;
  b.textContent = live ? t('status.llm', { name: t(name === 'gemini' ? 'product.gemini' : 'product.claude_code') }) : t('status.recorded');
}

function renderSwitcher() {
  const sw = S.switcher;
  const current = S.projects.find(p => p.id === S.pid);
  const dis = sw.submitting ? 'disabled' : '';
  const form = `<form id="project-form" novalidate>
      <input class="field" name="name" placeholder="${esc(t('project.name'))}" aria-label="${esc(t('project.name'))}" value="${esc(sw.name)}" ${dis}>
      <input class="field" name="repo" placeholder="${esc(t('project.repo_placeholder'))}" aria-label="${esc(t('project.repo'))}" value="${esc(sw.repo)}" ${dis}>
      <div class="actions"><button class="btn primary small" type="submit" ${dis}>${esc(t(sw.submitting ? 'project.creating' : 'project.create'))}</button>${button(t('common.cancel'), `data-create-cancel ${dis}`)}</div>
      ${sw.error ? `<div class="errline" role="alert">${esc(errorText(sw.error))}</div>` : ''}</form>`;
  // Keep focus and caret in the creation form across re-renders.
  const active = document.activeElement;
  const focused = active?.closest?.('#switcher') && active.name;
  const caret = focused ? active.selectionStart : null;
  $('#switcher').innerHTML = `<button class="switch-btn" type="button" data-switch-toggle aria-haspopup="true" aria-expanded="${sw.open}" ${S.busy ? 'disabled' : ''} ${S.busy ? `title="${esc(t('project.switch_busy'))}"` : ''}>${esc(current?.name || '…')} ▾</button>
    ${sw.open ? `<div class="menu" role="menu">${S.projects.map(p => `<button class="item${p.id === S.pid ? ' current' : ''}" type="button" role="menuitem" data-project="${esc(p.id)}">
        <span>${p.id === S.pid ? '✓ ' : ''}${esc(p.name)}</span><span class="n" title="${esc(t('project.pending'))}">${p.pending}</span></button>`).join('')}
      <hr>${sw.creating ? form : `<button class="item add" type="button" data-create-project>+ ${esc(t('project.create_new'))}</button>`}</div>` : ''}`;
  if (focused) {
    const el = $(`#switcher [name="${focused}"]`);
    el?.focus();
    if (el && caret !== null) el.setSelectionRange(caret, caret);
  }
}

async function createProject(form) {
  const sw = S.switcher;
  const fd = new FormData(form);
  sw.name = String(fd.get('name') || '');
  sw.repo = String(fd.get('repo') || '');
  sw.submitting = true;
  sw.error = null;
  renderSwitcher();
  try {
    const project = await api.createProject(sw.name.trim(), sw.repo.trim());
    S.switcher = { open: false, creating: false, submitting: false, error: null, name: '', repo: '' };
    S.view = 'overview';
    S.importOpen = null;
    S.projects = await api.projects();
    await switchProject(project.id);
  } catch (e) {
    sw.submitting = false;
    sw.error = e;  // keep the input so it can be corrected; the message is rendered from the error
    renderSwitcher();
    $('#switcher [name="name"]')?.focus();
  }
}

// ---- ImportPanel --------------------------------------------------------------

// Ids are numbered per project, so the demo's recorded answers fit any project that holds nothing but
// demo steps (an empty one included). Once other data is imported the script is hidden. Unknown until loaded.
export function demoEnabled() {
  if (!S.manifest || !S.overview || readOnlyShowcase(S.pid)) return false;  // the showcase is never written to
  const steps = S.manifest.steps || [];
  const sourceSteps = steps.filter(st => st.type !== 'prs');
  const demoPrTitles = new Set(steps.filter(st => st.type === 'prs').flatMap(st => st.pull_requests.map(p => p.title)));
  const isDemoSource = src => sourceSteps.some(st => st.title === src.title && (st.occurred_on || null) === (src.occurred_on || null));
  return S.overview.sources.every(isDemoSource) && S.prChecks.every(c => demoPrTitles.has(c.title));
}

function demoSteps() {
  return demoEnabled() ? S.manifest.steps || [] : [];
}

// Replay needs every demo source before the PR step (the server checks the same set by content).
export function replayReady() {
  if (readOnlyShowcase(S.pid)) return false;
  const steps = S.manifest?.steps || [];
  const prAt = steps.findIndex(s => s.type === 'prs');
  return prAt > 0 && steps.slice(0, prAt).every(stepDone);
}

function stepDone(step) {
  if (!S.overview) return false;
  if (step.type === 'prs') return step.pull_requests.every(p => S.prChecks.some(c => c.title === p.title));
  return S.overview.sources.some(s => s.title === step.title && (s.occurred_on || null) === (step.occurred_on || null));
}

function renderImportPanel() {
  const steps = demoSteps();
  const next = steps.findIndex(s => !stepDone(s));
  const importer = $('#importer');
  importer.hidden = S.view !== 'overview' || readOnlyShowcase(S.pid);
  const empty = !S.overview || (!S.overview.sources.length && !S.prChecks.length);
  const open = S.busy || (S.importOpen ?? empty);
  if (importer.open !== open) { importer.dataset.programmatic = '1'; importer.open = open; }
  const n = S.overview?.sources.length || 0;
  const summary = n || S.prChecks.length
    ? t('import.summary', { sources: tn('import.sources', n), prs: tn('import.prs', S.prChecks.length) })
    : t('import.title');
  $('#import-summary').innerHTML = `<span>${esc(summary)}</span>`;
  $('#import-panel').innerHTML = `
    <h2><span>${esc(t('import.title'))}</span></h2>
    <form class="upload" id="upload-form">
      <div class="row"><select name="kind" aria-label="${esc(t('import.source_type'))}"><option value="transcript">${esc(t('import.kind.transcript'))}</option><option value="slack">${esc(t('import.kind.slack'))}</option></select></div>
      <input type="file" name="file" required accept=".txt,.json,text/plain,application/json" aria-label="${esc(t('import.file'))}">
      <div class="row"><input type="text" name="title" placeholder="${esc(t('import.title_placeholder'))}"><input type="text" name="occurred_on" placeholder="${esc(t('import.date_placeholder'))}"></div>
      <div class="row"><button class="btn primary small" type="submit" ${S.busy ? 'disabled' : ''}>${esc(t(S.busy ? 'common.processing' : 'import.submit'))}</button></div>
    </form>
    ${steps.length ? `<h2><span>${esc(t('demo.title'))}</span>${next >= 0 ? `<button class="linkbtn" type="button" data-demo-all ${S.busy ? 'disabled' : ''}>${esc(t('demo.all'))}</button>` : ''}</h2>
    <ol class="steps">${steps.map((st, i) => {
      const done = stepDone(st);
      const cls = done ? 'done' : i === next ? 'next' : 'todo';
      return `<li class="step ${cls}"><span class="n">${done ? '✓' : i + 1}</span><div>
        <div class="meta">${esc(st.kind_label)}</div><div class="t">${esc(st.label || st.title)}</div><div class="d">${esc(st.desc || '')}</div>
        ${st.type !== 'prs' ? `<div class="act"><button class="linkbtn" type="button" data-raw-step="${i}">${esc(t('demo.raw'))}</button></div>` : ''}
        ${i === next ? `<div class="act">${button(t(S.busy ? 'common.processing' : 'import.submit'), `data-demo-step="${i}" ${S.busy ? 'disabled' : ''}`, true)}</div>` : ''}
      </div></li>`;
    }).join('')}</ol>` : ''}`;
}

function renderPipeline() {
  const p = S.pipe;
  $('#pipe-src').textContent = p.src;
  $('#pipe').innerHTML = p.stages.map((n, i) => {
    let cls = '';
    if (p.failed && i === p.at) cls = 'fail';
    else if (p.at === 99 || i < p.at) cls = 'ok';
    else if (i === p.at) cls = 'run';
    const label = p.stages === SOURCE_STAGES ? t('pipeline.stage_agent', { stage: t(n) }) : t(n);
    return `<div class="stage ${cls}"><span class="dot"></span>${esc(label)}</div>`;
  }).join('');
  $('#log').innerHTML = p.log.length
    ? p.log.map(l => `<div class="${l.error ? 'errline' : ''}"${l.error ? ' role="alert"' : ''}><b>${esc(t('common.label_prefix', { label: t(l.stage) }))}</b>${esc(logText(l))}${l.error && p.retry && !S.busy ? ` ${button(t('common.retry'), 'data-pipe-retry')}` : ''}</div>`).join('')
    : `<div class="muted">${esc(t('pipeline.empty'))}</div>`;
}

// A log line holds {stage, key, n?, params} or {stage, error}; both become text only when rendered.
function logText(l) {
  if (l.error) return `${problemTitle(l.error)}${l.error.detail ? ` (${l.error.detail})` : ''}`;
  return l.n === undefined ? t(l.key, l.params) : tn(l.key, l.n, l.params);
}

// Animate stages while the single synchronous request is in flight; stop on the last stage.
// On failure the log shows the error with a Retry button that runs ``retry`` (the same action again).
async function withPipeline(src, stages, work, retry = null) {
  S.busy = true;
  S.pipe = { src, stages, at: 0, log: [], failed: false, retry: null };
  render();
  let finished = false;
  const ticker = (async () => {
    for (let i = 0; i < stages.length - 1 && !finished; i++) { await sleep(650); if (!finished) { S.pipe.at = i + 1; renderPipeline(); } }
  })();
  try {
    const log = await work();
    finished = true;
    await ticker;
    S.pipe.at = 99;
    S.pipe.log = log;
    return true;
  } catch (e) {
    finished = true;
    await ticker;
    S.pipe.failed = true;
    S.pipe.retry = retry;
    S.importOpen = true;  // keep the panel open so the error and its Retry stay visible (review F-1)
    S.pipe.log = [{ stage: stages[S.pipe.at] || stages[0], error: e }];
    return false;
  } finally {
    S.busy = false;
    await refresh();
  }
}

function importLog(r) {
  if (r.duplicate) return [{ stage: 'pipeline.stage.ingestion', key: 'pipeline.log.duplicate' }];
  const discarded = r.discarded.filter(d => d.kind === 'decision').length;
  const alertDiscarded = r.discarded.length - discarded;
  const extracted = r.created.length + r.merged.length;
  const premises = r.assumption_alerts.length;
  // A line with `n` is rendered with tn() (English singular/plural, see i18n.tn).
  const plural = (key, n, params = {}) => ({ key, n, params });
  return [
    { stage: 'pipeline.stage.ingestion', ...plural('pipeline.log.segments', r.segment_count) },
    { stage: 'pipeline.stage.extractor', ...(discarded ? plural('pipeline.log.extracted_discarded', extracted, { discarded }) : plural('pipeline.log.extracted', extracted)) },
    { stage: 'pipeline.stage.topic_grouping', key: 'pipeline.log.topics', params: { existing: r.topics_existing, created: r.topics_created } },
    { stage: 'pipeline.stage.reconciler', key: 'pipeline.log.reconciled', params: { new: r.created.length - r.supersession_alerts.length - r.conflict_alerts.length, merged: r.merged.length, supersession: r.supersession_alerts.length, conflicts: r.conflict_alerts.length } },
    { stage: 'pipeline.stage.assumption_watcher', ...(premises ? plural('pipeline.log.premise_alerts', premises) : alertDiscarded ? plural('pipeline.log.no_premises_discarded', alertDiscarded) : { key: 'pipeline.log.no_premises' }) },
  ];
}

async function importForm(form) {
  const fd = new FormData(form);
  const file = fd.get('file');
  const title = (fd.get('title') || '').trim() || file?.name || '';
  const run = () => withPipeline(title, SOURCE_STAGES, async () => importLog(await api.importSource(fd)), run);
  await run();
  form.reset();
}

async function runDemoStep(i) {
  const st = demoSteps()[i];
  if (!st) return false;
  if (st.type === 'prs') {
    const ok = await withPipeline(st.label || st.title, PR_STAGES, async () => {
      const results = [];
      for (const p of st.pull_requests) {
        const diff = await api.demoFile(p.file);
        const { file, ...meta } = p;
        results.push(await api.prCheck({ ...meta, diff }));
      }
      const violated = results.filter(r => r.check.violated);
      return [
        { stage: PR_STAGES[0], key: 'pipeline.log.prs_received', n: results.length },
        { stage: PR_STAGES[1], key: 'pipeline.log.matching' },
        violated.length
          ? { stage: PR_STAGES[2], key: 'pipeline.log.prs_violate', params: { prs: violated.map(r => `PR #${r.check.pr_number}`).join(', ') } }
          : { stage: PR_STAGES[2], key: 'pipeline.log.no_violations' },
        { stage: PR_STAGES[3], key: 'pipeline.log.comment' },
      ];
    }, () => runDemoStep(i));
    if (ok) { S.view = 'prs'; S.prSel = null; render(); }
    return ok;
  }
  const fd = new FormData();
  fd.append('kind', st.kind);
  fd.append('title', st.title);
  if (st.occurred_on) fd.append('occurred_on', st.occurred_on);
  const ok = await withPipeline(st.label || st.title, SOURCE_STAGES, async () => {
    const text = await api.demoFile(st.file);
    fd.append('file', new Blob([text], { type: 'text/plain' }), st.file);
    return importLog(await api.importSource(fd));
  }, () => runDemoStep(i));
  if (ok) { S.view = 'overview'; render(); }
  return ok;
}

async function runDemoAll() {
  for (let i = 0; i < demoSteps().length; i++) {
    if (stepDone(demoSteps()[i])) continue;
    if (!(await runDemoStep(i))) return; // stop at the failing step; it stays retryable
  }
  S.view = 'overview';
  render();
}

// ---- MainTabs + routing ----------------------------------------------------

function renderTabs() {
  const open = S.overview?.open_alerts || [];
  const tabs = [
    ['overview', 'tabs.overview', open.length],
    ['topics', 'tabs.topics', 0],
    ['prs', 'tabs.prs', open.filter(a => a.kind === 'deviation').length],
    ['ask', 'tabs.ask', 0],
  ].filter(([k]) => !(k === 'ask' && readOnlyShowcase(S.pid)));
  $('#tabs').innerHTML = tabs.map(([k, l, c]) =>
    `<button class="tab" role="tab" type="button" data-view="${k}" aria-selected="${S.view === k}">${esc(t(l))}${c ? `<span class="count">${c}</span>` : ''}</button>`).join('');
}

function renderBanner() {
  if (S.loadError) {
    $('#banner').innerHTML = `<div class="banner"><span>${esc(message({ key: 'banner.load_failed', error: S.loadError }))}</span>${button(t('common.retry'), 'data-retry')}</div>`;
  } else if (S.notice) {
    $('#banner').innerHTML = `<div class="hint" role="${S.notice.alert ? 'alert' : 'status'}" style="display:flex;justify-content:space-between;gap:10px"><span>${esc(message(S.notice))}</span><button class="linkbtn" type="button" data-dismiss-notice>${esc(t('common.got_it'))}</button></div>`;
  } else {
    $('#banner').innerHTML = '';
  }
}

// “View raw content”: the imported source in the drawer, or the raw sample file before import.
async function showRawStep(i) {
  const st = demoSteps()[i];
  const imported = S.overview?.sources.find(s => s.title === st.title && (s.occurred_on || null) === (st.occurred_on || null));
  if (imported) return openDrawer(imported.id, '');
  try {
    // Show the file verbatim; only the server parses sources, so there is one set of parsing rules.
    openRawDrawer(st.label || st.title, st.kind_label, await api.demoFile(st.file));
  } catch (e) {
    S.notice = { key: 'notice.sample_read_failed', error: e };
    render();
  }
}

export function render() {
  renderLangToggle();
  renderBackend();
  renderRecorded(S.pid);
  renderSandboxBar(S);
  renderAccountPanel(S.pid);
  renderSwitcher();
  renderImportPanel();
  renderPipeline();
  renderBanner();
  renderTabs();
  S.readOnly = readOnlyKey(readOnlyShowcase(S.pid), signInMode());  // write controls become a sentence (readonly.js)
  const views = { overview: renderOverview, topics: renderTopics, prs: renderPullRequests, ask: renderAsk };
  $('#view').innerHTML = S.overview ? views[S.view](S) : '<div class="skeleton" style="height:120px"></div>';
  disableShowcaseWrites();
}

// Every control that would write is disabled on the read-only showcase, with the reason as its tooltip (DL-032 F1.3).
const SHOWCASE_WRITES = [
  '[data-resolve]', '[data-exception]', '[data-rename]', '[data-merge-toggle]', '[data-pick]', '[data-reaffirm]',
  '#diff-form [name="diff"]', '#diff-form [type="submit"]', '[data-ask]', '#askq', '#askform [type="submit"]',
].join(',');
function disableShowcaseWrites() {
  if (!readOnlyShowcase(S.pid)) return;
  const why = t(signInMode() ? 'auth.showcase_notice' : 'sandbox.read_only');
  $('#view').querySelectorAll(SHOWCASE_WRITES).forEach(el => { el.disabled = true; el.title = why; });
}

export function goTo(view) {
  S.view = view;
  S.focusDecision = null;
  if (view !== 'topics') S.topicId = null;
  render();
  window.scrollTo({ top: 0 });
}

// ---- events -------------------------------------------------------------------

$('#importer').addEventListener('toggle', e => {
  if (e.target.dataset.programmatic) { delete e.target.dataset.programmatic; return; }
  S.importOpen = e.target.open;
});

document.addEventListener('input', e => {
  if (e.target.closest('#project-form')) S.switcher[e.target.name] = e.target.value;
  accountActions.input(e);
  topicActions.input?.(e, S);
});

document.addEventListener('change', e => topicActions.change?.(e, S));

document.addEventListener('submit', e => {
  if (e.target.id === 'upload-form') { e.preventDefault(); if (!S.busy) importForm(e.target); return; }
  if (e.target.id === 'project-form') { e.preventDefault(); if (!S.switcher.submitting) createProject(e.target); return; }
  if (accountActions.submit(e)) return;
  if (topicActions.submit(e, S)) return;
  pageActions.submit?.(e, S);
});

document.addEventListener('click', e => {
  const t = e.target.closest('button');
  if (!e.target.closest('#switcher') && S.switcher.open) { S.switcher.open = false; S.switcher.creating = false; renderSwitcher(); }
  if (ACC.menu && !e.target.closest('.account')) closeAccountMenu();
  if (!t) { if (e.target === $('#scrim')) closeDrawer(); return; }
  if (t.hasAttribute('data-locale-toggle')) { switchLocale(); return; }
  if (t.hasAttribute('data-switch-toggle')) { S.switcher.open = !S.switcher.open; S.switcher.creating = false; S.switcher.error = null; renderSwitcher(); return; }
  if (t.dataset.project) { if (t.dataset.project !== S.pid) switchProject(t.dataset.project); else { S.switcher.open = false; renderSwitcher(); } return; }
  if (t.hasAttribute('data-create-project')) { S.switcher.creating = true; renderSwitcher(); $('#switcher [name="name"]')?.focus(); return; }
  if (t.hasAttribute('data-create-cancel')) { S.switcher.creating = false; S.switcher.error = null; renderSwitcher(); return; }
  if (sandboxActions.click(t)) return;
  if (accountActions.click(t)) return;
  if (t.hasAttribute('data-close')) return closeDrawer();
  if (t.hasAttribute('data-retry')) return refresh();
  if (t.hasAttribute('data-pipe-retry')) { if (!S.busy && S.pipe.retry) S.pipe.retry(); return; }
  if (t.hasAttribute('data-dismiss-notice')) { S.notice = null; render(); return; }
  if (t.dataset.rawStep !== undefined) return showRawStep(Number(t.dataset.rawStep));
  if (t.dataset.src) return openDrawer(t.dataset.src, t.dataset.loc);
  if (t.hasAttribute('data-demo-all')) return runDemoAll();
  if (t.dataset.demoStep !== undefined) return runDemoStep(Number(t.dataset.demoStep));
  if (t.dataset.view) return goTo(t.dataset.view);
  if (overviewActions.click(t, S)) return;
  if (topicActions.click(t, S)) return;
  pageActions.click(t, S);
});

document.addEventListener('keydown', e => {
  if (e.key !== 'Escape') return;
  if (!$('#drawer').hidden) return closeDrawer();
  if (S.switcher.open) { S.switcher.open = false; S.switcher.creating = false; renderSwitcher(); $('#switcher .switch-btn')?.focus(); return; }
  if (closeAccountMenu(true)) return;
  topicActions.escape?.(S);
});

(async function start() {
  LOCALE.busy = readStoredLocale(browserStorage()) === 'zh-TW';  // only a stored zh-TW choice downloads anything
  renderLangToggle();
  await Promise.all([loadRuntime(), initLocale({ storage: browserStorage(), fetcher: url => fetch(url), doc: document })]);
  LOCALE.busy = false;
  applyStatic(document);
  renderLangToggle();
  // DL-032: with Google sign-in on, anonymous sandboxes are retired; an old /s/<id>/ link shows the showcase.
  const signIn = await startAccount({
    switchTo: pid => switchProject(pid, { force: true }),
    refresh, render, busy: () => S.busy, pid: () => S.pid,
  });
  SB.id = signIn ? null : sandboxIdFromPath();
  if (signIn && sandboxIdFromPath()) history.replaceState(null, '', '/');
  if (SB.id && !(await loadSandbox())) { renderExpired(); return; }
  try { S.manifest = await api.demoManifest(); } catch { S.manifest = null; }
  try {
    S.projects = SB.id ? [SB.project] : await api.projects();
  } catch (e) {
    S.loadError = e;
    render();
    return;
  }
  const decisionId = linkedDecision();  // read before useProject rewrites the hash
  const linkedProject = rememberedProject();
  const hashProject = /(?:^#|&)p=/.test(location.hash);  // a link someone opened, not this browser's memory
  // A sandbox page always shows its own sandbox; a deep link still opens only when it names that project.
  useProject(SB.id || initialProject(S.projects, linkedProject));
  // A deep link to a project this account cannot see (e.g. someone else's workspace) says so (DL-032).
  if (signInMode() && hashProject && linkedProject !== S.pid) S.notice = { key: deniedKey(), alert: true };
  await refresh();
  // Decision numbers are per project: open only when the linked project is the one now showing.
  if (decisionId && linkedProject === S.pid) openDecision(S, decisionId);
  setInterval(pollPullRequests, PR_POLL_MS);
})();

window.addEventListener('hashchange', () => { if (!S.busy) followDeepLink(); });

// While the Pull requests page is open, pick up checks finished in the background (webhook).
// Only re-render when the list changed, and never while the user is typing or sending.
const PR_POLL_MS = 15000;
async function pollPullRequests() {
  if (S.view !== 'prs' || document.hidden || S.busy || S.exceptionFor || S.pending || S.diffBusy || S.replayBusy) return;
  if (document.activeElement?.matches('input, textarea, select')) return;
  const pid = S.pid;
  try {
    const checks = await api.prChecks();
    if (pid !== S.pid) return;
    const keys = list => list.map(c => `${c.key}:${c.alert_ids.length}`).join('|');
    if (keys(checks) !== keys(S.prChecks)) await refresh();
  } catch { /* the next poll retries; refresh() reports load errors */ }
}
