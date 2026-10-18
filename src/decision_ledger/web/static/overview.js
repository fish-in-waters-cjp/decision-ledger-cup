// Overview: IntroHero, NeedsAttention (AlertCard ×4 with TopicChip), resolved list, TopicCard grid.
import { api } from './api.js';
import { esc, pill, button, evidenceButton, quote, HEALTH, snippetAt, topicBadge, topicChip } from './components.js';
import { render, refresh, demoEnabled } from './app.js';
import { t, tn, problemTitle, errorText } from './i18n.js';
import { writeControls } from './readonly.js';

// Close an LLM-written summary with exactly one full stop before a follow-up sentence.
const sentence = text => { const s = String(text || '').trim(); return /[.!?]$/.test(s) ? s : `${s}.`; };

// An API error as one line: translated title plus the backend's detail (data, shown as is).
export const errorLine = e => errorText(e);

// Labels are dictionary keys, translated when rendered (DL-031).
export const ACTION_LABEL = {
  confirm_supersession: 'action.confirm_supersession',
  reject_supersession: 'action.reject_supersession',
  prefer: 'action.prefer',
  reconsider: 'action.reconsider',
  assumption_holds: 'action.assumption_holds',
  mark_exception: 'action.mark_exception',
  reaffirm: 'action.reaffirm',
};

export const KIND_LABEL = { supersession: 'alert.kind.supersession', conflict: 'alert.kind.conflict', assumption: 'alert.kind.assumption', deviation: 'alert.kind.deviation' };
export const KIND_COLOR = { supersession: 'teal', conflict: 'orange', assumption: 'amber', deviation: 'red' };
const KIND_ORDER = ['deviation', 'conflict', 'assumption', 'supersession'];

// The dispositions offered for an alert: [label, action, prefer_decision_id, primary], where label is
// {key, params} translated when rendered. Shared by AlertCard and InlineAlert.
const L = (key, params) => ({ key, params });
export function alertActions(a) {
  if (a.kind === 'supersession') return [[L('action.confirm_supersession'), 'confirm_supersession', '', true], [L('action.reject_supersession'), 'reject_supersession']];
  if (a.kind === 'conflict') {
    const [first, second] = a.decision_ids;
    return [[L('action.use', { id: first }), 'prefer', first, true], [L('action.use_instead', { id: second }), 'prefer', second], [L('action.review_both'), 'reconsider']];
  }
  if (a.kind === 'assumption') return [[L('action.reconsider'), 'reconsider', '', true], [L('action.assumption_holds'), 'assumption_holds']];
  return [[L('action.outdated_review'), 'reconsider']];  // an exception needs a note: see the PR view
}

export const decisionMap = S => Object.fromEntries((S.overview?.decisions || []).map(v => [v.decision.id, v]));
const sameEvidence = (a, b) => a.source_id === b.source_id && a.locator === b.locator;

function intro(S) {
  return `<section class="intro">
    <span class="label">${esc(t('intro.label'))}</span>
    <h2>${esc(t('intro.title'))}</h2>
    <p>${esc(t('intro.body'))}</p>
    <div class="trio">
      <div><b>${esc(t('intro.why.title'))}</b><span>${esc(t('intro.why.body'))}</span></div>
      <div><b>${esc(t('intro.deviation.title'))}</b><span>${esc(t('intro.deviation.body'))}</span></div>
      <div><b>${esc(t('intro.premise.title'))}</b><span>${esc(t('intro.premise.body'))}</span></div>
    </div>
<pre class="lifecycle">${esc(t('intro.lifecycle'))}</pre>
    ${demoEnabled() ? `<div class="actions"><button class="btn primary" type="button" data-demo-step="0">${esc(t('intro.start'))}</button>
      <button class="btn" type="button" data-demo-all>${esc(t('demo.all'))}</button></div>` : ''}
    <p class="quiet">${esc(t('intro.own'))}</p>
  </section>`;
}

function sideBox(S, view, evidence, extraClass, caption) {
  const d = view?.decision;
  if (!d) return '';
  return `<div class="alert-side ${extraClass}"><span class="label">${esc(caption)}</span><div class="stmt">${esc(d.statement)}</div>
    ${evidence.map(ev => `<div class="quote muted">${esc(ev.quote)}</div>${evidenceButton(S.overview.sources, ev)}`).join('')}</div>`;
}

export function actions(S, a, list = alertActions(a)) {
  const busy = S.pending === a.id;
  return writeControls(S, `<div class="actions">${list.map(([label, action, extra = '', primary = false]) =>
    button(busy && S.pendingAction === action + extra ? t('common.processing') : t(label.key, label.params),
      `data-resolve="${esc(a.id)}" data-action="${action}" ${extra ? `data-prefer="${esc(extra)}"` : ''} ${S.pending ? 'disabled' : ''}`, primary)).join('')}</div>
    ${S.cardError[a.id] ? `<div class="errline">${esc(errorLine(S.cardError[a.id]))}</div>` : ''}`);
}

export function alertCard(S, a) {
  const ds = decisionMap(S);
  if (a.kind === 'supersession') {
    const oldV = ds[a.superseded_id], newV = ds[a.superseding_id];
    const newEv = a.evidence.filter(ev => newV?.decision.evidence.some(x => sameEvidence(x, ev)));
    const oldEv = a.evidence.filter(ev => !newEv.includes(ev));
    return `<article class="acard teal"><div class="ah">${topicChip(S.overview.alert_topics?.[a.id])}${pill('outdated')}<b>${esc(t('alert.supersession.title', { new: a.superseding_id, old: a.superseded_id }))}</b></div>
      <div class="ab"><div class="split">
        ${sideBox(S, oldV, oldEv, '', t('alert.side.active', { id: a.superseded_id }))}
        ${sideBox(S, newV, newEv, 'teal', t('alert.side.new', { id: a.superseding_id }))}
      </div>
      <div class="hint">${esc(sentence(a.summary))} ${esc(t('alert.supersession.hint', { id: a.superseded_id }))}</div>
      ${actions(S, a)}</div></article>`;
  }
  if (a.kind === 'conflict') {
    const [first, second] = a.decision_ids;
    const v1 = ds[first], v2 = ds[second];
    const ev2 = a.evidence.filter(ev => v2?.decision.evidence.some(x => sameEvidence(x, ev)));
    const ev1 = a.evidence.filter(ev => !ev2.includes(ev));
    return `<article class="acard orange"><div class="ah">${topicChip(S.overview.alert_topics?.[a.id])}${pill('conflict')}<b>${esc(t('overview.conflict.title'))}</b></div>
      <div class="ab"><div class="split">
        ${sideBox(S, v1, ev1, '', `${t('alert.side.decision', { id: first })}${v1?.decision.decided_on ? ` · ${v1.decision.decided_on}` : ''}`)}
        ${sideBox(S, v2, ev2, 'bad', `${t('alert.side.decision', { id: second })}${v2?.decision.decided_on ? ` · ${v2.decision.decided_on}` : ''}`)}
      </div>
      <div class="hint">${esc(sentence(a.summary))} ${esc(t('alert.conflict.hint'))}</div>
      ${actions(S, a)}</div></article>`;
  }
  if (a.kind === 'assumption') {
    const v = ds[a.decision_ids[0]];
    const assumption = v?.decision.assumptions.find(x => x.text === a.assumption_text);
    // Original side = the assumption's own quotes, or (when those were unverifiable) the decision's evidence.
    const originalPool = assumption?.evidence.length ? assumption.evidence : v?.decision.evidence || [];
    const original = a.evidence.filter(ev => originalPool.some(x => sameEvidence(x, ev)));
    const fresh = a.evidence.filter(ev => !original.includes(ev));
    return `<article class="acard amber"><div class="ah">${topicChip(S.overview.alert_topics?.[a.id])}${pill('at_risk')}<b>${esc(t('alert.assumption.title', { id: a.decision_ids[0], statement: v?.decision.statement || '' }))}</b></div>
      <div class="ab"><div class="split">
        <div class="alert-side"><span class="label">${esc(t('alert.original_premise'))}</span><div class="quote">${esc(a.assumption_text || '')}</div>
          ${original.map(ev => `<div class="muted" style="font-size:13px">${esc(t('alert.original_quote', { quote: ev.quote }))}</div>${evidenceButton(S.overview.sources, ev)}`).join('')}</div>
        <div class="alert-side new"><span class="label">${esc(t('alert.new_info'))}</span>
          ${fresh.map(ev => `${quote(ev.quote)}<div class="muted" style="font-size:13px">${esc(t('alert.mentioned', { speaker: ev.speaker }))}</div>${evidenceButton(S.overview.sources, ev)}`).join('')}</div>
      </div>
      <div class="hint">${esc(sentence(a.summary))} ${esc(t('alert.assumption.hint'))}</div>
      ${actions(S, a)}</div></article>`;
  }
  const v = ds[a.decision_ids[0]];
  const check = S.prChecks.find(c => c.key === a.pr_check_key);
  return `<article class="acard red"><div class="ah">${topicChip(S.overview.alert_topics?.[a.id])}${pill('deviated')}<b>${esc(t('alert.deviation.title', { pr: check?.pr_number ? `PR #${check.pr_number}` : 'PR', id: a.decision_ids[0] }))}</b></div>
    <div class="ab"><div class="stmt">${esc(v?.decision.statement || '')}</div>
    <div class="rule" style="white-space:pre-wrap">${esc(check ? snippetAt(check.diff, a.location) : a.location || '')}</div>
    <div class="muted" style="font-size:14px">${esc(a.summary)}</div>
    <div class="actions"><button class="btn primary small" type="button" data-open-pr="${esc(a.pr_check_key || '')}">${esc(check?.pr_number ? t('alert.view_pr_number', { n: check.pr_number }) : t('alert.view_pr'))}</button>${a.evidence[0] ? evidenceButton(S.overview.sources, a.evidence[0]) : ''}</div></div></article>`;
}

export function resolutionText(a) {
  const kind = KIND_LABEL[a.kind] ? t(KIND_LABEL[a.kind]) : a.kind;
  const ids = a.decision_ids.map(i => `#${i}`).join(', ');
  if (a.status === 'obsolete') return t('resolution.obsolete', { kind, id: a.id, ids });
  const r = a.resolution || {};
  const label = r.action === 'prefer' ? t('action.use', { id: r.prefer_decision_id }) : ACTION_LABEL[r.action] ? t(ACTION_LABEL[r.action]) : r.action;
  return `${tn('resolution.text', a.decision_ids.length, { kind, id: a.id, ids, label })}${r.note ? ` — ${r.note}` : ''}`;
}

// MiniTimeline: the latest points of a topic; "+N" when older decisions are hidden.
function miniTimeline(card) {
  const hidden = card.decision_count - card.timeline.length;
  const pending = new Set(['deviated', 'conflict', 'at_risk', 'outdated']);
  return `<div class="mini" aria-hidden="true">${hidden > 0 ? `<span class="more">+${hidden}</span>` : ''}${card.timeline.map(p => {
    const color = HEALTH[p.health]?.c || 'grey';
    const cls = p.status === 'superseded' ? 'old' : pending.has(p.health) ? 'ring' : '';
    return `<span class="pt" style="--c:var(--${color === 'blue' ? 'accent' : color})"><span class="dot ${cls}"></span><span class="dt">${esc(p.decided_on || `#${p.decision_id}`)}</span></span>`;
  }).join('')}</div>`;
}

function topicCard(card) {
  const [first, ...others] = card.current;
  const now = first
    ? `<div><p class="now">${esc(first.decision.statement)}</p><p class="by">${esc(first.decision.decided_by)}${first.decision.decided_on ? ` · ${esc(first.decision.decided_on)}` : ''}</p>
        ${others.map(v => `<p class="vs">⚡ ${esc(t('topic.card.coexists', { statement: v.decision.statement, who: `${v.decision.decided_by}${v.decision.decided_on ? ` · ${v.decision.decided_on}` : ''}` }))}</p>`).join('')}</div>`
    : `<p class="none">${esc(t('topic.card.none'))}</p>`;
  return `<button class="card ${esc(card.health)}" type="button" role="link" data-topic="${esc(card.topic.id)}">
    <div class="head"><h3>${esc(card.topic.name)}</h3>${topicBadge(card.health)}</div>
    ${now}${miniTimeline(card)}
    <p class="meta">${esc(t('topic.card.premises'))} ${card.assumption_count} · ${esc(t('topic.card.sources'))} ${card.source_count} · ${esc(t('project.pending'))} ${card.pending_count ? `<b>${card.pending_count}</b>` : 0}</p>
  </button>`;
}

export function renderOverview(S) {
  if (!S.topics.length && !S.overview.sources.length) return intro(S);
  const open = [...S.overview.open_alerts].sort((a, b) => KIND_ORDER.indexOf(a.kind) - KIND_ORDER.indexOf(b.kind));
  const done = S.overview.resolved_alerts;
  return `<section>
      <div class="sec-h"><h2>${esc(t('overview.attention'))}</h2><span class="label">${esc(t('overview.open', { n: open.length }))}</span></div>
      ${open.length ? `<div class="alerts">${open.map(a => alertCard(S, a)).join('')}</div>` : `<p class="allclear">${esc(t('overview.all_clear'))}</p>`}
    </section>
    <section>
      <div class="sec-h"><h2>${esc(t('overview.topics'))}</h2><span class="label">${esc(tn('overview.topics_count', S.topics.length))}</span></div>
      ${S.topics.length ? `<div class="cards">${S.topics.map(topicCard).join('')}</div>` : `<div class="empty">${esc(t('overview.empty'))}</div>`}
    </section>
    ${done.length ? `<section class="resolved"><span class="label">${esc(t('overview.resolved'))}</span>${done.map(a => `<div>${esc(resolutionText(a))}</div>`).join('')}</section>` : ''}`;
}

// Returns true when the alert was resolved.
export async function resolveAlert(S, alertId, body) {
  S.pending = alertId;
  S.pendingAction = body.action + (body.prefer_decision_id || '');
  S.notice = null;
  delete S.cardError[alertId];
  render();
  let ok = false;
  try {
    await api.resolve(alertId, body);
    ok = true;
  } catch (e) {
    if (e.status === 409) S.notice = { key: 'notice.already_resolved', params: { id: alertId } };  // the card disappears, so tell the user here
    else S.cardError[alertId] = e;  // rendered with errorLine()
  } finally {
    S.pending = null;
    S.pendingAction = null;
    await refresh();
  }
  return ok;
}

export const overviewActions = {
  click(t, S) {
    if (t.dataset.resolve) {
      resolveAlert(S, t.dataset.resolve, { action: t.dataset.action, prefer_decision_id: t.dataset.prefer || null });
      return true;
    }
    if (t.dataset.openPr !== undefined) { S.prSel = t.dataset.openPr || null; S.view = 'prs'; render(); window.scrollTo({ top: 0 }); return true; }
    return false;
  },
};
