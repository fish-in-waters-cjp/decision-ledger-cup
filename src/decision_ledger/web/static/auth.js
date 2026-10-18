// Google sign-in (DL-032). The Firebase JS SDK is loaded from Google's CDN at a pinned version, only when the
// server reports DL_AUTH_MODE=firebase; its web config comes from /api/runtime (never hard-coded here).
// DL_AUTH_MODE=fake (local tests only) signs in with "fake:<uid>" tokens and no SDK.
// This module holds no UI text: callers render state through i18n.

const FIREBASE_VERSION = '10.14.1';
const SDK = `https://www.gstatic.com/firebasejs/${FIREBASE_VERSION}`;

export const AUTH = { mode: 'none', ready: false, user: null };  // user: { uid, name, email, photo }

let auth = null;          // firebase Auth instance (firebase mode)
let sdk = null;           // the firebase-auth module
let fakeUid = null;       // fake mode
const listeners = new Set();

const notify = () => listeners.forEach(fn => { try { fn(AUTH.user); } catch { /* a listener's error is its own */ } });

export function onAuthChange(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

// Read /api/runtime and start the matching sign-in mode; resolves once the first auth state is known.
export async function initAuth(fetchImpl = fetch) {
  let rt = { auth_mode: 'none' };
  try {
    const res = await fetchImpl('/api/runtime');
    if (res.ok) rt = await res.json();
  } catch { /* sign-in stays off; the showcase still works */ }
  AUTH.mode = rt.auth_mode || 'none';
  if (AUTH.mode === 'fake') {
    try { fakeUid = sessionStorage.getItem('dl.fakeUid'); } catch { fakeUid = null; }
    AUTH.user = fakeUid ? { uid: fakeUid, name: fakeUid, email: '', photo: '' } : null;
  } else if (AUTH.mode === 'firebase' && rt.firebase?.apiKey) {
    const app = await import(`${SDK}/firebase-app.js`);
    sdk = await import(`${SDK}/firebase-auth.js`);
    auth = sdk.getAuth(app.initializeApp(rt.firebase));
    await new Promise(resolve => {
      let first = true;
      sdk.onIdTokenChanged(auth, u => {
        AUTH.user = u ? { uid: u.uid, name: u.displayName || u.email || '', email: u.email || '', photo: u.photoURL || '' } : null;
        if (first) { first = false; resolve(); } else notify();
      });
    });
  } else {
    AUTH.mode = 'none';
  }
  AUTH.ready = true;
  return AUTH;
}

export const signInAvailable = () => AUTH.mode === 'firebase' || AUTH.mode === 'fake';

// Firebase: Google popup. Fake: the caller passes a test uid. A closed popup resolves to null, not an error.
export async function signIn(testUid) {
  if (AUTH.mode === 'fake') {
    fakeUid = (testUid || '').trim() || null;
    try { if (fakeUid) sessionStorage.setItem('dl.fakeUid', fakeUid); } catch { /* private mode */ }
    AUTH.user = fakeUid ? { uid: fakeUid, name: fakeUid, email: '', photo: '' } : null;
    notify();
    return AUTH.user;
  }
  if (!auth) return null;
  try {
    await sdk.signInWithPopup(auth, new sdk.GoogleAuthProvider());
  } catch (e) {
    if (e?.code === 'auth/popup-closed-by-user' || e?.code === 'auth/cancelled-popup-request') return null;
    throw e;
  }
  return AUTH.user;
}

export async function signOut() {
  if (AUTH.mode === 'fake') {
    fakeUid = null;
    try { sessionStorage.removeItem('dl.fakeUid'); } catch { /* private mode */ }
    AUTH.user = null;
    notify();
    return;
  }
  if (auth) await sdk.signOut(auth);
}

// The bearer token for API calls, or null when signed out. ``refresh`` forces a new token after a 401.
export async function idToken(refresh = false) {
  if (AUTH.mode === 'fake') return fakeUid ? `fake:${fakeUid}` : null;
  if (!auth?.currentUser) return null;
  return auth.currentUser.getIdToken(refresh);
}
