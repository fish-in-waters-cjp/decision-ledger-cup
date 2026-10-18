// DL-022 follow-up: a Hosting gateway timeout shows a readable sentence. Run with: node --test tests/web/api.test.mjs
import { test } from 'node:test';
import assert from 'node:assert/strict';
import { problemFor, GATEWAY_TIMEOUT } from '../../src/decision_ledger/web/static/api.js';

test('502, 503 and 504 without a problem body explain the 60 second limit', () => {
  for (const status of [502, 503, 504]) {
    assert.deepEqual(problemFor(status, '<html>Gateway error</html>'), { type: '/client/gateway-timeout', title: GATEWAY_TIMEOUT, status });
    assert.equal(problemFor(status, null).title, GATEWAY_TIMEOUT);
  }
});

test('problem JSON from the API wins, even on 502, 503 and 504', () => {
  for (const status of [502, 503, 504]) {
    const problem = { title: 'LLM unavailable', status, detail: 'x' };
    assert.equal(problemFor(status, problem), problem);
  }
});

test('other statuses keep the generic title', () => {
  assert.deepEqual(problemFor(418, null), { type: '/client/http', title: 'HTTP 418', status: 418 });
  assert.deepEqual(problemFor(404, '<html>'), { type: '/client/http', title: 'HTTP 404', status: 404 });
});

import { baseFor, setApiBase } from '../../src/decision_ledger/web/static/api.js';

test('without an api base every request stays same-origin', () => {
  setApiBase(null);
  assert.equal(baseFor('/api/projects/P1/sources'), '');
  assert.equal(baseFor('/api/projects/abcdefghijklmnop/ask'), '');
});

test('with an api base only the long LLM requests leave the origin', () => {
  setApiBase('https://decision-ledger-cpgrpijo3a-de.a.run.app/');
  const direct = 'https://decision-ledger-cpgrpijo3a-de.a.run.app';
  for (const path of ['/api/projects/P1/sources', '/api/projects/abcdefghijklmnop/ask', '/api/projects/abcdefghijklmnop/pr-check']) {
    assert.equal(baseFor(path), direct, path);
  }
  for (const path of ['/api/projects', '/api/projects/P1/overview', '/api/projects/P1/sources/S1', '/api/sandboxes', '/api/health', '/demo/manifest.json']) {
    assert.equal(baseFor(path), '', path);
  }
  setApiBase(null);
});

import { api, setProject, NO_RESPONSE } from '../../src/decision_ledger/web/static/api.js';

test('a network failure on the direct URL shows the slow-model hint', async () => {
  const realFetch = globalThis.fetch;
  globalThis.fetch = async () => { throw new TypeError('Failed to fetch'); };
  try {
    setApiBase('https://decision-ledger-cpgrpijo3a-de.a.run.app');
    setProject('P1');
    await assert.rejects(api.ask('why?'), e => e.message === NO_RESPONSE);
    setApiBase(null);
    await assert.rejects(api.ask('why?'), e => e.message !== NO_RESPONSE);
  } finally {
    globalThis.fetch = realFetch;
    setApiBase(null);
  }
});

import { SERVER_ERROR } from '../../src/decision_ledger/web/static/api.js';

test('a 500 problem shows the server-error hint, not the timeout hint', () => {
  const p = problemFor(500, { type: '/problems/internal-error', title: 'Internal server error', status: 500, detail: '' });
  assert.equal(p.title, SERVER_ERROR);
  assert.notEqual(p.title, NO_RESPONSE);
  assert.equal(problemFor(500, null).title, SERVER_ERROR);
});


// DL-032: signed-in requests carry the Firebase ID token; one 401 refreshes it, a second reports expiry.
import { setTokenSource } from '../../src/decision_ledger/web/static/api.js';

function stubFetch(statuses) {
  const seen = [];
  globalThis.fetch = async (url, init) => {
    seen.push({ url, auth: init.headers.authorization || null });
    const status = statuses.shift() ?? 200;
    return { ok: status < 400, status, text: async () => (status < 400 ? '[]' : JSON.stringify({ title: 'Sign in', status })) };
  };
  return seen;
}

test('requests carry the bearer token and send none when signed out', async () => {
  setApiBase(null);
  setProject('P1');
  let seen = stubFetch([200]);
  setTokenSource({ token: async () => 'tok-1' });
  await api.overview();
  assert.equal(seen[0].auth, 'Bearer tok-1');
  seen = stubFetch([200]);
  setTokenSource({ token: async () => null });
  await api.overview();
  assert.equal(seen[0].auth, null);
});

test('a 401 refreshes the token once and retries; a second 401 reports expiry', async () => {
  let expired = 0;
  let seen = stubFetch([401, 200]);
  setTokenSource({ token: async refresh => (refresh ? 'tok-new' : 'tok-old'), onExpired: () => { expired += 1; } });
  await api.me();
  assert.deepEqual(seen.map(s => s.auth), ['Bearer tok-old', 'Bearer tok-new']);
  assert.equal(expired, 0);
  seen = stubFetch([401, 401]);
  await assert.rejects(api.me());
  assert.equal(seen.length, 2);
  assert.equal(expired, 1);
});
