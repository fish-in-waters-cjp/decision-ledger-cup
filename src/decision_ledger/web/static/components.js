// Shared UI pieces: escaping, HealthPill, TopicHealthBadge, TopicChip, Button, EvidenceButton, SourceDrawer.
import { api } from './api.js';
import { t, problemTitle } from './i18n.js';

export const $ = sel => document.querySelector(sel);
// The quote characters are written as \u escapes so the regex holds no literal quote (tests/test_i18n.py
// scans string literals and does not parse regex literals).
export const esc = s => String(s ?? '').replace(/[&<>\u0022\u0027]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

// Labels are dictionary keys, translated when rendered (DL-031).
export const HEALTH = {
  deviated: { label: 'health.deviated', c: 'red' },
  conflict: { label: 'health.conflict', c: 'orange' },
  at_risk: { label: 'health.at_risk', c: 'amber' },
  outdated: { label: 'health.outdated', c: 'teal' },
  under_review: { label: 'health.under_review', c: 'blue' },
  healthy: { label: 'health.healthy', c: 'green' },
  superseded: { label: 'health.superseded', c: 'grey' },
};
export const HEALTH_ORDER = ['deviated', 'conflict', 'at_risk', 'outdated', 'under_review', 'healthy', 'superseded'];

export const pill = health => {
  const m = HEALTH[health];
  return `<span class="pill ${m ? m.c : 'grey'}">${esc(m ? t(m.label) : health)}</span>`;
};

// Topic traffic light: shape + text (● Stable / ▲ Premise in doubt / ◆ Conflict or deviation), never colour alone.
export const TOPIC_HEALTH = {
  alert: 'topic.health.alert',
  doubt: 'topic.health.doubt',
  stable: 'topic.health.stable',
};
export const topicBadge = health => `<span class="health ${esc(health)}">${esc(TOPIC_HEALTH[health] ? t(TOPIC_HEALTH[health]) : health)}</span>`;
export const topicChip = topic => topic
  ? `<button class="tchip" type="button" data-topic="${esc(topic.id)}">#${esc(topic.name)}</button>`
  : '';

export const button = (label, attrs = '', primary = false) =>
  `<button class="btn small${primary ? ' primary' : ''}" type="button" ${attrs}>${esc(label)}</button>`;

// Slack locators are epoch timestamps; show them as local "M/D HH:MM".
export function formatLocator(locator) {
  if (!/^\d{9,}(\.\d+)?$/.test(locator)) return locator;
  const d = new Date(parseFloat(locator) * 1000);
  const pad = n => String(n).padStart(2, '0');
  return `${d.getMonth() + 1}/${d.getDate()} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function sourceLabel(sources, sourceId) {
  const s = sources.find(x => x.id === sourceId);
  if (!s) return t('source.fallback', { id: sourceId });
  return [s.occurred_on, s.title].filter(Boolean).join(' ');
}

const KIND_LABEL = { transcript: 'source.kind.transcript', slack: 'source.kind.slack' };

// detailed=true adds the source type and speaker: "type · date title timestamp · speaker" (design-spec EvidenceList).
export function evidenceButton(sources, ev, detailed = false) {
  const kindKey = KIND_LABEL[sources.find(s => s.id === ev.source_id)?.kind];
  const kind = kindKey ? t(kindKey) : '';
  const label = `${detailed && kind ? `${kind} · ` : ''}${sourceLabel(sources, ev.source_id)} ${formatLocator(ev.locator)}${detailed && ev.speaker ? ` · ${ev.speaker}` : ''}`;
  return `<button class="src" type="button" data-src="${esc(ev.source_id)}" data-loc="${esc(ev.locator)}"><span class="ic">▶</span>${esc(label)}</button>`;
}

// ---- unified diff helpers (shared by the deviation card and the PR view) ----

export function parseDiff(diff) {
  const files = [];
  let cur = null, line = 0;
  for (const raw of diff.split('\n')) {
    if (raw.startsWith('diff --git')) { cur = null; continue; }
    if (raw.startsWith('+++ ')) { const p = raw.slice(4).trim().replace(/^b\//, ''); cur = { path: p, rows: [] }; files.push(cur); continue; }
    if (raw.startsWith('--- ') || !cur) continue;
    const m = raw.match(/^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@/);
    if (m) { line = Number(m[1]); continue; }
    if (raw.startsWith('+')) cur.rows.push([line++, '+', raw.slice(1)]);
    else if (raw.startsWith('-')) cur.rows.push(['', '-', raw.slice(1)]);
    else if (!raw.startsWith('\\')) cur.rows.push([line++, ' ', raw.replace(/^ /, '')]);
  }
  return files;
}

export function snippetAt(diff, location) {
  if (!location) return '';
  const cut = location.lastIndexOf(':');
  const path = location.slice(0, cut), ln = Number(location.slice(cut + 1));
  const row = parseDiff(diff).find(f => f.path === path)?.rows.find(([n, k]) => n === ln && k === '+');
  return row ? `${location}\n+ ${row[2]}` : location;
}

export const quote = text => `<span class="quote">${esc(text)}</span>`;

// ---- SourceDrawer ---------------------------------------------------------

export async function openDrawer(sourceId, locator) {
  const dr = $('#drawer');
  dr.innerHTML = `<div class="dh"><div><span class="label">${esc(t('drawer.original'))}</span><h3 style="font-size:18px">${esc(t('common.loading'))}</h3></div>${button(t('common.close'), 'data-close')}</div>`;
  dr.hidden = false;
  $('#scrim').hidden = false;
  dr.querySelector('[data-close]').focus();
  try {
    const s = await api.source(sourceId);
    const kind = t(s.kind === 'slack' ? 'source.kind.slack' : 'source.kind.transcript');
    dr.innerHTML = `<div class="dh"><div><span class="label">${esc(kind)}${s.occurred_on ? ` · ${esc(s.occurred_on)}` : ''}</span><h3 style="font-size:18px">${esc(s.title)}</h3></div>${button(t('common.close'), 'data-close')}</div>
      <div class="note">${esc(t(s.kind === 'slack' ? 'drawer.note.slack' : 'drawer.note.transcript'))}</div>
      <div>${s.segments.map(g => `<div class="seg ${g.locator === locator ? 'hl' : ''}"><span class="ts">${esc(formatLocator(g.locator))}</span><span><span class="who">${esc(g.speaker)}</span>${esc(g.text)}</span></div>`).join('')}</div>`;
    dr.querySelector('[data-close]').focus();
    const hl = dr.querySelector('.seg.hl');
    if (hl) hl.scrollIntoView({ block: 'center' });
  } catch (e) {
    dr.innerHTML = `<div class="dh"><div><span class="label">${esc(t('drawer.original'))}</span><h3 style="font-size:18px">${esc(t('drawer.not_found'))}</h3></div>${button(t('common.close'), 'data-close')}</div><p class="errline">${esc(problemTitle(e))}</p>`;
  }
}

// Raw demo file preview before it is imported (Demo script "View raw content").
export function openRawDrawer(title, kindLabel, text) {
  const dr = $('#drawer');
  dr.innerHTML = `<div class="dh"><div><span class="label">${esc(kindLabel)} · ${esc(t('drawer.not_imported'))}</span><h3 style="font-size:18px">${esc(title)}</h3></div>${button(t('common.close'), 'data-close')}</div>
    <div class="note">${esc(t('drawer.raw_note'))}</div>
    <pre class="rule" style="white-space:pre-wrap;margin:0">${esc(text)}</pre>`;
  dr.hidden = false;
  $('#scrim').hidden = false;
  dr.querySelector('[data-close]').focus();
}

export function closeDrawer() {
  $('#drawer').hidden = true;
  $('#scrim').hidden = true;
}
