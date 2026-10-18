// PullRequestView, AskPanel.
import { api } from './api.js';
import { $, esc, pill, button, evidenceButton, quote, parseDiff, snippetAt } from './components.js';
import { render, refresh, replayReady } from './app.js';
import { decisionMap, resolveAlert, resolutionText, errorLine } from './overview.js';
import { t, tn, problemTitle } from './i18n.js';
import { writeControls } from './readonly.js';

// ---- PullRequestView --------------------------------------------------------

function diffViewer(check, flagged) {
  return parseDiff(check.diff).map(f => `<div class="diff"><div class="fh">${esc(f.path)}</div><div class="code"><table><tbody>${f.rows.map(([n, k, txt]) => {
    const flag = k === '+' && flagged.has(`${f.path}:${n}`);
    return `<tr class="${flag ? 'flag' : k === '+' ? 'add' : k === '-' ? 'del' : ''}"><td class="ln">${n}</td><td>${esc(k)} ${esc(txt)}</td></tr>`;
  }).join('')}</tbody></table></div></div>`).join('');
}

function violationBlock(S, check, a) {
  const v = decisionMap(S)[a.decision_ids[0]];
  const d = v?.decision;
  const ev = a.evidence[0];
  const open = a.status === 'open';
  let controls = '';
  if (open && S.exceptionFor === a.id) {
    const sending = S.pending === a.id;
    controls = `<form class="prform" data-exception-form="${esc(a.id)}" style="padding:0;border:0"><label class="label" for="exc-${esc(a.id)}">${esc(t('pr.exception.label'))}</label>
      <textarea id="exc-${esc(a.id)}" name="note" required ${sending ? 'disabled' : ''} placeholder="${esc(t('pr.exception.placeholder'))}">${esc(S.exceptionNotes[a.id] || '')}</textarea>
      <div class="actions"><button class="btn primary small" type="submit" ${S.pending ? 'disabled' : ''}>${esc(t(sending ? 'common.processing' : 'common.submit'))}</button>${button(t('common.cancel'), `data-exception-cancel ${sending ? 'disabled' : ''}`)}</div></form>`;
  } else if (open) {
    controls = writeControls(S, `<div class="actions">${button(t('pr.exception'), `data-exception="${esc(a.id)}"`)}${button(t(S.pending === a.id ? 'common.processing' : 'action.outdated_review'), `data-resolve="${esc(a.id)}" data-action="reconsider" ${S.pending ? 'disabled' : ''}`)}</div>`);
  }
  return `<div style="display:flex;flex-direction:column;gap:12px">
    <div><span class="label">${esc(t('alert.side.decision', { id: a.decision_ids[0] }))}${d ? ` · ${esc(d.decided_by)}${d.decided_on ? ` · ${esc(d.decided_on)}` : ''}` : ''}</span>${!open ? ` <span class="pill grey">${esc(t('overview.resolved'))}</span>` : ''}
      <div class="stmt" style="margin-top:2px">${esc(d?.statement || '')}</div>
      ${d ? `<div class="muted" style="font-size:14px">${esc(t('pr.rationale', { text: d.rationale }))}</div>` : ''}</div>
    <div><span class="label">${esc(t('pr.possible_violation'))}</span><div class="rule" style="white-space:pre-wrap">${esc(snippetAt(check.diff, a.location))}</div>
      <div class="muted" style="font-size:13px;margin-top:4px">${esc(a.summary)}</div></div>
    ${ev ? `<div><span class="label">${esc(t('pr.meeting_quote'))}</span>${quote(ev.quote)}<div style="margin-top:6px">${evidenceButton(S.overview.sources, ev)}</div></div>` : ''}
    <div><a class="deeplink" href="#p=${encodeURIComponent(S.pid)}&d=${encodeURIComponent(a.decision_ids[0])}">${esc(t('pr.open_ledger'))}</a></div>
    ${controls}${S.cardError[a.id] ? `<div class="errline">${esc(errorLine(S.cardError[a.id]))}</div>` : ''}
  </div>`;
}

function botComment(S, check, alerts) {
  const anyOpen = alerts.some(a => a.status === 'open');
  const truncated = check.truncated ? `<div class="muted" style="font-size:13px">${esc(t('pr.truncated'))}</div>` : '';
  const botHead = `<span class="avatar">DL</span><b>decision-ledger</b><span class="muted">${esc(t('pr.bot'))}</span><span class="pill grey">${esc(t('pr.dry_run'))}</span>`;
  if (!check.violated) {
    const first = check.comment.split('\n')[0];
    return `<div class="comment ok"><div class="ch">${botHead}</div>
      <div class="cb">${esc(first)}${truncated}</div></div>`;
  }
  const replies = alerts.filter(a => a.status !== 'open').map(a =>
    `<div class="comment"><div class="ch"><span class="avatar h">${esc((check.author || '?')[0].toUpperCase())}</span><b>${esc(check.author || t('pr.author'))}</b></div><div class="cb">${esc(resolutionText(a))}</div></div>`).join('');
  return `<div class="comment ${anyOpen ? 'warn' : ''}"><div class="ch">${botHead}</div>
    <div class="cb"><b>${esc(tn('pr.violates', alerts.length))}</b>${alerts.map(a => violationBlock(S, check, a)).join('<hr style="border:0;border-top:1px solid var(--line)">')}${truncated}
    <div class="muted" style="font-size:13px">${esc(t('pr.advisory'))}</div></div></div>${replies}`;
}

function submitDiffForm(S) {
  if (S.readOnly) return writeControls(S, '');
  return `<form class="prform" id="diff-form"><span class="label">${esc(t('pr.check_diff'))}</span>
    <div class="upload" style="border:0;margin:0;padding:0"><div class="row"><input class="field" name="title" placeholder="${esc(t('import.title_placeholder'))}"><input class="field" name="pr_number" placeholder="${esc(t('pr.number_placeholder'))}" inputmode="numeric"></div></div>
    <textarea name="diff" required placeholder="${esc(t('pr.diff_placeholder'))}"></textarea>
    <div class="actions"><button class="btn primary small" type="submit" ${S.busy || S.diffBusy ? 'disabled' : ''}>${esc(t(S.diffBusy ? 'pr.checking' : 'pr.check'))}</button></div>
    ${S.diffError ? `<div class="errline">${esc(errorLine(S.diffError))}</div>` : ''}</form>`;
}

// DL-017: replay the built-in demo PRs (server-side fixtures, no GitHub) once the demo sources are in.
function replayBar(S) {
  if (!replayReady()) return '';
  const busy = S.busy || S.replayBusy ? 'disabled' : '';
  return `<div class="actions replay">${button(t(S.replayBusy === 87 ? 'pr.replaying' : 'pr.replay_violating'), `data-replay="87" ${busy}`)}${button(t(S.replayBusy === 88 ? 'pr.replaying' : 'pr.replay_compliant'), `data-replay="88" ${busy}`)}</div>
    ${S.replayError ? `<div class="errline" role="alert">${esc(errorLine(S.replayError))}</div>` : ''}`;
}

async function replay(S, prNumber) {
  if (S.replayBusy) return;
  S.replayBusy = prNumber; S.replayError = null; render();
  try {
    const res = await api.prCheck({ fixture_pr: prNumber });
    S.prSel = res.check.key;
  } catch (err) {
    S.replayError = err;  // rendered with errorLine()
  }
  S.replayBusy = null;
  await refresh();
}

export function renderPullRequests(S) {
  const checks = S.prChecks;
  if (!checks.length) return `${replayBar(S)}<div class="empty">${esc(t('pr.empty'))}</div>${submitDiffForm(S)}`;
  const allAlerts = [...S.overview.open_alerts, ...S.overview.resolved_alerts];
  const alertsOf = c => c.alert_ids.map(id => allAlerts.find(a => a.id === id)).filter(Boolean);
  if (!S.prSel || !checks.some(c => c.key === S.prSel)) {
    // Prefer a PR that still needs attention; otherwise the newest check.
    S.prSel = (checks.find(c => alertsOf(c).some(a => a.status === 'open')) || checks[0]).key;
  }
  const check = checks.find(c => c.key === S.prSel);
  const alerts = alertsOf(check);
  const flagged = new Set(alerts.filter(a => a.status === 'open').map(a => a.location));
  return `${replayBar(S)}<div class="prpick">${checks.map(c => {
      const open = alertsOf(c).some(a => a.status === 'open');
      return `<button type="button" data-pr="${esc(c.key)}" aria-pressed="${c.key === S.prSel}"><span class="mono">${c.pr_number ? `#${c.pr_number}` : esc(t('pr.diff_label'))}</span>${esc(c.title || t('pr.untitled'))}${open ? pill('deviated') : c.violated ? `<span class="pill grey">${esc(t('overview.resolved'))}</span>` : `<span class="pill green">${esc(t('pr.passed'))}</span>`}</button>`;
    }).join('')}</div>
    <div class="prhead"><h3>${esc(check.title || t('pr.untitled_diff'))} ${check.pr_number ? `<span class="muted mono" style="font-weight:400">#${check.pr_number}</span>` : ''}</h3>
      <span class="mono muted">${esc([check.author, check.repo, check.head_sha && check.head_sha.slice(0, 7)].filter(Boolean).join(' · '))}</span></div>
    ${diffViewer(check, flagged)}
    ${botComment(S, check, alerts)}
    ${submitDiffForm(S)}`;
}

// ---- AskPanel ---------------------------------------------------------------

// Data, not interface text: recorded answers are keyed by the question text, so these stay English (DL-031).
const DEFAULT_QUESTIONS = ['Why did the beta launch move?', 'How did the beta launch date change over time?', "Why can't payments code use the database client directly?", 'Is guest checkout allowed in the beta?'];

function answerBubble(S, m) {
  if (m.pending) return `<div class="bubble a"><span class="muted">${esc(t('ask.thinking'))}</span></div>`;
  if (m.error) return `<div class="bubble a"><span class="errline">${esc(problemTitle(m.error))}</span>${button(t('ask.resend'), `data-ask="${esc(m.question)}"`)}</div>`;
  const a = m.answer;
  if (!a.found) return `<div class="bubble a"><p>${esc(a.text)}</p></div>`;
  return `<div class="bubble a"><p>${esc(a.text)}</p>
    <ul class="minitl">${a.citations.map(c => `<li><span class="mono muted">#${esc(c.decision_id)}</span>${esc(c.statement)} ${c.status === 'superseded' ? pill('superseded') : ''}</li>`).join('')}</ul>
    <div class="cites">${a.citations.flatMap(c => c.evidence.map(ev => evidenceButton(S.overview.sources, ev))).join('')}</div></div>`;
}

export function renderAsk(S) {
  const questions = S.manifest?.questions || DEFAULT_QUESTIONS;
  const asking = S.chat.some(m => m.pending);
  return `<div class="chat">${S.chat.length ? S.chat.map(m => `<div class="bubble q">${esc(m.question)}</div>${answerBubble(S, m)}`).join('')
      : `<div class="empty">${esc(t('ask.empty'))}${S.overview.decisions.length ? '' : ` ${esc(t('ask.import_first'))}`}</div>`}</div>
    <div class="chips">${questions.map(q => `<button class="qchip" type="button" data-ask="${esc(q)}" ${asking ? 'disabled' : ''}>${esc(q)}</button>`).join('')}</div>
    <form class="askbar" id="askform"><input id="askq" type="text" placeholder="${esc(t('ask.placeholder'))}" autocomplete="off" aria-label="${esc(t('ask.question'))}"><button class="btn primary" type="submit" ${asking ? 'disabled' : ''}>${esc(t('ask.submit'))}</button></form>`;
}

async function ask(S, question) {
  const m = { question, pending: true };
  S.chat.push(m);
  render();
  try {
    m.answer = await api.ask(question);
  } catch (e) {
    m.error = e;  // rendered with problemTitle()
  }
  m.pending = false;
  render();
  $('#askq')?.focus();
}

// ---- events -------------------------------------------------------------------

export const pageActions = {
  click(btn, S) {
    if (btn.dataset.pr) { S.prSel = btn.dataset.pr; S.exceptionFor = null; render(); return; }
    if (btn.dataset.exception) { S.exceptionFor = btn.dataset.exception; render(); document.getElementById(`exc-${btn.dataset.exception}`)?.focus(); return; }
    if (btn.hasAttribute('data-exception-cancel')) { delete S.exceptionNotes[S.exceptionFor]; S.exceptionFor = null; render(); return; }
    if (btn.dataset.ask) { ask(S, btn.dataset.ask); return; }
    if (btn.dataset.replay) { replay(S, Number(btn.dataset.replay)); return; }
  },
  async submit(e, S) {
    const form = e.target;
    if (form.id === 'askform') {
      e.preventDefault();
      const q = $('#askq').value.trim();
      if (q) ask(S, q);
    } else if (form.dataset.exceptionForm) {
      e.preventDefault();
      // Keep the form (disabled) while sending; on failure the note stays so the user can retry.
      const alertId = form.dataset.exceptionForm;
      S.exceptionNotes[alertId] = new FormData(form).get('note').trim();  // kept per alert, never shared
      const ok = await resolveAlert(S, alertId, { action: 'mark_exception', note: S.exceptionNotes[alertId] });
      if (ok) { S.exceptionFor = null; delete S.exceptionNotes[alertId]; render(); }
    } else if (form.id === 'diff-form') {
      e.preventDefault();
      const fd = new FormData(form);
      const prNumber = parseInt(fd.get('pr_number'), 10);
      S.diffBusy = true; S.diffError = null; render();
      try {
        const res = await api.prCheck({ diff: fd.get('diff'), title: fd.get('title') || '', pr_number: Number.isNaN(prNumber) ? null : prNumber });
        S.prSel = res.check.key;
      } catch (err) {
        S.diffError = err;  // rendered with errorLine()
      }
      S.diffBusy = false;
      await refresh();
    }
  },
};
