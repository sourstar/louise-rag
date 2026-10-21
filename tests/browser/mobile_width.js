// Nothing wider than a phone's screen (operator, 2026-10-21: "in mobile mode
// the text can exceed the width of the screen, it shall not happen").
//
//   node mobile_width.js BASE_URL WIDTH PATH...   — run by tests/test_mobile_width_browser.py
//
// Loads each page of the running app at WIDTH CSS pixels, waits for its
// scripts to fill it, and lists every element whose box ends beyond the
// screen's right edge, and the page's own scroll width. Prints one PASS/FAIL
// line per page; exit 0 only when every page fits. Uses puppeteer-core where
// the dev image has it, Playwright otherwise.
const fs = require('fs');
const [BASE, WIDTH, ...PATHS] = process.argv.slice(2);
const W = +WIDTH, H = 780;
const TOKEN = process.env.ADMIN_TOKEN || '';   // the admin pages, as after /admin/login
const sleep = ms => new Promise(r => setTimeout(r, ms));

async function open() {
  const exe = process.env.PUPPETEER_EXECUTABLE_PATH || process.env.CHROMIUM
    || ['/usr/bin/chromium', '/usr/bin/chromium-browser'].find(p => fs.existsSync(p));
  try {
    const puppeteer = require('puppeteer-core');
    const browser = await puppeteer.launch({ executablePath: exe, args: ['--no-sandbox', '--disable-gpu'],
                                             headless: /headless-shell/.test(exe || '') ? 'shell' : true });
    return { browser, page: async () => { const p = await browser.newPage();
      await p.setViewport({ width: W, height: H, isMobile: true, hasTouch: true, deviceScaleFactor: 2 });
      if (TOKEN) await p.setCookie({ name: 'admin_token', value: TOKEN, url: BASE });
      return p; } };
  } catch (e) {
    const { chromium } = require('playwright');
    const browser = await chromium.launch(exe ? { executablePath: exe } : {});
    const ctx = await browser.newContext({ viewport: { width: W, height: H }, isMobile: true, hasTouch: true,
                                           deviceScaleFactor: 2 });
    if (TOKEN) await ctx.addCookies([{ name: 'admin_token', value: TOKEN, url: BASE }]);
    return { browser, page: () => ctx.newPage() };
  }
}

(async () => {
  const { browser, page: newPage } = await open();
  let failures = 0;
  for (const path of PATHS) {
    const page = await newPage();
    const errors = [];
    page.on('pageerror', e => errors.push(e.message));
    await page.goto(/^(https?|file):/.test(path) ? path : BASE + path, { waitUntil: 'load' });
    await sleep(1500);                                   // the page's fetches fill it
    // open every <details>, so what a tap reveals is measured too
    await page.evaluate(() => document.querySelectorAll('details').forEach(d => d.open = true));
    await sleep(300);
    const r = await page.evaluate((W) => {
      const over = [];
      for (const el of document.body.querySelectorAll('*')) {
        const b = el.getBoundingClientRect();
        if (!b.width || !b.height) continue;
        const cs = getComputedStyle(el);
        if (cs.position === 'fixed' && cs.display === 'none') continue;
        // a box that scrolls sideways hides text past its edge: as much an
        // overflow as the page's own (a vertical log is fine)
        if (/(auto|scroll)/.test(cs.overflowX) && el.scrollWidth > el.clientWidth + 1) {
          over.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''} scrolls sideways: ${el.scrollWidth}px in ${el.clientWidth}px`);
          continue;
        }
        // clipped on purpose (a bar's segments): not text running off
        let p = el.parentElement, clipped = false;
        while (p && p !== document.body) {
          if (/(hidden|clip)/.test(getComputedStyle(p).overflowX) && p.getBoundingClientRect().right <= W + 1) { clipped = true; break; }
          p = p.parentElement;
        }
        if (clipped || b.right <= W + 1) continue;
        // report the innermost offenders only
        if ([...el.children].some(c => c.getBoundingClientRect().right > W + 1)) continue;
        over.push(`${el.tagName.toLowerCase()}${el.id ? '#' + el.id : ''}${el.className && typeof el.className === 'string' ? '.' + el.className.trim().split(/\s+/).join('.') : ''}`
                  + ` right=${Math.round(b.right)} «${(el.textContent || '').trim().slice(0, 60)}»`);
      }
      return { scroll: document.documentElement.scrollWidth, over: over.slice(0, 15), n: over.length };
    }, W);
    if (process.env.SHOTS)                               // to look at, not to judge
      await page.screenshot({ path: `${process.env.SHOTS}/${path.replace(/^.*\//, '').replace(/\W+/g, '_') || 'page'}.png`, fullPage: true });
    const ok = r.scroll <= W && r.n === 0;
    if (!ok) failures++;
    console.log(`${ok ? 'PASS' : 'FAIL'} ${path} at ${W}px: page ${r.scroll}px wide, ${r.n} element(s) past the edge`
                + (errors.length ? ` (page errors: ${errors.slice(0, 2).join(' | ')})` : ''));
    for (const o of r.over) console.log('    ' + o);
    await page.close();
  }
  await browser.close();
  if (failures === 0) console.log('ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
