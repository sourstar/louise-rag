// What discards work asks first (operator, 2026-11-02: "for the buttons
// reprocess/rescan/remove add a confirmation step with a warning on the
// effect, to prevent accidental clicks").
//
//   node library_confirm.js DIR STATIC  — run by tests/test_library_confirm_browser.py
//
// DIR holds library.html, the Library page as the app rendered it; STATIC
// is the app's static dir. Every request the page makes is answered here
// (tests/browser/harness.js), so nothing reaches a real route: a POST is
// recorded and answered {"ok": true}. Opens a card's Options, clicks
// "reprocess lexical index…" — the warning; Enter, then Cancel: nothing
// posted; then the labelled button: one POST. The same for "remove…", which
// also wants the name typed. Prints one JSON line.
const fs = require('fs');
const { launch } = require('./harness');
const [DIR, STATIC] = process.argv.slice(2);
const BASE = 'http://louise.test';
const sleep = ms => new Promise(r => setTimeout(r, ms));

(async () => {
  const h = await launch({ width: 1100, height: 900 });
  const posts = [];
  h.route(req => {
    const u = new URL(req.url);
    if (req.method === 'POST') {
      posts.push({ path: u.pathname, body: req.postData || '' });
      return req.respond({ status: 200, contentType: 'application/json', body: '{"ok":true,"mode":"retire","retired_toml":"x"}' });
    }
    if (u.pathname === '/admin/library')
      return req.respond({ status: 200, contentType: 'text/html', body: fs.readFileSync(DIR + '/library.html', 'utf8') });
    if (u.pathname.startsWith('/static/')) {
      const f = STATIC + '/' + u.pathname.slice(8);
      if (fs.existsSync(f)) return req.respond({ status: 200, contentType: f.endsWith('.css') ? 'text/css' : 'application/javascript', body: fs.readFileSync(f, 'utf8') });
    }
    return req.respond({ status: 200, contentType: 'text/plain', body: '' });
  });
  const page = h.page;
  const card = '[data-collection="gutenberg"]:not([data-kind])';
  const modal = () => page.evaluate(() => {
    const b = document.getElementById('modal-backdrop');
    return { shown: getComputedStyle(b).display !== 'none', text: b.innerText.replace(/\s+/g, ' ').trim(),
             focused: document.activeElement && document.activeElement.id,
             buttons: [...b.querySelectorAll('.actions button')].map(x => x.textContent.trim()) };
  });
  const waitPosts = async n => { for (let i = 0; i < 40 && posts.length < n; i++) await sleep(100); };
  const load = async () => { await page.goto(BASE + '/admin/library', { waitUntil: 'load' }); await sleep(200); };
  const out = {};

  await load();
  out.closed = await page.evaluate(sel => {
    const opts = document.querySelector(sel + ' details[data-drawer="options"]');
    const btn = [...opts.querySelectorAll('button')].find(b => b.textContent.includes('reprocess'));
    return { open: opts.open, buttonVisible: btn.checkVisibility() };
  }, card);

  // reprocess: the warning, then Enter (Cancel has the focus), then Cancel — nothing posted
  await page.click(card + ' details[data-drawer="options"] > summary');
  await page.click(card + ' button[onclick^="confirmAction(\'reprocess\'"]');
  out.reprocess = await modal();
  await page.keyboard.press('Enter');
  await sleep(300);
  out.afterEnter = { shown: (await modal()).shown, posts: posts.length };
  await page.click(card + ' button[onclick^="confirmAction(\'reprocess\'"]');
  await page.click('#confirm-cancel');
  await sleep(300);
  out.afterCancel = { shown: (await modal()).shown, posts: posts.length };
  // and confirmed: one POST, to the reset route, for the lexical stage
  await page.click(card + ' button[onclick^="confirmAction(\'reprocess\'"]');
  await page.click('#confirm-go');
  await waitPosts(1);
  out.afterConfirm = posts.slice();

  // remove: the warning, Cancel — nothing; then the name typed and confirmed
  await sleep(500);
  await load();
  const before = posts.length;
  await page.click(card + ' details[data-drawer="options"] > summary');
  await page.click(card + ' button[onclick^="removeCollection("]');
  out.remove = await modal();
  await page.click('#confirm-cancel');
  await sleep(300);
  out.removeAfterCancel = { shown: (await modal()).shown, posts: posts.length - before };
  await page.click(card + ' button[onclick^="removeCollection("]');
  await page.click('#rm_confirm');
  await page.keyboard.type('gutenberg');
  await page.click('#confirm-go');
  await waitPosts(before + 1);
  out.removeAfterConfirm = posts.slice(before);

  await h.close();
  console.log(JSON.stringify(out));
})().catch(e => { console.error(e); process.exit(1); });
