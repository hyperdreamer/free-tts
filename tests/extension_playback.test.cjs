'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
const popupSource = fs.readFileSync(path.join(__dirname, '../extension/popup.js'), 'utf8');

class Deferred {
  constructor() { this.settled = false; this.promise = new Promise((resolve, reject) => { this._resolve = resolve; this._reject = reject; }); }
  resolve(value) { if (this.settled) return; this.settled = true; this._resolve(value); }
  reject(error) { if (this.settled) return; this.settled = true; this._reject(error); }
}
class VirtualClock {
  constructor() { this.now = 0; this.nextId = 1; this.timers = new Map(); }
  setTimeout(callback, delay = 0) { const id = this.nextId++; this.timers.set(id, { id, due: this.now + Math.max(0, delay), callback, repeat: 0 }); return id; }
  setInterval(callback, delay = 0) { const id = this.nextId++; const period = Math.max(1, delay); this.timers.set(id, { id, due: this.now + period, callback, repeat: period }); return id; }
  clearTimeout(id) { this.timers.delete(id); }
  clearInterval(id) { this.timers.delete(id); }
  async advance(milliseconds) {
    const target = this.now + milliseconds;
    for (let steps = 0; ; steps++) {
      assert.ok(steps < 10000, 'virtual timer step bound');
      const due = [...this.timers.values()].filter(t => t.due <= target).sort((a, b) => a.due - b.due || a.id - b.id)[0];
      if (!due) break;
      this.now = due.due;
      if (due.repeat) due.due += due.repeat; else this.timers.delete(due.id);
      await due.callback(); await Promise.resolve();
    }
    this.now = target; await Promise.resolve();
  }
}
class ScriptGate {
  constructor() { this.nextId = 1; this.entries = []; }
  dispatch(details) {
    const entry = { id: this.nextId++, target: details.target, world: details.world || 'ISOLATED', func: details.func, source: details.func.toString(), args: details.args || [], executed: false, result: undefined, error: null, completion: new Deferred() };
    this.entries.push(entry); return entry.completion.promise;
  }
  pending(predicate) { const entry = this.entries.find(e => !e.executed && predicate(e)); assert.ok(entry, 'expected a pending script request'); return entry; }
  async complete(entry, result) { entry.completion.resolve(result); await Promise.resolve(); }
  async fail(entry, error) { entry.completion.reject(error); await Promise.resolve(); }
}

// Only the DOM operations used by setup/render/highlight are needed here. Gesture
// behavior is exercised by the separately assigned real Chromium suite.
function fixtureDocument(onRemove = () => {}) {
  const nodes = [];
  const element = (tagName) => {
    const node = { tagName: tagName.toUpperCase(), children: [], style: {}, attributes: {}, listeners: new Map(), textContent: '',
      appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
      remove() { onRemove(this); if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); },
      addEventListener(type, callback) { this.listeners.set(type, [...(this.listeners.get(type) || []), callback]); },
      removeEventListener(type, callback) { this.listeners.set(type, (this.listeners.get(type) || []).filter(fn => fn !== callback)); },
      setAttribute(name, value) { this.attributes[name] = String(value); },
      getAttribute(name) { return this.attributes[name] ?? null; },
      querySelector(selector) { return this.querySelectorAll(selector)[0] || null; },
      querySelectorAll(selector) {
        const descendants = this.children.flatMap(child => [child, ...child.querySelectorAll('*')]);
        return descendants.filter(child => selector === '*' || (selector[0] === '#' ? child.id === selector.slice(1) : selector[0] === '.' ? child.className === selector.slice(1) : child.tagName === selector.toUpperCase()));
      },
      getBoundingClientRect() { return { left: 0, top: 0 }; },
    };
    nodes.push(node); return node;
  };
  const body = element('body'); body.innerText = 'First sentence. Second sentence.';
  return { body, title: 'Fixture', createElement: element, createTextNode(text) { const node = element('#text'); node.textContent = text; return node; },
    getElementById(id) { return body.querySelector(`#${id}`); }, querySelectorAll(selector) { return body.querySelectorAll(selector); },
    addEventListener: body.addEventListener.bind(body), removeEventListener: body.removeEventListener.bind(body),
    createTreeWalker() { return { nextNode: () => null }; },
  };
}

function harness() {
  const clock = new VirtualClock(), scripts = new ScriptGate(), storage = [], fetches = [], queries = [], messages = [], logs = [];
  let holdTabQueries = false, tabRemoved;
  const audioSpy = { constructors: [], plays: [], pauses: [], writes: [], media: [], effects: [] };
  const pages = new Map(); let listener, commandListener;
  const timers = Object.fromEntries(['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval'].map(key => [key, clock[key].bind(clock)]));
  const chrome = {
    runtime: { onMessage: { addListener(fn) { listener = fn; } } }, commands: { onCommand: { addListener(fn) { commandListener = fn; } } },
    tabs: { query() { if (!holdTabQueries) return Promise.resolve([{ id: 7 }]); const gate = new Deferred(); queries.push(gate); return gate.promise; },
      onRemoved: { addListener(fn) { tabRemoved = fn; } } }, scripting: { executeScript: details => scripts.dispatch(details) },
    storage: { sync: { get(defaults) { const gate = new Deferred(); storage.push({ defaults, gate }); return gate.promise; } } },
  };
  const quietConsole = { error: (...args) => logs.push(args), warn: (...args) => logs.push(args), log() {} };
  function page(tabId, world = 'MAIN') {
    if (!pages.has(tabId)) {
      const document = fixtureDocument(node => audioSpy.effects.push({ kind: 'remove', id: node.id, className: node.className, tabId })), contexts = {};
      for (const name of ['MAIN', 'ISOLATED']) {
        const events = new Map(), context = { ...timers, console: quietConsole, document, NodeFilter: { SHOW_TEXT: 4 }, navigator: {}, listeners: events, scrollX: 0, scrollY: 0, innerHeight: 900, scrollTo() {},
          getSelection: () => ({ toString: () => 'First sentence. Second sentence.', isCollapsed: false }),
          addEventListener(type, fn) { events.set(type, [...(events.get(type) || []), fn]); },
          removeEventListener(type, fn) { events.set(type, (events.get(type) || []).filter(cb => cb !== fn)); },
          postMessage(data) { messages.push({ tabId, world: name, data }); },
        };
        context.window = context;
        if (name === 'ISOLATED') {
          context.chrome = { runtime: { sendMessage(message) { messages.push({ tabId, world: name, data: message }); return Promise.resolve(); } } };
          context.Audio = function () { assert.fail('Audio constructed outside MAIN'); };
        } else {
          const media = { handlers: new Map(), setActionHandler(action, fn) { this.handlers.set(action, fn); audioSpy.effects.push({ kind: 'handler', action, cleared: fn === null, tabId }); } };
          Object.defineProperty(media, 'metadata', { get() { return this.data; }, set(value) { this.data = value; audioSpy.effects.push({ kind: 'metadata', cleared: value === null, tabId }); } });
          Object.defineProperty(media, 'playbackState', { get() { return this.state; }, set(value) { this.state = value; audioSpy.media.push({ value, time: clock.now }); } });
          context.navigator.mediaSession = media; context.MediaMetadata = function (value) { Object.assign(this, value); };
          context.Audio = function (url) {
            this.id = `audio-${audioSpy.constructors.length + 1}`; this.ended = false; this.paused = true;
            let currentTime = 3, src = url;
            Object.defineProperties(this, {
              currentTime: { get: () => currentTime, set: value => { currentTime = value; audioSpy.writes.push({ audio: this, field: 'currentTime', value }); } },
              src: { get: () => src, set: value => { src = value; audioSpy.writes.push({ audio: this, field: 'src', value }); audioSpy.effects.push({ kind: 'src', value, tabId }); } },
            });
            this.play = () => { this.paused = false; const outcome = new Deferred(); audioSpy.plays.push({ audio: this, time: clock.now, intentRevision: context.__freeTtsPlayback?.workerIntentRevision, mediaRevision: context.__freeTtsPlayback?.mediaRevision, outcome }); return outcome.promise; };
            this.pause = () => { this.paused = true; audioSpy.pauses.push({ audio: this, time: clock.now }); audioSpy.effects.push({ kind: 'pause', audio: this, tabId, keepaliveActive: clock.timers.has(context.__freeTtsKeepalive) }); };
            audioSpy.constructors.push({ audio: this, url, tabId });
          };
        }
        contexts[name] = vm.createContext(context);
      }
      pages.set(tabId, contexts);
    }
    return pages.get(tabId)[world];
  }
  const worker = vm.createContext({ chrome, ...timers, console: quietConsole, crypto: { randomUUID }, URL, AbortController, Uint8Array,
    Date: class extends Date { static now() { return clock.now; } }, btoa: value => Buffer.from(value, 'binary').toString('base64'),
    fetch(url, options) {
      const response = new Deferred(), body = new Deferred();
      const sentence = JSON.parse(options.body).ssml.includes('Second sentence.') ? 2 : 1;
      const entry = { url, options, response, body, sentence }; fetches.push(entry);
      options.signal.addEventListener('abort', () => { response.reject(new Error('aborted')); body.reject(new Error('aborted')); });
      return response.promise;
    },
  });
  vm.runInContext(`${source}\nglobalThis.__worker = { get pipeline() { return sentencePipeline; }, get active() { return activePlayback; }, get pendingStart() { return pendingStart; }, get leases() { return pageLeases; }, start: startSentenceTTS, stop: stopPlayback, poll: pollCompletion, setupMediaSession, showControlBar, pageOwner, buildPlaybackSnapshot, failOwnedPlayback };`, worker);
  assert.equal(typeof commandListener, 'function');
  const api = { clock, scripts, storage, fetches, queries, audioSpy, messages, logs, worker: worker.__worker, page,
    holdTabQueries(value = true) { holdTabQueries = value; },
    shortcut: () => commandListener('speak-selection'),
    closeTab: tabId => tabRemoved?.(tabId),
    navigateTab(tabId) { pages.delete(tabId); },
    start(text = 'First sentence. Second sentence.', tabId = 7) {
      const result = worker.__worker.start(tabId, text);
      const owner = worker.__worker.pipeline;
      if (owner) {
        const binding = { source: 'bar', leaseId: owner.leaseId, sessionId: owner.sessionId, lifecycleRevision: owner.lifecycleRevision };
        void api.send({ ...binding, action: 'setLoopState', enabled: false }, { tab: { id: tabId } });
        void api.send({ ...binding, action: 'setRepeatCurrentState', enabled: false }, { tab: { id: tabId } });
      }
      return result;
    },
    send(message, sender = { tab: { id: 7 } }) { return new Promise((resolve, reject) => {
      try {
        let responded = false;
        const pending = listener(message, sender, value => { responded = true; resolve(value); });
        if (pending !== true && !responded) resolve(undefined);
      } catch (error) { reject(error); }
    }); },
    async command(message, sender = {}) { const result = api.send(message, sender); await api.flush(); return result; },
    mediaAction(tabId, action) {
      const media = page(tabId, 'MAIN').navigator.mediaSession;
      const handler = media?.handlers.get(action);
      assert.equal(typeof handler, 'function', `missing MAIN media handler: ${action}`);
      handler();
      return messages.filter(message => message.tabId === tabId && message.world === 'MAIN').at(-1)?.data;
    },
    deliverRelay(tabId, data, validSource = true) {
      const isolated = page(tabId, 'ISOLATED');
      const sourceWindow = vm.runInContext('window', isolated), before = messages.length;
      for (const callback of isolated.listeners.get('message') || []) callback({ source: validSource ? sourceWindow : {}, data });
      return messages.slice(before).filter(message => message.tabId === tabId && message.world === 'ISOLATED').at(-1)?.data;
    },
    execute(entry) {
      assert.equal(entry.executed, false); entry.executed = true;
      try { entry.result = vm.runInContext(`(${entry.source})(...${JSON.stringify(entry.args)})`, page(entry.target.tabId, entry.world)); }
      catch (error) { entry.error = error; }
      return entry.result;
    },
    async complete(entry, result = entry.result) { assert.ok(entry.executed); if (entry.error) return api.fail(entry, entry.error); await scripts.complete(entry, [{ result }]); },
    fail: (entry, error) => scripts.fail(entry, error),
    async drain() { for (let step = 0; step < 24; step++) await Promise.resolve(); },
    releaseResponse(entry) { entry.response.resolve({ ok: true, status: 200, headers: { get: () => 'audio/mpeg' }, arrayBuffer: () => entry.body.promise }); },
    releaseBody(entry) { entry.body.resolve(Uint8Array.of(entry.sentence).buffer); },
    async flush({ holdScript = () => false, holdStorage = () => false, holdResponse = () => false, holdBody = () => false } = {}) {
      for (let step = 0; step < 300; step++) {
        await api.drain(); let progress = false;
        for (const entry of storage) if (!entry.gate.settled && !holdStorage(entry)) { entry.gate.resolve({ ...entry.defaults }); progress = true; }
        for (const entry of fetches) {
          if (!entry.response.settled && !holdResponse(entry)) { api.releaseResponse(entry); progress = true; }
          if (entry.response.settled && !entry.body.settled && !holdBody(entry)) { api.releaseBody(entry); progress = true; }
        }
        const entry = scripts.entries.find(e => !e.completion.settled && !holdScript(e));
        if (entry) { if (!entry.executed) api.execute(entry); await api.complete(entry); progress = true; }
        if (!progress) return;
      }
      assert.fail('microtask/script step bound');
    },
    async ready(holdScript = () => false) { await api.flush({ holdScript }); await clock.advance(200); await api.flush({ holdScript }); },
    async snapshot() { return api.send({ action: 'getPlaybackState', source: 'popup' }, {}); },
    async pause(holdScript = () => false) {
      const result = api.send({ action: 'pausePlayback', source: 'popup' }, {});
      assert.equal(api.worker.pipeline?.isPaused, true, 'pause accepts worker intent before any page/load await');
      await api.drain();
      assert.ok(scripts.entries.some(e => !e.completion.settled && e.world === 'MAIN' && (e.args[0]?.paused === true || e.source.includes('s.pause()'))), 'MAIN pause dispatched while loading remains held');
      await api.flush({ holdScript, holdStorage: () => true, holdResponse: () => true, holdBody: () => true });
      return result;
    },
    assertPausedBar() {
      const button = page(7, 'ISOLATED').document.getElementById('free-tts-toggle'); assert.ok(button);
      assert.equal(button.textContent, '▶'); assert.equal(button.title, 'Resume'); assert.equal(button.getAttribute('aria-label'), 'Resume');
    },
    assertNoUnintendedTimers() { assert.equal(clock.timers.size, 0, `remaining timers: ${JSON.stringify([...clock.timers.values()].map(t => ({ due: t.due, repeat: t.repeat })))}`); },
    async finish() {
      const stopped = api.send({ action: 'stopPlayback', source: 'popup' }, {}); await api.flush(); await stopped;
      for (const play of audioSpy.plays) play.outcome.resolve();
      await api.flush(); api.assertNoUnintendedTimers();
    },
  };
  return api;
}

function popupAdapter(h, initial = {}) {
  const ids = ['voiceSelect', 'textInput', 'speedSlider', 'speedInput', 'speakBtn', 'stopBtn', 'optionsLink',
    'statusDot', 'hostInput', 'portInput', 'serverConfig', 'moreOptionsLink'];
  const elements = new Map();
  const makeElement = id => {
    const classes = new Set();
    const node = { id, value: '', textContent: '', title: '', disabled: false, className: '', children: [], listeners: new Map(),
      classList: { add: value => classes.add(value), remove: value => classes.delete(value), contains: value => classes.has(value),
        toggle(value) { if (classes.has(value)) { classes.delete(value); return false; } classes.add(value); return true; } },
      addEventListener(type, callback) { this.listeners.set(type, [...(this.listeners.get(type) || []), callback]); },
      appendChild(child) { this.children.push(child); return child; }, replaceChildren(...children) { this.children = children; } };
    elements.set(id, node); return node;
  };
  for (const id of ids) makeElement(id);
  const document = { getElementById: id => elements.get(id), createElement: tag => makeElement(`${tag}-${elements.size}`) };
  const requests = [], warnings = [], heldActions = new Set(), failures = new Map(), overrides = new Map();
  const adapter = {
    requests, elements, warnings,
    holdNext(action) { heldActions.add(action); },
    failNext(action, error = new Error('transport unavailable')) { failures.set(action, error); },
    overrideNext(action, response) { overrides.set(action, response); },
    async release(entry) {
      await entry.actualPromise;
      entry.gate.resolve(entry.actual);
      await Promise.resolve();
    },
  };
  const runtime = {
    sendMessage(message) {
      const entry = { message, actual: null, gate: new Deferred() };
      requests.push(entry);
      if (failures.has(message.action)) {
        const error = failures.get(message.action); failures.delete(message.action);
        entry.actualPromise = Promise.reject(error);
        return entry.actualPromise;
      }
      if (overrides.has(message.action)) {
        entry.actualPromise = Promise.resolve(overrides.get(message.action)); overrides.delete(message.action);
      } else entry.actualPromise = h.send(message, {});
      const result = entry.actualPromise.then(value => { entry.actual = value; return value; });
      if (heldActions.delete(message.action)) return entry.gate.promise;
      return result;
    },
    openOptionsPage() {},
  };
  const chrome = { runtime, storage: { sync: { get: defaults => Promise.resolve({ ...defaults, ...initial }), set: () => Promise.resolve() } },
    tabs: { query: () => Promise.resolve([{ id: 7 }]) }, scripting: { executeScript: () => Promise.resolve([{ result: '' }]) } };
  const context = vm.createContext({ chrome, document, URL, AbortSignal, console: { warn(...args) { warnings.push(args); }, error() {} },
    fetch: async () => ({ ok: true, json: async () => ({ voices: [] }) }) });
  const instrumented = popupSource.replace(/\ninit\(\);\s*$/, '\nglobalThis.__popupInit = init();');
  assert.notEqual(instrumented, popupSource, 'popup startup invocation must be captured by the VM adapter');
  vm.runInContext(`${instrumented}\nglobalThis.__popup = { speak, stop, get state() { return popupState; }, get workerId() { return acceptedWorkerId; }, get viewRevision() { return renderedViewRevision; }, apply: typeof applyPopupSnapshot === 'function' ? applyPopupSnapshot : null, handle: typeof handlePopupResponse === 'function' ? handlePopupResponse : null };`, context);
  adapter.context = context;
  adapter.ready = context.__popupInit;
  return adapter;
}

const activation = entry => entry.world === 'MAIN' && (entry.func.name === 'injectActivation' || entry.source.includes('new Audio('));
const highlight = entry => entry.world === 'ISOLATED' && entry.source.includes('const allRects');
const renderUpdate = entry => entry.world === 'ISOLATED' && entry.source.includes('const btn = document.getElementById("free-tts-toggle")');

// Removing early shell publication or the loader's pause/ownership checks must
// expose an audible start at one of these independently held boundaries.
test('W01 pause_loading_boundaries', async t => {
  for (const boundary of ['settings', 'cache-tick', 'fallback-response', 'fetch-body', 'highlight-execution', 'highlight-completion']) {
    await t.test(boundary, async () => {
      const h = harness(); const started = h.start(); started.catch(() => {});
      let held;
      if (boundary === 'settings') {
        await h.flush({ holdStorage: e => 'serverUrl' in e.defaults });
      } else if (boundary === 'fallback-response' || boundary === 'fetch-body') {
        await h.flush({ holdResponse: () => true }); await h.clock.advance(10000); await h.drain();
        assert.equal(h.fetches.length, 3, 'two preloads and one fallback at the 10s bound'); held = h.fetches[2];
        if (boundary === 'fetch-body') { h.releaseResponse(held); await h.drain(); }
      } else {
        await h.flush();
        if (boundary.startsWith('highlight')) {
          await h.clock.advance(200); await h.flush({ holdScript: highlight }); held = h.scripts.pending(highlight);
          if (boundary === 'highlight-completion') h.execute(held);
        }
      }
      const pipeline = h.worker.pipeline; assert.ok(pipeline, 'initializing session is visible before settings resolves');
      const attempt = pipeline.attempt, loader = attempt?.loadPromise;
      await h.pause(entry => entry === held);
      if (boundary === 'settings') assert.equal(h.storage.find(entry => 'serverUrl' in entry.defaults).gate.settled, false);
      if (boundary === 'cache-tick') assert.equal(h.clock.now, 0);
      if (boundary === 'fallback-response') assert.equal(held.response.settled, false);
      if (boundary === 'fetch-body') assert.equal(held.body.settled, false);
      if (boundary.startsWith('highlight')) assert.equal(held.completion.settled, false);
      assert.equal((await h.snapshot()).state, 'paused');
      assert.equal(h.audioSpy.constructors.length, 0); assert.equal(h.audioSpy.plays.length, 0);
      await h.flush(); await h.clock.advance(200); await h.flush();
      assert.equal(h.audioSpy.constructors.length, 0, 'loader completion while paused must not construct Audio');
      assert.equal(h.audioSpy.plays.length, 0, 'loader completion while paused must not call play');
      assert.equal(h.worker.pipeline, pipeline); assert.equal(pipeline.attempt, attempt, 'pause retains the pending attempt');
      assert.equal(attempt.phase, 'ready'); if (loader) assert.equal(attempt.loadPromise, loader, 'one retained loader');
      h.assertPausedBar();
      const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {}); await h.flush(); await resumed;
      assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
      assert.equal(h.audioSpy.constructors[0].url, 'data:audio/mpeg;base64,AQ==');
      assert.equal(pipeline.attempt, attempt);
      assert.equal(h.fetches.filter(entry => entry.sentence === 1).length, boundary.startsWith('fallback') || boundary === 'fetch-body' ? 2 : 1, 'one current-sentence preload and at most one fallback');
      await started; await h.finish();
    });
  }
});

// Removing activation's exact intent/paused check must construct and play the
// queued old Audio after the newer MAIN pause boundary.
test('W02 activation_executes_after_pause', async () => {
  const h = harness(); const started = h.start(); started.catch(() => {}); await h.ready(activation);
  const old = h.scripts.pending(activation); const paused = await h.pause(entry => entry === old);
  const receipt = h.execute(old); await h.complete(old); await h.flush();
  assert.equal(h.audioSpy.constructors.length, 0, 'stale activation must not construct Audio');
  assert.equal(h.audioSpy.plays.length, 0, 'stale activation must not call play');
  assert.equal(receipt.disposition, 'no-effect'); assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, true);
  assert.equal(h.page(7).__freeTtsPlayback.audioId, null); assert.equal(h.page(7).__freeTtsAudio, null);
  assert.equal((await h.snapshot()).state, 'paused'); h.assertPausedBar();
  assert.equal(paused.accepted, true); assert.equal(paused.pageApplied, true);
  await started; await h.finish();
});

// Removing the live play-serial check would let an old fulfillment overwrite
// paused Media Session status. Both settlements must leave the worker/UI paused.
test('W03 activation_executes_before_pause', async t => {
  for (const outcome of ['fulfill', 'reject']) await t.test(outcome, async () => {
    const h = harness(); const started = h.start(); started.catch(() => {}); await h.ready(activation);
    const old = h.scripts.pending(activation); h.execute(old);
    assert.equal(h.audioSpy.plays.length, 1); const audio = h.audioSpy.plays[0].audio;
    await h.pause(entry => entry === old);
    assert.equal(audio.paused, true); assert.equal(h.audioSpy.pauses.at(-1).audio, audio);
    const boundary = h.audioSpy.media.length, pausedRevision = h.page(7).__freeTtsPlayback.audioRevision;
    const play = h.audioSpy.plays[0];
    if (outcome === 'fulfill') play.outcome.resolve(); else play.outcome.reject(new Error('invalidated play'));
    await h.drain(); await h.complete(old); await h.flush();
    assert.equal(h.audioSpy.plays.length, 1); assert.equal(h.audioSpy.constructors.length, 1);
    assert.equal(h.audioSpy.media.slice(boundary).some(change => change.value === 'playing'), false);
    assert.equal(audio.currentTime, 3); assert.equal(audio.paused, true);
    assert.equal((await h.snapshot()).state, 'paused'); h.assertPausedBar();
    assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, true);
    assert.equal(h.page(7).__freeTtsPlayback.audioStatus, 'paused');
    assert.equal(h.page(7).__freeTtsPlayback.audioError, null);
    assert.equal(h.page(7).__freeTtsPlayback.audioRevision, pausedRevision, 'invalidated outcome cannot publish an observation');
    assert.equal(h.messages.filter(message => message.data.action === 'audioPlaybackEvent').length, 0);
    assert.equal(h.audioSpy.writes.filter(write => write.field === 'currentTime').length, 0);
    await started; await h.finish();
  });
});

// F-1: loading must not strand ready bytes when a running intent executes
// after activation, including when the no-effect activation completes last.
test('F-1 running_intent_applies_after_load', async t => {
  for (const state of ['initial', 'resumed']) await t.test(state, async () => {
    const h = harness(); const started = h.start();
    let resumed;
    if (state === 'resumed') {
      await h.ready(highlight); await started; await h.pause(highlight);
      resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    }
    const runningIntent = entry => entry.func.name === 'injectWorkerIntent' && !entry.args[0].paused;
    await h.ready(entry => runningIntent(entry) || activation(entry));
    const intent = h.scripts.pending(runningIntent), pipeline = h.worker.pipeline, attempt = pipeline.attempt;
    const loader = attempt.loadPromise;
    assert.equal(attempt.phase, 'ready'); assert.equal(attempt.dataUrl, 'data:audio/mpeg;base64,AQ==');
    // A gated implementation dispatches no activation yet. Exercise any early
    // activation in the unfixed implementation, keeping its completion pending.
    const early = h.scripts.entries.find(entry => !entry.executed && activation(entry));
    if (early) assert.equal(h.execute(early).disposition, 'no-effect');
    assert.equal(h.audioSpy.constructors.length, 0); assert.equal(h.audioSpy.plays.length, 0);
    h.execute(intent); await h.complete(intent);
    await h.flush({ holdScript: entry => entry === early || activation(entry) });
    if (early) await h.complete(early);
    await h.flush();
    const response = await (resumed || started);
    assert.equal(response.disposition, 'applied'); assert.equal(response.pageApplied, true);
    assert.equal(h.audioSpy.constructors.length, 1, 'ready attempt must activate after its running intent applies');
    assert.equal(h.audioSpy.plays.length, 1); assert.equal(h.worker.pipeline, pipeline);
    assert.equal(pipeline.attempt, attempt); assert.equal(attempt.loadPromise, loader);
    assert.equal(h.fetches.filter(entry => entry.sentence === 1).length, 1, 'no replacement loader');
    assert.equal((await h.snapshot()).state, 'playing');
    await h.finish();
  });
});

// F-3: desired worker state alone cannot acknowledge a redundant pause while
// the accepted revision's MAIN boundary is still pending.
test('F-3 redundant_pause_waits_for_main_receipt', async t => {
  for (const order of ['applied', 'superseded']) await t.test(order, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const audio = h.audioSpy.plays[0].audio;
    const first = h.send({ action: 'pausePlayback', source: 'popup' }, {}); await h.drain();
    const pause = h.scripts.pending(entry => entry.func.name === 'injectWorkerIntent' && entry.args[0].paused);
    let acknowledged = false;
    const second = h.send({ action: 'pausePlayback', source: 'popup' }, {}); second.then(() => { acknowledged = true; });
    await h.flush({ holdScript: entry => entry === pause });
    assert.equal(acknowledged, false, 'redundant pause must not acknowledge before MAIN applies it');
    assert.equal(audio.paused, false); assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, false);
    assert.equal(h.scripts.entries.filter(entry => entry.func.name === 'injectWorkerIntent' && entry.args[0].paused).length, 1, 'one shared pause application');
    let resumed;
    if (order === 'superseded') {
      resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
      await h.flush({ holdScript: entry => entry === pause });
    }
    h.execute(pause); await h.drain();
    if (order === 'applied') assert.equal(acknowledged, false, 'execution alone is not the worker-facing receipt');
    await h.complete(pause); await h.flush();
    for (const response of await Promise.all([first, second])) {
      assert.equal(response.accepted, true); assert.equal(response.disposition, order);
      assert.equal(response.pageApplied, order === 'applied');
      assert.equal(response.state, order === 'applied' ? 'paused' : 'playing');
    }
    assert.equal(audio.paused, order === 'applied');
    if (resumed) await resumed; else h.assertPausedBar();
    await h.finish();
  });
});

test('W04 stale_resume_both_execution_orders', async t => {
  for (const order of ['resume-before-pause', 'pause-before-resume']) await t.test(order, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started; await h.pause();
    const pipeline = h.worker.pipeline, audio = h.audioSpy.constructors[0].audio;
    const initialPlayCount = h.audioSpy.plays.length;
    const isIntent = entry => entry.func.name === 'injectWorkerIntent';
    const isToggleRender = entry => entry.world === 'ISOLATED' && entry.source.includes('const paused = current.state');
    const resume = h.send({ action: 'resumePlayback', source: 'popup' }, {}); await h.drain();
    const resumeIntent = h.scripts.pending(entry => isIntent(entry) && entry.args[0].paused === false);
    const delayedResumeRender = h.scripts.pending(isToggleRender);
    assert.ok(delayedResumeRender, 'the accepted-state render is dispatched independently of the MAIN receipt');
    let resumeReceipt;
    let pause;
    if (order === 'resume-before-pause') {
      resumeReceipt = h.execute(resumeIntent);
      pause = h.send({ action: 'pausePlayback', source: 'popup' }, {}); await h.drain();
      const pauseIntent = h.scripts.pending(entry => isIntent(entry) && entry.args[0].paused === true);
      h.execute(pauseIntent); await h.complete(pauseIntent); await h.drain();
      const pauseRenders = h.scripts.entries.filter(entry => !entry.executed && isToggleRender(entry) && entry !== delayedResumeRender);
      assert.ok(pauseRenders.length >= 1);
      for (const pauseRender of pauseRenders) { h.execute(pauseRender); await h.complete(pauseRender); }
      await pause;
      await h.complete(resumeIntent);
      h.execute(delayedResumeRender); await h.complete(delayedResumeRender);
    } else {
      pause = h.send({ action: 'pausePlayback', source: 'popup' }, {}); await h.drain();
      const pauseIntent = h.scripts.pending(entry => isIntent(entry) && entry.args[0].paused === true);
      const pauseReceipt = h.execute(pauseIntent); await h.complete(pauseIntent); await h.drain();
      const pauseRenders = h.scripts.entries.filter(entry => !entry.executed && isToggleRender(entry) && entry !== delayedResumeRender);
      assert.ok(pauseRenders.length >= 1);
      for (const pauseRender of pauseRenders) { h.execute(pauseRender); await h.complete(pauseRender); }
      await pause;
      assert.equal(pauseReceipt.disposition, 'applied');
      resumeReceipt = h.execute(resumeIntent); await h.complete(resumeIntent);
      h.execute(delayedResumeRender); await h.complete(delayedResumeRender);
    }
    await h.flush();
    const resumeResponse = await resume;
    assert.equal(resumeReceipt.disposition, order === 'resume-before-pause' ? 'applied' : 'stale');
    assert.equal(resumeResponse.state, 'paused');
    assert.equal((await h.snapshot()).state, 'paused');
    assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, true);
    assert.equal(audio.paused, true);
    assert.equal(h.audioSpy.plays.length, order === 'resume-before-pause' ? initialPlayCount + 1 : initialPlayCount,
      'only a resume that executes before pause may invoke play');
    h.assertPausedBar();
    assert.equal(h.worker.pipeline, pipeline);
    await h.finish();
  });
});

// A newer running intent must retain the sole pending play's outcome, while a
// newer applied pause must invalidate it even when older intents execute last.
test('W05 rapid_toggle_parity', async t => {
  for (const count of [2, 3]) for (const outcome of ['fulfill', 'reject']) await t.test(`${count}-toggles-${outcome}`, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, owner = { leaseId: pipeline.leaseId, sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision };
    const originalPlay = h.audioSpy.plays[0], originalAudio = originalPlay.audio;
    const audioId = h.page(7).__freeTtsPlayback.audioId, startSerial = h.page(7).__freeTtsPlayback.startSerial;
    const button = h.page(7, 'ISOLATED').document.getElementById('free-tts-toggle');
    const beforeRevision = pipeline.intentRevision, beforeState = pipeline.isPaused;
    button.textContent = '▶'; button.title = 'Resume'; button.setAttribute('aria-label', 'Resume');
    button.listeners.get('click')[0]();
    const firstMessage = h.messages.at(-1).data;
    assert.equal(firstMessage.action, 'togglePlayback', 'the command is derived from worker state, never the stale glyph');
    assert.equal(firstMessage.source, 'bar'); assert.equal(firstMessage.leaseId, owner.leaseId);
    assert.equal(firstMessage.sessionId, owner.sessionId); assert.equal(firstMessage.lifecycleRevision, owner.lifecycleRevision);

    const invalidBindings = [
      [{ ...firstMessage, leaseId: 'wrong-lease' }, { tab: { id: 7 } }],
      [firstMessage, { tab: { id: 8 } }],
      [{ ...firstMessage, sessionId: 'wrong-session' }, { tab: { id: 7 } }],
      [{ ...firstMessage, lifecycleRevision: owner.lifecycleRevision + 1 }, { tab: { id: 7 } }],
      [firstMessage, {}],
    ];
    for (const [message, sender] of invalidBindings) {
      assert.equal((await h.send(message, sender)).accepted, false, 'unbound or stale bar origins are ignored');
    }
    assert.equal(pipeline.intentRevision, beforeRevision);
    assert.equal(pipeline.isPaused, beforeState);

    const responses = [h.send(firstMessage, { tab: { id: 7 } })];
    for (let i = 1; i < count; i++) responses.push(h.send({ ...firstMessage }, { tab: { id: 7 } }));
    assert.equal(pipeline.intentRevision, beforeRevision + count);
    assert.equal(pipeline.isPaused, count % 2 === 0 ? beforeState : !beforeState);
    await h.drain();
    const intents = h.scripts.entries.filter(entry => entry.func.name === 'injectWorkerIntent' && entry.args[0].intentRevision > beforeRevision);
    const isToggleRender = entry => entry.world === 'ISOLATED' && entry.source.includes('const paused = current.state');
    assert.equal(intents.length, count);
    const descending = [...intents].sort((a, b) => b.args[0].intentRevision - a.args[0].intentRevision);
    const renders = h.scripts.entries.filter(entry => !entry.executed && isToggleRender(entry));
    assert.equal(renders.length, count);
    const rendersNewestFirst = [...renders].sort((a, b) => b.args[1].viewRevision - a.args[1].viewRevision);
    for (const entry of descending) h.execute(entry);
    for (const entry of rendersNewestFirst) h.execute(entry);
    for (const entry of rendersNewestFirst.slice().reverse()) await h.complete(entry);
    for (const entry of descending.slice().reverse()) await h.complete(entry);
    await h.drain();
    const refreshes = h.scripts.entries.filter(entry => !entry.executed && isToggleRender(entry));
    for (const entry of refreshes.slice().reverse()) h.execute(entry);
    for (const entry of refreshes) await h.complete(entry);
    await h.flush(); await Promise.all(responses);

    const paused = count % 2 === 0 ? beforeState : !beforeState;
    assert.equal((await h.snapshot()).state, paused ? 'paused' : 'playing');
    assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, paused);
    const rendered = h.page(7, 'ISOLATED').document.getElementById('free-tts-toggle');
    const action = paused ? 'Resume' : 'Pause';
    assert.equal(rendered.textContent, paused ? '▶' : '⏸');
    assert.equal(rendered.title, action); assert.equal(rendered.getAttribute('aria-label'), action);

    const record = h.page(7).__freeTtsPlayback, media = h.page(7).navigator.mediaSession;
    const audioRevision = record.audioRevision;
    assert.equal(record.audio, originalAudio); assert.equal(record.audioId, audioId);
    assert.equal(record.startSerial, paused ? startSerial + 1 : startSerial);
    assert.equal(h.audioSpy.plays.length, 1, 'reordered running intent must retain the pending play without replaying');
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(originalAudio.currentTime, 3);
    assert.equal(h.audioSpy.writes.filter(write => write.field === 'currentTime').length, 0);
    assert.equal(media.playbackState, 'paused', 'pending play has not established audible success');
    assert.equal(h.messages.filter(message => message.data.action === 'audioPlaybackEvent').length, 0);
    if (outcome === 'fulfill') originalPlay.outcome.resolve();
    else originalPlay.outcome.reject(new Error('pending original play failed'));
    await h.drain(); await h.flush();

    const events = h.messages.filter(message => message.data.action === 'audioPlaybackEvent');
    if (paused) {
      assert.equal(record.audioStatus, 'paused'); assert.equal(record.audioError, null);
      assert.equal(record.audioRevision, audioRevision, 'pause invalidates either old play outcome');
      assert.equal(events.length, 0); assert.equal(media.playbackState, 'paused');
      assert.equal(originalAudio.paused, true);
    } else {
      const status = outcome === 'fulfill' ? 'playing' : 'error';
      assert.equal(record.audioStatus, status, 'the still-live original play must settle under the latest running intent');
      if (outcome === 'fulfill') assert.equal(record.audioError, null);
      else {
        assert.equal(record.audioError.code, 'play-rejection');
        assert.equal(record.audioError.message, 'pending original play failed');
      }
      assert.equal(record.audioRevision, audioRevision + 1);
      assert.equal(media.playbackState, outcome === 'fulfill' ? 'playing' : 'paused');
      assert.equal(events.length, 1, 'publish the retained invocation outcome once');
      const event = events[0];
      assert.equal(event.world, 'MAIN'); assert.equal(event.tabId, 7);
      assert.equal(event.data.leaseId, owner.leaseId); assert.equal(event.data.sessionId, owner.sessionId);
      assert.equal(event.data.lifecycleRevision, owner.lifecycleRevision);
      assert.equal(event.data.intentRevision, beforeRevision + count); assert.equal(event.data.mediaRevision, 0);
      assert.equal(event.data.attemptId, pipeline.attempt.attemptId); assert.equal(event.data.audioId, audioId);
      assert.equal(event.data.startSerial, startSerial); assert.equal(event.data.audioRevision, audioRevision + 1);
      assert.equal(event.data.paused, false); assert.equal(event.data.audioStatus, status);
      assert.equal(event.data.audioError, record.audioError);
    }
    assert.equal(h.audioSpy.plays.length, 1, 'settlement cannot retry play');
    assert.equal(record.audio, originalAudio); assert.equal(record.audioId, audioId);
    assert.equal(h.worker.pipeline, pipeline);
    await h.finish();
  });
});

test('W06 unfinished_resume_identity', async () => {
  const h = harness(); const started = h.start(); await h.ready(); await started;
  const pipeline = h.worker.pipeline, attempt = pipeline.attempt;
  const originalAudio = h.audioSpy.constructors[0].audio, originalId = pipeline.attempt.audioId;
  const fetchCount = h.fetches.length, constructorCount = h.audioSpy.constructors.length;
  const currentTime = originalAudio.currentTime, initialPlayCount = h.audioSpy.plays.length;
  await h.pause();
  const activationCount = h.scripts.entries.filter(activation).length;
  const resumePromise = h.send({ action: 'resumePlayback', source: 'popup' }, {});
  await h.flush();
  const resume = await resumePromise;
  const playCountAfterResume = h.audioSpy.plays.length;
  const redundantPromise = h.send({ action: 'resumePlayback', source: 'popup' }, {});
  await h.flush();
  const redundant = await redundantPromise;

  assert.equal(resume.accepted, true); assert.equal(redundant.accepted, true);
  assert.equal(h.worker.pipeline.attempt, attempt);
  assert.equal(pipeline.attempt.audioId, originalId); assert.equal(originalId, `${pipeline.sessionId}:${attempt.attemptId}`);
  assert.equal(h.audioSpy.constructors.length, constructorCount);
  assert.equal(h.audioSpy.constructors[0].audio, originalAudio);
  assert.equal(originalAudio.currentTime, currentTime);
  assert.equal(h.audioSpy.writes.filter(write => write.field === 'currentTime').length, 0);
  assert.equal(h.fetches.length, fetchCount);
  assert.equal(playCountAfterResume, initialPlayCount + 1, 'resume invokes play once on the unfinished Audio');
  assert.equal(h.audioSpy.plays.length, playCountAfterResume, 'redundant resume does not invoke play again');
  assert.equal(h.scripts.entries.filter(activation).length, activationCount,
    'a known unfinished Audio resumes through its identity, not a replacement activation');
  assert.ok(attempt.pollTimer, 'the attempt retains its one completion observer');
  assert.equal([...h.clock.timers.values()].filter(timer => timer.due === h.clock.now + 500).length, 1);
  assert.equal(h.page(7).__freeTtsPlayback.audioId, originalId);
  assert.equal(h.page(7).__freeTtsPlayback.audio, originalAudio);
  await h.finish();
});

// F-2: leaving the MAIN record playing/starting after direct pause suppresses
// the next resume, and failing to invalidate its serial admits the old outcome.
test('F-2 direct_media_pause_then_popup_resume', async t => {
  for (const outcome of ['fulfilled', 'pending-fulfill', 'pending-reject']) await t.test(outcome, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const initial = h.audioSpy.plays[0], audio = initial.audio;
    if (outcome === 'fulfilled') { initial.outcome.resolve(); await h.drain(); }
    const media = h.page(7).navigator.mediaSession, record = h.page(7).__freeTtsPlayback;
    media.handlers.get('pause')();
    assert.equal(audio.paused, true, 'direct pause takes effect without the worker');
    const boundary = h.audioSpy.media.length;
    const synced = h.send(h.deliverRelay(7, h.messages.at(-1).data)); await h.flush(); await synced;
    if (outcome === 'pending-reject') initial.outcome.reject(new Error('invalidated direct pause'));
    else initial.outcome.resolve();
    await h.drain();
    assert.equal(h.audioSpy.media.slice(boundary).some(change => change.value === 'playing'), false, 'direct pause fences the old play outcome');
    const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {}); await h.flush();
    const response = await resumed;
    assert.equal(h.audioSpy.plays.length, 2, 'popup resume must play the directly paused Audio');
    assert.equal(response.disposition, 'applied'); assert.equal(response.pageApplied, true);
    assert.equal(h.audioSpy.plays[1].audio, audio); assert.equal(audio.currentTime, 3);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(audio.paused, false);
    assert.equal(record.audioStatus, 'starting'); assert.equal(record.audioError, null);
    assert.equal(h.audioSpy.writes.filter(write => write.field === 'currentTime').length, 0);
    // The direct play handler must use the same live invocation bookkeeping too.
    media.handlers.get('pause')();
    assert.equal(record.desiredPaused, true); assert.equal(record.audioStatus, 'paused');
    media.handlers.get('play')();
    assert.equal(record.desiredPaused, false); assert.equal(record.audioStatus, 'starting');
    assert.equal(h.audioSpy.plays.length, 3); assert.equal(h.audioSpy.plays[2].audio, audio);
    assert.equal(media.playbackState, 'paused', 'direct play is not audible success before settlement');
    h.audioSpy.plays[2].outcome.resolve(); await h.drain();
    assert.equal(record.audioStatus, 'playing'); assert.equal(media.playbackState, 'playing');
    await h.finish();
  });
});

const completionPoll = entry => entry.world === 'MAIN' && entry.source.includes('const ended = !!record.audio.ended');

async function boundModeMessage(h, action, enabled, holdScript = () => false) {
  const pipeline = h.worker.pipeline;
  const result = h.send({ action, enabled, source: 'bar', leaseId: pipeline.leaseId,
    sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision }, { tab: { id: pipeline.tabId } });
  await h.flush({ holdScript });
  return result;
}

async function startOwnedEndPoll(h, attempt) {
  const audio = h.audioSpy.constructors.find(item => item.audio === h.page(7).__freeTtsAudio)?.audio;
  assert.ok(audio, 'attempt owns a MAIN-world Audio');
  audio.ended = true;
  await h.clock.advance(500);
  await h.drain();
  const poll = h.scripts.pending(completionPoll);
  assert.equal(poll.args[1], attempt.attemptId, 'poll is bound to the captured attempt');
  const sample = h.execute(poll);
  assert.equal(sample.owned, true);
  assert.equal(sample.ended, true);
  return poll;
}

async function finishOwnedEndPoll(h, poll) {
  await h.complete(poll);
  await h.drain();
}

test('W07 resume_during_pending_load', async t => {
  for (const finalState of ['running-before-load', 'paused-through-ready']) await t.test(finalState, async () => {
    const h = harness();
    const started = h.start(); started.catch(() => {});
    await h.flush({ holdResponse: () => true });
    await started;
    await h.clock.advance(10000); await h.drain();
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, loader = attempt.loadPromise;
    assert.ok(loader, 'the single current-attempt load is pending');
    assert.ok(h.fetches.some(entry => entry.sentence === 1 && !entry.response.settled), 'fallback fetch is held');

    const actions = finalState === 'running-before-load'
      ? ['pausePlayback', 'resumePlayback', 'pausePlayback', 'resumePlayback']
      : ['pausePlayback', 'resumePlayback', 'pausePlayback'];
    const commands = actions.map(action => h.send({ action, source: 'popup' }, {}));
    assert.equal(pipeline.isPaused, finalState === 'paused-through-ready');
    await h.flush({ holdResponse: () => true, holdBody: () => true });
    await Promise.all(commands);
    assert.equal(h.audioSpy.constructors.length, 0);
    assert.equal(h.audioSpy.plays.length, 0);
    assert.equal(attempt.loadPromise, loader, 'rapid intent changes retain the same loader');

    await h.flush(); await h.clock.advance(200); await h.flush();
    await h.drain();
    assert.equal(pipeline.attempt, attempt);
    assert.equal(attempt.loadPromise, loader);
    assert.equal(attempt.dataUrl, 'data:audio/mpeg;base64,AQ==');
    if (finalState === 'paused-through-ready') {
      assert.equal(attempt.phase, 'ready');
      assert.equal(h.audioSpy.constructors.length, 0, 'ending paused cannot start at load completion');
      assert.equal(h.audioSpy.plays.length, 0, 'ending paused cannot play at load completion');
      const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
      await h.flush(); await resumed;
    }
    assert.equal(h.audioSpy.constructors.length, 1);
    assert.equal(h.audioSpy.plays.length, 1, 'latest running intent activates once');
    assert.equal(h.audioSpy.constructors[0].url, attempt.dataUrl);
    const fetchCount = h.fetches.filter(entry => entry.sentence === 1).length;
    const playCount = h.audioSpy.plays.length, constructorCount = h.audioSpy.constructors.length;
    const poll = await startOwnedEndPoll(h, attempt);
    await finishOwnedEndPoll(h, poll);
    await h.flush(); await h.clock.advance(200); await h.flush();
    assert.equal(attempt.completionObserved, true);
    assert.equal(attempt.completionConsumed, true);
    assert.equal(pipeline.currentIdx, 1, 'one accepted completion advances exactly once');
    assert.equal(pipeline.attempt.idx, 1);
    assert.equal(h.audioSpy.constructors.length, constructorCount + 1);
    assert.equal(h.audioSpy.plays.length, playCount + 1);
    assert.equal(h.fetches.filter(entry => entry.sentence === 1).length, fetchCount,
      'completion handling reuses the owned loader and does not refetch its sentence');
    const scriptCount = h.scripts.entries.length;
    await h.worker.poll(pipeline, attempt); await h.drain();
    assert.equal(h.scripts.entries.length, scriptCount, 'duplicate completion cannot create another advancement');
    await h.finish();
  });
});

test('W08 ready_paused_resume', async () => {
  const h = harness();
  const started = h.start(); started.catch(() => {});
  await h.flush({ holdResponse: () => true });
  const paused = h.pause();
  await paused;
  const pipeline = h.worker.pipeline, attempt = pipeline.attempt, loader = attempt.loadPromise;
  await h.flush(); await h.clock.advance(200); await h.flush();
  await started;
  assert.equal(attempt.phase, 'ready');
  assert.equal(attempt.dataUrl, 'data:audio/mpeg;base64,AQ==');
  assert.equal(attempt.loadPromise, loader);
  assert.equal(h.audioSpy.constructors.length, 0);

  const first = h.send({ action: 'resumePlayback', source: 'popup' }, {});
  const second = h.send({ action: 'resumePlayback', source: 'popup' }, {});
  await h.flush();
  const [firstResult, secondResult] = await Promise.all([first, second]);
  assert.equal(firstResult.accepted, true); assert.equal(secondResult.accepted, true);
  assert.equal(pipeline.attempt, attempt);
  assert.equal(attempt.loadPromise, loader);
  assert.equal(h.audioSpy.constructors.length, 1);
  assert.equal(h.audioSpy.plays.length, 1);
  assert.equal(h.audioSpy.constructors[0].url, attempt.dataUrl);
  await h.finish();
});

test('W09 end_observed_paused_replay', async () => {
  const h = harness();
  const started = h.start(); await h.ready(); await started;
  const pipeline = h.worker.pipeline, endedAttempt = pipeline.attempt;
  await h.pause();
  const poll = await startOwnedEndPoll(h, endedAttempt);
  await finishOwnedEndPoll(h, poll);
  assert.equal(pipeline.attempt, endedAttempt);
  assert.equal(endedAttempt.phase, 'ended');
  assert.equal(endedAttempt.completionObserved, true);
  assert.equal(endedAttempt.completionObservedWhilePaused, true);
  assert.equal(endedAttempt.completionConsumed, false);
  assert.equal(pipeline.currentIdx, 0);

  const constructors = h.audioSpy.constructors.length, plays = h.audioSpy.plays.length;
  const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
  await h.flush(); await resumed;
  assert.notEqual(pipeline.attempt, endedAttempt);
  assert.equal(pipeline.attempt.idx, 0);
  assert.equal(endedAttempt.retired, true);
  assert.equal(endedAttempt.completionConsumed, true);
  assert.equal(h.audioSpy.constructors.length, constructors + 1);
  assert.equal(h.audioSpy.plays.length, plays + 1);
  const successor = pipeline.attempt;
  const scriptCount = h.scripts.entries.length;
  await h.worker.poll(pipeline, endedAttempt);
  await h.drain();
  assert.equal(h.scripts.entries.length, scriptCount, 'a duplicate old end sample cannot dispatch a successor poll');
  assert.equal(pipeline.attempt, successor);
  assert.equal(pipeline.currentIdx, 0);
  await h.finish();
});

// F-10: a fresh paused observation must retain completion before a later
// resume, even when the periodic completion poll has not run yet.
test('W09 F-10 paused_observation_before_poll', async () => {
  const h = harness(); const started = h.start(); await h.ready(); await started;
  const pipeline = h.worker.pipeline, endedAttempt = pipeline.attempt, endedAudio = h.page(7).__freeTtsAudio;
  await h.pause();
  endedAudio.ended = true;
  const pollCount = h.scripts.entries.filter(completionPoll).length;
  const pause = h.mediaAction(7, 'pause');
  await h.command(h.deliverRelay(7, pause), { tab: { id: 7 } });

  assert.equal(h.scripts.entries.filter(completionPoll).length, pollCount, 'fresh read observes the end before any 500 ms poll');
  assert.equal(pipeline.attempt, endedAttempt); assert.equal(pipeline.currentIdx, 0);
  assert.equal(endedAttempt.completionObserved, true, 'owned ended read must accept completion');
  assert.equal(endedAttempt.completionObservedWhilePaused, true, 'completion uses accepted paused intent at read acceptance');
  assert.equal(endedAttempt.completionConsumed, false); assert.equal(endedAttempt.phase, 'ended');
  assert.equal(h.audioSpy.plays.length, 1);

  await h.command({ action: 'resumePlayback', source: 'popup' });
  const replay = pipeline.attempt;
  assert.notEqual(replay, endedAttempt); assert.notEqual(replay.attemptId, endedAttempt.attemptId);
  assert.equal(replay.idx, 0); assert.equal(pipeline.currentIdx, 0);
  assert.equal(endedAttempt.retired, true); assert.equal(endedAttempt.completionConsumed, true);
  assert.equal(h.audioSpy.constructors.length, 2); assert.equal(h.audioSpy.plays.length, 2);
  assert.notEqual(h.page(7).__freeTtsAudio, endedAudio, 'resume creates a fresh same-index Audio');
  await h.clock.advance(1000); await h.flush();
  await h.worker.poll(pipeline, endedAttempt); await h.flush();
  assert.equal(pipeline.attempt, replay); assert.equal(pipeline.currentIdx, 0, 'later polls cannot advance the replay from the old end');
  assert.equal(h.audioSpy.constructors.length, 2); assert.equal(h.audioSpy.plays.length, 2);
  await h.finish();
});

// F-4: MAIN play cannot replay an ended element itself; media resume must
// consume the same attempt-owned paused completion as an explicit resume.
test('W09 F-4 media_resume_after_paused_completion', async () => {
  const h = harness();
  const started = h.start(); await h.ready(); await started;
  const pipeline = h.worker.pipeline, endedAttempt = pipeline.attempt;
  const endedAudio = h.page(7).__freeTtsAudio;
  await h.pause();
  const poll = await startOwnedEndPoll(h, endedAttempt);
  await finishOwnedEndPoll(h, poll);
  assert.equal(endedAttempt.completionObservedWhilePaused, true);
  assert.equal(endedAttempt.completionConsumed, false);

  const fetchCount = h.fetches.length;
  h.page(7).navigator.mediaSession.handlers.get('play')();
  assert.equal(h.audioSpy.plays.length, 1, 'direct media play cannot play ended A');
  const notification = h.messages.at(-1);
  assert.equal(notification.world, 'MAIN');
  assert.equal(notification.data.action, 'mediaResume');
  const resumed = h.send(h.deliverRelay(7, notification.data), { tab: { id: 7 } });
  await h.flush(); await resumed;
  await h.clock.advance(2000); await h.flush();

  const replay = pipeline.attempt;
  assert.notEqual(replay, endedAttempt, 'media resume must create a fresh replay attempt');
  assert.notEqual(replay.attemptId, endedAttempt.attemptId);
  assert.equal(replay.idx, 0); assert.equal(pipeline.currentIdx, 0);
  assert.equal(endedAttempt.retired, true); assert.equal(endedAttempt.completionConsumed, true);
  assert.equal(pipeline.isPaused, false);
  assert.equal(h.audioSpy.constructors.length, 2);
  assert.equal(h.audioSpy.plays.length, 2, 'one media replay');
  assert.notEqual(h.audioSpy.plays[1].audio, endedAudio);
  assert.equal(h.page(7).__freeTtsPlayback.attemptId, replay.attemptId);
  assert.equal(h.fetches.length, fetchCount, 'replay uses retained session bytes');
  const scriptCount = h.scripts.entries.length;
  await h.worker.poll(pipeline, endedAttempt); await h.drain();
  assert.equal(h.scripts.entries.length, scriptCount, 'old completion cannot advance the media replay');
  assert.equal(pipeline.attempt, replay);
  await h.finish();
});

// F-5: a redundant resume must share replay selection readiness. Otherwise
// its early no-effect activation can coalesce away the creator's later start.
test('W09 F-5 duplicate_resume_during_paused_end_selection', async t => {
  for (const boundary of ['selection-execution', 'selection-receipt']) await t.test(boundary, async () => {
    const h = harness();
    const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, endedAttempt = pipeline.attempt;
    await h.pause();
    const poll = await startOwnedEndPoll(h, endedAttempt);
    await finishOwnedEndPoll(h, poll);
    assert.equal(endedAttempt.completionObservedWhilePaused, true);
    const fetchCount = h.fetches.length;

    const first = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    const replay = pipeline.attempt;
    assert.notEqual(replay, endedAttempt);
    assert.equal(replay.idx, 0);
    const selectingReplay = entry => entry.func.name === 'injectSelectAttempt' && entry.args[0].attemptId === replay.attemptId;
    await h.flush({ holdScript: entry => selectingReplay(entry) || activation(entry) });
    const selection = h.scripts.pending(selectingReplay);
    if (boundary === 'selection-receipt') h.execute(selection);

    const second = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    await h.flush({ holdScript: entry => entry === selection || activation(entry) });
    const loader = replay.loadPromise;
    assert.ok(loader, 'the redundant resume may prepare bytes while selection is pending');
    assert.equal(replay.dataUrl, 'data:audio/mpeg;base64,AQ==');
    // Exercise an ungated implementation's activation before selection, holding
    // its no-effect receipt until after the replay creator has finished.
    const early = h.scripts.entries.find(entry => !entry.executed && activation(entry) && entry.args[0].attemptId === replay.attemptId);
    if (early) {
      const receipt = h.execute(early);
      if (boundary === 'selection-execution') assert.equal(receipt.disposition, 'no-effect');
    }
    assert.equal(h.audioSpy.constructors.length, 1, 'activation must await the matching selection receipt');
    assert.equal(h.audioSpy.plays.length, 1, 'selection readiness cannot be inferred from intent application');

    if (!selection.executed) h.execute(selection);
    await h.complete(selection);
    await h.flush({ holdScript: entry => entry === early });
    await Promise.all([first, second]);
    if (early) await h.complete(early);
    await h.flush(); await h.clock.advance(2000); await h.flush();

    assert.equal(pipeline.attempt, replay);
    assert.equal(h.page(7).__freeTtsPlayback.attemptId, replay.attemptId);
    assert.equal(endedAttempt.retired, true); assert.equal(endedAttempt.completionConsumed, true);
    assert.equal(replay.loadPromise, loader, 'both resumes share one loader');
    assert.equal(h.fetches.length, fetchCount);
    assert.equal(h.audioSpy.constructors.length, 2, 'replay must activate after selection despite the earlier same-tuple request');
    assert.equal(h.audioSpy.plays.length, 2, 'two resumes produce one replay');
    assert.equal(h.audioSpy.plays[1].audio, h.page(7).__freeTtsAudio);
    assert.equal(replay.audioId, h.page(7).__freeTtsPlayback.audioId);
    assert.ok(replay.pollTimer, 'the replay retains its completion observer');
    assert.equal([...h.clock.timers.values()].filter(timer => timer.due === h.clock.now + 500).length, 1);
    const redundant = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    await h.flush(); await redundant;
    assert.equal(h.audioSpy.constructors.length, 2); assert.equal(h.audioSpy.plays.length, 2);
    await h.finish();
  });
});

test('W10 end_observed_after_resume', async t => {
  for (const policy of ['modes-off', 'repeat-current', 'loop-at-last']) await t.test(policy, async () => {
    const h = harness();
    const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline;
    if (policy === 'loop-at-last') {
      const binding = { source: 'bar', leaseId: pipeline.leaseId, sessionId: pipeline.sessionId,
        lifecycleRevision: pipeline.lifecycleRevision, action: 'jumpToSentence', idx: 1 };
      const jumped = h.send(binding, { tab: { id: pipeline.tabId } });
      await h.flush(); await jumped;
      assert.equal(pipeline.currentIdx, 1);
      await boundModeMessage(h, 'setLoopState', true);
    } else if (policy === 'repeat-current') {
      await boundModeMessage(h, 'setRepeatCurrentState', true);
    }
    const endedAttempt = pipeline.attempt;
    await h.pause();
    const poll = await startOwnedEndPoll(h, endedAttempt);
    const playCount = h.audioSpy.plays.length, constructorCount = h.audioSpy.constructors.length;

    const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    await h.flush({ holdScript: entry => entry === poll }); await resumed;
    assert.equal(pipeline.attempt, endedAttempt, 'resume before poll acceptance keeps the same attempt');
    assert.equal(h.audioSpy.plays.length, playCount, 'resume never plays the physically ended Audio');
    assert.equal(h.audioSpy.constructors.length, constructorCount);

    await finishOwnedEndPoll(h, poll);
    await h.flush(); await h.clock.advance(200); await h.flush();
    const expectedIndex = policy === 'modes-off' ? 1 : 0;
    assert.equal(pipeline.currentIdx, expectedIndex);
    assert.notEqual(pipeline.attempt, endedAttempt);
    assert.equal(pipeline.attempt.idx, expectedIndex);
    assert.equal(endedAttempt.completionObserved, true);
    assert.equal(endedAttempt.completionObservedWhilePaused, false);
    assert.equal(h.audioSpy.constructors.length, constructorCount + 1);
    assert.equal(h.audioSpy.plays.length, playCount + 1);
    await h.finish();
  });
});

test('W11 pause_at_completion_decision', async t => {
  await t.test('pause-before-decision', async () => {
    const h = harness();
    const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, endedAttempt = pipeline.attempt;
    const poll = await startOwnedEndPoll(h, endedAttempt);
    await boundModeMessage(h, 'setRepeatCurrentState', true, entry => entry === poll);
    h.page(7, 'ISOLATED').document.getElementById('free-tts-repeat-current').checked = false;
    await h.pause(entry => entry === poll);
    await finishOwnedEndPoll(h, poll);
    assert.equal(pipeline.currentIdx, 0);
    assert.equal(pipeline.attempt, endedAttempt);
    assert.equal(endedAttempt.completionObserved, true);
    assert.equal(endedAttempt.completionObservedWhilePaused, true);
    assert.equal(endedAttempt.phase, 'ended');
    assert.equal(h.audioSpy.plays.length, 1);
    await h.finish();
  });

  await t.test('decision-before-pause', async () => {
    const h = harness();
    const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, endedAttempt = pipeline.attempt;
    const poll = await startOwnedEndPoll(h, endedAttempt);
    await boundModeMessage(h, 'setRepeatCurrentState', true, entry => entry === poll);
    h.page(7, 'ISOLATED').document.getElementById('free-tts-repeat-current').checked = false;
    const originalPlayCount = h.audioSpy.plays.length;
    await finishOwnedEndPoll(h, poll);
    assert.equal(pipeline.currentIdx, 0, 'decision uses the worker-owned repeat mode, not the DOM checkbox');
    assert.notEqual(pipeline.attempt, endedAttempt);
    assert.equal(pipeline.attempt.idx, 0);
    assert.equal(endedAttempt.completionConsumed, true);

    const pause = h.pause(activation);
    await pause;
    assert.equal(pipeline.isPaused, true);
    assert.equal(pipeline.attempt.phase, 'ready');
    assert.equal(h.audioSpy.constructors.length, 1, 'new replay does not construct Audio while its activation is held');
    assert.equal(h.audioSpy.plays.length, originalPlayCount);
    await h.flush();
    assert.equal(h.audioSpy.constructors.length, 1, 'the queued replay activation is fenced by the newer pause');
    assert.equal(h.audioSpy.plays.length, originalPlayCount);
    const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    await h.flush(); await resumed;
    assert.equal(h.audioSpy.constructors.length, 2);
    assert.equal(h.audioSpy.plays.length, originalPlayCount + 1);
    await h.finish();
  });
});

const selectionRead = entry => entry.source.includes('window.getSelection()?.toString()');
const highlightInit = entry => entry.source.includes('window.__freeTtsSentences = sents');
const sessionInstall = entry => entry.func.name === 'injectPlaybackSession';
const leaseClaim = entry => entry.func.name === 'injectPageLease' && entry.args[1] === 'claim';
const mediaSetup = entry => entry.func.name === 'injectMediaSession';

function assertIdlePage(h, tabId = 7) {
  assert.equal(h.page(tabId).__freeTtsAudio ?? null, null);
  assert.equal(h.page(tabId).navigator.mediaSession.playbackState ?? 'none', 'none');
  assert.equal(h.page(tabId, 'ISOLATED').document.getElementById('free-tts-bar'), null);
  assert.equal(h.page(tabId, 'ISOLATED').__freeTtsSentences, undefined);
}

// Removing initializer ownership or the in-page retirement fence permits old
// setup to recreate resources even though its worker has already stopped.
test('W12 stop_initializer', async t => {
  for (const boundary of ['target', 'selection', 'settings', 'session-install', 'lease-claim', 'media-setup', 'highlight-init']) await t.test(boundary, async () => {
    const h = harness(); let started, held, settings;
    if (boundary === 'target') {
      h.holdTabQueries();
      started = h.send({ action: 'speakSentences', text: 'First sentence.', source: 'popup' }, {});
      assert.equal(h.queries.length, 1);
    } else if (boundary === 'selection') {
      started = h.shortcut(); await h.drain(); held = h.scripts.pending(selectionRead);
    } else {
      started = h.start();
      settings = h.storage.find(entry => 'serverUrl' in entry.defaults);
      const predicate = { 'session-install': sessionInstall, 'lease-claim': leaseClaim, 'media-setup': mediaSetup, 'highlight-init': highlightInit }[boundary];
      await h.flush({ holdScript: predicate || (() => false), holdStorage: entry => boundary === 'settings' && entry === settings });
      if (predicate) held = h.scripts.entries.find(entry => !entry.executed && predicate(entry));
      if (boundary !== 'lease-claim' && predicate) assert.ok(held);
    }
    const stopped = h.send({ action: 'stopPlayback', source: 'popup' }, {});
    assert.equal((await h.snapshot()).state, 'idle', 'retirement is public before cleanup completes');
    await h.flush({ holdScript: entry => entry === held, holdStorage: entry => entry === settings });
    await stopped;
    const afterFence = h.scripts.entries.length;
    if (boundary === 'target') h.queries[0].resolve([{ id: 7 }]);
    if (held) { if (!held.executed) h.execute(held); await h.complete(held); }
    await h.flush(); await started;
    assert.equal(h.scripts.entries.length, afterFence, 'obsolete initialization dispatches no continuation after stop');
    assert.equal((await h.snapshot()).state, 'idle'); assert.equal(h.worker.pipeline, null);
    assert.equal(h.audioSpy.constructors.length, 0); assert.equal(h.audioSpy.plays.length, 0);
    assertIdlePage(h); await h.finish();
  });
});

// Publication by completion order, or retired cleanup rediscovering tab state,
// would replace or erase the explicitly newer session in these schedules.
test('W13 replace_initializer_latest_wins', async t => {
  for (const boundary of ['target', 'settings']) for (const tabId of [7, 8]) await t.test(`${boundary}-successor-tab-${tabId}`, async () => {
    const h = harness(); let first, oldSettings, oldPipeline;
    if (boundary === 'target') {
      h.holdTabQueries();
      first = h.send({ action: 'speakSentences', text: 'First sentence.', source: 'popup' }, {});
      assert.equal(h.worker.pipeline, null);
    } else {
      first = h.start(); oldPipeline = h.worker.pipeline;
      oldSettings = h.storage.find(entry => 'serverUrl' in entry.defaults);
      await h.flush({ holdStorage: entry => entry === oldSettings });
      assert.equal(oldPipeline.attempt.phase, 'initializing');
    }
    const second = h.start('First sentence. Second sentence.', tabId), successor = h.worker.pipeline;
    assert.ok(successor); assert.notEqual(successor, oldPipeline);
    const retirement = h.scripts.entries.find(entry => !entry.executed && entry.func.name === 'clearMediaSession');
    const holds = { holdStorage: entry => entry === oldSettings, holdScript: entry => entry === retirement };
    await h.flush(holds); await h.clock.advance(200);
    await h.flush(holds); await second;
    assert.equal(h.worker.pipeline, successor);
    assert.equal(h.page(tabId).__freeTtsPlayback.sessionId, successor.sessionId);
    const audio = h.page(tabId).__freeTtsAudio, cache = [...successor.cache], scripts = h.scripts.entries.length;
    assert.ok(audio, 'successor has activated');
    if (boundary === 'target') h.queries[0].resolve([{ id: 7 }]);
    else oldSettings.gate.resolve({ serverUrl: 'http://localhost:9999/old/', voice: 'old-voice', speed: 99 });
    if (retirement) { h.execute(retirement); await h.complete(retirement); }
    await h.flush(); await first;
    assert.equal(h.scripts.entries.slice(scripts).some(entry => sessionInstall(entry) || mediaSetup(entry) || highlightInit(entry) || activation(entry)), false,
      'losing start dispatches no further setup');
    assert.equal(h.worker.pipeline, successor); assert.equal(h.worker.active.sessionId, successor.sessionId);
    assert.equal(h.page(tabId).__freeTtsAudio, audio); assert.equal(audio.paused, false);
    assert.equal(successor.serverUrl, 'http://localhost:5000');
    assert.equal(successor.voice, 'en-US-AvaMultilingualNeural'); assert.equal(successor.speed, 0);
    assert.deepEqual([...successor.cache], cache); assert.equal(successor.attempt.completionObservedWhilePaused, false);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
    assert.ok(h.page(tabId, 'ISOLATED').document.getElementById('free-tts-bar'));
    if (tabId !== 7) assertIdlePage(h, 7);
    await h.finish();
  });
});

// A missing captured pipeline/attempt check would start obsolete audio, write
// a retired cache, or replace successor highlights at one of these boundaries.
test('W14 replace_pending_attempt_boundaries', async t => {
  for (const boundary of ['cache', 'fetch', 'body', 'highlight', 'activation']) for (const tabId of [7, 8]) await t.test(`${boundary}-successor-tab-${tabId}`, async () => {
    const h = harness(); const first = h.start(); let held;
    if (boundary === 'fetch' || boundary === 'body') {
      await h.flush({ holdResponse: () => true }); await h.clock.advance(10000); await h.drain();
      assert.equal(h.fetches.length, 3);
      if (boundary === 'body') { h.releaseResponse(h.fetches[2]); await h.drain(); }
    } else if (boundary === 'cache') await h.flush();
    else { await h.ready(boundary === 'highlight' ? highlight : activation); held = h.scripts.pending(boundary === 'highlight' ? highlight : activation); }
    await first;
    const predecessor = h.worker.pipeline, oldAttempt = predecessor.attempt, oldFetches = [...h.fetches], oldCache = [...predecessor.cache];
    const second = h.start('First sentence. Second sentence.', tabId), successor = h.worker.pipeline;
    assert.notEqual(successor, predecessor); assert.equal(predecessor.cancelled, true); assert.equal(oldAttempt.retired, true);
    const retirement = h.scripts.entries.find(entry => !entry.executed && entry.func.name === 'clearMediaSession');
    const holds = { holdScript: entry => entry === held || entry === retirement, holdResponse: entry => oldFetches.includes(entry), holdBody: entry => oldFetches.includes(entry) };
    await h.flush(holds); await h.clock.advance(200); await h.flush(holds); await second;
    const audio = h.page(tabId).__freeTtsAudio, cache = [...successor.cache], idx = successor.currentIdx;
    assert.ok(audio); const scripts = h.scripts.entries.length;
    if (retirement) { h.execute(retirement); await h.complete(retirement); }
    await h.flush({ ...holds, holdScript: entry => entry === held });
    if (held) { h.execute(held); await h.complete(held); }
    await h.flush(); await h.clock.advance(200); await h.flush();
    assert.equal(h.worker.pipeline, successor); assert.equal(successor.currentIdx, idx);
    assert.equal(h.scripts.entries.slice(scripts).some(entry => activation(entry) || mediaSetup(entry) || highlight(entry)), false,
      'obsolete work cannot dispatch activation or more loading');
    assert.deepEqual([...predecessor.cache], oldCache, 'retired preloads and fallback cannot write cache');
    assert.deepEqual([...successor.cache], cache); assert.equal(successor.attempt.completionObservedWhilePaused, false);
    assert.equal(h.page(tabId).__freeTtsPlayback.sessionId, successor.sessionId);
    assert.equal(h.page(tabId).__freeTtsAudio, audio); assert.equal(audio.paused, false);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
    assert.ok(h.page(tabId, 'ISOLATED').document.getElementById('free-tts-bar'));
    if (tabId !== 7) assertIdlePage(h, 7);
    await h.finish();
  });
});

// A selection dispatched by a retired owner cannot target the active Audio.
test('navigation_dispatched_before_replacement_is_owner_bound', async () => {
  const h = harness(); const first = h.start(); await h.ready(); await first;
  const selectingAttempt = entry => entry.func.name === 'injectSelectAttempt';
  const next = h.send({ action: 'nextSentence', source: 'popup' }, {}); await h.drain();
  const held = h.scripts.pending(selectingAttempt);
  const second = h.start(), successor = h.worker.pipeline;
  await h.ready(entry => entry === held); await second;
  const audio = h.page(7).__freeTtsAudio, plays = h.audioSpy.plays.length;
  h.execute(held); await h.complete(held); await h.flush(); await next;
  assert.equal(h.worker.pipeline, successor); assert.equal(successor.currentIdx, 0);
  assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(audio.paused, false); assert.notEqual(audio.src, '');
  assert.equal(h.audioSpy.plays.length, plays);
  await h.finish();
});

// F-7: optional API failures must not escape MAIN setup and retire ordinary
// bar/audio playback; one unsupported handler must not suppress the others.
test('F-7 optional_media_setup_failure_is_nonfatal', async t => {
  for (const failure of ['play', 'pause', 'previoustrack', 'nexttrack', 'stop', 'metadata-constructor', 'metadata-write', 'unsupported-api']) await t.test(failure, async () => {
    const h = harness(), main = h.page(7), media = main.navigator.mediaSession;
    function unavailable() { throw new Error('optional media setup unavailable'); };
    if (failure === 'unsupported-api') delete main.navigator.mediaSession;
    else if (failure === 'metadata-constructor') main.MediaMetadata = unavailable;
    else if (failure === 'metadata-write') {
      main.navigator.mediaSession = new Proxy(media, {
        set(target, key, value) { if (key === 'metadata' && value !== null) unavailable(); return Reflect.set(target, key, value); },
      });
    } else {
      const register = media.setActionHandler;
      media.setActionHandler = function (action, fn) { if (action === failure && fn !== null) unavailable(); return register.call(this, action, fn); };
    }
    const started = h.start(), pipeline = h.worker.pipeline;
    await h.ready(); const response = await started;
    assert.equal(response.ok, true); assert.equal(response.pageApplied, true);
    assert.equal((await h.snapshot()).state, 'playing'); assert.equal((await h.snapshot()).error, null);
    assert.equal(h.worker.pipeline, pipeline); assert.equal(pipeline.cancelled, false);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
    const audio = main.__freeTtsAudio;
    assert.equal(audio.paused, false); assert.ok(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'));
    assert.ok(h.logs.length > 0, 'optional Media Session failure is logged');
    if (failure !== 'unsupported-api') {
      for (const action of ['play', 'pause', 'previoustrack', 'nexttrack', 'stop']) {
        if (action !== failure) assert.equal(typeof media.handlers.get(action), 'function', `${action} remains available`);
      }
      assert.equal([...h.clock.timers.values()].filter(timer => timer.repeat === 25000).length, 1);
    }
    await h.pause(); h.assertPausedBar(); assert.equal(audio.paused, true);
    const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {}); await h.flush(); await resumed;
    assert.equal(main.__freeTtsAudio, audio); assert.equal(audio.paused, false);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 2);
    await h.finish();
  });
});

test('current_setup_failure_retires_only_captured_owner', async () => {
  const h = harness(); const first = h.start();
  await h.flush({ holdScript: mediaSetup });
  const held = h.scripts.pending(mediaSetup), predecessor = h.worker.pipeline;
  await h.fail(held, new Error('media setup unavailable')); await h.flush(); await first;
  assert.equal((await h.snapshot()).state, 'idle');
  assert.equal(predecessor.cancelled, true); assert.equal(h.audioSpy.constructors.length, 0);
  assert.equal(h.scripts.entries.some(highlightInit), false);
  await h.finish();
});

test('highlight_color_failure_is_nonfatal', async () => {
  const h = harness(); const first = h.start();
  await h.flush({ holdStorage: entry => 'highlightColor' in entry.defaults });
  const color = h.storage.find(entry => 'highlightColor' in entry.defaults);
  color.gate.reject(new Error('highlight color unavailable')); await h.ready(); await first;
  assert.equal((await h.snapshot()).state, 'playing');
  assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
  await h.finish();
});

// A session-bound stop preserves an unrelated unpublished start ticket;
// unbound popup/shortcut stop intentionally cancels both kinds of ownership.
test('bound_stop_preserves_pending_start', async t => {
  for (const origin of ['bar', 'popup', 'shortcut']) await t.test(origin, async () => {
    const h = harness(); const first = h.start(); await h.ready(); await first;
    const predecessor = h.worker.pipeline;
    h.holdTabQueries();
    const second = h.send({ source: 'popup', action: 'speakSentences', text: 'Second sentence.' }, {});
    const ticket = h.worker.pendingStart;
    assert.ok(ticket);
    const stopped = origin === 'shortcut' ? h.shortcut() : h.send(origin === 'bar'
      ? { action: 'stopPlayback', source: 'bar', ...{ leaseId: predecessor.leaseId, sessionId: predecessor.sessionId, lifecycleRevision: predecessor.lifecycleRevision } }
      : { action: 'stopPlayback', source: 'popup' }, origin === 'bar' ? { tab: { id: 7 } } : {});
    assert.equal(ticket.cancelled, origin !== 'bar');
    await h.flush(); await stopped;
    h.queries[0].resolve([{ id: 8 }]);
    await h.ready(); await second;
    if (origin === 'bar') {
      assert.ok(h.worker.pipeline); assert.notEqual(h.worker.pipeline.sessionId, predecessor.sessionId);
      assert.equal(h.worker.pipeline.tabId, 8); assert.equal(h.audioSpy.constructors.length, 2);
    } else {
      assert.equal(h.worker.pipeline, null); assert.equal(h.audioSpy.constructors.length, 1);
    }
    await h.finish();
  });
});

// A rejected old initializer still has no authority. Its response must describe
// the successor instead of leaking rejection or initiating successor cleanup.
test('retired_initializer_failure_is_inert', async () => {
  const h = harness();
  const first = h.send({ action: 'speakSentences', text: 'First sentence.', source: 'popup' }, {});
  await h.flush({ holdScript: sessionInstall });
  const held = h.scripts.pending(sessionInstall), predecessor = h.worker.pipeline;
  const second = h.start(); const successor = h.worker.pipeline;
  await h.ready(entry => entry === held); await second;
  const audio = h.page(7).__freeTtsAudio;
  await h.fail(held, new Error('obsolete installation failure')); await h.flush();
  const response = await first;
  assert.equal(response.sessionId, successor.sessionId);
  assert.equal(response.state, 'playing'); assert.equal(h.worker.pipeline, successor);
  assert.equal(predecessor.cancelled, true); assert.equal(h.page(7).__freeTtsAudio, audio);
  assert.equal(audio.paused, false); assert.ok(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'));
  await h.finish();
});

const retiredHighlights = entry => entry.world === 'ISOLATED' && entry.source.includes('delete window.__freeTtsSentences') && entry.func.name !== 'injectViewSession' && entry.func.name !== 'injectPageLease';
const retiredBar = entry => entry.world === 'ISOLATED' && entry.source.includes('view.barCreated = false');

// Completion, error and explicit stop must use the same captured retirement.
// Holding each cleanup at dispatch and completion exposes rediscovered globals.
test('W18 terminal_cleanup_overlap', async t => {
  for (const terminal of ['stop', 'completion', 'error']) for (const part of ['media', 'highlights', 'bar']) for (const boundary of ['dispatch', 'completion']) {
    await t.test(`${terminal}-${part}-${boundary}`, async () => {
      const h = harness(); const first = h.start('First sentence.');
      let settings, terminalResult;
      if (terminal === 'error') {
        settings = h.storage.find(entry => 'serverUrl' in entry.defaults);
        await h.flush({ holdStorage: entry => entry === settings });
        settings.gate.reject(new Error('current settings failure')); await h.drain();
      } else {
        await h.ready(); await first;
        if (terminal === 'stop') terminalResult = h.send({ action: 'stopPlayback', source: 'popup' }, {});
        else {
          const poll = await startOwnedEndPoll(h, h.worker.pipeline.attempt);
          await finishOwnedEndPoll(h, poll);
        }
      }
      const predicate = { media: entry => entry.func.name === 'clearMediaSession', highlights: retiredHighlights, bar: retiredBar }[part];
      await h.flush({ holdScript: predicate });
      const held = h.scripts.pending(predicate);
      if (boundary === 'completion') h.execute(held);
      assert.equal((await h.snapshot()).state, 'idle');
      const second = h.start(), successor = h.worker.pipeline;
      await h.ready(entry => entry === held); await second;
      const audio = h.page(7).__freeTtsAudio, bar = h.page(7, 'ISOLATED').document.getElementById('free-tts-bar');
      assert.ok(audio); assert.ok(bar);
      const media = h.page(7).navigator.mediaSession, handler = media.handlers.get('play'), metadata = media.metadata;
      if (!held.executed) h.execute(held);
      await h.complete(held); await h.flush(); await first;
      if (terminalResult) {
        const response = await terminalResult;
        assert.equal(response.pageApplied, part !== 'media' || boundary === 'completion',
          'F-8: a retirement rejected by a successor has no MAIN application receipt');
        assert.equal(response.sessionId, successor.sessionId);
      }
      assert.equal(h.worker.pipeline, successor); assert.equal(h.worker.active.sessionId, successor.sessionId);
      assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(audio.paused, false);
      assert.equal(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'), bar);
      assert.equal(h.page(7, 'ISOLATED').__freeTtsSentences[0], 'First sentence.');
      assert.equal(media.handlers.get('play'), handler); assert.equal(media.metadata, metadata);
      assert.equal(h.clock.timers.has(h.page(7).__freeTtsKeepalive), true);
      await h.finish();
    });
  }
});

test('retirement_cleanup_order_and_failure_continuation', async t => {
  for (const failure of ['none', 'injection', 'media-api']) await t.test(failure, async () => {
    const h = harness(); const first = h.start(); await h.ready(); await first;
    const view = h.page(7, 'ISOLATED'), media = h.page(7).navigator.mediaSession;
    const overlay = view.document.createElement('div'); overlay.className = 'free-tts-highlight-overlay'; view.document.body.appendChild(overlay);
    const oldControl = view.document.getElementById('free-tts-toggle');
    // Queue an actual old bar selection timeout so teardown must cancel it.
    for (const callback of view.document.body.listeners.get('mouseup')) callback();
    const effectsAt = h.audioSpy.effects.length;
    if (failure === 'media-api') {
      const register = media.setActionHandler;
      media.setActionHandler = function (action, fn) { if (action === 'play' && fn === null) throw new Error('unsupported clear'); return register.call(this, action, fn); };
    }
    const stopped = h.send({ action: 'stopPlayback', source: 'popup' }, {});
    if (failure === 'injection') {
      const cleanup = h.scripts.pending(entry => entry.func.name === 'clearMediaSession');
      await h.fail(cleanup, new Error('retirement injection unavailable'));
    }
    await h.flush(); const response = await stopped;
    assert.equal(response.ok, failure === 'none');
    assert.equal(response.pageApplied, failure !== 'injection', 'F-8: dispatch alone cannot acknowledge MAIN retirement');
    if (failure !== 'none') assert.equal(response.commandError.code, 'cleanup-failure');
    if (failure === 'injection') assert.equal(h.page(7).__freeTtsAudio.paused, false, 'failed MAIN cleanup never paused the Audio');
    assert.equal((await h.snapshot()).state, 'idle');
    assert.equal(view.document.getElementById('free-tts-bar'), null);
    assert.equal(view.document.querySelectorAll('.free-tts-highlight-overlay').length, 0);
    const messages = h.messages.length; oldControl.listeners.get('click')[0]();
    await h.clock.advance(150); await h.drain();
    assert.equal(h.messages.length, messages, 'removed control and its selection timer cannot send');
    if (failure !== 'injection') {
      const effects = h.audioSpy.effects.slice(effectsAt);
      const pauseAt = effects.findIndex(effect => effect.kind === 'pause');
      const highlightAt = effects.findIndex(effect => effect.kind === 'remove' && effect.className === 'free-tts-highlight-overlay');
      const barAt = effects.findIndex(effect => effect.kind === 'remove' && effect.id === 'free-tts-bar');
      assert.ok(pauseAt > 0); assert.equal(effects[pauseAt].keepaliveActive, false);
      assert.ok(effects.findIndex(effect => effect.kind === 'metadata' && effect.cleared) < pauseAt);
      for (const action of ['pause', 'previoustrack', 'nexttrack', 'stop']) assert.ok(effects.findIndex(effect => effect.kind === 'handler' && effect.action === action && effect.cleared) < pauseAt);
      assert.ok(pauseAt < highlightAt && highlightAt < barAt);
      assert.equal(media.playbackState, 'none'); assert.equal(h.page(7).__freeTtsAudio, null);
    } else {
      // Retrying the captured production cleanup is allowed after a transport
      // failure, and ensures fixture disposal never leaves timers/audio behind.
      const failed = h.scripts.entries.find(entry => entry.func.name === 'clearMediaSession' && entry.completion.settled && !entry.executed);
      h.execute(failed);
    }
    for (const play of h.audioSpy.plays) play.outcome.resolve();
    await h.flush(); h.assertNoUnintendedTimers();
  });
});

// F-8: only a valid captured MAIN receipt acknowledges stop. Later isolated
// cleanup failures still report the MAIN application that actually occurred.
test('F-8 stop_requires_owned_main_retirement_receipt', async t => {
  for (const failure of ['missing-main-receipt', 'wrong-main-owner', 'wrong-retirement-floor', 'isolated-highlights', 'isolated-bar']) await t.test(failure, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const audio = h.page(7).__freeTtsAudio;
    const stopped = h.send({ action: 'stopPlayback', source: 'popup' }, {});
    let failed;
    if (failure.endsWith('receipt') || failure.startsWith('wrong-')) {
      const main = h.scripts.pending(entry => entry.func.name === 'clearMediaSession');
      const receipt = h.execute(main);
      await h.complete(main, failure === 'missing-main-receipt' ? null : { ...receipt,
        ...(failure === 'wrong-main-owner' ? { sessionId: 'unrelated-session' } : { retirementRevision: main.args[1] + 1 }) });
    } else {
      const predicate = failure === 'isolated-highlights' ? retiredHighlights : retiredBar;
      await h.flush({ holdScript: predicate }); failed = h.scripts.pending(predicate);
      await h.fail(failed, new Error('isolated cleanup unavailable'));
    }
    await h.flush(); const response = await stopped;
    assert.equal(response.ok, false); assert.equal(response.accepted, true);
    assert.equal(response.disposition, 'failed'); assert.equal(response.commandError.code, 'cleanup-failure');
    assert.equal(response.pageApplied, failure.startsWith('isolated-'));
    assert.equal(response.state, 'idle'); assert.equal(response.sessionId, null);
    assert.equal(audio.paused, true); assert.equal(audio.src, ''); assert.equal(h.page(7).__freeTtsAudio, null);
    if (failure === 'isolated-highlights') assert.equal(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'), null,
      'bar cleanup continues after highlight cleanup failed');
    if (failed) h.execute(failed);
    await h.finish();
  });
});

async function runPageFunction(h, template, args, world = template.world) {
  const completion = h.scripts.dispatch({ target: template.target, world, func: template.func, args });
  const entry = h.scripts.entries.at(-1); h.execute(entry); await h.complete(entry);
  return (await completion)[0].result;
}

// CAS against a token alone would win after stop advances that token's floor;
// implicit cross-lease installation or equal retired installation recreates UI.
test('lease_and_retirement_fences_survive_idle', async t => {
  for (const world of ['MAIN', 'ISOLATED']) await t.test(world, async () => {
    const h = harness(); const first = h.start(); await h.ready(); await first;
    const pipeline = h.worker.pipeline;
    const claim = h.scripts.entries.find(entry => leaseClaim(entry) && entry.world === world);
    assert.ok(claim, 'lease established in both worlds');
    const install = h.scripts.entries.find(entry => entry.func.name === (world === 'MAIN' ? 'injectPlaybackSession' : 'injectViewSession'));
    const context = h.page(7, world), key = world === 'MAIN' ? '__freeTtsPlayback' : '__freeTtsView';
    const live = context[key], audio = h.page(7).__freeTtsAudio;
    const effects = h.audioSpy.effects.length;
    const badInstall = await runPageFunction(h, install, [{ ...install.args[0], leaseId: 'unclaimed-lease', lifecycleRevision: pipeline.lifecycleRevision + 100 }, ...install.args.slice(1)]);
    assert.equal(badInstall.installed, false); assert.equal(context[key], live);
    assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(h.audioSpy.effects.length, effects);
    const observed = { leaseId: live.leaseId, lifecycleFloor: live.lifecycleFloor };
    const stopped = h.send({ action: 'stopPlayback', source: 'popup' }, {}); await h.flush(); await stopped;
    const retired = context[key], floor = retired.lifecycleFloor;
    assert.ok(floor > observed.lifecycleFloor);
    const losing = await runPageFunction(h, claim, [world, 'claim', observed, 'losing-lease', 'other-worker']);
    assert.equal(losing.claimed, false); assert.equal(context[key], retired); assert.equal(retired.lifecycleFloor, floor);
    const staleInstall = await runPageFunction(h, install, install.args);
    assert.equal(staleInstall.installed, false);
    const oldEffects = h.scripts.entries.filter(entry => world === 'MAIN' ? activation(entry) || mediaSetup(entry)
      : highlightInit(entry) || highlight(entry) || entry.source.includes('const initialLoopEnabled'));
    const played = h.audioSpy.plays.length, constructed = h.audioSpy.constructors.length;
    for (const effect of oldEffects) await runPageFunction(h, effect, effect.args);
    assert.equal(h.audioSpy.plays.length, played); assert.equal(h.audioSpy.constructors.length, constructed);
    assert.equal(context[key].lifecycleFloor, floor); assertIdlePage(h);
    await h.finish();
  });
});

// F-6: a worker's per-tab entry outlives a document. A fresh document must
// establish both world records again, and subsequent starts must still work.
test('F-6 playback_recovers_after_page_navigation', async () => {
  const h = harness(); const first = h.start(); await h.ready(); await first;
  const lease = h.worker.leases.get(7), oldMain = h.page(7), oldView = h.page(7, 'ISOLATED');
  await h.finish(); h.navigateTab(7);
  assert.notEqual(h.page(7), oldMain); assert.notEqual(h.page(7, 'ISOLATED'), oldView);
  for (let restart = 1; restart <= 2; restart++) {
    const started = h.start(), pipeline = h.worker.pipeline;
    await h.ready(); const response = await started;
    assert.equal(response.ok, true, `start ${restart} after navigation succeeds`);
    assert.equal(response.pageApplied, true); assert.equal(response.sessionId, pipeline.sessionId);
    assert.equal(h.worker.leases.get(7), lease, 'the worker retains its per-tab lease entry');
    assert.equal(h.worker.pipeline, pipeline); assert.equal((await h.snapshot()).state, 'playing');
    assert.equal(h.page(7).__freeTtsPlayback.sessionId, pipeline.sessionId);
    assert.equal(h.page(7, 'ISOLATED').__freeTtsView.sessionId, pipeline.sessionId);
    assert.ok(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'));
    assert.equal(h.audioSpy.constructors.length, restart + 1); assert.equal(h.audioSpy.plays.length, restart + 1);
    assert.equal(h.page(7).__freeTtsAudio.paused, false);
    await h.finish();
  }
});

test('shared_lease_claim_abandoned_owner_and_closed_tab', async t => {
  for (const outcome of ['fulfilled', 'rejected-after-execution']) await t.test(outcome, async () => {
    const h = harness(); const first = h.start();
    await h.flush({ holdScript: leaseClaim });
    const held = h.scripts.pending(leaseClaim);
    const predecessor = h.worker.pipeline, establishing = h.worker.leases.get(7).promise;
    const second = h.start(), successor = h.worker.pipeline;
    await h.flush({ holdScript: entry => entry === held });
    assert.equal(h.worker.leases.get(7).promise, establishing, 'one per-tab establishment in flight');
    assert.equal(h.scripts.entries.filter(leaseClaim).length, 1, 'successor shares the held claim');
    h.execute(held);
    if (outcome === 'fulfilled') await h.complete(held);
    else await h.fail(held, new Error('retired claimant transport failure'));
    await h.ready(); await Promise.all([first, second]);
    assert.equal(predecessor.cancelled, true); assert.equal(h.worker.pipeline, successor);
    assert.equal(h.page(7).__freeTtsPlayback.sessionId, successor.sessionId);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
    h.closeTab(7); await h.flush();
    assert.equal(h.worker.leases.size, 0); assert.equal((await h.snapshot()).state, 'idle');
    await h.finish();
  });
});

// Another explicit claimant wins after each read. The current owner must stop
// after three failed claims rather than spin or implicitly overwrite that lease.
test('lease_claim_contention_is_bounded', async () => {
  const h = harness(); const first = h.send({ action: 'speakSentences', text: 'First sentence.', source: 'popup' }, {});
  await h.drain(); const pipeline = h.worker.pipeline;
  for (let conflict = 0; conflict < 3; conflict++) {
    await h.flush({ holdScript: leaseClaim });
    const held = h.scripts.pending(leaseClaim);
    const record = h.page(7).__freeTtsPlayback;
    const competing = await runPageFunction(h, held, ['MAIN', 'claim', { leaseId: record?.leaseId ?? null, lifecycleFloor: record?.lifecycleFloor ?? 0 }, `contender-${conflict}`, 'other-worker']);
    assert.equal(competing.claimed, true);
    const receipt = h.execute(held); assert.equal(receipt.claimed, false);
    await h.complete(held);
  }
  await h.flush(); const response = await first;
  assert.equal(h.scripts.entries.filter(entry => leaseClaim(entry) && entry.args[3] === pipeline.leaseId).length, 3);
  assert.equal(response.ok, false); assert.equal(response.commandError.code, 'page-operation-failure');
  assert.equal((await h.snapshot()).state, 'idle'); assert.equal(h.worker.pipeline, null);
  assert.equal(h.audioSpy.constructors.length, 0); assert.equal(h.audioSpy.plays.length, 0);
  await h.finish();
});

function boundNavigation(h, action, idx) {
  const pipeline = h.worker.pipeline;
  return h.send({ action, idx, source: 'bar', leaseId: pipeline.leaseId,
    sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision }, { tab: { id: pipeline.tabId } });
}

// Reusing an index as ownership, or retiring only after the navigation injection
// completes, revives the old loader/activation when navigation returns to idx 0.
test('W15 same_index_and_return_navigation', async t => {
  for (const route of ['same-index', 'away-and-back']) for (const boundary of ['cache', 'fallback', 'highlight', 'activation']) {
    await t.test(`${route}-${boundary}`, async () => {
      const h = harness(); const started = h.start(); let held, fallback;
      if (boundary === 'fallback') {
        await h.flush({ holdResponse: () => true }); await h.clock.advance(10000); await h.drain();
        fallback = h.fetches[2]; assert.ok(fallback, 'old current-sentence fallback is pending');
        await h.flush({ holdResponse: entry => entry === fallback });
      } else if (boundary === 'cache') await h.flush();
      else { await h.ready(boundary === 'highlight' ? highlight : activation); held = h.scripts.pending(boundary === 'highlight' ? highlight : activation); }
      await started;
      const pipeline = h.worker.pipeline, original = pipeline.attempt, originalLoader = original.loadPromise;
      const commands = [boundNavigation(h, 'jumpToSentence', route === 'same-index' ? 0 : 1)];
      const second = pipeline.attempt;
      assert.notEqual(second.attemptId, original.attemptId, 'navigation allocates a new attempt before any await');
      assert.equal(original.retired, true, 'the displaced attempt loses observer/load authority synchronously');
      if (route === 'away-and-back') {
        commands.push(boundNavigation(h, 'jumpToSentence', 0));
        assert.ok(pipeline.attempt.attemptId > second.attemptId, 'returning to idx 0 is another distinct attempt');
        assert.equal(second.retired, true);
      }
      const latest = pipeline.attempt;
      const holds = { holdScript: entry => entry === held, holdResponse: entry => entry === fallback };
      await h.flush(holds); await Promise.all(commands);
      const record = h.page(7).__freeTtsPlayback, audio = record.audio;
      assert.ok(audio, 'the selected successor activates without releasing obsolete work');
      assert.equal(record.attemptId, latest.attemptId); assert.equal(record.attemptFloor, latest.attemptId);
      assert.equal(latest.audioId, record.audioId); assert.notEqual(latest.loadPromise, originalLoader);
      assert.equal(pipeline.currentIdx, 0); assert.equal(latest.idx, 0);
      assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
      assert.equal(h.audioSpy.plays[0].audio, audio);
      assert.equal(h.audioSpy.constructors[0].url, 'data:audio/mpeg;base64,AQ==');
      const scripts = h.scripts.entries.length, cache = [...pipeline.cache];
      if (held) { h.execute(held); await h.complete(held); }
      if (fallback) { h.releaseResponse(fallback); fallback.body.resolve(Uint8Array.of(9).buffer); }
      await h.flush(); await h.clock.advance(200); await h.flush();
      assert.deepEqual([...pipeline.cache], cache, 'obsolete fallback bytes cannot overwrite the selected attempt cache');
      assert.equal(h.worker.pipeline, pipeline); assert.equal(pipeline.attempt, latest);
      assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(record.audioId, latest.audioId);
      assert.equal(h.audioSpy.constructors.length, 1, 'old work cannot create another Audio at the same index');
      assert.equal(h.audioSpy.plays.length, 1, 'old work cannot play after returning to its index');
      assert.equal(latest.completionObserved, false); assert.equal(latest.completionConsumed, false);
      assert.equal(h.scripts.entries.slice(scripts).some(entry => activation(entry) || highlight(entry)), false);
      assert.ok(latest.pollTimer, 'only the latest attempt owns a scheduled completion observer');
      assert.equal([...h.clock.timers.values()].filter(timer => !timer.repeat).length, 1);
      await h.finish();
    });
  }

  await t.test('stale-selection-cannot-dispose-newer-same-index-audio', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, original = pipeline.attempt, originalAudio = h.page(7).__freeTtsAudio;
    const first = boundNavigation(h, 'jumpToSentence', 0), displaced = pipeline.attempt;
    assert.ok(displaced.attemptId > original.attemptId);
    const selectingDisplaced = entry => entry.func.name === 'injectSelectAttempt' && entry.args[0].attemptId === displaced.attemptId;
    await h.flush({ holdScript: entry => selectingDisplaced(entry) || activation(entry) });
    const selection = h.scripts.pending(selectingDisplaced);
    const second = boundNavigation(h, 'jumpToSentence', 1);
    const third = boundNavigation(h, 'jumpToSentence', 0), latest = pipeline.attempt;
    await h.flush({ holdScript: entry => entry === selection }); await Promise.all([second, third]);
    const record = h.page(7).__freeTtsPlayback, audio = record.audio;
    assert.notEqual(audio, originalAudio); assert.equal(record.attemptFloor, latest.attemptId);
    const pauses = h.audioSpy.pauses.length, plays = h.audioSpy.plays.length;
    const receipt = h.execute(selection);
    await h.complete(selection); await h.flush(); await first;
    assert.equal(record.audio, audio, 'stale selection cannot replace or clear successor Audio');
    assert.equal(record.attemptId, latest.attemptId); assert.equal(receipt.disposition, 'stale');
    assert.equal(record.audioId, latest.audioId); assert.equal(audio.paused, false); assert.notEqual(audio.src, '');
    assert.equal(h.audioSpy.pauses.length, pauses, 'stale selection cannot pause/dispose successor Audio');
    assert.equal(h.audioSpy.plays.length, plays); assert.equal(pipeline.attempt, latest);
    await h.finish();
  });

  await t.test('navigation-during-session-installation', async () => {
    const h = harness(); const started = h.start();
    await h.flush({ holdScript: sessionInstall });
    const held = h.scripts.pending(sessionInstall), pipeline = h.worker.pipeline, original = pipeline.attempt;
    const jump = boundNavigation(h, 'jumpToSentence', 0), latest = pipeline.attempt;
    assert.ok(latest.attemptId > original.attemptId); assert.equal(original.retired, true);
    h.execute(held); await h.complete(held); await h.ready();
    assert.ok(h.page(7).__freeTtsAudio, 'new selection cannot deadlock the shared session-install barrier');
    assert.equal(h.page(7).__freeTtsPlayback.attemptId, latest.attemptId);
    assert.equal(latest.audioId, h.page(7).__freeTtsPlayback.audioId);
    assert.equal(h.audioSpy.constructors.length, 1); assert.equal(h.audioSpy.plays.length, 1);
    await Promise.all([started, jump]); await h.finish();
  });

  await t.test('invalid-navigation-and-previous-at-zero-have-no-effects', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started; await h.pause();
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, audio = h.page(7).__freeTtsAudio;
    const snapshot = await h.snapshot(), scripts = h.scripts.entries.length, pauses = h.audioSpy.pauses.length;
    for (const idx of [-1, 2, 0.5, '0', null, undefined]) {
      const response = await boundNavigation(h, 'jumpToSentence', idx);
      assert.equal(response.ok, false); assert.equal(response.accepted, false);
      assert.equal(response.commandError.code, 'invalid-argument');
      assert.equal(response.sessionId, pipeline.sessionId); assert.equal(response.state, 'paused');
      assert.equal(pipeline.attempt, attempt); assert.equal(attempt.retired, false);
      assert.equal(h.scripts.entries.length, scripts, 'invalid jumps dispatch nothing');
      assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(h.audioSpy.pauses.length, pauses);
    }
    const previous = await boundNavigation(h, 'prevSentence');
    assert.equal(previous.accepted, false); assert.equal(previous.disposition, 'ignored');
    assert.equal(pipeline.attempt, attempt); assert.equal(pipeline.isPaused, true);
    assert.equal(h.scripts.entries.length, scripts); assert.deepEqual(await h.snapshot(), snapshot);
    await h.finish();
  });

  await t.test('previous-and-next-select-fresh-attempts-and-running-intent', async () => {
    const h = harness(); const started = h.worker.start(7, 'First sentence. Second sentence.'); await h.ready(); await started;
    const pipeline = h.worker.pipeline;
    assert.equal(pipeline.loopEnabled, true); assert.equal(pipeline.repeatCurrentEnabled, false);
    for (const [action, idx] of [['nextSentence', 1], ['prevSentence', 0]]) {
      await h.pause();
      const previous = pipeline.attempt, revision = pipeline.intentRevision;
      const navigation = boundNavigation(h, action), latest = pipeline.attempt;
      assert.ok(latest.attemptId > previous.attemptId); assert.equal(previous.retired, true);
      assert.equal(latest.idx, idx); assert.equal(pipeline.currentIdx, idx);
      assert.equal(pipeline.isPaused, false); assert.equal(pipeline.intentRevision, revision + 1);
      assert.equal(pipeline.localIntentFloor, pipeline.intentRevision);
      await h.flush(); const response = await navigation;
      assert.equal(response.accepted, true); assert.equal(response.sessionId, pipeline.sessionId);
      assert.equal(h.page(7).__freeTtsPlayback.attemptId, latest.attemptId);
      assert.equal(h.page(7).__freeTtsPlayback.audioId, latest.audioId);
      assert.equal(h.page(7).__freeTtsAudio.paused, false);
    }
    assert.equal(h.audioSpy.constructors.length, 3); await h.finish();
  });

  await t.test('stale-navigation-origins-cannot-bind-to-successor', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const predecessor = h.worker.pipeline;
    const owner = { leaseId: predecessor.leaseId, sessionId: predecessor.sessionId, lifecycleRevision: predecessor.lifecycleRevision };
    const replacement = h.start(); await h.ready(); await replacement;
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, audio = h.page(7).__freeTtsAudio;
    const snapshot = await h.snapshot(), scripts = h.scripts.entries.length;
    for (const action of ['prevSentence', 'nextSentence', 'jumpToSentence']) {
      const response = await h.send({ action, idx: 0, source: 'bar', ...owner }, { tab: { id: pipeline.tabId } });
      assert.equal(response.accepted, false); assert.equal(response.sessionId, pipeline.sessionId);
      assert.equal(pipeline.attempt, attempt); assert.equal(h.page(7).__freeTtsAudio, audio);
      assert.equal(h.scripts.entries.length, scripts); assert.deepEqual(await h.snapshot(), snapshot);
    }
    await h.finish();
  });

  await t.test('navigation-selects-without-overwriting-newer-pause', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, original = pipeline.attempt;
    const jump = boundNavigation(h, 'jumpToSentence', 0), latest = pipeline.attempt;
    assert.notEqual(latest.attemptId, original.attemptId);
    const selectingLatest = entry => entry.func.name === 'injectSelectAttempt' && entry.args[0].attemptId === latest.attemptId;
    await h.flush({ holdScript: entry => selectingLatest(entry) || activation(entry) });
    const selection = h.scripts.pending(selectingLatest);
    await h.pause(entry => entry === selection || activation(entry));
    const plays = h.audioSpy.plays.length;
    h.execute(selection); await h.complete(selection); await h.flush(); await jump;
    assert.equal(pipeline.isPaused, true); assert.equal(pipeline.attempt, latest);
    assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, true, 'selection itself never writes desired running state');
    assert.equal(h.page(7).__freeTtsPlayback.attemptId, latest.attemptId);
    assert.equal(h.page(7).__freeTtsAudio, null); assert.equal(h.audioSpy.plays.length, plays);
    h.assertPausedBar(); await h.finish();
  });
});

// Reading the global Audio alias or checking only idx 0 lets an old poll inspect
// or advance its same-index successor; overlapping timer polls are also a bug.
test('W16 poll_identity_and_no_overlap', async t => {
  for (const execution of ['before-navigation', 'after-navigation']) await t.test(execution, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, original = pipeline.attempt, originalAudio = h.page(7).__freeTtsAudio;
    originalAudio.ended = true;
    await h.clock.advance(500); await h.drain();
    const poll = h.scripts.pending(completionPoll);
    assert.equal(original.pollInFlight, true);
    await h.clock.advance(2000); await h.drain();
    assert.equal(h.scripts.entries.filter(completionPoll).length, 1, 'only one old poll may remain in flight');
    if (execution === 'before-navigation') {
      const sample = h.execute(poll); assert.equal(sample.owned, true); assert.equal(sample.ended, true);
    }
    const jumped = boundNavigation(h, 'jumpToSentence', 0), latest = pipeline.attempt;
    assert.equal(original.retired, true, 'navigation retires the old observer before page effects');
    assert.ok(latest.attemptId > original.attemptId);
    await h.flush({ holdScript: entry => entry === poll }); await jumped;
    const record = h.page(7).__freeTtsPlayback, audio = record.audio;
    assert.notEqual(audio, originalAudio); assert.equal(record.audioId, latest.audioId);
    assert.equal(record.attemptId, latest.attemptId); assert.equal(pipeline.currentIdx, 0);
    let successorReads = 0;
    Object.defineProperty(audio, 'ended', { configurable: true, get() { successorReads++; return true; } });
    const revision = record.audioRevision;
    const sample = poll.executed ? poll.result : h.execute(poll);
    await h.complete(poll); await h.flush();
    assert.equal(successorReads, 0, 'old poll must not inspect successor Audio even transiently');
    if (execution === 'after-navigation') assert.equal(sample.owned, false, 'an old attempt cannot observe the replacement Audio');
    assert.equal(record.audioRevision, revision, 'old poll cannot write successor end/status observations');
    assert.equal(pipeline.attempt, latest, 'obsolete completion cannot advance or retire the successor attempt'); assert.equal(pipeline.currentIdx, 0);
    assert.equal(latest.completionObserved, false); assert.equal(latest.completionObservedWhilePaused, false);
    assert.equal(latest.completionConsumed, false); assert.equal(h.page(7).__freeTtsAudio, audio);
    assert.equal(h.audioSpy.constructors.length, 2); assert.equal(h.audioSpy.plays.length, 2);
    assert.equal(original.pollTimer, null, 'old poll completion cannot schedule another old observer');
    assert.ok(latest.pollTimer); assert.equal(latest.pollInFlight, false);
    Object.defineProperty(audio, 'ended', { configurable: true, writable: true, value: false });
    await h.finish();
  });

  await t.test('missing-audio-is-not-completion', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, record = h.page(7).__freeTtsPlayback, audio = record.audio;
    record.audio = null; // The legacy global alias still points at the old Audio.
    await h.clock.advance(500); await h.drain(); const poll = h.scripts.pending(completionPoll);
    const sample = h.execute(poll); assert.equal(sample.owned, false);
    await h.complete(poll); await h.flush();
    assert.equal(attempt.completionObserved, false); assert.equal(attempt.completionConsumed, false);
    assert.equal(pipeline.currentIdx, 0); assert.equal(h.audioSpy.constructors.length, 1);
    record.audio = audio; await h.finish();
  });
});

// Removing captured-owner checks from settings/load/script failures, or live
// Audio/serial checks from play outcomes, would retire or damage same-index S2.
test('W17 late_failure', async t => {
  for (const failure of ['settings', 'fallback', 'intent-script', 'activation-script', 'selection-script', 'play']) await t.test(`${failure}-after-replacement`, async () => {
    const h = harness(); const first = h.start(); let held, settings, fallback, navigation;
    if (failure === 'settings') {
      settings = h.storage.find(entry => 'serverUrl' in entry.defaults);
      await h.flush({ holdStorage: entry => entry === settings });
    } else if (failure === 'fallback') {
      await h.flush({ holdResponse: () => true }); await h.clock.advance(10000); await h.drain();
      fallback = h.fetches[2]; assert.ok(fallback);
    } else if (failure.endsWith('script')) {
      if (failure === 'selection-script') {
        await h.ready(); await first;
        navigation = boundNavigation(h, 'jumpToSentence', 0);
        const predicate = entry => entry.func.name === 'injectSelectAttempt';
        await h.flush({ holdScript: predicate }); held = h.scripts.pending(predicate);
      } else {
        const predicate = failure === 'intent-script' ? entry => entry.func.name === 'injectWorkerIntent' : activation;
        await h.ready(predicate); held = h.scripts.pending(predicate);
      }
    } else { await h.ready(); await first; }
    const predecessor = h.worker.pipeline;
    const oldPlay = failure === 'play' ? h.audioSpy.plays[0] : null;
    const second = h.start(), successor = h.worker.pipeline;
    const holds = { holdScript: entry => entry === held, holdStorage: entry => entry === settings, holdResponse: entry => entry === fallback };
    await h.flush(holds); await h.clock.advance(200); await h.flush(holds); await second;
    const audio = h.page(7).__freeTtsAudio, record = h.page(7).__freeTtsPlayback;
    const bar = h.page(7, 'ISOLATED').document.getElementById('free-tts-bar');
    const media = h.page(7).navigator.mediaSession, metadata = media.metadata, playHandler = media.handlers.get('play');
    const snapshot = await h.snapshot(), cache = [...successor.cache], scripts = h.scripts.entries.length;
    assert.ok(audio); assert.ok(bar); assert.equal(record.sessionId, successor.sessionId);
    if (settings) settings.gate.reject(new Error('obsolete settings rejection'));
    else if (fallback) fallback.response.reject(new Error('obsolete fallback rejection'));
    else if (held) await h.fail(held, new Error('obsolete script rejection'));
    else oldPlay.outcome.reject(new Error('obsolete play rejection'));
    await h.flush(); await first;
    if (navigation) {
      const response = await navigation;
      assert.equal(response.sessionId, successor.sessionId, 'obsolete selection rejection responds with the current owner snapshot');
      assert.equal(response.ok, true, 'an obsolete failure cannot fail the current owner');
    }
    assert.equal(predecessor.cancelled, true); assert.equal(h.worker.pipeline, successor);
    assert.equal(h.worker.active.sessionId, successor.sessionId); assert.equal(successor.attempt.audioId, record.audioId);
    assert.deepEqual(await h.snapshot(), snapshot); assert.deepEqual([...successor.cache], cache);
    assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(audio.paused, false); assert.notEqual(audio.src, '');
    assert.equal(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'), bar);
    assert.equal(media.metadata, metadata); assert.equal(media.handlers.get('play'), playHandler);
    assert.equal(h.scripts.entries.slice(scripts).some(entry => entry.func.name === 'clearMediaSession'), false);
    assert.equal(record.audioError, null); assert.equal(successor.attempt.completionObservedWhilePaused, false);
    await h.finish();
  });

  await t.test('play-rejection-invalidated-by-pause-is-nonfatal', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, play = h.audioSpy.plays[0];
    await h.pause(); const snapshot = await h.snapshot(), messages = h.messages.length;
    play.outcome.reject(new Error('expected invalidated play rejection')); await h.flush();
    assert.equal(h.worker.pipeline, pipeline); assert.equal(pipeline.attempt, attempt);
    assert.deepEqual(await h.snapshot(), snapshot); assert.equal(play.audio.paused, true);
    assert.equal(h.page(7).__freeTtsPlayback.audioError, null); assert.equal(h.messages.length, messages);
    h.assertPausedBar(); await h.finish();
  });

  await t.test('future-preload-rejection-is-nonfatal', async () => {
    const h = harness(); const started = h.start();
    await h.flush({ holdResponse: entry => entry.sentence === 2 }); await h.clock.advance(200);
    await h.flush({ holdResponse: entry => entry.sentence === 2 }); await started;
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, audio = h.page(7).__freeTtsAudio;
    const snapshot = await h.snapshot(), pending = h.fetches.filter(entry => entry.sentence === 2 && !entry.response.settled);
    assert.ok(pending.length > 0);
    for (const entry of pending) entry.response.reject(new Error('future sentence unavailable'));
    await h.flush();
    assert.equal(h.worker.pipeline, pipeline); assert.equal(pipeline.attempt, attempt);
    assert.deepEqual(await h.snapshot(), snapshot); assert.equal(h.page(7).__freeTtsAudio, audio);
    assert.equal(audio.paused, false); assert.equal(pipeline.cache.has(1), false);
    assert.ok(h.page(7, 'ISOLATED').document.getElementById('free-tts-bar'));
    await h.finish();
  });
});

test('W19 media_direct_without_worker', async t => {
  await t.test('unfinished-audio', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const main = h.page(7), record = main.__freeTtsPlayback, audio = main.__freeTtsAudio;
    const initialTime = audio.currentTime, initialSequence = record.mediaSequence, initialMediaRevision = record.mediaRevision;
    const pause = h.mediaAction(7, 'pause');
    assert.equal(audio.paused, true, 'pause applies before worker delivery');
    assert.equal(audio.currentTime, initialTime, 'pause preserves the current position');
    assert.equal(record.mediaSequence, initialSequence + 1);
    assert.equal(record.mediaRevision, initialMediaRevision + 1);
    assert.deepEqual([pause.intentRevision, pause.mediaSequence, pause.mediaRevision, pause.paused],
      [record.workerIntentRevision, initialSequence + 1, initialMediaRevision + 1, true]);
    const play = h.mediaAction(7, 'play');
    assert.equal(main.__freeTtsAudio, audio, 'resume reuses the unfinished Audio identity');
    assert.equal(audio.currentTime, initialTime, 'resume preserves the current position');
    assert.equal(audio.paused, false, 'resume applies before worker delivery');
    assert.equal(record.mediaSequence, initialSequence + 2);
    assert.equal(record.mediaRevision, initialMediaRevision + 2);
    assert.equal(play.audioId, record.audioId);
    assert.equal(vm.runInContext('typeof chrome', main), 'undefined', 'MAIN has no extension API access');
    await h.finish();
  });

  await t.test('loading-without-audio', async () => {
    const h = harness(); const started = h.start();
    await h.flush({ holdResponse: () => true }); await started;
    const main = h.page(7), record = main.__freeTtsPlayback;
    assert.equal(main.__freeTtsAudio, null);
    const sequence = record.mediaSequence, revision = record.mediaRevision, constructors = h.audioSpy.constructors.length;
    const pause = h.mediaAction(7, 'pause');
    assert.equal(record.desiredPaused, true); assert.equal(pause.paused, true); assert.equal(pause.audioId, null);
    assert.equal(record.mediaSequence, sequence + 1); assert.equal(record.mediaRevision, revision + 1);
    const play = h.mediaAction(7, 'play');
    assert.equal(record.desiredPaused, false);
    assert.equal(record.mediaSequence, sequence + 2);
    assert.equal(record.mediaRevision, revision + 2);
    assert.equal(play.audioId, null);
    assert.equal(h.audioSpy.constructors.length, constructors, 'media play never constructs Audio while loading');
    assert.equal(h.audioSpy.plays.length, 0);
    await h.finish();
  });

  await t.test('ended-audio-is-not-replayed-directly', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const main = h.page(7), record = main.__freeTtsPlayback, audio = main.__freeTtsAudio;
    audio.ended = true;
    const plays = h.audioSpy.plays.length, constructors = h.audioSpy.constructors.length;
    const sequence = record.mediaSequence, revision = record.mediaRevision, position = audio.currentTime;
    const play = h.mediaAction(7, 'play');
    assert.equal(record.desiredPaused, false); assert.equal(play.audioId, record.audioId);
    assert.equal(main.__freeTtsAudio, audio); assert.equal(audio.currentTime, position);
    assert.equal(record.mediaSequence, sequence + 1); assert.equal(record.mediaRevision, revision + 1);
    assert.equal(h.audioSpy.plays.length, plays, 'ended Audio requires worker continuation');
    assert.equal(h.audioSpy.constructors.length, constructors, 'MAIN media play cannot allocate a replay');
    await h.finish();
  });
});

test('W20 media_pause_fences_queued_start', async t => {
  for (const operation of ['activation', 'resume']) await t.test(operation, async () => {
    const h = harness(); const started = h.start(); await h.ready(operation === 'activation' ? activation : () => false);
    await started;
    if (operation === 'resume') await h.pause();
    const match = operation === 'activation' ? activation : entry => entry.func.name === 'injectWorkerIntent' && !entry.args[0].paused;
    const resume = operation === 'resume' ? h.send({ action: 'resumePlayback', source: 'popup' }, {}) : null;
    if (resume) await h.flush({ holdScript: match });
    const queued = h.scripts.pending(match), main = h.page(7), record = main.__freeTtsPlayback;
    const revision = record.mediaRevision, constructors = h.audioSpy.constructors.length, plays = h.audioSpy.plays.length;
    const pause = h.mediaAction(7, 'pause');
    assert.equal(record.mediaRevision, revision + 1, 'direct page pause advances the activation fence synchronously');
    assert.equal(pause.mediaRevision, revision + 1);
    const receipt = h.execute(queued); await h.complete(queued); await h.flush();
    assert.equal(receipt.disposition, operation === 'activation' ? 'no-effect' : 'superseded-by-media');
    assert.equal(h.audioSpy.constructors.length, constructors, 'old operation cannot construct after direct pause');
    assert.equal(h.audioSpy.plays.length, plays, 'old operation cannot play after direct pause');
    assert.equal(main.__freeTtsPlayback.desiredPaused, true);
    if (resume) assert.equal((await resume).disposition, 'superseded');
    await h.finish();
  });
});

test('W21 media_notification_reordering', async t => {
  for (const finalAction of ['play', 'pause']) await t.test(`final-${finalAction}`, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, main = h.page(7), plays = h.audioSpy.plays.length;
    const first = h.mediaAction(7, 'pause');
    const second = h.mediaAction(7, 'play');
    const last = finalAction === 'pause' ? h.mediaAction(7, 'pause') : second;
    const newest = h.deliverRelay(7, last);
    assert.equal(newest.source, 'media');
    await h.command(newest, { tab: { id: 7 } });
    const stale = h.deliverRelay(7, first);
    await h.command(stale, { tab: { id: 7 } });
    const duplicate = h.deliverRelay(7, last);
    await h.command(duplicate, { tab: { id: 7 } });
    const expectedPaused = finalAction === 'pause';
    assert.equal(pipeline.isPaused, expectedPaused, 'highest admitted media revision controls the worker snapshot');
    assert.equal((await h.snapshot()).state, expectedPaused ? 'paused' : 'playing');
    assert.equal(h.audioSpy.plays.length, plays + 1, 'worker reconciliation never replays direct page effects');
    assert.equal(pipeline.lastMediaSequence, last.mediaSequence);
    assert.equal(main.__freeTtsPlayback.mediaRevision, last.mediaRevision);
    await h.finish();
  });
});

test('W22 mixed_media_worker_intents', async t => {
  await t.test('media-pause-while-page-selection-is-pending', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline;
    const navigating = h.send({ action: 'jumpToSentence', source: 'popup', idx: 1 }, {});
    const selection = entry => entry.func.name === 'injectSelectAttempt';
    await h.flush({ holdScript: selection });
    const held = h.scripts.pending(selection), attempt = pipeline.attempt;
    const pause = h.mediaAction(7, 'pause');
    const syncing = h.send(h.deliverRelay(7, pause), { tab: { id: 7 } });
    await h.flush({ holdScript: entry => entry === held });
    assert.equal(h.worker.pipeline, pipeline, 'not-yet-selected Audio is not a current-owner failure');
    assert.equal((await h.snapshot()).state, 'paused');
    assert.equal(pipeline.attempt, attempt); assert.equal(pipeline.currentIdx, 1);
    await h.flush(); await navigating; await syncing;
    assert.equal(h.worker.pipeline, pipeline); assert.equal((await h.snapshot()).state, 'paused');
    assert.equal(h.page(7).__freeTtsPlayback.attemptId, attempt.attemptId);
    assert.equal(h.page(7).__freeTtsPlayback.desiredPaused, true);
    await h.finish();
  });

  await t.test('popup-intent-outlives-duplicate-media', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pause = h.mediaAction(7, 'pause'), notification = h.deliverRelay(7, pause);
    await h.command(notification, { tab: { id: 7 } });
    assert.equal(h.worker.pipeline.isPaused, true);
    const resumed = await h.command({ action: 'resumePlayback', source: 'popup' });
    assert.equal(resumed.state, 'playing');
    await h.command(notification, { tab: { id: 7 } });
    assert.equal((await h.snapshot()).state, 'playing', 'duplicate old media pause cannot roll back popup intent');
    await h.finish();
  });

  await t.test('later-bar-toggle-with-conflict-receipt-delayed', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started; await h.pause();
    const pipeline = h.worker.pipeline, record = h.page(7).__freeTtsPlayback, plays = h.audioSpy.plays.length;
    const resumed = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    const resumeMatch = entry => entry.func.name === 'injectWorkerIntent' && !entry.args[0].paused;
    await h.flush({ holdScript: resumeMatch }); const old = h.scripts.pending(resumeMatch);
    const pause = h.mediaAction(7, 'pause'), receipt = h.execute(old);
    assert.equal(receipt.disposition, 'superseded-by-media');
    const toggle = boundNavigation(h, 'togglePlayback');
    assert.equal(pipeline.isPaused, true, 'later toggle uses accepted worker running intent');
    const latestIntent = pipeline.intentRevision;
    await h.flush({ holdScript: entry => entry === old }); const newest = await toggle;
    assert.equal(newest.state, 'paused'); assert.equal(newest.disposition, 'superseded'); assert.equal(newest.pageApplied, false);
    const ignored = await h.send(h.deliverRelay(7, pause), { tab: { id: 7 } });
    assert.equal(ignored.accepted, false, 'older media worker base is below the new local floor');
    await h.complete(old); await h.flush(); const older = await resumed;
    assert.equal(older.state, 'paused'); assert.equal(older.intentRevision, latestIntent);
    assert.equal(record.desiredPaused, true); assert.equal(h.audioSpy.plays.length, plays, 'no conflict receipt retries running intent');
    const fresh = await h.command({ action: 'resumePlayback', source: 'popup' });
    assert.equal(fresh.state, 'playing'); assert.equal(h.audioSpy.plays.length, plays + 1); await h.finish();
  });

  await t.test('page-pause-wins-old-worker-base', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    await h.pause();
    const pipeline = h.worker.pipeline, record = h.page(7).__freeTtsPlayback, plays = h.audioSpy.plays.length;
    const resumePromise = h.send({ action: 'resumePlayback', source: 'popup' }, {});
    const pendingIntent = entry => entry.func.name === 'injectWorkerIntent' && entry.args[0].paused === false;
    await h.flush({ holdScript: pendingIntent });
    const resumeScript = h.scripts.pending(pendingIntent);
    const revision = record.mediaRevision;
    const notification = h.mediaAction(7, 'pause');
    assert.equal(record.mediaRevision, revision + 1);
    const receipt = h.execute(resumeScript);
    assert.equal(receipt.disposition, 'superseded-by-media');
    await h.complete(resumeScript); await h.flush();
    const response = await resumePromise;
    assert.equal(record.desiredPaused, true);
    assert.equal(h.audioSpy.plays.length, plays, 'conflicted running intent is never blindly retried');
    assert.equal(response.disposition, 'superseded');
    assert.equal(response.state, 'paused');
    assert.equal(pipeline.mediaRevision, revision + 1);
    assert.equal(notification.paused, true);
    const retry = await h.command({ action: 'resumePlayback', source: 'popup' });
    assert.equal(retry.state, 'playing', 'a new post-reconciliation command can resume');
    await h.finish();
  });
});

test('W23 originating_session_messages', async () => {
  const h = harness(); const first = h.start('First sentence.'); await h.ready(); await first;
  const predecessor = h.worker.pipeline, old = { source: 'bar', action: 'togglePlayback', leaseId: predecessor.leaseId,
    sessionId: predecessor.sessionId, lifecycleRevision: predecessor.lifecycleRevision };
  const mediaOld = { ...old, source: 'media', action: 'mediaPause', intentRevision: predecessor.intentRevision,
    mediaSequence: 1, mediaRevision: 1, paused: true, attemptId: predecessor.attempt.attemptId,
    audioId: predecessor.attempt.audioId, audioStatus: 'paused', startSerial: 1, audioRevision: 2 };
  const second = h.start('First sentence.'); const successor = h.worker.pipeline;
  await h.ready(); await second;
  const staleBar = await h.send(old, { tab: { id: 7 } });
  const staleMedia = await h.command(mediaOld, { tab: { id: 7 } });
  const wrongTab = await h.send(old, { tab: { id: 8 } });
  assert.equal(staleBar.sessionId, successor.sessionId);
  assert.equal(staleMedia.sessionId, successor.sessionId);
  assert.equal(wrongTab.sessionId, successor.sessionId);
  assert.equal(staleBar.accepted, false); assert.equal(staleMedia.accepted, false); assert.equal(wrongTab.accepted, false);
  for (const action of ['jumpToSentence', 'togglePlayback', 'setLoopState', 'setRepeatCurrentState', 'stopPlayback']) {
    for (const tabId of [7, 8]) {
      const ignored = await h.command({ ...old, action, idx: 0, enabled: true }, { tab: { id: tabId } });
      assert.equal(ignored.disposition, 'ignored'); assert.equal(ignored.accepted, false);
      assert.equal(ignored.sessionId, successor.sessionId); assert.equal(h.worker.pipeline, successor);
    }
  }
  for (const action of ['mediaPause', 'mediaResume', 'nextSentence', 'prevSentence', 'stopPlayback']) {
    const ignored = await h.command({ ...mediaOld, action }, { tab: { id: 7 } });
    assert.equal(ignored.disposition, 'ignored'); assert.equal(ignored.accepted, false); assert.equal(ignored.sessionId, successor.sessionId);
  }
  assert.equal(successor.isPaused, false, 'old session media cannot mutate the replacement');
  const popupPause = await h.command({ action: 'pausePlayback', source: 'popup' });
  assert.equal(popupPause.sessionId, successor.sessionId); assert.equal(popupPause.state, 'paused');
  await h.finish();

  const pending = harness(); const oldStart = pending.start('First sentence.'); await pending.ready(); await oldStart;
  const owner = pending.worker.pipeline;
  pending.holdTabQueries();
  const newTicket = pending.send({ action: 'speakSentences', source: 'popup', text: 'Second sentence.' }, {});
  await pending.drain(); const ticket = pending.worker.pendingStart;
  assert.ok(ticket, 'new target lookup ticket is live while the prior session remains published');
  const stopPromise = pending.send({ source: 'bar', action: 'stopPlayback', leaseId: owner.leaseId,
    sessionId: owner.sessionId, lifecycleRevision: owner.lifecycleRevision }, { tab: { id: 7 } });
  await pending.flush();
  const stop = await stopPromise;
  assert.equal(stop.sessionId, null);
  assert.equal(pending.worker.pendingStart, ticket, 'bound predecessor stop cannot cancel an unrelated pending start');
  pending.queries[0].resolve([{ id: 7 }]); await pending.flush(); await newTicket;
  assert.notEqual(pending.worker.pipeline?.sessionId, owner.sessionId);
  await pending.finish();
});

test('W24 current_errors', async t => {
  for (const unsupported of ['absent', 'handlers', 'metadata', 'playback-state']) await t.test(`optional-media-${unsupported}`, async () => {
    const h = harness(), main = h.page(7), media = main.navigator.mediaSession;
    if (unsupported === 'absent') delete main.navigator.mediaSession;
    else if (unsupported === 'handlers') media.setActionHandler = () => { throw new Error('handlers unavailable'); };
    else main.navigator.mediaSession = new Proxy(media, { set(target, key, value) {
      if (key === (unsupported === 'metadata' ? 'metadata' : 'playbackState')) throw new Error('optional media property unavailable');
      return Reflect.set(target, key, value);
    } });
    const started = h.start(); await h.ready(); const response = await started;
    assert.equal(response.ok, true); assert.equal(response.state, 'playing');
    assert.equal(h.audioSpy.plays.length, 1, 'optional media failure cannot prevent Audio play');
    h.audioSpy.plays[0].outcome.resolve(); await h.clock.advance(500); await h.flush();
    assert.equal((await h.snapshot()).audioStatus, 'playing');
    await h.pause(); h.assertPausedBar(); assert.equal(main.__freeTtsAudio.paused, true);
    assert.equal((await h.snapshot()).error, null); await h.finish();
  });

  await t.test('settings-failure-is-terminal', async () => {
    const h = harness(); const started = h.start();
    const settings = h.storage.find(entry => 'serverUrl' in entry.defaults);
    await h.flush({ holdStorage: entry => entry === settings });
    settings.gate.reject(new Error('settings unavailable')); await h.flush(); await started;
    assert.equal((await h.snapshot()).error.code, 'settings-failure');
    assert.equal(h.worker.pipeline, null); assertIdlePage(h); await h.finish();
  });

  for (const failure of ['load-failure', 'load-timeout']) await t.test(failure, async () => {
    const h = harness(); const started = h.start();
    await h.flush({ holdResponse: () => true }); await started;
    await h.clock.advance(10000); await h.drain();
    const fallback = h.fetches[2]; assert.ok(fallback, 'fallback dispatched after cache bound');
    if (failure === 'load-timeout') {
      for (const fetch of h.fetches) fetch.body.promise.catch(() => {});
      await h.clock.advance(120000);
    } else fallback.response.reject(new Error('current fetch failed'));
    await h.flush();
    assert.equal(h.worker.pipeline, null);
    const snapshot = await h.snapshot(); assert.equal(snapshot.state, 'idle'); assert.equal(snapshot.error.code, failure);
    assertIdlePage(h); await h.finish();
  });

  for (const operation of ['intent', 'activation', 'poll', 'render', 'create-bar']) {
    for (const failure of ['reject', 'no-receipt']) await t.test(`${operation}-${failure}`, async () => {
      const h = harness(); let started, command;
      const match = operation === 'intent' ? entry => entry.func.name === 'injectWorkerIntent'
        : operation === 'activation' ? activation : operation === 'poll' ? completionPoll
        : operation === 'create-bar' ? entry => entry.source.includes('const bar = document.createElement') : renderUpdate;
      if (operation === 'intent' || operation === 'render' || operation === 'poll') {
        started = h.start(); await h.ready(); await started;
        if (operation === 'poll') { await h.clock.advance(500); await h.drain(); }
        else command = h.send({ action: 'pausePlayback', source: 'popup' }, {});
      } else started = h.start();
      await h.ready(match);
      if (operation === 'create-bar') {
        const pipeline = h.worker.pipeline;
        command = h.worker.showControlBar(pipeline, h.worker.buildPlaybackSnapshot(),
          { loopEnabled: pipeline.loopEnabled, repeatCurrentEnabled: pipeline.repeatCurrentEnabled });
        await h.drain();
      }
      const held = ['create-bar', 'render'].includes(operation) ? h.scripts.entries.findLast(entry => !entry.executed && match(entry)) : h.scripts.pending(match);
      if (failure === 'reject') await h.fail(held, new Error(`${operation} injection failed`));
      else { h.execute(held); await h.complete(held, null); }
      await h.flush(); await started;
      const response = command && operation !== 'create-bar' ? await command : await h.snapshot();
      if (operation === 'create-bar') await command;
      assert.equal(response.state, 'idle', `${operation} failure retires its owner`);
      const expected = operation === 'poll' ? 'poll-failure' : ['render', 'create-bar'].includes(operation) ? 'render-failure' : 'page-operation-failure';
      assert.equal(response.error.code, expected);
      if (command && operation !== 'create-bar') { assert.equal(response.ok, false); assert.equal(response.disposition, 'failed'); }
      assert.equal(h.worker.pipeline, null); assertIdlePage(h); await h.finish();
    });
  }

  await t.test('missing-owned-audio-is-terminal', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt;
    h.page(7).__freeTtsPlayback.audio = null;
    await h.clock.advance(500); await h.flush();
    assert.equal((await h.snapshot()).error.code, 'missing-audio'); assert.equal(h.worker.pipeline, null);
    assert.equal(pipeline.currentIdx, 0); assert.equal(attempt.completionObserved, false);
    await h.finish();
  });

  for (const operation of ['activation', 'render']) await t.test(`obsolete-${operation}-failure-after-pause`, async () => {
    const h = harness(), match = operation === 'activation' ? activation : renderUpdate;
    const started = h.start(); await h.ready(match);
    const held = h.scripts.pending(match), pipeline = h.worker.pipeline, attempt = pipeline.attempt;
    await h.pause(entry => entry === held);
    const snapshot = await h.snapshot();
    await h.fail(held, new Error('obsolete operation failed')); await h.flush(); await started;
    assert.equal(h.worker.pipeline, pipeline); assert.equal(pipeline.attempt, attempt);
    assert.deepEqual(await h.snapshot(), snapshot); assert.equal(snapshot.error, null); h.assertPausedBar();
    await h.finish();
  });

  await t.test('missing-live-bar-is-terminal', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    h.page(7, 'ISOLATED').document.getElementById('free-tts-bar').remove();
    const response = await h.command({ action: 'pausePlayback', source: 'popup' });
    assert.equal(response.state, 'idle'); assert.equal(response.error.code, 'render-failure');
    assert.equal(response.ok, false); await h.finish();
  });

  await t.test('current-play-rejection-is-terminal', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const play = h.audioSpy.plays.at(-1); play.outcome.reject(new Error('current play rejected')); await h.drain();
    const event = h.messages.findLast(message => message.tabId === 7 && message.world === 'MAIN' && message.data.action === 'audioPlaybackEvent');
    assert.ok(event, 'current play outcome is reported from MAIN');
    const forwarded = h.deliverRelay(7, event.data);
    const response = await h.command(forwarded, { tab: { id: 7 } });
    assert.equal(response.state, 'idle');
    assert.equal(response.error.code, 'play-rejection');
    assert.equal(h.worker.pipeline, null);
    await h.finish();
  });

  await t.test('poll-failure-is-terminal', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    await h.clock.advance(500); await h.drain();
    const poll = h.scripts.pending(completionPoll);
    await h.fail(poll, new Error('poll transport failed')); await h.flush();
    assert.equal(h.worker.pipeline, null, 'owned observer failure retires the affected session');
    const snapshot = await h.snapshot();
    assert.equal(snapshot.state, 'idle'); assert.equal(snapshot.error.code, 'poll-failure');
    await h.finish();
  });

  await t.test('response-is-canonical-while-play-promise-is-pending', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    await h.pause();
    const before = h.audioSpy.plays.length;
    const response = await h.command({ action: 'resumePlayback', source: 'popup' });
    assert.equal(h.audioSpy.plays.length, before + 1);
    assert.equal(response.state, 'playing'); assert.equal(response.phase, 'starting');
    assert.equal(response.audioStatus, 'starting', 'accepted intent is not reported as audible success');
    assert.equal(response.pageApplied, true);
    await h.finish();
  });
});

// F-9: polling an error from a media revision not yet known to the worker
// cannot consume its audioRevision and hide the rejection from reconciliation.
test('W24 F-9 poll_before_unreconciled_play_rejection', async t => {
  for (const delivery of ['withheld', 'action-before-outcome']) await t.test(delivery, async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    h.audioSpy.plays[0].outcome.resolve(); await h.drain();
    const initialOutcome = h.messages.findLast(message => message.world === 'MAIN' && message.data.action === 'audioPlaybackEvent').data;
    await h.command(h.deliverRelay(7, initialOutcome), { tab: { id: 7 } });
    await h.pause();
    const pipeline = h.worker.pipeline, attempt = pipeline.attempt, audio = h.page(7).__freeTtsAudio;
    const resume = h.mediaAction(7, 'play');
    assert.equal(h.audioSpy.plays.length, 2); assert.equal(h.audioSpy.plays[1].audio, audio);
    h.audioSpy.plays[1].outcome.reject(new Error('direct media play rejected before poll'));
    await h.drain();
    const failure = h.messages.findLast(message => message.world === 'MAIN' && message.data.action === 'audioPlaybackEvent').data;
    assert.equal(failure.audioStatus, 'error'); assert.ok(failure.mediaRevision > pipeline.mediaRevision);
    await h.clock.advance(500); await h.drain();
    const poll = h.scripts.pending(completionPoll), sample = h.execute(poll);
    assert.equal(sample.audioStatus, 'error'); assert.equal(sample.audioRevision, failure.audioRevision);
    await h.complete(poll); await h.drain();
    assert.ok(attempt.audioRevision < failure.audioRevision, 'unreconciled poll cannot consume the rejected observation revision');
    assert.ok(h.scripts.entries.some(entry => !entry.executed && entry.func.name === 'injectPlaybackObservation'),
      'poll requests a fresh owned sample before any activation continuation');

    if (delivery === 'action-before-outcome') {
      await h.command(h.deliverRelay(7, resume), { tab: { id: 7 } });
      await h.command(h.deliverRelay(7, failure), { tab: { id: 7 } });
    }
    await h.flush();
    assert.equal(h.audioSpy.plays.length, 2, 'poll and delayed relay delivery cannot retry the rejected play');
    const snapshot = await h.snapshot();
    assert.equal(snapshot.state, 'idle', 'poll must reconcile the current rejection even without relay delivery');
    assert.equal(snapshot.error?.code, 'play-rejection');
    assert.equal(snapshot.error?.message, 'direct media play rejected before poll');
    assert.equal(h.worker.pipeline, null); assert.equal(pipeline.currentIdx, 0);
    assert.equal(attempt.completionObserved, false); assertIdlePage(h);
    await h.finish();
  });
});

test('W26 relay_and_world_invariants', async () => {
  const h = harness(); const started = h.start(); await h.ready(); await started;
  const pipeline = h.worker.pipeline, owner = h.worker.pageOwner(pipeline), isolated = h.page(7, 'ISOLATED');
  for (let i = 0; i < 2; i++) { const setup = h.worker.setupMediaSession(pipeline); await h.flush(); await setup; }
  assert.equal(isolated.listeners.get('message').length, 1, 'repeated relay installation remains idempotent');
  const keepalives = [...h.clock.timers.values()].filter(timer => timer.repeat === 25000);
  assert.equal(keepalives.length, 1, 'repeated media setup leaves one keepalive interval');
  const full = h.mediaAction(7, 'pause');
  for (const field of ['action', 'leaseId', 'sessionId', 'lifecycleRevision', 'intentRevision', 'mediaSequence', 'mediaRevision',
    'paused', 'attemptId', 'audioId', 'audioStatus', 'startSerial', 'audioRevision']) assert.ok(field in full, `missing token field ${field}`);
  const forwarded = h.deliverRelay(7, full);
  assert.deepEqual(Object.fromEntries(Object.entries(forwarded).filter(([key]) => key !== 'source')),
    Object.fromEntries(Object.entries(full)), 'relay preserves the complete token');
  assert.equal(forwarded.source, 'media');
  const record = h.page(7).__freeTtsPlayback, audio = h.page(7).__freeTtsAudio;
  for (const [action, command] of [['previoustrack', 'prevSentence'], ['nexttrack', 'nextSentence'], ['stop', 'stopPlayback']]) {
    const sequence = record.mediaSequence, mediaRevision = record.mediaRevision, effects = h.audioSpy.effects.length;
    const notification = h.mediaAction(7, action);
    assert.equal(notification.action, command); assert.equal(notification.mediaSequence, sequence + 1);
    assert.equal(record.mediaRevision, mediaRevision); assert.equal(record.desiredPaused, true);
    assert.equal(h.page(7).__freeTtsAudio, audio); assert.equal(h.audioSpy.effects.length, effects,
      'media navigation/stop only requests worker action');
  }
  assert.equal(h.deliverRelay(7, full, false), undefined, 'relay rejects non-window message sources');
  assert.equal(vm.runInContext('typeof chrome', h.page(7, 'MAIN')), 'undefined');
  assert.ok(owner.sessionId);
  const mainEffects = h.scripts.entries.filter(entry => (entry.source.includes('mediaSession') || entry.source.includes('record.audio')) &&
    !(entry.func.name === 'injectPageLease' && entry.args[0] === 'ISOLATED'));
  assert.ok(mainEffects.length > 0);
  assert.ok(mainEffects.every(entry => entry.world === 'MAIN'), 'Audio and Media Session effects execute only in MAIN');
  assert.equal(source.includes('free-tts-media-session'), false);
  await h.finish();
  const cleanup = h.scripts.entries.find(entry => entry.func.name === 'clearMediaSession');
  const first = await runPageFunction(h, cleanup, cleanup.args, 'MAIN');
  const second = await runPageFunction(h, cleanup, cleanup.args, 'MAIN');
  assert.equal(first.disposition, 'applied'); assert.equal(second.disposition, 'applied');
  for (const action of ['play', 'pause', 'previoustrack', 'nexttrack', 'stop']) assert.equal(h.page(7).navigator.mediaSession.handlers.get(action), null);
  assert.equal(h.clock.timers.has(h.page(7).__freeTtsKeepalive), false);
  for (const play of h.audioSpy.plays) play.outcome.resolve();
  await h.flush(); h.assertNoUnintendedTimers();
});

test('W27 snapshot_and_popup_responses', async t => {
  await t.test('popup-start-pause-resume-stop-during-settings', async () => {
    const h = harness(), popup = popupAdapter(h); await popup.ready;
    const consumer = popup.context.__popup;
    assert.equal(consumer.state, 'idle'); popup.elements.get('textInput').value = 'First sentence.';
    const heldSettings = entry => 'serverUrl' in entry.defaults;
    const started = consumer.speak(); await h.drain(); await h.flush({ holdStorage: heldSettings }); await started;
    assert.equal(consumer.state, 'playing'); assert.equal((await h.snapshot()).phase, 'initializing');
    const pause = consumer.speak(); await h.flush({ holdStorage: heldSettings }); await pause;
    assert.equal(consumer.state, 'paused');
    const resume = consumer.speak(); await h.flush({ holdStorage: heldSettings }); await resume;
    assert.equal(consumer.state, 'playing'); assert.equal(h.audioSpy.constructors.length, 0);
    const stopped = consumer.stop(); await h.flush({ holdStorage: heldSettings }); await stopped;
    assert.equal(consumer.state, 'idle'); assert.equal(popup.elements.get('stopBtn').disabled, true);
    assert.ok(popup.requests.every(entry => entry.message.source === 'popup'));
    await h.flush(); await h.finish();
  });

  await t.test('canonical-view-order-and-worker-epoch', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const popup = popupAdapter(h); await popup.ready;
    const consumer = popup.context.__popup;
    assert.equal(consumer.state, 'playing');
    assert.equal(typeof consumer.apply, 'function', 'popup exposes the shared canonical snapshot consumer');
    assert.equal(typeof consumer.handle, 'function', 'popup commands use the shared response consumer');
    const oldWorker = consumer.workerId, oldRevision = consumer.viewRevision;
    popup.holdNext('pausePlayback');
    const pause = consumer.speak();
    await h.drain(); const held = popup.requests.findLast(entry => entry.message.action === 'pausePlayback');
    assert.ok(held); await h.flush(); await held.actualPromise;
    const resume = await h.command({ action: 'resumePlayback', source: 'popup' });
    assert.equal(resume.state, 'playing');
    assert.equal(consumer.apply(resume), true);
    await popup.release(held); await pause;
    assert.equal(consumer.state, 'playing', 'older delayed command snapshot cannot roll back a newer view');

    const ignored = { ...resume, state: 'paused', disposition: 'ignored', viewRevision: resume.viewRevision + 1 };
    await consumer.handle(ignored, 'pausePlayback');
    assert.equal(consumer.state, 'paused', 'ignored responses render the worker snapshot rather than requested intent');
    await consumer.handle({ ...ignored, state: 'playing', disposition: 'superseded', viewRevision: ignored.viewRevision + 1 }, 'pausePlayback');
    assert.equal(consumer.state, 'playing', 'a superseded pause renders the actual running state');
    const commandError = { code: 'cleanup-failure', message: 'page unavailable' };
    await consumer.handle({ ...ignored, ok: false, commandError, state: 'paused', disposition: 'failed', viewRevision: ignored.viewRevision + 2 }, 'stopPlayback');
    assert.equal(consumer.state, 'paused', 'a failed stop renders its actual owner snapshot');
    assert.equal(popup.warnings.at(-1)[1], commandError);
    const fresh = { ...resume, workerId: `${oldWorker}-restart`, viewRevision: 1, state: 'idle', sessionId: null,
      intentRevision: null, mediaRevision: null, phase: 'idle', audioStatus: 'none' };
    assert.equal(consumer.apply(fresh), true);
    assert.equal(consumer.workerId, fresh.workerId); assert.equal(consumer.viewRevision, 1);
    await consumer.handle({ ...resume, workerId: oldWorker, viewRevision: oldRevision + 100, state: 'playing' }, 'resumePlayback',
      { requestEpoch: 1, workerIdAtIssue: oldWorker, acceptedEpochAtIssue: 0 });
    assert.equal(consumer.state, 'idle', 'response from the former worker cannot overwrite the new worker');
    await h.finish();
  });

  await t.test('transport-error-refreshes-once-and-retains-confirmed-state', async () => {
    const h = harness(); const started = h.start(); await h.ready(); await started;
    const popup = popupAdapter(h); await popup.ready; const consumer = popup.context.__popup;
    popup.failNext('pausePlayback');
    const button = popup.elements.get('speakBtn');
    await button.listeners.get('click')[0]();
    assert.equal(popup.requests.filter(entry => entry.message.action === 'getPlaybackState').length, 2,
      'initial read plus exactly one bounded transport refresh');
    assert.equal(consumer.state, 'playing', 'transport failure does not guess idle or paused');
    popup.failNext('pausePlayback'); popup.failNext('getPlaybackState');
    await button.listeners.get('click')[0]();
    assert.equal(popup.requests.filter(entry => entry.message.action === 'getPlaybackState').length, 3,
      'failed refresh does not recursively refresh again');
    assert.equal(consumer.state, 'playing', 'failed command and refresh retain the last confirmed state');
    await h.finish();
  });
});
