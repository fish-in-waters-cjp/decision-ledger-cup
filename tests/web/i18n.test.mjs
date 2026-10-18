// DL-031: language switching. Run with: node --test tests/web/i18n.test.mjs
import { test, beforeEach } from 'node:test';
import assert from 'node:assert/strict';
import {
  t, tn, getLocale, setLocale, setDictionary, readStoredLocale, loadZh, initLocale, problemTitle, applyStatic, errorText, STORAGE_KEY,
} from '../../src/decision_ledger/web/static/i18n.js';
import { EN } from '../../src/decision_ledger/web/static/locales/en.js';

const ZH = { 'test.hello': '你好，{name}', 'test.items.other': '{n} 個項目', 'problem.not-found': '找不到資料', 'client.http': 'HTTP {status} 錯誤' };

function memoryStorage(initial = {}) {
  const data = { ...initial };
  return { getItem: k => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); }, data };
}
const brokenStorage = { getItem() { throw new Error('blocked'); }, setItem() { throw new Error('blocked'); } };
const fakeDoc = () => ({ documentElement: { lang: 'en' } });

beforeEach(() => {
  setDictionary('en', { ...EN, 'test.hello': 'Hello, {name}', 'test.only-en': 'Only English', 'test.items.one': '{n} item', 'test.items.other': '{n} items' });
  setDictionary('zh-TW', null);
  setLocale('en');
});

test('t interpolates parameters and falls back to English, then to the key', () => {
  assert.equal(t('test.hello', { name: 'Mika' }), 'Hello, Mika');
  setDictionary('zh-TW', ZH);
  setLocale('zh-TW');
  assert.equal(t('test.hello', { name: 'Mika' }), '你好，Mika');
  assert.equal(t('test.only-en'), 'Only English');  // missing in zh-TW -> English, never the key
  assert.equal(t('test.nowhere'), 'test.nowhere');
});

test('tn picks English singular and plural; Chinese has one form', () => {
  assert.equal(tn('test.items', 1), '1 item');
  assert.equal(tn('test.items', 3), '3 items');
  setDictionary('zh-TW', ZH);
  setLocale('zh-TW');
  assert.equal(tn('test.items', 1), '1 個項目');
  assert.equal(tn('test.items', 3), '3 個項目');
});

test('the default is English and bad or blocked storage never throws', () => {
  assert.equal(getLocale(), 'en');
  assert.equal(readStoredLocale(memoryStorage()), 'en');
  assert.equal(readStoredLocale(memoryStorage({ [STORAGE_KEY]: 'fr' })), 'en');
  assert.equal(readStoredLocale(memoryStorage({ [STORAGE_KEY]: 'zh-TW' })), 'zh-TW');
  assert.equal(readStoredLocale(brokenStorage), 'en');
  assert.equal(readStoredLocale(undefined), 'en');
});

test('setLocale persists the choice and refuses Chinese until its dictionary is loaded', () => {
  const storage = memoryStorage();
  assert.equal(setLocale('zh-TW', { storage }), 'en');
  assert.equal(storage.data[STORAGE_KEY], undefined);
  setDictionary('zh-TW', ZH);
  assert.equal(setLocale('zh-TW', { storage }), 'zh-TW');
  assert.equal(storage.data[STORAGE_KEY], 'zh-TW');
  assert.equal(setLocale('en', { storage: brokenStorage }), 'en');  // blocked storage still switches
  assert.equal(setLocale('xx', { storage }), 'en');
});

test('loadZh succeeds, fails and times out without throwing', async () => {
  assert.equal(await loadZh(async () => ({ ok: true, json: async () => ZH })), true);
  setDictionary('zh-TW', null);
  assert.equal(await loadZh(async () => ({ ok: false, json: async () => ({}) })), false);
  assert.equal(await loadZh(async () => { throw new Error('offline'); }), false);
  assert.equal(await loadZh(() => new Promise(() => {}), 20), false);
});

test('initLocale: English never fetches; stored Chinese loads; a failed load stays English and keeps the stored choice', async () => {
  let fetched = 0;
  const fetcher = async () => { fetched += 1; return { ok: true, json: async () => ZH }; };
  const doc = fakeDoc();
  assert.equal(await initLocale({ storage: memoryStorage(), fetcher, doc }), 'en');
  assert.equal(fetched, 0);
  assert.equal(doc.documentElement.lang, 'en');

  const zhDoc = fakeDoc();
  assert.equal(await initLocale({ storage: memoryStorage({ [STORAGE_KEY]: 'zh-TW' }), fetcher, doc: zhDoc }), 'zh-TW');
  assert.equal(zhDoc.documentElement.lang, 'zh-Hant');

  setDictionary('zh-TW', null);
  const storage = memoryStorage({ [STORAGE_KEY]: 'zh-TW' });
  const failDoc = fakeDoc();
  assert.equal(await initLocale({ storage, fetcher: async () => { throw new Error('x'); }, doc: failDoc }), 'en');
  assert.equal(failDoc.documentElement.lang, 'en');
  assert.equal(storage.data[STORAGE_KEY], 'zh-TW');  // try again on the next load
});

test('problemTitle translates known problem and client types and keeps unknown backend titles', () => {
  setDictionary('zh-TW', ZH);
  setLocale('zh-TW');
  assert.equal(problemTitle({ type: '/problems/not-found', title: 'Not found' }), '找不到資料');
  assert.equal(problemTitle({ type: '/client/http', status: 500, title: 'HTTP 500' }), 'HTTP 500 錯誤');
  assert.equal(problemTitle({ type: '/problems/brand-new', title: 'Something new' }), 'Something new');
  setLocale('en');
  assert.equal(problemTitle({ type: '/problems/not-found', title: 'Not found' }), EN['problem.not-found']);
});

test('applyStatic translates text, title, aria-label and data-i18n-set-<attr> attributes', () => {
  setDictionary('zh-TW', { 'a.text': '文字', 'a.title': '標題', 'a.label': '標籤', 'a.more': '展開' });
  setLocale('zh-TW');
  const el = {
    dataset: { i18n: 'a.text', i18nTitle: 'a.title', i18nAriaLabel: 'a.label' },
    attributes: [{ name: 'data-i18n-set-data-expand', value: 'a.more' }],
    set: {}, textContent: '', title: '',
    setAttribute(name, value) { this.set[name] = value; },
  };
  const root = { querySelectorAll: () => [el] };
  applyStatic(root);
  assert.equal(el.textContent, '文字');
  assert.equal(el.title, '標題');
  assert.equal(el.set['aria-label'], '標籤');
  assert.equal(el.set['data-expand'], '展開');
});

test('loadZh downloads once when startup and a click ask together', async () => {
  let calls = 0;
  const fetcher = () => { calls += 1; return new Promise(r => setTimeout(() => r({ ok: true, json: async () => ZH }), 10)); };
  const [a, b] = await Promise.all([loadZh(fetcher), loadZh(fetcher)]);
  assert.equal(a && b, true);
  assert.equal(calls, 1);
});

test('errorText keeps the translated title and appends backend detail as data', () => {
  setDictionary('zh-TW', { ...ZH, 'common.error_detail': '{title}：{detail}' });
  setLocale('zh-TW');
  assert.equal(errorText({ type: '/problems/not-found', title: 'Not found', detail: 'topic T9' }), '找不到資料：topic T9');
  assert.equal(errorText({ type: '/problems/not-found', title: 'Not found' }), '找不到資料');
});

test('data-i18n-set-<attr> never writes event handlers or URLs', () => {
  const el = {
    dataset: {},
    attributes: [
      { name: 'data-i18n-set-onclick', value: 'test.hello' },
      { name: 'data-i18n-set-href', value: 'test.hello' },
      { name: 'data-i18n-set-data-i18n', value: 'test.hello' },
      { name: 'data-i18n-set-aria-description', value: 'test.only-en' },
    ],
    set: {},
    setAttribute(name, value) { this.set[name] = value; },
  };
  applyStatic({ querySelectorAll: sel => (sel === '*' ? [el] : []) });
  assert.deepEqual(Object.keys(el.set), ['aria-description']);
});
