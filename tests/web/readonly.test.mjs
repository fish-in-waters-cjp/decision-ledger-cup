// DL-032: on the read-only showcase, alert dispositions and other write controls are a sentence, not buttons that
// answer 403. Run with: node --test tests/web/readonly.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';

// The view modules run in a browser; a permissive stand-in DOM lets them load here (app.js is the entry, as in
// the page, so the circular imports initialise in the same order).
const el = new Proxy(function () {}, {
  get: (_, k) => (k === 'querySelectorAll' ? () => [] : k === Symbol.toPrimitive ? () => '' : k === 'then' ? undefined : el),
  set: () => true,
  apply: () => el,
});
Object.assign(globalThis, {
  document: el, window: el, matchMedia: () => ({ matches: false }), history: { replaceState() {} },
  location: { pathname: '/', hash: '', search: '' }, fetch: async () => { throw new Error('offline'); },
});
const store = { getItem: () => null, setItem() {}, removeItem() {} };
Object.defineProperty(globalThis, 'localStorage', { value: store, configurable: true });
Object.defineProperty(globalThis, 'sessionStorage', { value: store, configurable: true });

await import('../../src/decision_ledger/web/static/app.js');
const { actions } = await import('../../src/decision_ledger/web/static/overview.js');
const { readOnlyKey, writeControls, unlessReadOnly } = await import('../../src/decision_ledger/web/static/readonly.js');
const { EN } = await import('../../src/decision_ledger/web/static/locales/en.js');

const state = readOnly => ({ readOnly, pending: null, pendingAction: null, cardError: {} });
const ALERTS = [
  { id: '1', kind: 'conflict', decision_ids: ['1', '5'] },
  { id: '2', kind: 'assumption', decision_ids: ['2'] },
  { id: '3', kind: 'supersession', decision_ids: ['3', '4'] },
  { id: '4', kind: 'deviation', decision_ids: ['6'] },
];

test('the read-only key follows the showcase and the sign-in mode', () => {
  assert.equal(readOnlyKey(false, true), null);
  assert.equal(readOnlyKey(true, true), 'showcase.actions_sign_in');
  assert.equal(readOnlyKey(true, false), 'sandbox.read_only');
  assert.match(EN['showcase.actions_sign_in'], /sign in with Google/i);
});

test('alert cards on the showcase show the sentence and no disposition buttons', () => {
  for (const a of ALERTS) {
    const html = actions(state('showcase.actions_sign_in'), a);
    assert.doesNotMatch(html, /data-resolve|<button/, a.kind);
    assert.match(html, /class="ro-note"/);
    assert.ok(html.includes(EN['showcase.actions_sign_in']));
  }
});

test('alert cards in my workspace keep their disposition buttons', () => {
  const html = actions(state(null), ALERTS[0]);
  assert.equal((html.match(/data-resolve="1"/g) || []).length, 3);  // Use #1, Use #5 instead, Review both
  assert.match(html, /data-prefer="5"/);
  assert.doesNotMatch(html, /ro-note/);
});

test('other write controls follow the same rule', () => {
  const controls = '<div class="acts"><button data-rename>Rename</button></div>';
  assert.equal(writeControls(state(null), controls), controls);
  assert.doesNotMatch(writeControls(state('showcase.actions_sign_in'), controls), /data-rename/);
  assert.equal(unlessReadOnly(state('showcase.actions_sign_in'), controls), '');
  assert.equal(unlessReadOnly(state(null), controls), controls);
});
