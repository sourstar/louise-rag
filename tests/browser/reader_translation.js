// The Ask page's reader, translated as it is read (operator, 2026-09-18:
// "the visible part first, then the same amount of text on bottom and top,
// then as I scroll, more text is translated in the direction of the
// scrolling"), in headless Chromium.
//
//   node reader_translation.js DIR     — run by tests/test_reader_translation_browser.py
//
// DIR holds ask.html and fragment.html, rendered by the app itself: the
// real Ask page and a real reader fragment of 150 paragraphs. The
// translation endpoint is faked: it records what each request carries and
// answers after DELAY ms. Prints one PASS/FAIL line per check; exit 0 only
// when every check passes.
const puppeteer = require('puppeteer-core');
const fs = require('fs');
const DIR = process.argv[2];
const ASK = fs.readFileSync(DIR + '/ask.html', 'utf8');
const FRAG = fs.readFileSync(DIR + '/fragment.html', 'utf8');
let DELAY = 400;
const reqs = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));
const name = t => { const m = t.match(/^\[(\d+)\]/); return m ? +m[1] : t; };
let failures = 0;
function check(ok, what, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + what + (detail !== undefined ? ' — ' + JSON.stringify(detail) : ''));
  if (!ok) failures++;
}

(async () => {
  const exe = process.env.PUPPETEER_EXECUTABLE_PATH
    || ['/usr/bin/chromium', '/usr/bin/chromium-browser'].find(p => fs.existsSync(p));
  const browser = await puppeteer.launch({ executablePath: exe, args: ['--no-sandbox', '--disable-gpu'],
                                           headless: /headless-shell/.test(exe) ? 'shell' : true });
  const page = await browser.newPage();
  await page.setViewport({ width: 1400, height: 900 });
  await page.setRequestInterception(true);
  const byReq = new Map();
  page.on('request', req => {
    const u = new URL(req.url());
    if (u.pathname === '/ask') return req.respond({ status: 200, contentType: 'text/html', body: ASK });
    if (u.pathname.startsWith('/read/')) return req.respond({ status: 200, contentType: 'text/html', body: FRAG });
    if (u.pathname === '/ask/translate/stream') {
      const body = JSON.parse(req.postData());
      const rec = { i: reqs.length, t: Date.now(), lang: body.lang, units: body.items.map(i => name(i.text)), state: 'open' };
      reqs.push(rec); byReq.set(req, rec);
      const out = body.items.map(i => JSON.stringify({ id: i.id, text: body.lang.toUpperCase() + ': ' + i.text + ' — tradotto con molta cura, parola per parola' })).join('\n')
        + '\n' + JSON.stringify({ done: true, failed: false, missed: [] }) + '\n';
      setTimeout(() => {
        if (rec.state !== 'open') return;
        req.respond({ status: 200, contentType: 'application/x-ndjson', body: out })
          .then(() => { if (rec.state === 'open') rec.state = 'done'; }).catch(() => { rec.state = 'aborted'; });
      }, DELAY);
      return;
    }
    return req.respond({ status: 200, contentType: 'text/plain', body: '' });
  });
  page.on('requestfailed', req => { const r = byReq.get(req); if (r) r.state = 'aborted'; });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));

  const state = () => page.evaluate(() => {
    const [i0, i1] = readerVisible();
    return { i0, i1, names: rd.units.map(u => u.dataset.orig), lens: rd.units.map(u => u.dataset.orig.length),
             tl: rd.units.map(u => u.dataset.tl || ''), note: document.getElementById('reader-lang').textContent,
             bar: document.getElementById('reader-title').textContent };
  });
  const settle = async () => {
    for (let quiet = 0; quiet < 2;) {
      const n = reqs.length; await sleep(DELAY + 400);
      const open = reqs.some(r => r.state === 'open');
      quiet = (!open && reqs.length === n) ? quiet + 1 : 0;
    }
  };
  const scrollBy = async (px, steps, every) => {
    for (let k = 0; k < steps; k++) {
      await page.evaluate(d => { document.getElementById('reader-content').scrollTop += d; }, px);
      await sleep(every);
    }
  };
  const top = i => page.evaluate(i => rd.units[i].getBoundingClientRect().top, i);
  // the rule, computed independently of the page's own readerPlan
  const seqs = (s, a, b) => s.names.slice(a, b + 1).map(name).filter(u => typeof u === 'number');
  const window_ = (s, first, dir, pendingOnly) => {
    let span = 0; for (let i = s.i0; i <= s.i1; i++) span += s.lens[i]; span = Math.max(span, 1200);
    const out = [];
    for (const d of (first ? [1, -1] : [dir])) {
      let n = 0;
      for (let i = d > 0 ? s.i1 + 1 : s.i0 - 1; i >= 0 && i < s.lens.length && n < span; i += d) { n += s.lens[i]; if (!pendingOnly || !s.tl[i]) out.push(name(s.names[i])); }
    }
    return out;
  };

  await page.goto('http://harness/ask');
  await page.select('#lang', 'it');
  await page.evaluate(() => openReader('c', '1', 'b1:60', 'The Lighthouse'));
  await sleep(50);
  const s0 = await state();
  const visible0 = s0.names.slice(s0.i0, s0.i1 + 1).map(name);

  // 1. what is on screen first, with the panel's title
  while (reqs.length < 1) await sleep(20);
  check(reqs[0].lang === 'it', '1st request is in the chosen language', reqs[0].lang);
  check(JSON.stringify(reqs[0].units) === JSON.stringify(['The Lighthouse', ...visible0]),
        '1st request = the title bar + exactly the visible units', { sent: reqs[0].units, visible: visible0 });

  // 2. then as much text again below and above — and the view does not move
  while (reqs.length < 2) await sleep(20);
  const s1 = await state();
  const anchor = await top(s0.i0);
  await settle();
  check(Math.abs((await top(s0.i0)) - anchor) < 1.5, 'text translated ABOVE the view does not move it',
        { before: anchor, after: await top(s0.i0) });
  check(JSON.stringify(reqs[1].units) === JSON.stringify(window_(s1, true, 1, true)),
        '2nd request = the same amount of text below, then above, less what is done', { sent: reqs[1].units, want: window_(s1, true, 1, true) });
  check(reqs.length === 2, 'nothing more while the reader does not scroll', reqs.length);
  let s = await state();
  check(s.note.startsWith('translated from en'), 'the panel says what it did', s.note);
  check(s.bar.startsWith('IT: '), 'the title bar is translated', s.bar);
  const aboveFloor = Math.min(...reqs[1].units.filter(u => typeof u === 'number'));

  // 3. scrolling down: more below, never above
  const before3 = reqs.length, s3 = await state();
  await scrollBy(120, 14, 150);
  await settle();
  const down = reqs.slice(before3).flatMap(r => r.units).filter(u => typeof u === 'number');
  check(down.length > 0 && down.every(u => u > Math.max(...seqs(s3, s3.i0, s3.i1))), 'scrolling down sends only text below', down);
  s = await state();
  check(s.tl.slice(s.i0, s.i1 + 1).every(t => t === 'it'), 'everything on screen is translated');
  const ahead = window_(s, false, 1).filter(u => typeof u === 'number');
  check(ahead.every(u => s.tl[s.names.findIndex(n => name(n) === u)] === 'it'),
        'a screenful ahead is ready before it is reached', ahead);
  check(s.tl.every((t, i) => !t || name(s.names[i]) >= aboveFloor || typeof name(s.names[i]) !== 'number'),
        'nothing above the first window was ever translated');

  // 4. a jump while a request is busy off screen: what is on screen wins
  DELAY = 2500;
  const n4 = reqs.length;
  for (let k = 0; k < 20 && reqs.length === n4; k++) { await scrollBy(80, 1, 0); await sleep(200); }
  const busy = reqs[reqs.length - 1];
  const sb = await state();
  check(busy.i === n4 && busy.units.filter(u => typeof u === 'number').every(u => u > Math.max(...seqs(sb, sb.i0, sb.i1))),
        'a lookahead request is busy off screen', busy.units);
  const t0 = Date.now();
  await page.evaluate(() => { const el = document.getElementById('c130'); document.getElementById('reader-content').scrollTop += el.getBoundingClientRect().top - 200; });
  while (reqs.length === busy.i + 1 && Date.now() - t0 < 2000) await sleep(20);
  const jump = reqs[reqs.length - 1], sj = await state();
  const visJ = sj.names.slice(sj.i0, sj.i1 + 1).map(name);
  check(jump !== busy && Date.now() - t0 < 1000 && JSON.stringify(jump.units) === JSON.stringify(visJ),
        'after a jump the visible text is asked for at once', { ms: jump.t - t0, sent: jump.units, visible: visJ });
  await settle();
  const sa = await state();
  const busyNums = busy.units.filter(u => typeof u === 'number');
  check(busyNums.every(u => sa.tl[sa.names.findIndex(n => name(n) === u)] === ''),
        'the off-screen request gave way: none of it was painted', busyNums);
  DELAY = 400;

  // 5. scrolling up: more above, never below
  const before5 = reqs.length, s5 = await state();
  await scrollBy(-120, 14, 150);
  await settle();
  const up = reqs.slice(before5).flatMap(r => r.units).filter(u => typeof u === 'number');
  check(up.length > 0 && up.every(u => u < Math.min(...seqs(s5, s5.i0, s5.i1))), 'scrolling up sends only text above', up);
  s = await state();
  const sent = new Set(reqs.flatMap(r => r.units).filter(u => typeof u === 'number'));
  console.log(`effort: ${sent.size} of 150 paragraphs sent, ${reqs.length} requests`);

  // 6. another language starts over from the screen; "original" restores all
  const before6 = reqs.length;
  await page.select('#lang', 'de');
  while (reqs.length === before6) await sleep(20);
  const s6 = await state();
  check(reqs[before6].lang === 'de' && JSON.stringify(reqs[before6].units) ===
        JSON.stringify(['The Lighthouse', ...s6.names.slice(s6.i0, s6.i1 + 1).map(name)]),
        'a new language: the screen first', reqs[before6].units);
  await settle();
  check((await page.evaluate(() => rd.units.filter(u => u.textContent.startsWith('IT: ')).length)) === 0,
        'no Italian left after switching to German');
  await page.select('#lang', 'original');
  await sleep(300);
  check(await page.evaluate(() => rd.units.every(u => u.textContent === u.dataset.orig)
        && document.getElementById('reader-title').textContent === 'The Lighthouse'),
        '"original" restores every unit byte for byte');

  check(errors.length === 0, 'no page errors', errors);
  await browser.close();
  console.log(failures ? `${failures} FAILED` : 'ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
