const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const { chromium } = require('playwright');

const base = process.env.DASHBOARD_URL || 'http://127.0.0.1:8080';
const artifacts = path.resolve(__dirname, '../artifacts');
fs.mkdirSync(artifacts, {recursive: true});

(async () => {
  const browser = await chromium.launch({headless: true});
  const failures = [];
  const record = [];
  try {
    const page = await browser.newPage({viewport: {width: 1440, height: 1250}});
    page.on('pageerror', error => failures.push(error.message));
    await page.goto(base);
    await page.waitForFunction(() => document.querySelector('#status').textContent === 'Live');
    assert.equal(await page.locator('#error').isVisible(), false);
    const metricResponse = await page.request.get(base + '/api/metrics');
    const metrics = await metricResponse.json();
    assert.equal(metrics.collector.denied_process_reads, 0);
    assert.ok(metrics.memory.total_bytes > 0);
    assert.equal(await page.locator('#hostname').textContent(), metrics.hostname);
    assert.equal(await page.locator('#cores .core').count(), metrics.cpu.cores.length);
    assert.equal(await page.locator('.gpu-card').count(), metrics.gpus.length);
    assert.deepEqual(await page.locator('.gpu-name').allTextContents(), metrics.gpus.map(gpu => gpu.name));
    for (const value of await page.locator('.gpu-value').allTextContents()) assert.match(value, /^(\d+\.\d%|—)$/);
    assert.equal(await page.locator('.filesystem').count(), metrics.disk.filesystems.length);
    fs.writeFileSync(path.join(artifacts, 'live-sample.json'), JSON.stringify(metrics, null, 2));
    record.push('Page matches host identity, GPU inventory, CPU cores and filesystems from the metrics API');

    await page.getByRole('button', {name: 'Pause', exact: true}).click();
    const pausedTime = await page.locator('#updated').textContent();
    await page.waitForTimeout(2500);
    assert.equal(await page.locator('#updated').textContent(), pausedTime);
    assert.equal(await page.locator('#status').textContent(), 'Paused');
    await page.getByRole('button', {name: 'Resume', exact: true}).click();
    await page.waitForFunction(previous => document.querySelector('#updated').textContent !== previous, pausedTime);
    record.push('Pause freezes the display; resume gets fresh data');

    const gpuDetails = page.locator('.process-details').first();
    const diskDetails = page.locator('.extra-disks');
    const hasGpu = await gpuDetails.count() > 0;
    const hasExtraDisks = await diskDetails.count() > 0;
    if (hasGpu) await gpuDetails.locator('summary').click();
    if (hasExtraDisks) await diskDetails.locator('summary').click();
    await page.waitForTimeout(2500);
    if (hasExtraDisks) {
      assert.equal(await diskDetails.getAttribute('open'), '');
      await diskDetails.locator('summary').click();
    }
    if (hasGpu) {
      assert.equal(await gpuDetails.getAttribute('open'), '');
      assert.ok(await gpuDetails.locator('.gpu-processes li').count() > 0);
      await gpuDetails.locator('summary').click();
    }
    record.push('Available GPU process and filesystem details stay open across refreshes');

    const previousCpu = await page.locator('#cpu-value').textContent();
    await page.route('**/api/metrics', route => route.fulfill({status: 503, contentType: 'application/json', body: '{"error":"stale"}'}));
    await page.waitForFunction(() => document.querySelector('#status').textContent === 'Stale');
    assert.equal(await page.locator('#error').isVisible(), true);
    assert.equal(await page.locator('#cpu-value').textContent(), previousCpu);
    await page.unroute('**/api/metrics');
    await page.waitForFunction(() => document.querySelector('#status').textContent === 'Live');
    assert.equal(await page.locator('#error').isVisible(), false);
    record.push('Stale data warning, retained last values and automatic recovery');
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path: path.join(artifacts, 'desktop.png'), fullPage: true});

    await page.setViewportSize({width: 390, height: 844});
    await page.waitForTimeout(100);
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    await page.screenshot({path: path.join(artifacts, 'mobile.png'), fullPage: true});
    await page.setViewportSize({width: 320, height: 740});
    assert.equal(await page.evaluate(() => document.documentElement.scrollWidth > innerWidth), false);
    record.push('Desktop, 390px phone and 320px narrow layout without horizontal overflow');
    assert.deepEqual(failures, []);
    record.push('No browser JavaScript errors');
    fs.writeFileSync(path.join(artifacts, 'browser-checks.json'), JSON.stringify({passed: record, errors: failures}, null, 2));
    console.log(JSON.stringify(record, null, 2));
  } finally { await browser.close(); }
})().catch(error => { console.error(error); process.exitCode = 1; });
