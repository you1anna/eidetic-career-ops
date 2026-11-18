// Reading-only guide journey; imported into the regular desktop/iPhone smoke suite.
import { mkdirSync } from 'node:fs';
import { join } from 'node:path';

export async function checkSystemMap(page, base, name, check) {
  await page.goto(`${base}/#week`);
  await page.getByRole('link', { name: 'How it works' }).click();
  await page.waitForURL(`${base}/system-map`);
  await page.getByRole('heading', { name: 'How work-ops works', exact: true }).waitFor({ state: 'visible' });
  check(`${name} system map: reachable from workspace`, page.url() === `${base}/system-map` &&
    await page.title() === 'How work-ops works · System map' &&
    await page.getByRole('heading', { name: 'How work-ops works', exact: true }).isVisible());
  const writes = [];
  const onRequest = r => { if (!['GET', 'HEAD'].includes(r.method())) writes.push(r.method()); };
  page.on('request', onRequest);
  try {
    const sections = await page.locator('.jump a').evaluateAll(links => links.map(a => a.getAttribute('href')));
    check(`${name} system map: all section links resolve`, await page.evaluate(ids => ids.every(id => document.querySelector(id)), sections));
    await page.getByRole('link', { name: 'Codex research', exact: true }).click();
    check(`${name} system map: section navigation works`, page.url().endsWith('#research'));
    for (const summary of await page.locator('details > summary').all()) {
      await summary.click();
      check(`${name} system map: disclosure opens — ${await summary.textContent()}`,
        await summary.locator('..').evaluate(el => el.open));
    }
    await page.locator('img').evaluateAll(images => Promise.all(images.map(i => i.decode())));
    check(`${name} system map: all four diagrams render`, await page.locator('img').count() === 4 &&
      await page.locator('img').evaluateAll(images => images.every(i => i.naturalWidth > 0)));
    check(`${name} system map: no page horizontal overflow`, await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth + 1));
    check(`${name} system map: boundaries and clocks visible`,
      await page.getByText('Not implemented: autonomous self-improvement.', { exact: true }).isVisible() &&
      (await page.locator('#schedule').innerText()).includes('Europe/London'));
    const popupPromise = page.waitForEvent('popup');
    await page.locator('#overview .downloads a').first().click();
    const popup = await popupPromise;
    await popup.waitForLoadState();
    check(`${name} system map: readable SVG opens separately`, popup.url() === `${base}/system-map/system-map.svg` &&
      await popup.locator('svg').count() === 1);
    await popup.close();
    const downloadPromise = page.waitForEvent('download');
    await page.locator('#overview a[download]').first().click();
    const download = await downloadPromise;
    check(`${name} system map: PNG download works`, download.suggestedFilename() === 'system-map.png' && !await download.failure());
    check(`${name} system map: reading has no write requests`, writes.length === 0, writes.join(', '));
    await page.getByRole('link', { name: '← Back to workspace', exact: true }).click();
    await page.waitForSelector('#content > *');
    check(`${name} system map: return to workspace`, page.url() === `${base}/#week` && await page.title() === 'work/ops');
  } finally { page.off('request', onRequest); }
}

// Capture only after app-flow assertions; WebKit's screenshot instrumentation can emit CSP warnings.
export async function captureSystemMap(page, base, name) {
    if (process.env.WO_MAP_SCREENSHOTS) {
      mkdirSync(process.env.WO_MAP_SCREENSHOTS, { recursive: true });
      // Default screenshot options: avoid WebKit's CSP-rejected animation/caret style injection.
      await page.goto(`${base}/system-map`);
      await page.screenshot({ path: join(process.env.WO_MAP_SCREENSHOTS, `${name}-map-top.png`), caret: 'initial' });
      await page.locator('#research').scrollIntoViewIfNeeded();
      await page.getByText('What does Codex receive and control?', { exact: true }).click();
      await page.locator('.research-flow').screenshot({ path: join(process.env.WO_MAP_SCREENSHOTS, `${name}-map-research.png`), caret: 'initial' });
    }

}
