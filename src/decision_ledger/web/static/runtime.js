// Runtime facts (DL-020): recorded mode, public mode and the showcase project. Read from /api/health,
// not /healthz, because Google's front end reserves /healthz in production.

import { setApiBase } from './api.js';
import { t } from './i18n.js';
import { esc } from './components.js';

export const RT = { recorded: false, public_mode: false, showcase_project: null, showcase_recorded: false, showcase: 'absent' };

export async function loadRuntime() {
  try {
    const res = await fetch('/api/health');
    if (res.ok) Object.assign(RT, await res.json());
    setApiBase(RT.api_base);
  } catch { /* the banner is informational; the app works without it */ }
  return RT;
}

// "Recorded run" whenever the answers on screen come from recorded LLM responses.
export function isRecorded(pid) {
  return Boolean(RT.recorded || (RT.showcase_recorded && pid && pid === RT.showcase_project));
}

export function renderRecorded(pid) {
  const el = document.getElementById('recorded');
  if (!el) return;
  const on = isRecorded(pid);
  el.hidden = !on;
  el.innerHTML = on ? `<span class="recorded-banner" title="${esc(t('status.recorded.hint'))}">${esc(t('status.recorded'))}</span>` : '';
}
