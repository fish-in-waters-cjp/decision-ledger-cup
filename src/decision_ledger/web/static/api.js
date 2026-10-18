// Thin fetch wrapper: JSON in/out, RFC 7807 problems surfaced as ApiError.
// Titles stay English here; pages translate them with i18n.problemTitle(err) from `type` (DL-031).
import { EN } from './locales/en.js';

export class ApiError extends Error {
  constructor(problem) {
    super(problem.title || EN['client.request-failed']);
    this.title = this.message;
    this.status = problem.status;
    this.detail = problem.detail || '';
    this.type = problem.type || '';
    if (problem.file) this.file = problem.file;
  }
}

// Firebase Hosting cuts calls to Cloud Run at 60 s, so requests that run the model (imports, questions,
// PR checks) go straight to the Cloud Run URL from /api/health's api_base. Everything else stays same-origin.
let apiBase = '';
export const setApiBase = url => { apiBase = (url || '').replace(/\/+$/, ''); };
const LONG_REQUEST = /^\/api\/projects\/[^/]+\/(sources|ask|pr-check)$/;
export const baseFor = path => (apiBase && LONG_REQUEST.test(path) ? apiBase : '');

// Signed-in requests (DL-032): auth.js supplies the Firebase ID token; a 401 refreshes it once, then reports expiry.
let tokens = { token: async () => null, onExpired: () => {} };
export const setTokenSource = source => { tokens = { ...tokens, ...source }; };

async function send(url, init, token) {
  const headers = { ...init.headers };
  if (token) headers.authorization = `Bearer ${token}`;
  return fetch(baseFor(url) + url, { ...init, headers });
}

async function request(method, url, body) {
  const init = { method, headers: {} };
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) {
    init.headers['content-type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  let res;
  try {
    const token = await tokens.token(false);
    res = await send(url, init, token);
    if (res.status === 401 && token) {
      const fresh = await tokens.token(true);
      if (fresh) res = await send(url, init, fresh);
      if (res.status === 401) tokens.onExpired();
    }
  } catch (e) {
    // On the direct Cloud Run URL a platform error (503/504 after 300 s) has no CORS headers, so the browser
    // reports a network failure instead of a status: treat it like a slow model call.
    if (baseFor(url)) throw new ApiError({ type: '/client/no-response', title: NO_RESPONSE, status: 0, detail: String(e) });
    throw new ApiError({ type: '/client/network', title: EN['client.network'], status: 0, detail: String(e) });
  }
  const text = await res.text();
  const data = text ? safeJson(text) : null;
  if (!res.ok) throw new ApiError(problemFor(res.status, data));
  return data;
}

const GATEWAY_STATUSES = new Set([502, 503, 504]);
export const SERVER_ERROR = EN['client.server-error'];
export const NO_RESPONSE = EN['client.no-response'];
export const GATEWAY_TIMEOUT = EN['client.gateway-timeout'];

// The problem to show for a failed response. Firebase Hosting cuts Cloud Run calls at 60 s and answers with a
// gateway error page (no problem JSON; the exact status is unverified, so 502, 503 and 504 are all covered).
// API errors always carry problem JSON, so they keep their own title.
export function problemFor(status, data) {
  if (status === 500) return { type: '/client/server-error', title: SERVER_ERROR, status, detail: (data && data.detail) || '' };
  if (data && data.title) return data;
  if (GATEWAY_STATUSES.has(status)) return { type: '/client/gateway-timeout', title: GATEWAY_TIMEOUT, status };
  return { type: '/client/http', title: EN['client.http'].replace('{status}', status), status };
}

function safeJson(text) {
  try { return JSON.parse(text); } catch { return text; }
}

// The current project; every ledger call is scoped to it (one place builds the URL prefix).
let project = null;
export const setProject = id => { project = id; };
const P = path => `/api/projects/${encodeURIComponent(project)}${path}`;
const id = v => encodeURIComponent(v);

export const api = {
  projects: () => request('GET', '/api/projects'),
  createProject: (name, repo) => request('POST', '/api/projects', { name, repo: repo || null }),
  overview: () => request('GET', P('/overview')),
  topics: () => request('GET', P('/topics')),
  topic: topicId => request('GET', P(`/topics/${id(topicId)}`)),
  renameTopic: (topicId, name) => request('POST', P(`/topics/${id(topicId)}/rename`), { name }),
  mergeTopic: (topicId, into) => request('POST', P(`/topics/${id(topicId)}/merge`), { into }),
  moveDecisions: (topicId, decisionIds, into) => request('POST', P(`/topics/${id(topicId)}/move`), { decision_ids: decisionIds, into }),
  splitTopic: (topicId, decisionIds, name) => request('POST', P(`/topics/${id(topicId)}/split`), { decision_ids: decisionIds, name }),
  source: sourceId => request('GET', P(`/sources/${id(sourceId)}`)),
  prChecks: () => request('GET', P('/pr-checks')),
  importSource: form => request('POST', P('/sources'), form),
  prCheck: body => request('POST', P('/pr-check'), body),
  resolve: (alertId, body) => request('POST', P(`/alerts/${id(alertId)}/resolve`), body),
  reaffirm: (decisionId, note) => request('POST', P(`/decisions/${id(decisionId)}/reaffirm`), { note }),
  ask: question => request('POST', P('/ask'), { question }),
  sandbox: sid => request('GET', `/api/sandboxes/${id(sid)}`),
  createSandbox: () => request('POST', '/api/sandboxes'),
  resetSandbox: sid => request('POST', `/api/sandboxes/${id(sid)}/reset`),
  me: () => request('GET', '/api/me'),
  resetMe: () => request('POST', '/api/me/reset'),
  updateProject: (pid, repo) => request('PATCH', `/api/projects/${id(pid)}`, { repo: repo || null }),
  demoManifest: () => request('GET', '/demo/manifest.json'),
  demoFile: async file => {
    const res = await fetch(`/demo/${file}`);
    if (!res.ok) throw new ApiError({ type: '/client/sample-missing', title: EN['client.sample-missing'].replace('{file}', file), status: res.status, file });
    return res.text();
  },
};
