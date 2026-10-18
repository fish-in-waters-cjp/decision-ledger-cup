// Write controls on the read-only showcase (DL-032): one sentence instead of buttons that would only answer 403.
// app.js sets S.readOnly to the sentence's dictionary key on the showcase (null elsewhere) before each render.
import { esc } from './components.js';
import { t } from './i18n.js';

export const readOnlyKey = (isShowcase, signIn) => (isShowcase ? (signIn ? 'showcase.actions_sign_in' : 'sandbox.read_only') : null);

// The controls, or the sentence that says why there are none.
export function writeControls(S, html) {
  return S.readOnly ? `<p class="ro-note">${esc(t(S.readOnly))}</p>` : html;
}

// The controls, or nothing (when a sentence nearby already explains).
export const unlessReadOnly = (S, html) => (S.readOnly ? '' : html);
