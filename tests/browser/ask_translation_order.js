// The Ask page's translation order (operator, 2026-10-21), in a browser:
// the answer box first; then every entry's visible text, in order; then,
// entry by entry, what is behind it (the rest of its passage, the reader's
// first screen); an entry opened meanwhile takes the model at once.
//
//   node ask_translation_order.js DIR   — run by tests/test_ask_translation_order_browser.py
//
// DIR holds ask.html, results.html, frag_<k>.html and screen_<k>.json,
// rendered by the app itself. The translation endpoint is faked: it records
// each request and answers after DELAY ms. One PASS/FAIL line per check.
const fs = require('fs');
const { launch } = require('./harness');
const DIR = process.argv[2];
const file = f => fs.readFileSync(DIR + '/' + f, 'utf8');
const N = +file('n.txt');
let DELAY = 250;
let ANSWER_MISS = false;      // the fake marks the answer missed
const reqs = [];
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failures = 0;
function check(ok, what, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + what + (detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 600) : ''));
  if (!ok) failures++;
}
// what an item is, from its text
function kind(t) {
  t = t.trim();
  if (t.startsWith('The answer cites')) return 'answer';
  if (/^Title \d+$/.test(t)) return 'title';
  if (/^Chapter \d+$/.test(t)) return 'chapter';
  if (/^<\d+\.0>/.test(t) && /<\d+\.23>/.test(t)) return 'whole';   // the passage entire: the reader's chunk
  if (/^<\d+\.0>/.test(t)) return 'head';
  if (/^<\d+\.\d+>/.test(t)) return 'tail';
  return 'screen';            // the reader's text: R<k>.<i> chunks, Part headings, Book titles
}
const hitOf = t => { const m = t.trim().match(/^(?:<|R|Title |Chapter |Book |Part )(\d+)/); return m ? +m[1] : null; };

(async () => {
  const h = await launch({ width: 1400, height: 900 });
  const page = h.page;
  const byReq = new Map();
  h.route(req => {
    const u = new URL(req.url);
    if (u.pathname === '/ask') return req.respond({ status: 200, contentType: 'text/html', body: file('ask.html') });
    if (u.pathname === '/ask/query') return req.respond({ status: 200, contentType: 'text/html', body: file('results.html') });
    let m = u.pathname.match(/^\/read\/c\/(\d+)\/screen$/);
    if (m) return req.respond({ status: 200, contentType: 'application/json', body: file(`screen_${m[1]}.json`) });
    m = u.pathname.match(/^\/read\/c\/(\d+)$/);
    if (m) return req.respond({ status: 200, contentType: 'text/html', body: file(`frag_${m[1]}.html`) });
    if (u.pathname === '/ask/translate/stream') {
      const body = JSON.parse(req.postData);
      const rec = { i: reqs.length, t: Date.now(), lang: body.lang, texts: body.items.map(i => i.text), state: 'open' };
      reqs.push(rec); byReq.set(req.raw, rec);
      const miss = i => ANSWER_MISS && kind(i.text) === 'answer';
      const out = body.items.map(i => JSON.stringify(miss(i) ? { id: i.id, missed: true }
                                                             : { id: i.id, text: body.lang.toUpperCase() + ': ' + i.text })).join('\n')
        + '\n' + JSON.stringify({ done: true, failed: false, missed: body.items.filter(miss).map(i => i.id) }) + '\n';
      setTimeout(() => {
        if (rec.state !== 'open') return;
        Promise.resolve(req.respond({ status: 200, contentType: 'application/x-ndjson', body: out }))
          .then(() => { if (rec.state === 'open') rec.state = 'done'; }).catch(() => { rec.state = 'aborted'; });
      }, DELAY);
      return;
    }
    return req.respond({ status: 200, contentType: 'text/plain', body: '' });
  });
  h.onFailed(req => { const r = byReq.get(req); if (r) r.state = 'aborted'; });
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  const settle = async () => {
    for (let quiet = 0; quiet < 2;) {
      const n = reqs.length; await sleep(DELAY + 400);
      quiet = (!reqs.some(r => r.state === 'open') && reqs.length === n) ? quiet + 1 : 0;
    }
  };

  await page.goto('http://harness/ask');
  await page.evaluate(async () => { document.getElementById('q').value = 'whales'; await search(); });
  // an answer on the page, written in English
  await page.evaluate(() => {
    answerRaw = 'The answer cites [1] and [2]. ' + 'It is long enough to be streamed. '.repeat(20);
    answerLang = 'en'; answerShown = 'en';
    document.getElementById('answer-pane').style.display = 'block';
    document.getElementById('answer').textContent = answerRaw; linkCitations();
  });
  check(reqs.length === 0, 'nothing is translated in "original"', reqs.length);

  // 1. switch to Italian
  await h.select('#lang', 'it');
  await settle();
  const kinds = reqs.map(r => r.texts.map(kind));
  check(JSON.stringify(kinds[0]) === '["answer"]', '1a. the answer box first, alone', kinds[0]);
  const firstBehind = kinds.findIndex(k => k.some(x => x === 'tail' || x === 'screen'));
  const visibleReqs = reqs.slice(1, firstBehind);
  const visibleKinds = visibleReqs.flatMap(r => r.texts.map(kind));
  check(firstBehind > 1 && visibleKinds.every(k => ['title', 'chapter', 'head'].includes(k)),
        '2. then only the entries\' visible text', visibleKinds);
  const count = k => visibleKinds.filter(x => x === k).length;
  check(count('title') === N && count('chapter') === N && count('head') === N,
        '2. every entry\'s visible text (title, chapter, the clamp\'s part) before anything behind it',
        { title: count('title'), chapter: count('chapter'), head: count('head') });
  const order = visibleReqs.flatMap(r => r.texts).map(hitOf);
  check(order.every((k, i) => i === 0 || k >= order[i - 1]), '2. the entries in order', order);
  const behind = reqs.slice(firstBehind);
  const behindHits = behind.map(r => [...new Set(r.texts.map(hitOf))]);
  check(behindHits.every(hs => hs.length === 1) && behindHits.map(hs => hs[0]).every((k, i, a) => i === 0 || k > a[i - 1]),
        '3. then behind each entry, one entry at a time, in order', behindHits);
  check(behind.every(r => !r.texts.map(kind).includes('tail')) && behind.every(r => r.texts.map(kind).includes('whole')),
        '3. the reader\'s screen carries the whole passage: the rest is not translated on its own', behind.map(r => r.texts.map(kind)));
  const one = await page.evaluate(() => [...document.querySelectorAll('#results .snip.tr')].map(s =>
    ({ parted: !!s.dataset.parted, text: s.textContent, orig: s.dataset.orig })));
  check(one.every(o => !o.parted && o.text === 'IT: ' + o.orig),
        'ONE passage, ONE translation: each entry shows the reader\'s whole-passage translation, not its pieces',
        one.filter(o => o.parted || o.text !== 'IT: ' + o.orig).map(o => o.text.slice(0, 60)));
  const all = await page.evaluate(() => pageUnits().every(u => u.dataset.tl === 'it'));
  check(all, 'in the end every unit on the page is Italian');

  // 2. an entry opened after its reader screen was translated: no wait
  const before2 = reqs.length;
  await page.evaluate(n => { const h = document.querySelectorAll('#hits .hit')[n - 1];
    openReader(h.dataset.collection, h.dataset.book, h.dataset.cid, h.dataset.title); }, 3);
  await sleep(150);
  const r2 = await page.evaluate(() => { const [i0, i1] = readerVisible();
    return { tl: rd.units.slice(i0, i1 + 1).map(u => u.dataset.tl || ''), bar: rd.bar.dataset.tl || '' }; });
  check(r2.tl.length > 0 && r2.tl.every(t => t === 'it') && r2.bar === 'it',
        'an opened entry\'s first screen is already Italian, from what was translated ahead', r2);
  const firstAfter = reqs.slice(before2)[0];
  await settle();
  const sentOnOpen = reqs.slice(before2).flatMap(r => r.texts);
  const screen3 = JSON.parse(file('screen_3.json')).texts;
  check(!sentOnOpen.some(t => screen3.includes(t)), 'nothing of it is asked for again', sentOnOpen.filter(t => screen3.includes(t)));
  await page.evaluate(() => closeReader());

  // 3. a click while the page is being translated: the model moves at once
  DELAY = 3000;
  const before3 = reqs.length;
  await h.select('#lang', 'de');
  while (reqs.length === before3) await sleep(20);
  const busy = reqs[before3];
  check(busy.texts.map(kind).join() === 'answer', 'a new language starts with the answer again', busy.texts.map(kind));
  const t0 = Date.now();
  await page.evaluate(n => { const h = document.querySelectorAll('#hits .hit')[n - 1];
    openReader(h.dataset.collection, h.dataset.book, h.dataset.cid, h.dataset.title); }, N);
  while (reqs.length === before3 + 1 && Date.now() - t0 < 2500) await sleep(20);
  const jump = reqs[before3 + 1];
  await sleep(100);
  const vis = await page.evaluate(() => { const [i0, i1] = readerVisible(); return [rd.bar, ...rd.units.slice(i0, i1 + 1)].map(u => u.dataset.orig); });
  check(jump && jump.t - t0 < 1000 && JSON.stringify(jump.texts) === JSON.stringify(vis),
        '1b. a click: the shown text is asked for at once', jump && { ms: jump.t - t0, sent: jump.texts, visible: vis });
  check(busy.state === 'aborted', '1b. the translation in progress is paused', busy.state);
  DELAY = 250;
  await settle();
  const after = reqs.slice(before3 + 2).map(r => r.texts.map(kind));
  const back = after.findIndex(k => k.includes('answer'));
  check(back >= 0 && after.slice(0, back).every(k => k.every(x => x === 'screen')),
        '1b/1d. the reader\'s screens below and above, then the page goes on from the answer', after.slice(0, back + 1));
  const deAll = await page.evaluate(() => pageUnits().every(u => u.dataset.tl === 'de')
    && document.getElementById('answer').textContent.startsWith('DE: '));
  check(deAll, '1d. and finishes the page');

  // 4. "original" restores every passage whole, byte for byte
  await page.evaluate(() => closeReader());
  await h.select('#lang', 'original');
  await sleep(200);
  const back0 = await page.evaluate(() => [...document.querySelectorAll('#results .tr')].every(u =>
    u.textContent === u.dataset.orig && !u.dataset.parted && !u.querySelector('.tr-part'))
    && document.getElementById('answer').textContent === answerRaw);
  check(back0, '"original" restores every unit and the answer byte for byte');

  // 5. an answer the model misses keeps its original AND the page says so
  // (operator, 2026-10-28: every entry translated, the answer not, no word)
  ANSWER_MISS = true;
  await h.select('#lang', 'fr');
  await settle();
  const missed = await page.evaluate(() => ({
    answer: document.getElementById('answer').textContent === answerRaw,
    entries: pageUnits().every(u => u.dataset.tl === 'fr'),
    note: (document.getElementById('lang-note') || { textContent: '' }).textContent }));
  check(missed.answer && missed.entries && /the answer could not be translated/.test(missed.note),
        '5. a missed answer stays in its language, the entries translate, and the note names the answer', missed);
  ANSWER_MISS = false;

  check(errors.length === 0, 'no page errors', errors);
  console.log(`requests: ${reqs.length}`);
  await h.close();
  console.log(failures ? `${failures} FAILED` : 'ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
