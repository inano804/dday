const assert = require('node:assert/strict');
const { readFileSync } = require('node:fs');
const path = require('node:path');
const { test } = require('node:test');
const vm = require('node:vm');

const script = readFileSync(path.join(__dirname, '../public/dashboard.js'), 'utf8')
  .replace('document.addEventListener("DOMContentLoaded", boot);', '');

function page() {
  const elements = new Map();
  const storage = new Map();
  const context = vm.createContext({
    Intl, Date, Map, AbortController, setTimeout, clearTimeout,
    document: {
      getElementById(id) {
        if (!elements.has(id)) elements.set(id, { style: {}, querySelectorAll: () => [] });
        return elements.get(id);
      },
    },
    sessionStorage: {
      getItem: key => storage.get(key) || null, setItem: (key, value) => storage.set(key, value),
      removeItem: key => storage.delete(key),
    },
    fetch: async () => { throw new Error('Unexpected fetch'); },
    renders: [],
  });
  vm.runInContext(script + '\nrender = payload => renders.push(payload);', context);
  return { context, elements, run: code => vm.runInContext(code, context) };
}

function payload(id, extra = {}) {
  const series = [
    { date: '2026-09-17', close: 100, volume: 1000 },
    { date: '2026-09-18', close: 101, volume: 1200 },
  ];
  return {
    index: { id, name: id }, asOf: '2026-09-18', generatedAt: new Date().toISOString(), series,
    latest: series[1], activeDistributionDays: [], oneYearDistributionDays: [],
    summary: { activeDistributionCount: 0, recent25DistributionCount: 0, oneYearDistributionCount: 0,
      netPressure: 0, accumulationRecentCount: 0, stallingCount: 0 }, ...extra,
  };
}

test('revisiting an index renders from memory without another request', async () => {
  const p = page();
  let requests = 0;
  p.context.fetch = async () => { requests++; return { ok: true, json: async () => payload('kospi') }; };
  await p.run('loadData("kospi")');
  await p.run('loadData("kospi")');
  assert.equal(requests, 1);
  assert.equal(p.context.renders.length, 2);
});

test('refresh re-fetches the source with a unique URL and no-store', async () => {
  const p = page();
  const requests = [];
  p.context.fetch = async (url, options) => {
    requests.push({ url, options });
    return { ok: true, json: async () => payload('kospi') };
  };
  await p.run('loadData("kospi")');
  await p.run('refreshData()');
  assert.equal(requests.length, 2);
  assert.match(requests[1].url, /refresh=1&_=/);
  assert.equal(requests[1].options.cache, 'no-store');
});

test('a late previous response cannot replace the selected index', async () => {
  const p = page();
  let finishFirst;
  let firstSignal;
  p.context.fetch = (url, options) => {
    if (url.includes('kospi')) {
      firstSignal = options.signal;
      return new Promise(resolve => { finishFirst = () => resolve({ ok: true, json: async () => payload('kospi') }); });
    }
    return Promise.resolve({ ok: true, json: async () => payload('kosdaq') });
  };
  const first = p.run('loadData("kospi")');
  await p.run('loadData("kosdaq")');
  assert.equal(firstSignal.aborted, true);
  finishFirst();
  await first;
  assert.deepEqual(p.context.renders.map(item => item.index.id), ['kosdaq']);
  assert.equal(p.elements.get('status').textContent, '완료');
});

test('stale browser data renders immediately and survives a network error', async () => {
  const p = page();
  p.context.saved = payload('nasdaq', { generatedAt: new Date(Date.now() - 3600000).toISOString() });
  p.run('rememberPayload("nasdaq", saved)');
  p.context.fetch = async () => { throw new Error('Offline'); };
  await p.run('loadData("nasdaq")');
  assert.equal(p.context.renders.length, 1);
  assert.equal(p.elements.get('status').textContent, '저장된 데이터');
  assert.equal(p.elements.get('refreshButton').disabled, false);
});

test('an older server fallback never replaces newer browser data', async () => {
  const p = page();
  p.context.saved = payload('kospi');
  p.run('rememberPayload("kospi", saved)');
  p.context.fetch = async () => ({ ok: true, json: async () => payload('kospi', {
    stale: true, asOf: '2026-07-30', warning: 'Saved data',
  }) });
  await p.run('loadData("kospi", { forceRefresh: true })');
  const displayed = p.context.renders.at(-1);
  assert.equal(displayed.asOf, '2026-09-18');
  assert.equal(displayed.stale, true);
  assert.equal(p.elements.get('errorBox').textContent, 'Saved data');
});

test('slow fetches time out and restore the refresh button', async () => {
  const p = page();
  p.context.setTimeout = callback => setTimeout(callback, 5);
  p.context.fetch = (_, { signal }) => new Promise((resolve, reject) => {
    signal.addEventListener('abort', () => reject(new Error('Aborted')));
  });
  await p.run('loadData("kospi")');
  assert.match(p.elements.get('errorBox').textContent, /응답이 지연/);
  assert.equal(p.elements.get('refreshButton').disabled, false);
});

test('a malformed saved payload is discarded and re-fetched', async () => {
  const p = page();
  p.context.sessionStorage.setItem('market-v2:kospi', JSON.stringify({
    payload: { index: { id: 'kospi' }, series: [] }, fetchedAt: Date.now(),
  }));
  let requests = 0;
  p.context.fetch = async () => {
    requests++;
    return { ok: true, json: async () => payload('kospi') };
  };
  await p.run('loadData("kospi")');
  assert.equal(requests, 1);
  assert.equal(p.context.renders.length, 1);
  assert.equal(p.elements.get('status').textContent, '완료');
});

test('malformed source data is not cached', async () => {
  const p = page();
  p.context.fetch = async () => ({ ok: true, json: async () => ({ index: { id: 'kospi' }, series: [] }) });
  await p.run('loadData("kospi")');
  assert.equal(p.context.renders.length, 0);
  assert.equal(p.context.sessionStorage.getItem('market-v2:kospi'), null);
  assert.equal(p.elements.get('status').textContent, '오류');
});
