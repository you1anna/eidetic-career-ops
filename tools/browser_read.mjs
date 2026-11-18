// Read one page in headless Chromium for `wo fetch --browser` (tools/fetch.py): pages whose content
// arrives by script. Attended use only; scheduled ingest reads documented APIs. The browser keeps its
// own headless identity, and a challenge page is reported as one, never worked around.
// Usage: node tools/browser_read.mjs <url>  → one JSON object on stdout.
import { createRequire } from 'node:module';
import { launchBrowser } from './browser_launch.mjs';

const require = createRequire(new URL('../tests/ui/package.json', import.meta.url));
const { chromium } = require('playwright-core');

const url = process.argv[2];
// The Python policy owner supplies fixed API identities. Block them in browser subrequests too.
const endpoints = JSON.parse(process.argv[3]);
if (!endpoints || !Object.keys(endpoints).length) throw new Error('API dispatch rules required');
const browser = await launchBrowser(chromium);
try {
  const context = await browser.newContext({serviceWorkers:'block'});
  let blocked = false;
  await context.route('**/*', route => {
    const u=new URL(route.request().url()), entry=endpoints[u.hostname.toLowerCase()];
    let path=u.pathname;
    for(let n=0;n<3;n++){try{path=decodeURIComponent(path);}catch{break;}}
    path=new URL('/'+path.replace(/^\/+/,''),u.origin).pathname.toLowerCase()+'/';
    if (entry && path.startsWith(entry[1].toLowerCase())) {blocked=true; return route.abort('blockedbyclient');}
    return route.continue();
  });
  const page = await context.newPage();
  const response = await page.goto(url, { waitUntil: 'networkidle', timeout: 30000 });
  if (blocked) throw new Error('Covered API read deferred: use the budgeted VPS HTTP reader');
  const headers = response ? await response.allHeaders() : {};
  process.stdout.write(JSON.stringify({
    status: response ? response.status() : 0,
    url: page.url(),
    headers,
    text: await page.innerText('body').catch(() => ''),
    html: await page.content(),
  }));
} finally {
  await browser.close();
}
