'use strict';

const test = require('node:test');
const assert = require('node:assert/strict');
const vm = require('node:vm');
const fs = require('node:fs');
const path = require('node:path');
const { randomUUID } = require('node:crypto');
const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');

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
function fixtureDocument() {
  const nodes = [];
  const element = (tagName) => {
    const node = { tagName: tagName.toUpperCase(), children: [], style: {}, attributes: {}, listeners: new Map(), textContent: '',
      appendChild(child) { child.parentNode = this; this.children.push(child); return child; },
      remove() { if (this.parentNode) this.parentNode.children = this.parentNode.children.filter(n => n !== this); },
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
  const clock = new VirtualClock(), scripts = new ScriptGate(), storage = [], fetches = [], messages = [], logs = [];
  const audioSpy = { constructors: [], plays: [], pauses: [], writes: [], media: [] };
  const pages = new Map(); let listener, commandListener;
  const timers = Object.fromEntries(['setTimeout', 'setInterval', 'clearTimeout', 'clearInterval'].map(key => [key, clock[key].bind(clock)]));
  const chrome = {
    runtime: { onMessage: { addListener(fn) { listener = fn; } } }, commands: { onCommand: { addListener(fn) { commandListener = fn; } } },
    tabs: { query: async () => [{ id: 7 }] }, scripting: { executeScript: details => scripts.dispatch(details) },
    storage: { sync: { get(defaults) { const gate = new Deferred(); storage.push({ defaults, gate }); return gate.promise; } } },
  };
  const quietConsole = { error: (...args) => logs.push(args), warn: (...args) => logs.push(args), log() {} };
  function page(tabId, world = 'MAIN') {
    if (!pages.has(tabId)) {
      const document = fixtureDocument(), contexts = {};
      for (const name of ['MAIN', 'ISOLATED']) {
        const events = new Map(), context = { ...timers, console: quietConsole, document, NodeFilter: { SHOW_TEXT: 4 }, navigator: {}, scrollX: 0, scrollY: 0, innerHeight: 900, scrollTo() {},
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
          const media = { handlers: new Map(), setActionHandler(action, fn) { this.handlers.set(action, fn); } };
          Object.defineProperty(media, 'playbackState', { get() { return this.state; }, set(value) { this.state = value; audioSpy.media.push({ value, time: clock.now }); } });
          context.navigator.mediaSession = media; context.MediaMetadata = function (value) { Object.assign(this, value); };
          context.Audio = function (url) {
            this.id = `audio-${audioSpy.constructors.length + 1}`; this.ended = false; this.paused = true;
            let currentTime = 3, src = url;
            Object.defineProperties(this, {
              currentTime: { get: () => currentTime, set: value => { currentTime = value; audioSpy.writes.push({ audio: this, field: 'currentTime', value }); } },
              src: { get: () => src, set: value => { src = value; audioSpy.writes.push({ audio: this, field: 'src', value }); } },
            });
            this.play = () => { this.paused = false; const outcome = new Deferred(); audioSpy.plays.push({ audio: this, time: clock.now, intentRevision: context.__freeTtsPlayback?.workerIntentRevision, mediaRevision: context.__freeTtsPlayback?.mediaRevision, outcome }); return outcome.promise; };
            this.pause = () => { this.paused = true; audioSpy.pauses.push({ audio: this, time: clock.now }); };
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
  vm.runInContext(`${source}\nglobalThis.__worker = { get pipeline() { return sentencePipeline; }, get active() { return activePlayback; }, start: startSentenceTTS };`, worker);
  assert.equal(typeof commandListener, 'function');
  const api = { clock, scripts, storage, fetches, audioSpy, messages, logs, worker: worker.__worker, page,
    start(text = 'First sentence. Second sentence.', tabId = 7) {
      const result = worker.__worker.start(tabId, text);
      const owner = worker.__worker.pipeline;
      if (owner) {
        const binding = { source: 'bar', sessionId: owner.sessionId, lifecycleRevision: owner.lifecycleRevision };
        void api.send({ ...binding, action: 'setLoopState', enabled: false }, { tab: { id: tabId } });
        void api.send({ ...binding, action: 'setRepeatCurrentState', enabled: false }, { tab: { id: tabId } });
      }
      return result;
    },
    send(message, sender = { tab: { id: 7 } }) { return new Promise((resolve, reject) => { try { listener(message, sender, resolve); } catch (error) { reject(error); } }); },
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
const activation = entry => entry.world === 'MAIN' && (entry.func.name === 'injectActivation' || entry.source.includes('new Audio('));
const highlight = entry => entry.world === 'ISOLATED' && entry.source.includes('const allRects');

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
