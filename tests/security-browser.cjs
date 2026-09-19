const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const browser = await chromium.connectOverCDP(process.argv[2]);
  const baseUrl = process.argv[3] || 'http://127.0.0.1:8000';
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  const errors = [];
  page.on('pageerror', error => errors.push(error.message));
  await page.addInitScript(() => {
    window.securityCanary = 0;
    window.cspViolations = [];
    document.addEventListener('securitypolicyviolation', event => window.cspViolations.push(event.effectiveDirective));
    sessionStorage.setItem('market-v2:kospi', JSON.stringify({ payload: { index: { id: 'kospi' }, series: [] }, fetchedAt: Date.now() }));
  });
  try {
    const response = await page.goto(baseUrl);
    const headers = response.headers();
    assert(headers['content-security-policy'].includes("script-src 'self'"));
    assert(!headers['content-security-policy'].includes('unsafe-inline'));
    assert.equal(headers['x-frame-options'], 'DENY');
    assert.equal(headers['x-content-type-options'], 'nosniff');
    await page.waitForFunction(() => typeof chart !== 'undefined' && chart && !isLoading);
    assert.equal(await page.locator('#status').textContent(), '완료');
    assert.deepEqual(await page.evaluate(() => window.cspViolations), []);
    const cacheRecovered = await page.evaluate(() => JSON.parse(sessionStorage.getItem('market-v2:kospi')).payload.series.length > 200);
    assert(cacheRecovered);
    const reusedChart = await page.evaluate(() => {
      const before = chart;
      excludeWitching = !excludeWitching;
      render(lastPayload);
      return before === chart;
    });
    assert(reusedChart);
    const injection = await page.evaluate(() => {
      const value = '<img src=x onerror="window.securityCanary=1">';
      const forged = JSON.parse(JSON.stringify(lastPayload));
      forged.followThrough.latest = value;
      forged.summary.oneYearDistributionCount = value;
      renderSignalStrip(forged);
      const tbody = document.createElement('tbody');
      renderRows(tbody, document.createElement('div'), [{ ...lastPayload.latest, date: value }]);
      const result = {
        images: elements.signalStrip?.querySelectorAll('img').length || document.querySelectorAll('.signal-strip img').length,
        cellImages: tbody.querySelectorAll('img').length,
        literalText: tbody.textContent.includes(value),
        canary: window.securityCanary,
      };
      render(lastPayload);
      return result;
    });
    assert.equal(injection.images, 0);
    assert.equal(injection.cellImages, 0);
    assert.equal(injection.literalText, true);
    assert.equal(injection.canary, 0);
    await page.evaluate(() => {
      const script = document.createElement('script');
      script.textContent = 'window.securityCanary = 99';
      document.body.append(script);
      script.remove();
    });
    await page.waitForFunction(() => window.cspViolations.includes('script-src-elem'));
    assert.equal(await page.evaluate(() => window.securityCanary), 0);
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ baseUrl, cacheRecovered, reusedChart, injection, inlineScriptBlocked: true, errors }, null, 2));
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
