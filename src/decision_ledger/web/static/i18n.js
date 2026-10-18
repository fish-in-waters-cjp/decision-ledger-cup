// Interface language (DL-031): English by default, Traditional Chinese on request.
// English lives in locales/en.js (always available, the fallback for every key); Chinese is fetched
// from locales/zh-TW.json only when needed. Nothing here touches the DOM or storage at import time.
import { EN } from './locales/en.js';

export const LOCALES = ['en', 'zh-TW'];
export const STORAGE_KEY = 'dl.locale';
const HTML_LANG = { en: 'en', 'zh-TW': 'zh-Hant' };
const dictionaries = { en: EN, 'zh-TW': null };
let current = 'en';
let pendingZh = null;

export function getLocale() { return current; }
export function htmlLang(locale = current) { return HTML_LANG[locale] || 'en'; }
export function hasDictionary(locale) { return Boolean(dictionaries[locale]); }
export function setDictionary(locale, dictionary) { dictionaries[locale] = dictionary; }

function lookup(key) {
  const own = dictionaries[current];
  if (own && Object.prototype.hasOwnProperty.call(own, key)) return own[key];
  if (Object.prototype.hasOwnProperty.call(dictionaries.en, key)) return dictionaries.en[key];
  return null;
}

function fill(text, params) {
  return params ? text.replace(/\{(\w+)\}/g, (all, name) => (name in params ? String(params[name]) : all)) : text;
}

// Translate `key`; a key missing in Chinese falls back to English, and only a key missing in English shows itself.
export function t(key, params) {
  const text = lookup(key);
  return fill(text === null ? key : text, params);
}

// Plural: English uses `key.one` for 1 and `key.other` otherwise; Chinese has `key.other` only.
export function tn(key, n, params = {}) {
  const form = current === 'en' && n === 1 ? `${key}.one` : `${key}.other`;
  const text = lookup(form) ?? lookup(`${key}.other`) ?? key;
  return fill(text, { n, ...params });
}

export function readStoredLocale(storage) {
  try {
    const value = storage ? storage.getItem(STORAGE_KEY) : null;
    return LOCALES.includes(value) ? value : 'en';
  } catch {
    return 'en';
  }
}

// Switch locale; Chinese is refused (stays English) until its dictionary is loaded. Storage errors are ignored.
export function setLocale(locale, { storage } = {}) {
  current = LOCALES.includes(locale) && dictionaries[locale] ? locale : 'en';
  if (storage && current === locale) {
    try { storage.setItem(STORAGE_KEY, current); } catch { /* private mode: the choice lasts for this page only */ }
  }
  return current;
}

// Fetch the Chinese dictionary once; false on any failure or after `timeoutMs`.
export async function loadZh(fetcher, timeoutMs = 5000) {
  if (dictionaries['zh-TW']) return true;
  if (pendingZh) return pendingZh;  // one download even if startup and a click ask at the same time
  pendingZh = fetchZh(fetcher, timeoutMs).finally(() => { pendingZh = null; });
  return pendingZh;
}

async function fetchZh(fetcher, timeoutMs) {
  let timer;
  try {
    const timeout = new Promise((_, reject) => { timer = setTimeout(() => reject(new Error('timeout')), timeoutMs); });
    const res = await Promise.race([fetcher('/locales/zh-TW.json'), timeout]);
    if (!res || !res.ok) return false;
    const dictionary = await Promise.race([res.json(), timeout]);
    if (!dictionary || typeof dictionary !== 'object') return false;
    dictionaries['zh-TW'] = dictionary;
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

// Startup: English unless Chinese was stored and its dictionary loads. A failed load keeps the stored
// choice so the next visit tries again.
export async function initLocale({ storage, fetcher, doc, timeoutMs } = {}) {
  const wanted = readStoredLocale(storage);
  current = wanted === 'zh-TW' && fetcher && (await loadZh(fetcher, timeoutMs)) ? 'zh-TW' : 'en';
  if (doc && doc.documentElement) doc.documentElement.lang = htmlLang();
  return current;
}

// Title for an error: backend `/problems/<slug>` and client `/client/<name>` types are translated;
// an unknown type keeps the backend's English title.
export function problemTitle(error) {
  const type = (error && error.type) || '';
  const match = type.match(/^\/(problems|client)\/([a-z0-9-]+)$/);
  const key = match ? `${match[1] === 'problems' ? 'problem' : 'client'}.${match[2]}` : null;
  if (key && lookup(key) !== null) return t(key, { status: error.status, file: error.file, ...(error.params || {}) });
  return (error && error.title) || t('client.request-failed');
}

// A one-line error: the translated title, plus the backend's detail (data, left as sent) when there is one.
export function errorText(error) {
  return error && error.detail ? t('common.error_detail', { title: problemTitle(error), detail: error.detail }) : problemTitle(error);
}

// Attributes data-i18n-set-<attr> may write: text-bearing ones only, never event handlers or URLs.
const SETTABLE = /^(?:title|placeholder|alt|aria-[a-z-]+|data-(?!i18n)[a-z0-9-]+)$/;

// Static markup: data-i18n (text), data-i18n-title, data-i18n-aria-label, and data-i18n-set-<attr>="key"
// for any other attribute (for example CSS `content: attr(data-expand)` labels).
export function applyStatic(root) {
  root.querySelectorAll('[data-i18n]').forEach(el => { el.textContent = t(el.dataset.i18n); });
  root.querySelectorAll('[data-i18n-title]').forEach(el => { el.title = t(el.dataset.i18nTitle); });
  root.querySelectorAll('[data-i18n-aria-label]').forEach(el => { el.setAttribute('aria-label', t(el.dataset.i18nAriaLabel)); });
  root.querySelectorAll('*').forEach(el => {
    for (const attr of Array.from(el.attributes || [])) {
      if (!attr.name.startsWith('data-i18n-set-')) continue;
      const target = attr.name.slice('data-i18n-set-'.length);
      if (SETTABLE.test(target)) el.setAttribute(target, t(attr.value));
    }
  });
}
