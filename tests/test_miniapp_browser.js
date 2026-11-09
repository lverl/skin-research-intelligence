'use strict';

// Real shipped HTML/CSS/JS in Chrome at 375px. Every HTTP request is intercepted.
// The temporary Chrome profile and screenshot are retained; no user files are removed.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const {spawn} = require('node:child_process');

const root = path.resolve(__dirname, '..');
const html = fs.readFileSync(path.join(root, 'web/miniapp.html'), 'utf8');
const chrome = process.env.CHROME_PATH || [
  'C:/Program Files/Google/Chrome/Application/chrome.exe',
  'C:/Program Files (x86)/Google/Chrome/Application/chrome.exe'
].find(candidate => fs.existsSync(candidate));

class CDP {
  constructor(socket) {
    this.socket = socket;
    this.next = 0;
    this.pending = new Map();
    this.listeners = new Map();
    socket.addEventListener('message', event => {
      const message = JSON.parse(event.data);
      if (message.id) {
        const pending = this.pending.get(message.id);
        if (!pending) return;
        clearTimeout(pending.timer);
        this.pending.delete(message.id);
        if (message.error) pending.reject(new Error(JSON.stringify(message.error)));
        else pending.resolve(message.result || {});
      } else {
        for (const listener of this.listeners.get(message.method) || []) listener(message);
      }
    });
  }
  send(method, params = {}, sessionId) {
    const id = ++this.next;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {this.pending.delete(id); reject(new Error('CDP timed out: ' + method));}, 10000);
      this.pending.set(id, {resolve, reject, timer});
      this.socket.send(JSON.stringify({id, method, params, ...(sessionId ? {sessionId} : {})}));
    });
  }
  on(method, listener) {
    const listeners = this.listeners.get(method) || [];
    listeners.push(listener);
    this.listeners.set(method, listeners);
  }
}

async function launch() {
  const scratch = path.join(root, '_temp/scratch');
  fs.mkdirSync(scratch, {recursive: true});
  const profile = fs.mkdtempSync(path.join(scratch, 'reader-ui001-chrome-'));
  const child = spawn(chrome, [
    '--headless=new', '--disable-gpu', '--remote-debugging-port=0', '--remote-debugging-address=127.0.0.1',
    '--user-data-dir=' + profile, '--no-first-run', '--no-default-browser-check',
    '--disable-background-networking', '--disable-component-update', '--disable-default-apps', '--disable-sync',
    '--disable-extensions', '--metrics-recording-only', '--safebrowsing-disable-auto-update',
    '--disable-features=Translate,MediaRouter,OptimizationHints', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1',
    'about:blank'
  ], {windowsHide: true, stdio: ['ignore', 'ignore', 'pipe']});
  const endpoint = await new Promise((resolve, reject) => {
    let output = '';
    const timer = setTimeout(() => {child.kill(); reject(new Error('Chrome did not open local CDP'));}, 20000);
    child.once('error', error => {clearTimeout(timer); reject(error);});
    child.once('exit', code => {clearTimeout(timer); reject(new Error('Chrome exited before CDP: ' + code));});
    child.stderr.on('data', chunk => {
      output += chunk.toString();
      const match = output.match(/DevTools listening on (ws:\/\/127\.0\.0\.1:\d+\/devtools\/browser\/[^\s]+)/);
      if (match) {clearTimeout(timer); resolve(match[1]);}
    });
  });
  const socket = new WebSocket(endpoint);
  await new Promise((resolve, reject) => {socket.addEventListener('open', resolve, {once: true}); socket.addEventListener('error', reject, {once: true});});
  return {child, socket, cdp: new CDP(socket), profile};
}

function fixtureBootstrap() {
  window.Telegram = {WebApp: {initData: 'offline-cdp-reader-owner41', ready() {}, expand() {}}};
  window.__requests = [];
  window.__failFeed = false;
  window.__failSaved = false;
  const answer = {status: 'failed', claims: [], sources: [], limitations: [], contradictions: [], missing_data: []};
  let history = Array.from({length: 7}, (_, i) => ({id: i + 1, question: i === 0 ? 'ДлинныйВопросБезПробелов'.repeat(24) : 'Вопрос о ретиноле ' + (i + 1),
    created_at: '2026-11-09T12:00:00Z', content: answer}));
  const source = {url: 'https://example.org/synthetic-source', kind: 'research', source_role: 'research_candidate', scope: 'abstract', published_at: '2024-01-01'};
  const feed = Array.from({length: 12}, (_, i) => ({id: i + 1, finding_id: 101 + i, material_ids: [i + 1], fact_ids: [201 + i],
    title: i === 0 ? 'ДлиннаяНаходкаБезПробелов'.repeat(22) : 'Находка об уходе ' + (i + 1), summary: 'Короткое понятное объяснение находки.',
    sources: [source], url: source.url, source_role: source.source_role, kind: source.kind, text_scope: 'abstract'}));
  window.fetch = async (input, options = {}) => {
    const url = new URL(String(input), location.href);
    window.__requests.push({path: url.pathname, query: url.search, method: options.method || 'GET'});
    const okay = value => ({ok: true, json: async () => value});
    const page = (rows, fallback) => {
      const limit = Number(url.searchParams.get('limit') || fallback);
      const offset = Number(url.searchParams.get('offset') || 0);
      return rows.slice(offset, offset + limit);
    };
    if (url.pathname === '/api/miniapp/session') return okay({telegram_id: 41, role: 'owner'});
    if (url.pathname === '/api/miniapp/answers') return okay(page(history, 5));
    if (url.pathname === '/api/miniapp/feed') {
      if (window.__failFeed) return {ok: false, json: async () => ({error: 'Не удалось загрузить ленту. Попробуйте позже.'})};
      return okay(page(feed, 10));
    }
    if (url.pathname === '/api/miniapp/saved') {
      if (window.__failSaved) return {ok: false, json: async () => ({error: 'Не удалось загрузить сохранённое. Попробуйте позже.'})};
      return okay([{id: 71, title: 'Старая закладка', url: 'https://example.org/legacy', summary: '',
        review_status: 'needs_review', notice: 'Материал ожидает проверки для вашей ленты.'}]);
    }
    if (url.pathname === '/api/miniapp/ask') {
      const payload = JSON.parse(options.body);
      history = [{id: 99, question: payload.question, created_at: '2026-11-09T13:00:00Z', content: answer}, ...history];
      return okay(answer);
    }
    if (url.pathname === '/api/miniapp/save') return okay({saved: true});
    throw Error('Unexpected offline fixture request: ' + url.pathname);
  };
}

async function page(browser) {
  const {cdp} = browser;
  const {targetId} = await cdp.send('Target.createTarget', {url: 'about:blank'});
  const {sessionId} = await cdp.send('Target.attachToTarget', {targetId, flatten: true});
  const unexpected = [];
  const interceptionErrors = [];
  cdp.on('Fetch.requestPaused', message => {
    if (message.sessionId !== sessionId) return;
    const {requestId, request} = message.params;
    const options = request.url === 'https://reader.invalid/miniapp'
      ? {requestId, responseCode: 200, responseHeaders: [{name: 'Content-Type', value: 'text/html; charset=utf-8'}], body: Buffer.from(html).toString('base64')}
      : request.url === 'https://telegram.org/js/telegram-web-app.js'
        ? {requestId, responseCode: 200, responseHeaders: [{name: 'Content-Type', value: 'application/javascript'}], body: ''}
        : null;
    if (!options) unexpected.push(request.url);
    cdp.send(options ? 'Fetch.fulfillRequest' : 'Fetch.failRequest', options || {requestId, errorReason: 'BlockedByClient'}, sessionId)
      .catch(error => interceptionErrors.push(error.message));
  });
  await cdp.send('Page.enable', {}, sessionId);
  await cdp.send('Fetch.enable', {patterns: [{urlPattern: '*'}]}, sessionId);
  await cdp.send('Emulation.setDeviceMetricsOverride', {width: 375, height: 812, deviceScaleFactor: 1, mobile: true}, sessionId);
  await cdp.send('Page.addScriptToEvaluateOnNewDocument', {source: '(' + fixtureBootstrap.toString() + ')();'}, sessionId);
  await cdp.send('Page.navigate', {url: 'https://reader.invalid/miniapp'}, sessionId);
  const evaluate = async expression => {
    const result = await cdp.send('Runtime.evaluate', {expression, awaitPromise: true, returnByValue: true}, sessionId);
    if (result.exceptionDetails) throw new Error(result.exceptionDetails.text + ': ' + JSON.stringify(result.exceptionDetails.exception));
    return result.result.value;
  };
  const until = async (expression, message) => {
    const deadline = Date.now() + 5000;
    while (Date.now() < deadline) {
      if (await evaluate(expression)) return;
      await new Promise(resolve => setTimeout(resolve, 25));
    }
    throw new Error(message || expression);
  };
  await until('document.querySelectorAll("#history details").length===5', 'Initial history must contain five real collapsed details');
  return {targetId, sessionId, evaluate, until, unexpected, interceptionErrors};
}

test('actual Mini App at 375px keeps tabs private, details compact, pages explicit and errors visible',
  {skip: !chrome ? 'Chrome executable is required for the real browser acceptance test' : false, timeout: 45000}, async t => {
    const browser = await launch();
    t.after(async () => {
      try {await browser.cdp.send('Browser.close');} catch {}
      browser.socket.close();
      if (browser.child.exitCode === null) browser.child.kill();
    });
    const app = await page(browser);
    const {evaluate, until} = app;
    assert.equal(await evaluate('innerWidth'), 375, 'Test must use real 375px CSS viewport');
    assert.equal(await evaluate('document.getElementById("ask-panel").contains(document.getElementById("history"))'), true);
    assert.equal(await evaluate('[...document.querySelectorAll("#history details")].every(d=>!d.open)'), true);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=375'), true, 'Long question must wrap without horizontal overflow');
    assert.equal(await evaluate('document.querySelector("#history details .status")===null'), true, 'Collapsed history has one short status, without duplicate body status');
    await evaluate('document.querySelector("[data-panel=feed-panel]").click()');
    await until('document.querySelectorAll("#feed .material").length===10', 'Feed initially contains ten findings');
    assert.equal(await evaluate('document.getElementById("ask-panel").getBoundingClientRect().height'), 0);
    assert.equal(await evaluate('document.getElementById("history").getBoundingClientRect().height'), 0, 'History is hidden with the ask panel');
    assert.equal(await evaluate('document.documentElement.scrollWidth<=375'), true, 'Long finding must wrap without horizontal overflow');
    assert.equal(await evaluate('document.getElementById("feed-more").getBoundingClientRect().height>0'), true);
    await evaluate('document.getElementById("feed-more").click()');
    await until('document.querySelectorAll("#feed .material").length===12', 'More adds the next feed page');
    assert.equal(await evaluate('window.__requests.some(r=>r.path==="/api/miniapp/feed"&&new URLSearchParams(r.query).get("offset")==="10")'), true);
    const screenshot = await browser.cdp.send('Page.captureScreenshot', {format: 'png', captureBeyondViewport: false}, app.sessionId);
    const screenshotDir = path.join(root, '_temp/screenshots');
    fs.mkdirSync(screenshotDir, {recursive: true});
    fs.writeFileSync(path.join(screenshotDir, 'miniapp-ui001-375-' + Date.now() + '.png'), Buffer.from(screenshot.data, 'base64'));
    await evaluate('document.querySelector("[data-panel=saved-panel]").click()');
    await until('document.getElementById("saved").textContent.includes("Старая закладка")');
    assert.equal(await evaluate('document.getElementById("history").getBoundingClientRect().height'), 0);
    assert.match(await evaluate('document.getElementById("saved").textContent'), /ожидает проверки/);
    await evaluate('document.querySelector("[data-panel=ask-panel]").click()');
    await evaluate('document.getElementById("history-more").click()');
    await until('document.querySelectorAll("#history details").length===7');
    assert.equal(await evaluate('window.__requests.some(r=>r.path==="/api/miniapp/answers"&&new URLSearchParams(r.query).get("offset")==="5")'), true);
    await evaluate('document.querySelector("#history summary").click()');
    assert.equal(await evaluate('document.querySelector("#history details").open'), true);
    await evaluate('document.getElementById("question").value="Что помогает от пигментных пятен?";document.getElementById("ask-form").requestSubmit()');
    await until('(document.getElementById("status").textContent+document.getElementById("result").textContent).includes("Проверка не завершена. Попробуйте позже.")');
    assert.equal(await evaluate('(document.getElementById("status").textContent+document.getElementById("result").textContent).split("Проверка не завершена. Попробуйте позже.").length-1'), 1, 'Form result has exactly one status');
    assert.equal(await evaluate('document.getElementById("ask-button").disabled'), false);
    assert.equal(await evaluate('document.documentElement.scrollWidth<=375'), true);
    assert.deepEqual(app.interceptionErrors, []);
    assert.ok(app.unexpected.every(url => url === 'https://reader.invalid/favicon.ico'), 'Unexpected HTTP requests were blocked: ' + app.unexpected.join(', '));

    const errorApp = await page(browser);
    await errorApp.evaluate('window.__failFeed=true;window.__failSaved=true;document.querySelector("[data-panel=feed-panel]").click()');
    await errorApp.until('document.getElementById("feed-panel").innerText.includes("Не удалось загрузить ленту")', 'Feed failure must be visible in the active panel');
    await errorApp.evaluate('document.querySelector("[data-panel=saved-panel]").click()');
    await errorApp.until('document.getElementById("saved-panel").innerText.includes("Не удалось загрузить сохранённое")', 'Saved failure must be visible in the active panel');
    assert.equal(await errorApp.evaluate('document.getElementById("ask-panel").getBoundingClientRect().height'), 0);
  });
