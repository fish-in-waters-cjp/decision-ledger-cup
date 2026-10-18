// Topics tab: TopicList, TopicDetail (TopicTimeline, InlineAlert, AssumptionList, RenameTopicForm, MergeTopicPanel,
// and the selection bar that moves decisions to another topic or splits them into a new one).
import { api } from './api.js';
import { $, esc, pill, button, evidenceButton, quote, HEALTH, topicBadge } from './components.js';
import { render, refresh } from './app.js';
import { actions, resolutionText, ACTION_LABEL, KIND_COLOR } from './overview.js';
import { t, tn, problemTitle, errorText } from './i18n.js';
import { writeControls, unlessReadOnly } from './readonly.js';

// Labels are dictionary keys, translated when rendered (DL-031).
const EVENT_LABEL = { created: 'event.created', merged: 'event.merged', ...ACTION_LABEL, prefer: 'event.prefer' };
const GLYPH = { added: '+', updates: '↻', conflicts: '⚡' };
const ASSUMPTION_STATE = {  // [class, glyph, label key]
  holds: ['ok', '✓', 'assumption.holds'],
  confirmed: ['ok', '✓', 'assumption.confirmed'],
  doubted: ['doubt', '▲', 'assumption.doubted'],
  overturned: ['no', '✕', 'assumption.overturned'],
};

// An API error kept for an inline form: the translated title, plus the backend's detail when it has one.
const formError = e => errorText(e);

export async function loadTopic(S, topicId, rerender = true) {
  const pid = S.pid;
  try {
    const topic = await api.topic(topicId);
    if (pid !== S.pid || topicId !== S.topicId) return;  // stale: another project or topic is showing now
    S.topic = topic;
    S.topicError = null;
    if (S.focusScroll) {
      S.focusScroll = false;  // scroll once; later reloads of the topic keep the reader's position
      const target = S.focusDecision;
      requestAnimationFrame(() => document.getElementById(`decision-${target}`)?.scrollIntoView({ block: 'center' }));
    }
  } catch (e) {
    if (pid !== S.pid || topicId !== S.topicId) return;
    S.topic = null;
    S.topicError = e;  // rendered with problemTitle()
  }
  if (rerender) render();
}

// Deep link target: open the topic that holds ``decisionId`` and mark that node (false when unknown).
export function openDecision(S, decisionId) {
  const view = S.overview?.decisions.find(v => v.decision.id === decisionId);
  if (!view) return false;
  openTopic(S, view.decision.topic_id);
  S.focusDecision = decisionId;  // set after openTopic, which clears it for ordinary navigation
  S.focusScroll = true;
  render();
  return true;
}

function openTopic(S, topicId) {
  Object.assign(S, {
    view: 'topics', topicId, topic: null, topicError: null, focusDecision: null, focusScroll: false,
    renaming: false, renameError: null, merging: false, mergeInto: '', mergeError: null,
    selected: [], moveInto: '', splitName: '', moveError: null,
  });
  render();
  window.scrollTo({ top: 0 });
  loadTopic(S, topicId);
}

// ---- TopicList ----------------------------------------------------------------

function topicRows(S) {
  const q = S.topicQuery.trim().toLowerCase();
  const rows = S.topics.filter(c => !q || c.topic.name.toLowerCase().includes(q));
  if (!rows.length) return `<div class="empty">${esc(t('topics.no_match'))}</div>`;
  return `<div class="tlist">${rows.map(c => `<button class="trow" type="button" data-topic="${esc(c.topic.id)}">
      <span class="n">${esc(c.topic.name)}</span>${topicBadge(c.health)}
      <span class="m">${esc(tn('topics.decisions', c.decision_count))}${c.timeline.length && c.timeline.at(-1).decided_on ? ` · ${esc(c.timeline.at(-1).decided_on)}` : ''}</span></button>`).join('')}</div>`;
}

function renderList(S) {
  if (!S.topics.length) return `<div class="empty">${esc(t('topics.empty'))}</div>`;
  return `<div class="sec-h"><h2>${esc(t('overview.topics'))}</h2><span class="label">${esc(tn('overview.topics_count', S.topics.length))}</span></div>
    <div class="tsearch"><input class="field" type="search" id="topic-search" placeholder="${esc(t('topics.search'))}" aria-label="${esc(t('topics.search'))}" value="${esc(S.topicQuery)}"></div>
    <div id="topic-rows">${topicRows(S)}</div>`;
}

// ---- TopicDetail --------------------------------------------------------------

const backLink = () => `<div><button class="linkbtn" type="button" data-topics-back>${esc(t('topics.back'))}</button></div>`;

function header(S, tp) {
  const title = S.renaming
    ? `<form id="rename-form"><input class="field" name="name" aria-label="${esc(t('topics.name'))}" value="${esc(S.renameValue)}" ${S.renameBusy ? 'disabled' : ''}>
        <button class="btn primary small" type="submit" ${S.renameBusy ? 'disabled' : ''}>${esc(t(S.renameBusy ? 'common.saving' : 'common.save'))}</button>${button(t('common.cancel'), `data-rename-cancel ${S.renameBusy ? 'disabled' : ''}`)}</form>`
    : `<h1>${esc(tp.topic.name)}</h1>${topicBadge(tp.health)}
      ${writeControls(S, `<div class="acts">${button(t('topics.rename'), 'data-rename')}${button(t('topics.merge_into'), `data-merge-toggle aria-expanded="${S.merging}"`)}</div>`)}`;
  return `${backLink()}
    <div class="detail-head">${title}</div>
    ${S.renameError ? `<div class="errline" role="alert">${esc(formError(S.renameError))}</div>` : ''}`;
}

function mergePanel(S, tp) {
  if (!S.merging) return '';
  const targets = S.topics.filter(c => c.topic.id !== tp.topic.id);
  const target = targets.find(c => c.topic.id === S.mergeInto);
  const busy = S.mergeBusy ? 'disabled' : '';
  return `<form class="merge-panel" id="merge-form">
    <span>${esc(t('topics.merge.hint'))}</span>
    <select class="field" name="into" aria-label="${esc(t('topics.merge.target'))}" ${busy}><option value="">${esc(t('topics.merge.choose'))}</option>${targets.map(c =>
      `<option value="${esc(c.topic.id)}" ${c.topic.id === S.mergeInto ? 'selected' : ''}>${esc(c.topic.name)}</option>`).join('')}</select>
    <button class="btn danger" type="submit" ${!target || S.mergeBusy ? 'disabled' : ''}>${esc(t(S.mergeBusy ? 'topics.merging' : 'topics.merge'))}</button>${button(t('common.cancel'), `data-merge-toggle ${busy}`)}
    ${target ? `<span class="quiet">${esc(t('topics.merge.after', { name: target.topic.name }))}</span>` : ''}
    ${S.mergeError ? `<span class="errline" role="alert">${esc(formError(S.mergeError))}</span>` : ''}</form>`;
}

function changeLabel(node) {
  const d = node.decision;
  const parts = node.changes.map(c => {
    if (c.kind === 'updates') return `↻ ${esc(t('change.updates', { ref: c.ref }))}`;
    if (c.kind === 'conflicts') return `⚡ ${esc(t('change.conflicts', { ref: c.ref }))}`;
    if (c.kind === 'merged_sources') return `⧉ ${esc(tn('change.merged_sources', c.count))}`;
    return `+ ${esc(t('change.added'))}`;
  });
  return `${parts[0]} · #${esc(d.id)}${parts.slice(1).map(p => ` · ${p}`).join('')}`;
}

function inlineAlert(S, a) {
  if (a.status !== 'open') return `<div class="inline-alert done">${esc(t('inline.resolved', { text: resolutionText(a) }))}</div>`;
  const c = KIND_COLOR[a.kind];
  const other = a.kind === 'conflict' ? a.decision_ids.find(i => i !== a.decision_ids.at(-1)) : null;
  const check = a.kind === 'deviation' ? S.prChecks.find(x => x.key === a.pr_check_key) : null;
  return `<div class="inline-alert" style="--k:var(--${c});--ks:var(--${c}-soft)">
    <span class="t">${other ? `${esc(t('inline.conflicts', { id: other }))} ` : ''}${esc(a.summary)}${a.location ? ` (${esc(a.location)})` : ''}</span>
    ${actions(S, a)}
    ${check ? `<div class="actions"><button class="linkbtn" type="button" data-open-pr="${esc(check.key)}">${esc(check.pr_number ? t('alert.view_pr_number', { n: check.pr_number }) : t('alert.view_pr'))}</button></div>` : ''}
  </div>`;
}

function timelineNode(S, node) {
  const d = node.decision;
  const color = HEALTH[node.health]?.c || 'grey';
  const old = d.status === 'superseded';
  const glyph = GLYPH[node.changes[0].kind] || '+';
  const reviewing = d.status === 'under_review';
  return `<li id="decision-${esc(d.id)}" class="tnode${old ? ' old' : ''}${S.focusDecision === d.id ? ' focus' : ''}" style="--c:var(--${color === 'blue' ? 'accent' : color})">
    <span class="date">${esc(d.decided_on || '')}</span><span class="rail"><span class="glyph${old ? ' old' : ''}" aria-hidden="true">${glyph}</span></span>
    <div class="body">
      ${unlessReadOnly(S, `<label class="pick"><input type="checkbox" data-pick="${esc(d.id)}" ${S.selected.includes(d.id) ? 'checked' : ''} ${S.moveBusy ? 'disabled' : ''}> ${esc(t('topics.select', { id: d.id }))}</label>`)}
      <span class="kind">${changeLabel(node)}${old && d.superseded_by ? ` · ${esc(t('topics.superseded_by', { id: d.superseded_by }))}` : ''}</span>
      <p class="stmt">${esc(d.statement)}</p>
      ${reviewing || old ? `<div>${pill(node.health)}</div>` : ''}
      ${d.rationale ? `<p class="why">${esc(d.rationale)}</p>` : ''}
      <p class="who">${esc(d.decided_by)}</p>
      ${d.evidence[0] ? quote(d.evidence[0].quote) : ''}
      <div class="srcs">${d.evidence.map(ev => evidenceButton(S.overview.sources, ev)).join('')}</div>
      ${reviewing && !S.readOnly ? `<div class="actions">${button(t(S.reaffirming === d.id ? 'common.processing' : 'topics.reaffirm'), `data-reaffirm="${esc(d.id)}" ${S.reaffirming ? 'disabled' : ''}`, true)}</div>
        ${S.reaffirmError?.id === d.id ? `<div class="errline">${esc(problemTitle(S.reaffirmError.error))}</div>` : ''}` : ''}
      ${node.alerts.map(a => inlineAlert(S, a)).join('')}
      ${node.events.length ? `<div class="events">${node.events.map(e => `<span>${esc(EVENT_LABEL[e.kind] ? t(EVENT_LABEL[e.kind]) : e.kind)}${e.alert_id ? ` ${esc(t('topics.event_alert', { id: e.alert_id }))}` : ''}${e.note ? `—${esc(e.note)}` : ''}</span>`).join('')}</div>` : ''}
    </div></li>`;
}

function assumptionList(S, tp) {
  const items = tp.assumptions.map(a => {
    const [cls, glyph, label] = ASSUMPTION_STATE[a.state];
    return `<div class="assum"><span class="st ${cls}">${glyph} ${esc(t(label))}</span><span class="tx">${esc(a.text)}</span>
      <span class="of">#${esc(a.decision_id)}${a.on ? ` · ${esc(t(a.state === 'overturned' ? 'assumption.overturned_by' : 'assumption.mentioned_by', { on: a.on }))}` : ''}</span>
      ${a.evidence.length ? `<div class="srcs">${a.evidence.map(ev => evidenceButton(S.overview.sources, ev)).join('')}</div>` : ''}</div>`;
  });
  return `<aside class="aside-col"><h2>${esc(t('topics.premises'))}</h2>${items.join('') || `<p class="quiet">${esc(t('topics.no_premises'))}</p>`}</aside>`;
}

// Selection bar: move the selected decisions to another topic, or split them into a new one.
function selectionBar(S, tp) {
  if (!S.selected.length) return '';
  const targets = S.topics.filter(c => c.topic.id !== tp.topic.id);
  const busy = S.moveBusy ? 'disabled' : '';
  return `<div class="selbar" role="region" aria-label="${esc(t('topics.sel.region'))}">
    <span class="label">${esc(tn('topics.sel.count', S.selected.length))}</span>
    <form id="move-form"><select class="field" name="into" aria-label="${esc(t('topics.sel.move_target'))}" ${busy}><option value="">${esc(t('topics.sel.move_to'))}</option>${targets.map(c =>
      `<option value="${esc(c.topic.id)}" ${c.topic.id === S.moveInto ? 'selected' : ''}>${esc(c.topic.name)}</option>`).join('')}</select>
      <button class="btn small" type="submit" ${!S.moveInto || S.moveBusy ? 'disabled' : ''}>${esc(t(S.moveBusy === 'move' ? 'topics.sel.moving' : 'topics.sel.move'))}</button></form>
    <form id="split-form"><input class="field" name="name" placeholder="${esc(t('topics.sel.new_name'))}" aria-label="${esc(t('topics.sel.new_name'))}" value="${esc(S.splitName)}" ${busy}>
      <button class="btn small" type="submit" ${S.moveBusy ? 'disabled' : ''}>${esc(t(S.moveBusy === 'split' ? 'topics.sel.splitting' : 'topics.sel.split'))}</button></form>
    ${button(t('topics.sel.clear'), `data-pick-clear ${busy}`)}
    ${S.moveError ? `<span class="errline" role="alert">${esc(formError(S.moveError))}</span>` : ''}</div>`;
}

function renderDetail(S) {
  if (S.topicError) {
    return `${backLink()}
      <div class="empty"><p class="errline">${esc(problemTitle(S.topicError))}</p>${button(t('common.retry'), 'data-topic-retry')}</div>`;
  }
  const tp = S.topic;
  if (!tp || tp.topic.id !== S.topicId) {
    return `<div class="skeleton" style="height:36px;width:40%"></div>${'<div class="skeleton" style="height:96px"></div>'.repeat(3)}`;
  }
  if (tp.merged_into) {
    return `${backLink()}
      <div class="empty">${esc(t('topics.merged_into', { name: tp.topic.name }))} <button class="linkbtn" type="button" data-topic="${esc(tp.merged_into.id)}">${esc(tp.merged_into.name)}</button></div>`;
  }
  return `${header(S, tp)}${mergePanel(S, tp)}${selectionBar(S, tp)}
    <div class="detail-grid"><ol class="timeline">${tp.nodes.map(n => timelineNode(S, n)).join('')}</ol>${assumptionList(S, tp)}</div>`;
}

export function renderTopics(S) {
  return S.topicId ? renderDetail(S) : renderList(S);
}

// ---- actions ------------------------------------------------------------------

async function rename(S, form) {
  S.renameValue = String(new FormData(form).get('name') || '');
  S.renameBusy = true;
  S.renameError = null;
  render();
  try {
    await api.renameTopic(S.topicId, S.renameValue);
    S.renaming = false;
  } catch (e) {
    S.renameError = e;  // keep the input for correction; rendered with formError()
  }
  S.renameBusy = false;
  await refresh();
  if (S.renaming) $('#rename-form [name="name"]')?.focus();
}

async function merge(S) {
  S.mergeBusy = true;
  S.mergeError = null;
  render();
  try {
    const target = await api.mergeTopic(S.topicId, S.mergeInto);
    Object.assign(S, { merging: false, mergeInto: '', topicId: target.id, topic: null });
  } catch (e) {
    S.mergeError = e;
  }
  S.mergeBusy = false;
  await refresh();
}

async function moveSelected(S, kind) {
  const total = S.topic?.nodes.length || 0;
  S.moveBusy = kind;
  S.moveError = null;
  render();
  try {
    const target = kind === 'move'
      ? await api.moveDecisions(S.topicId, S.selected, S.moveInto)
      : await api.splitTopic(S.topicId, S.selected, S.splitName);
    const emptied = kind === 'move' && S.selected.length >= total;
    const noticeKey = kind === 'move' ? (emptied ? 'notice.moved_emptied' : 'notice.moved') : 'notice.split';
    S.notice = { key: noticeKey, n: S.selected.length, params: { name: target.name } };
    Object.assign(S, { selected: [], moveInto: '', splitName: '' });
    if (emptied) { S.topicId = null; S.topic = null; }
  } catch (e) {
    S.moveError = e;  // keep the selection and inputs for correction
  }
  S.moveBusy = false;
  await refresh();
}

async function reaffirm(S, decisionId) {
  S.reaffirming = decisionId;
  S.reaffirmError = null;
  render();
  try {
    await api.reaffirm(decisionId, '');
  } catch (e) {
    S.reaffirmError = { id: decisionId, error: e };
  }
  S.reaffirming = null;
  await refresh();
}

export const topicActions = {
  click(btn, S) {
    if (btn.dataset.topic) { openTopic(S, btn.dataset.topic); return true; }
    if (btn.hasAttribute('data-topics-back')) { S.topicId = null; S.topic = null; render(); return true; }
    if (btn.hasAttribute('data-topic-retry')) { S.topicError = null; render(); loadTopic(S, S.topicId); return true; }
    if (btn.hasAttribute('data-rename')) {
      Object.assign(S, { renaming: true, renameValue: S.topic.topic.name, renameError: null, merging: false });
      render();
      $('#rename-form [name="name"]')?.select();
      return true;
    }
    if (btn.hasAttribute('data-rename-cancel')) { S.renaming = false; S.renameError = null; render(); return true; }
    if (btn.hasAttribute('data-pick-clear')) { Object.assign(S, { selected: [], moveInto: '', splitName: '', moveError: null }); render(); return true; }
    if (btn.hasAttribute('data-merge-toggle')) { S.merging = !S.merging; S.mergeInto = ''; S.mergeError = null; render(); return true; }
    if (btn.dataset.reaffirm) { reaffirm(S, btn.dataset.reaffirm); return true; }
    return false;
  },
  input(e, S) {
    if (e.target.id === 'topic-search') { S.topicQuery = e.target.value; $('#topic-rows').innerHTML = topicRows(S); }
    if (e.target.closest('#rename-form')) S.renameValue = e.target.value;
    if (e.target.closest('#split-form')) S.splitName = e.target.value;
  },
  change(e, S) {
    if (e.target.closest('#merge-form') && e.target.name === 'into') { S.mergeInto = e.target.value; render(); }
    if (e.target.closest('#move-form') && e.target.name === 'into') { S.moveInto = e.target.value; render(); }
    if (e.target.dataset.pick !== undefined) {
      const picked = e.target.dataset.pick;
      S.selected = e.target.checked ? [...S.selected, picked] : S.selected.filter(i => i !== picked);
      S.moveError = null;
      render();
    }
  },
  submit(e, S) {
    if (e.target.id === 'rename-form') { e.preventDefault(); if (!S.renameBusy) rename(S, e.target); return true; }
    if (e.target.id === 'merge-form') { e.preventDefault(); if (!S.mergeBusy && S.mergeInto) merge(S); return true; }
    if (e.target.id === 'move-form') { e.preventDefault(); if (!S.moveBusy && S.moveInto) moveSelected(S, 'move'); return true; }
    if (e.target.id === 'split-form') { e.preventDefault(); if (!S.moveBusy) moveSelected(S, 'split'); return true; }
    return false;
  },
  escape(S) {
    if (S.renaming && !S.renameBusy) { S.renaming = false; S.renameError = null; render(); }
    else if (S.merging && !S.mergeBusy) { S.merging = false; render(); }
  },
};
