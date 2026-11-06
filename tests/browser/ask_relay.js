// The Ask page's relay (app/relay.py; operator, 2026-10-28), in a browser:
// a connection the browser drops is picked up where it stopped, a question
// that makes no progress and a question the node dropped are SAID, and a
// reload picks the question up again.
//
//   node ask_relay.js DIR   — run by tests/test_ask_relay_browser.py
//
// DIR holds ask.html and results.html, rendered by the app itself. The node
// is faked: each job answers its event requests from a script. One PASS/FAIL
// line per check.
const fs = require('fs');
const { launch } = require('./harness');
const DIR = process.argv[2];
const file = f => fs.readFileSync(DIR + '/' + f, 'utf8');
const N = +file('n.txt');
const sleep = ms => new Promise(r => setTimeout(r, ms));
let failures = 0;
function check(ok, what, detail) {
  console.log((ok ? 'PASS ' : 'FAIL ') + what + (detail !== undefined ? ' — ' + JSON.stringify(detail).slice(0, 600) : ''));
  if (!ok) failures++;
}
const ev = (id, kind, data) => (id ? `id: ${id}\n` : '') + `event: ${kind}\ndata: ${JSON.stringify(data)}\n\n`;
const RESULT = () => ({ html: file('results.html'), search_id: 'sid' });

// the fake node: per job, the bodies its event requests get, in order
const jobs = {};
const started = [];          // POST /ask/relay bodies
const eventReqs = [];        // {id, last}
let nextIds = [];
function script(id, bodies, status = 200) { jobs[id] = { bodies, status, served: 0 }; }

(async () => {
  const h = await launch({ width: 1200, height: 900 });
  const page = h.page;
  const errors = [];
  page.on('pageerror', e => errors.push(e.message));
  h.route(req => {
    const u = new URL(req.url);
    if (u.pathname === '/ask') return req.respond({ status: 200, contentType: 'text/html', body: file('ask.html') });
    if (u.pathname === '/ask/relay' && req.method === 'POST') {
      started.push(JSON.parse(req.postData));
      return req.respond({ status: 200, contentType: 'application/json', body: JSON.stringify({ id: nextIds.shift() }) });
    }
    let m = u.pathname.match(/^\/ask\/relay\/([^/]+)\/events$/);
    if (m) {
      const job = jobs[m[1]];
      // the last event a reconnect names: the page's OWN `?after=`, never the
      // browser's Last-Event-ID (Chromium 154 sends none that is seen —
      // München, 2026-11-02), so the check holds on every browser
      eventReqs.push({ id: m[1], last: u.searchParams.get('after') || null });
      if (!job || job.status !== 200)
        return req.respond({ status: 404, contentType: 'application/json', body: '{"detail":"gone"}' });
      const body = job.bodies[Math.min(job.served++, job.bodies.length - 1)];
      return req.respond({ status: 200, contentType: 'text/event-stream', body: 'retry: 50\n\n' + body });
    }
    m = u.pathname.match(/^\/ask\/relay\/([^/]+)$/);
    if (m && req.method === 'GET') {
      const job = jobs[m[1]];
      return req.respond(job && job.status === 200
        ? { status: 200, contentType: 'application/json', body: '{"state":"done"}' }
        : { status: 404, contentType: 'application/json', body: '{"detail":"gone"}' });
    }
    return req.respond({ status: 200, contentType: 'text/plain', body: '' });
  });
  const until = async (fn, ms = 5000) => {
    const t0 = Date.now();
    while (Date.now() - t0 < ms) { if (await page.evaluate(fn)) return true; await sleep(30); }
    return false;
  };
  const watchNotes = () => page.evaluate(() => {
    window.__notes = []; window.__toasts = [];
    const rn = relayNote; relayNote = function (t, a) { __notes.push(t || ''); return rn(t, a); };
    const ts = toast; toast = function (m) { __toasts.push(m); return ts(m); };
  });

  await page.goto('http://harness/ask');
  await watchNotes();

  // 1. the connection drops mid-question while the node says it is stuck;
  //    the page reconnects naming the last event, and nothing is lost
  nextIds = ['s1'];
  script('s1', [
    ev(null, 'status', { state: 'running' }) + ev(1, 'stuck', { where: 'rerank', quiet_s: 61 }),
    ev(null, 'status', { state: 'running' }) + ev(2, 'moving', { where: 'rerank' })
      + ev(3, 'result', RESULT()) + ev(4, 'end', { state: 'done', reason: '' }),
  ]);
  const ok1 = await page.evaluate(async () => { document.getElementById('q').value = 'whales'; return await search(); });
  const s1 = eventReqs.filter(r => r.id === 's1');
  const notes1 = await page.evaluate(() => __notes);
  check(ok1 === true, '1. the search ends done', ok1);
  check(s1.length === 2 && s1[0].last === null && s1[1].last === '1',
        '1. the reconnect names the last event it saw (?after=)', s1);
  check(notes1.some(t => /no progress for 61 s \(last step: rerank\)/.test(t)), '1. stuck is said, with its step', notes1);
  check(notes1.some(t => /connection to the node lost — reconnecting/.test(t)), '1. the lost connection is said', notes1);
  check(await page.evaluate(() => __toasts.some(t => /reconnected/.test(t))), '1. and its return');
  check(await page.evaluate(n => document.querySelectorAll('#hits .hit').length === n, N), '1. the results are on the page');
  check(await page.evaluate(() => document.getElementById('relay-note').style.display === 'none'),
        '1. the notice is gone once the question is done');
  check(started[0].kind === 'search' && started[0].search.query === 'whales' && started[0].replaces.length === 0,
        '1. the search was started on the node', started[0]);

  // 2. the node drops the question: said, with Run again — which runs it again
  nextIds = ['s2', 's3'];
  script('s2', [ev(null, 'status', { state: 'running' }) + ev(1, 'end', { state: 'dropped', reason: 'nobody listened to it for 120 s' })]);
  script('s3', [ev(1, 'result', RESULT()) + ev(2, 'end', { state: 'done', reason: '' })]);
  const ok2 = await page.evaluate(async () => await search());
  const note2 = await page.evaluate(() => document.getElementById('relay-note').textContent);
  check(ok2 === false && /the search was dropped: nobody listened to it for 120 s/.test(note2), '2. a dropped question is said', note2);
  await page.evaluate(() => [...document.querySelectorAll('#relay-note button')].find(b => b.textContent === 'Run again').click());
  check(await until(() => document.getElementById('relay-note').style.display === 'none'
                     && document.querySelectorAll('#hits .hit').length > 0), '2. Run again runs it again');
  check(started.length === 3 && started[2].kind === 'search', '2. as a new job', started.length);

  // 3. an answer, then a reload: the page picks the question up again
  nextIds = ['s4', 'a1'];
  script('s4', [ev(1, 'result', RESULT()) + ev(2, 'end', { state: 'done', reason: '' })]);
  script('a1', [ev(1, 'delta', { text: 'The whale ' }) + ev(2, 'delta', { text: 'rose [1].' }) + ev(3, 'end', { state: 'done', reason: '' })]);
  await page.evaluate(async () => { document.getElementById('q').value = 'whales rising'; await ask(); });
  const ans = await page.evaluate(() => document.getElementById('answer').textContent);
  check(ans === 'The whale rose [1].', '3. the answer arrives through the relay', ans);
  const askBody = started.find(b => b.kind === 'answer');
  check(askBody && askBody.answer.search_id === 'sid' && askBody.answer.messages[0].content === 'whales rising',
        '3. the answer is written from the page\'s search', askBody);
  await page.reload();
  check(await until(() => document.getElementById('answer').textContent === 'The whale rose [1].'
                     && document.querySelectorAll('#hits .hit').length > 0),
        '3. a reload brings back the results and the answer');
  check(await page.evaluate(() => document.getElementById('q').value === 'whales rising'), '3. and the question');
  check(await page.evaluate(() => document.getElementById('relay-note').style.display === 'none'), '3. with no notice');

  // 4. the node restarted while the page was away, mid-question: said
  await page.evaluate(() => sessionStorage.setItem('ask.relay', JSON.stringify({ q: 'x', search: { id: 'gone', ended: false } })));
  await page.reload();
  check(await until(() => /no longer holds it/.test(document.getElementById('relay-note').textContent)),
        '4. a question the node no longer holds is said',
        await page.evaluate(() => document.getElementById('relay-note').textContent));
  // ...and one that had simply finished and expired is not
  await page.evaluate(() => sessionStorage.setItem('ask.relay', JSON.stringify({ q: 'x', search: { id: 'gone', ended: true } })));
  await page.reload();
  await sleep(300);
  check(await page.evaluate(() => document.getElementById('relay-note').style.display === 'none'),
        '4. a finished question that expired starts the page afresh, silently');

  check(errors.length === 0, 'no page errors', errors);
  await h.close();
  console.log(failures ? `${failures} FAILED` : 'ALL PASSED');
  process.exit(failures ? 1 : 0);
})().catch(e => { console.error(e); process.exit(2); });
