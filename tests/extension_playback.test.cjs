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
  vm.runInContext(`${source}\nglobalThis.__worker = { get pipeline() { return sentencePipeline; }, get active() { return activePlayback; }, start: startSentenceTTS, poll: pollCompletion };`, worker);
  assert.equal(typeof commandListener, 'function');
  const api = { clock, scripts, storage, fetches, audioSpy, messages, logs, worker: worker.__worker, page,
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
    const synced = h.send(h.messages.at(-1).data); await h.flush(); await synced;
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

const completionPoll = entry => entry.world === 'MAIN' && entry.source.includes('record.audio.ended');

function boundModeMessage(h, action, enabled) {
  const pipeline = h.worker.pipeline;
  return h.send({ action, enabled, source: 'bar', leaseId: pipeline.leaseId,
    sessionId: pipeline.sessionId, lifecycleRevision: pipeline.lifecycleRevision }, { tab: { id: pipeline.tabId } });
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
  const resumed = h.send(notification.data, { tab: { id: 7 } });
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
    await boundModeMessage(h, 'setRepeatCurrentState', true);
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
    await boundModeMessage(h, 'setRepeatCurrentState', true);
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
