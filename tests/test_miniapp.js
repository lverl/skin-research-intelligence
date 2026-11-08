'use strict';

// Execute the actual shipped inline script. No browser packages, network or Telegram sends.
const test = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const html = fs.readFileSync(path.join(__dirname, '../web/miniapp.html'), 'utf8');
const script = [...html.matchAll(/<script(?:\s[^>]*)?>([\s\S]*?)<\/script>/g)]
  .map(match => match[1]).find(source => source.includes('function showAnswer'));
assert.ok(script, 'The real Mini App inline script must be tested');

class Element {
  constructor(tag = 'div', id = '') {
    this.tagName = tag.toUpperCase();
    this.id = id;
    this.children = [];
    this.listeners = new Map();
    this.attributes = new Map();
    this.dataset = {};
    this.value = '';
    this.disabled = false;
    this._text = '';
    const classes = new Set();
    this.classList = {
      add: (...names) => names.forEach(name => classes.add(name)),
      remove: (...names) => names.forEach(name => classes.delete(name)),
      contains: name => classes.has(name),
      toggle(name, force) {
        const enabled = force === undefined ? !classes.has(name) : force;
        if (enabled) classes.add(name); else classes.delete(name);
        return enabled;
      }
    };
  }
  get childNodes() { return this.children; }
  get textContent() { return this._text + this.children.map(child => child.textContent ?? String(child)).join(' '); }
  set textContent(value) { this._text = String(value); this.children = []; }
  append(...children) { this.children.push(...children); }
  appendChild(child) { this.append(child); return child; }
  replaceChildren(...children) { this._text = ''; this.children = [...children]; }
  setAttribute(name, value) { this.attributes.set(name, String(value)); }
  getAttribute(name) { return this.attributes.get(name); }
  addEventListener(name, listener) { this.listeners.set(name, listener); }
}

function descend(element) {
  return [element, ...element.children.flatMap(child => child instanceof Element ? descend(child) : [])];
}

async function boot({response = {}, history = [], error = null, networkError = null, initData = 'fixture-telegram-init'} = {}) {
  const elements = new Map([...html.matchAll(/\bid="([^"]+)"/g)].map(match => [match[1], new Element('div', match[1])]));
  const tabs = ['ask-panel', 'feed-panel', 'saved-panel'].map(panel => {
    const button = new Element('button');
    button.dataset.panel = panel;
    return button;
  });
  const document = {
    getElementById: id => elements.get(id) || null,
    createElement: tag => new Element(tag),
    querySelectorAll(selector) {
      if (selector === '[data-panel]') return tabs;
      if (selector === '#ask-panel,#feed-panel,#saved-panel') return tabs.map(tab => elements.get(tab.dataset.panel));
      throw new Error('Unmodelled DOM selector: ' + selector);
    }
  };
  const calls = [];
  let savedRows = history;
  let currentResponse = response;
  let currentError = error;
  const fetch = async (url, options = {}) => {
    calls.push({url, options});
    if (url === '/api/miniapp/ask') {
      if (networkError) throw networkError;
      if (currentError) return {ok: false, json: async () => ({error: currentError})};
      const sent = JSON.parse(options.body);
      savedRows = [{id: 1, question: sent.question, created_at: '2026-11-08T12:00:00Z', content: currentResponse}];
      return {ok: true, json: async () => currentResponse};
    }
    if (url === '/api/miniapp/answers') return {ok: true, json: async () => savedRows};
    if (url === '/api/miniapp/session') return {ok: true, json: async () => ({telegram_id: 42, role: 'owner'})};
    throw new Error('Unexpected API request: ' + url);
  };
  const context = vm.createContext({document, fetch, window: {Telegram: {WebApp: {initData, ready() {}, expand() {}}}},
    console, Date, Set, URL, setTimeout, clearTimeout});
  vm.runInContext(script, context, {filename: 'web/miniapp.html'});
  await new Promise(resolve => setImmediate(resolve));
  return {
    elements, calls, context,
    nextResponse(value) { currentResponse = value; currentError = null; },
    async submit(question = 'Что известно о ретиноле?') {
      elements.get('question').value = question;
      const listener = elements.get('ask-form').listeners.get('submit');
      assert.equal(typeof listener, 'function');
      await listener({preventDefault() {}});
      await new Promise(resolve => setImmediate(resolve));
    },
    renderHistory(rows) { context.renderHistory(rows); }
  };
}

function source() {
  return {url: 'https://www.aad.org/public/fixture', title: 'Публичный материал AAD', kind: 'expert',
    scope: 'excerpt', source_role: 'professional_information'};
}

function verified(status = 'completed') {
  return {status, claims: [{text: 'Подтверждённое утверждение фикстуры', fact_ids: [101]}], sources: [source()],
    limitations: [], contradictions: [], missing_data: [], source_checks: []};
}

test('completed form response displays the verified claim, original link and Russian type', async () => {
  const app = await boot({response: verified()});
  await app.submit();
  assert.match(app.elements.get('status').textContent, /Ответ готов/i);
  const rendered = app.elements.get('result').textContent;
  assert.match(rendered, /Подтверждённое утверждение фикстуры/);
  assert.doesNotMatch(rendered, /professional_information|\bexpert\b/);
  assert.ok(descend(app.elements.get('result')).some(element => element.tagName === 'A' && element.href === source().url));
  const request = app.calls.find(call => call.url === '/api/miniapp/ask');
  assert.equal(request.options.headers['X-Telegram-Init-Data'], 'fixture-telegram-init');
  assert.equal(JSON.parse(request.options.body).question, 'Что известно о ретиноле?');
  assert.equal(app.elements.get('ask-button').disabled, false);
});

test('partial response keeps claim, limits and source without calling it ready', async () => {
  const response = {...verified('partial'), limitations: ['Часть источников недоступна'],
    source_checks: [{name: 'Cochrane', status: 'failed', planned: true, complete: false,
      code: 'timeout', reason: 'Источник не ответил вовремя'}]};
  const app = await boot({response});
  await app.submit();
  assert.match(app.elements.get('status').textContent.toLowerCase(), /частич|непол|по части/);
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
  const rendered = app.elements.get('result').textContent;
  assert.match(rendered, /Подтверждённое утверждение/);
  assert.match(rendered, /Часть источников недоступна/);
  assert.ok(descend(app.elements.get('result')).some(element => element.href === source().url));
});

test('completed with no claims is displayed as insufficient, never ready', async () => {
  const app = await boot({response: {status: 'completed', claims: [], sources: []}});
  await app.submit();
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
  assert.match((app.elements.get('status').textContent + app.elements.get('result').textContent).toLowerCase(), /недостат|нет проверенных/);
});

test('blank claim object does not make completed response substantively ready', async () => {
  const app = await boot({response: {status: 'completed', claims: [{text: '   ', fact_ids: []}], sources: []}});
  await app.submit();
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
});

test('failed research is a technical failure instead of absence of studies', async () => {
  const app = await boot({response: {status: 'failed', claims: [], sources: [],
    limitations: ['Проверка источников не завершена'], source_checks: [{name: 'Источник', status: 'failed',
      planned: true, complete: false, code: 'dns_error', reason: 'Не удалось определить адрес источника'}]}});
  await app.submit();
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
  assert.match(app.elements.get('status').textContent.toLowerCase(), /не заверш|сбой|не удалось|ошиб/);
  assert.match(app.elements.get('result').textContent, /Проверка источников не завершена/);
});

test('successful empty research displays insufficient data', async () => {
  const app = await boot({response: {status: 'insufficient_data', claims: [], sources: [],
    missing_data: ['В проверенном объёме данных недостаточно']}});
  await app.submit();
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
  assert.match((app.elements.get('status').textContent + app.elements.get('result').textContent).toLowerCase(), /недостат/);
});

test('necessary clarification is visible and recorded in history without chat continuation', async () => {
  const app = await boot({response: {status: 'needs_clarification', claims: [], sources: [],
    clarification: 'Как называется крем?'}});
  await app.submit('Эффективен ли этот крем?');
  assert.match(app.elements.get('result').textContent, /Как называется крем\?/);
  assert.match(app.elements.get('history').textContent, /Как называется крем\?/);
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
  assert.equal(app.elements.get('ask-button').disabled, false);
});

test('request failure preserves the entered question and restores submit button', async () => {
  const app = await boot({error: 'Не удалось выполнить исследование'});
  await app.submit('Мой исходный вопрос');
  assert.equal(app.elements.get('question').value, 'Мой исходный вопрос');
  assert.equal(app.elements.get('ask-button').disabled, false);
  assert.match(app.elements.get('status').textContent, /Не удалось/);
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
});

test('native fetch rejection shows a safe Russian retry message and keeps the question', async () => {
  const canary = 'testOnlyBrowserCanary_FIX002';
  const app = await boot({networkError: new TypeError('Failed to fetch ' + canary)});
  await app.submit('Исходный вопрос после сетевого сбоя');
  const rendered = app.elements.get('status').textContent + ' ' + app.elements.get('result').textContent;
  assert.doesNotMatch(rendered, /Failed to fetch|TypeError/);
  assert.ok(!rendered.includes(canary));
  assert.match(rendered, /[А-Яа-я]/);
  assert.match(rendered.toLowerCase(), /повтор|попроб|позже/);
  assert.doesNotMatch(rendered, /Ответ готов/i);
  assert.equal(app.elements.get('question').value, 'Исходный вопрос после сетевого сбоя');
  assert.equal(app.elements.get('ask-button').disabled, false);
});

test('failed result retains the question and allows an ordinary form retry', async () => {
  const app = await boot({response: {status: 'failed', claims: [], sources: [], limitations: ['Проверка не завершена']}});
  const question = 'Мой вопрос о ретиноле';
  await app.submit(question);
  assert.equal(app.elements.get('question').value, question);
  assert.equal(app.elements.get('ask-button').disabled, false);
  assert.doesNotMatch(app.elements.get('status').textContent, /Ответ готов/i);
  app.nextResponse(verified());
  await app.submit(question);
  assert.equal(app.calls.filter(call => call.url === '/api/miniapp/ask').length, 2);
  assert.match(app.elements.get('status').textContent, /Ответ готов/i);
  assert.match(app.elements.get('result').textContent, /Подтверждённое утверждение/);
});

test('history renders saved failure and partial status, limitations and sources', async () => {
  const rows = [
    {id: 1, question: 'Первый вопрос', created_at: '2026-11-08T12:00:00Z', content: {...verified('partial'),
      limitations: ['Сохранённое ограничение']}},
    {id: 2, question: 'Второй вопрос', created_at: '2026-11-08T13:00:00Z', content: {status: 'failed', claims: [],
      limitations: ['Проверка источников не завершена'], sources: []}}
  ];
  const app = await boot({history: rows});
  const rendered = app.elements.get('history').textContent;
  assert.match(rendered, /Первый вопрос/);
  assert.match(rendered, /Второй вопрос/);
  assert.match(rendered, /Сохранённое ограничение/);
  assert.match(rendered, /Проверка источников не завершена/);
  assert.ok(descend(app.elements.get('history')).some(element => element.href === source().url));
  assert.doesNotMatch(rendered, /professional_information|\bexpert\b/);
});

test('without Telegram initialization the form stays unavailable and no private API is fetched', async () => {
  const app = await boot({initData: ''});
  assert.equal(app.elements.get('ask-form').classList.contains('hidden'), true);
  assert.equal(app.calls.length, 0);
});
