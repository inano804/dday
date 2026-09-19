const assert = require('node:assert/strict');
const { chromium } = require(process.env.PLAYWRIGHT_MODULE || 'playwright');

async function main() {
  const browser = await chromium.connectOverCDP(process.argv[2]);
  const baseUrl = process.argv[3] || 'http://127.0.0.1:8000';
  const errors = [];
  const context = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  const page = await context.newPage();
  page.on('pageerror', error => errors.push(error.message));
  const range = () => page.evaluate(() => ({
    min: chart.scales.x.min, max: chart.scales.x.max,
    dates: [chart.data.labels[chart.scales.x.min], chart.data.labels[chart.scales.x.max]],
  }));
  const tableOrder = () => page.evaluate(() => ['recent25Rows', 'oneYearRows'].map(id => {
    const dates = [...document.querySelectorAll(`#${id} tr td:first-child`)].map(cell => cell.textContent);
    return { id, count: dates.length, latest: dates[0], descending: dates.every((date, i) => !i || dates[i - 1] >= date) };
  }));

  try {
    await page.goto(baseUrl);
    await page.waitForFunction(() => typeof chart !== 'undefined' && chart && !isLoading);
    const tables = await tableOrder();
    assert(tables.every(table => table.count > 0 && table.descending));
    assert(await page.evaluate(() => lastPayload.series.every((row, i, all) => !i || all[i - 1].date <= row.date)));
    const original = await range();
    const originalPriceSpan = await page.evaluate(() => chart.scales.price.max - chart.scales.price.min);
    await page.locator('#marketChart').scrollIntoViewIfNeeded();
    let box = await page.locator('#marketChart').boundingBox();
    await page.mouse.move(box.x + box.width * 0.65, box.y + box.height * 0.5);
    const scrollBefore = await page.evaluate(() => scrollY);
    await page.mouse.wheel(0, -600);
    await page.waitForFunction(max => chart.scales.x.max - chart.scales.x.min < max, original.max);
    const wheelIn = await range();
    assert(Math.abs(await page.evaluate(() => scrollY) - scrollBefore) < 2);
    await page.mouse.wheel(0, 600);
    await page.waitForFunction(span => chart.scales.x.max - chart.scales.x.min > span, wheelIn.max - wheelIn.min);
    const wheelOut = await range();
    assert(wheelOut.min >= original.min && wheelOut.max <= original.max);

    for (let i = 0; i < 4; i++) await page.getByRole('button', { name: '확대', exact: true }).click();
    const beforePan = await range();
    await page.locator('#marketChart').scrollIntoViewIfNeeded();
    box = await page.locator('#marketChart').boundingBox();
    await page.mouse.move(box.x + box.width * 0.5, box.y + box.height * 0.5);
    await page.mouse.down();
    await page.mouse.move(box.x + box.width * 0.65, box.y + box.height * 0.5, { steps: 12 });
    await page.mouse.up();
    const afterPan = await range();
    assert.notEqual(afterPan.min, beforePan.min);
    assert(afterPan.min >= 0 && afterPan.max <= original.max);

    const preserved = await page.evaluate(async () => {
      const dates = () => [chart.data.labels[chart.scales.x.min], chart.data.labels[chart.scales.x.max]];
      const before = dates();
      await refreshData();
      return { before, after: dates(), error: elements.errorBox.textContent };
    });
    assert.deepEqual(preserved.after, preserved.before);
    assert.equal(preserved.error, '');
    await page.getByRole('checkbox', { name: '만기일 제외' }).check();
    assert.deepEqual((await range()).dates, preserved.before);

    await page.getByRole('button', { name: '전체 범위로 복원' }).click();
    assert.deepEqual(await range(), original);
    assert(await page.getByRole('button', { name: '축소', exact: true }).isDisabled());
    for (let i = 0; i < 40 && await page.getByRole('button', { name: '확대', exact: true }).isEnabled(); i++) {
      await page.getByRole('button', { name: '확대', exact: true }).click();
    }
    const smallest = await range();
    assert.equal(smallest.max - smallest.min, 4);
    assert(await page.evaluate(() => chart.scales.price.max - chart.scales.price.min) < originalPriceSpan);
    assert(await page.getByRole('button', { name: '확대', exact: true }).isDisabled());
    await page.getByRole('button', { name: '전체 범위로 복원' }).click();
    assert.equal(await page.evaluate(() => chart.scales.price.max - chart.scales.price.min), originalPriceSpan);
    await page.locator('.chart-panel').scrollIntoViewIfNeeded();
    await page.mouse.move(0, 0);
    await page.screenshot({ path: '/tmp/jusik-chart-desktop.png' });

    await page.getByRole('button', { name: 'KOSDAQ', exact: true }).click();
    await page.waitForFunction(() => !isLoading && lastPayload.index.id === 'kosdaq');
    assert.equal((await range()).min, 0);
    assert.equal((await range()).max, await page.evaluate(() => lastPayload.series.length - 1));
    assert((await tableOrder()).every(table => table.descending));

    const mobileContext = await browser.newContext({ viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true });
    const mobile = await mobileContext.newPage();
    mobile.on('pageerror', error => errors.push(error.message));
    mobile.on('requestfailed', request => errors.push(`${request.url()}: ${request.failure()?.errorText}`));
    await mobile.goto(baseUrl);
    await mobile.waitForFunction(() => typeof chart !== 'undefined' && chart && !isLoading);
    await mobile.locator('.chart-panel').scrollIntoViewIfNeeded();
    await mobile.getByRole('button', { name: '확대', exact: true }).click();
    assert(await mobile.evaluate(() => chart.isZoomedOrPanned()));
    assert(await mobile.evaluate(() => document.documentElement.scrollWidth <= innerWidth));
    assert(await mobile.locator('.chart-actions img').evaluateAll(images => images.every(image => image.complete && image.naturalWidth > 0)));
    await mobile.screenshot({ path: '/tmp/jusik-chart-mobile.png' });

    await mobile.getByRole('button', { name: '전체 범위로 복원' }).click();
    await mobile.locator('#marketChart').scrollIntoViewIfNeeded();
    const touchBox = await mobile.locator('#marketChart').boundingBox();
    const cdp = await mobileContext.newCDPSession(mobile);
    const centerX = touchBox.x + touchBox.width / 2;
    const centerY = touchBox.y + touchBox.height / 2;
    const touches = offset => [
      { x: centerX - offset, y: centerY, id: 1 },
      { x: centerX + offset, y: centerY, id: 2 },
    ];
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: touches(30) });
    for (const offset of [40, 50, 60, 70, 80]) {
      await cdp.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: touches(offset) });
    }
    await cdp.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
    await mobile.waitForFunction(() => chart.scales.x.max - chart.scales.x.min < chart.data.labels.length - 1);
    const touchRange = await mobile.evaluate(() => ({ min: chart.scales.x.min, max: chart.scales.x.max }));
    await mobileContext.close();
    assert.deepEqual(errors, []);
    console.log(JSON.stringify({ baseUrl, tables, original, wheelIn, wheelOut, beforePan, afterPan, smallest, touchRange, refreshPreservesRange: true, errors }, null, 2));
  } finally {
    await context.close();
    await browser.close();
  }
}

main().catch(error => { console.error(error); process.exitCode = 1; });
