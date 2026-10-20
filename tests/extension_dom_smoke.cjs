'use strict';

// Real DOM coverage for the serialized callbacks dispatched by the worker. No
// extension package, web server, audio service, or third-party CDP client is used.
const assert = require('node:assert/strict');
const fs = require('node:fs');
const os = require('node:os');
const path = require('node:path');
const vm = require('node:vm');
const { spawn } = require('node:child_process');

async function captureCallbacks() {
  const calls = [];
  const context = vm.createContext({
    console, crypto: { randomUUID: () => 'fixture-worker' }, URL,
    chrome: {
      runtime: { onMessage: { addListener() {} } }, commands: { onCommand: { addListener() {} } },
      tabs: { onRemoved: { addListener() {} } },
      storage: { sync: { get: async defaults => ({ ...defaults }) } },
      scripting: { executeScript: async details => {
        calls.push({ source: details.func.toString(), args: details.args || [], world: details.world || 'ISOLATED' });
        return [{ result: 'rendered' }];
      } },
    },
  });
  const source = fs.readFileSync(path.join(__dirname, '../extension/background.js'), 'utf8');
  vm.runInContext(`${source}\nglobalThis.fixtureWorker = {
    bind(owner) { sentencePipeline = { ...owner, cancelled: false, installPromise: Promise.resolve() }; activePlayback = pageOwner(sentencePipeline); return sentencePipeline; },
    showControlBar, updateControlBar, initPageHighlighting, hideControlBar,
    claim(owner) { return chrome.scripting.executeScript({ target: { tabId: owner.tabId }, func: injectPageLease, args: ['ISOLATED', 'claim', { leaseId: null, lifecycleFloor: 0 }, owner.leaseId, 'fixture-worker'] }); },
    install(owner) { return chrome.scripting.executeScript({ target: { tabId: owner.tabId }, func: injectViewSession, args: [owner] }); }
  };`, context);
  const api = context.fixtureWorker;
  const owner = api.bind({ tabId: 7, leaseId: 'fixture-lease', sessionId: 'fixture-session', lifecycleRevision: 1 });
  const snapshot = { workerId: 'fixture-worker', state: 'playing', sessionId: owner.sessionId, viewRevision: 1,
    intentRevision: 1, mediaRevision: 0, phase: 'loading', audioStatus: 'none', error: null };
  const modes = { loopEnabled: true, repeatCurrentEnabled: false };
  const captured = {};
  for (const [name, invoke] of [
    ['claim', () => api.claim(owner)], ['install', () => api.install(owner)],
    ['highlight', () => api.initPageHighlighting(7, ['First sentence.', 'Second sentence.'], owner)],
    ['show', () => api.showControlBar(owner, snapshot, modes)],
    ['update', () => api.updateControlBar(owner, snapshot, modes)],
    ['hide', () => api.hideControlBar(owner, 2)],
  ]) {
    const before = calls.length;
    await invoke();
    assert.equal(calls.length, before + 1, `${name} must dispatch the production callback`);
    captured[name] = calls.at(-1);
    assert.equal(captured[name].world, 'ISOLATED', `${name} belongs in the isolated world`);
  }
  return { captured, snapshot, modes };
}

function fixtureBootstrap(bundle) {
  const callbacks = Object.fromEntries(Object.entries(bundle.captured).map(([name, call]) => [name, { ...call, func: (0, eval)(`(${call.source})`) }]));
  const run = (name, args = callbacks[name].args) => callbacks[name].func(...args);
  const messages = [], errors = [], timers = new Map(), listeners = [];
  let now = 0, nextTimer = 1, lifecycle = 1, revision = 1, owner, snapshot;
  const add = EventTarget.prototype.addEventListener, remove = EventTarget.prototype.removeEventListener;
  EventTarget.prototype.addEventListener = function (type, callback, options) {
    const capture = typeof options === 'boolean' ? options : !!options?.capture;
    if (!listeners.some(item => item.target === this && item.type === type && item.callback === callback && item.capture === capture)) {
      listeners.push({ target: this, type, callback, capture });
    }
    return add.call(this, type, callback, options);
  };
  EventTarget.prototype.removeEventListener = function (type, callback, options) {
    const capture = typeof options === 'boolean' ? options : !!options?.capture;
    const index = listeners.findIndex(item => item.target === this && item.type === type && item.callback === callback && item.capture === capture);
    if (index >= 0) listeners.splice(index, 1);
    return remove.call(this, type, callback, options);
  };
  add.call(window, 'error', event => errors.push(event.message));
  add.call(window, 'unhandledrejection', event => errors.push(String(event.reason)));
  window.setTimeout = (callback, delay = 0) => {
    const id = nextTimer++;
    timers.set(id, { id, due: now + Math.max(0, Number(delay)), callback });
    return id;
  };
  window.clearTimeout = id => timers.delete(id);
  window.__advance = milliseconds => {
    const end = now + milliseconds;
    for (let steps = 0; ; steps++) {
      if (steps > 1000) throw new Error('Fixture timer step limit');
      const timer = [...timers.values()].filter(item => item.due <= end).sort((a, b) => a.due - b.due || a.id - b.id)[0];
      if (!timer) break;
      timers.delete(timer.id); now = timer.due; timer.callback();
    }
    now = end;
  };
  window.chrome = { runtime: { sendMessage(message) {
    messages.push(structuredClone(message));
    return Promise.resolve({ ...snapshot, ok: true, accepted: true, disposition: 'applied', pageApplied: false, commandError: null });
  } } };
  const paragraph = document.getElementById('fixture-text'), text = paragraph.firstChild;
  const mouse = (target, type, extra = {}) => target.dispatchEvent(new MouseEvent(type, {
    bubbles: true, composed: true, cancelable: true, button: 0, clientX: 40, clientY: 160, ...extra,
  }));
  const select = (start, end = start) => {
    const range = document.createRange(); range.setStart(text, start); range.setEnd(text, end);
    const selection = window.getSelection(); selection.removeAllRanges(); selection.addRange(range);
    document.dispatchEvent(new Event('selectionchange'));
  };
  const attrs = () => {
    const bar = document.getElementById('free-tts-bar'), toggle = bar?.querySelector('#free-tts-toggle');
    return bar && { html: bar.outerHTML, left: bar.style.left, top: bar.style.top,
      toggle: [toggle.textContent, toggle.title, toggle.getAttribute('aria-label')],
      loop: bar.querySelector('#free-tts-loop').checked, repeat: bar.querySelector('#free-tts-repeat-current').checked };
  };
  const documentListeners = () => listeners.filter(item => item.target === document || item.target === window).length;
  const state = () => ({ messages: structuredClone(messages), errors: [...errors], timers: timers.size,
    listeners: documentListeners(), owner: { ...owner }, attrs: attrs() });
  const targets = name => {
    const bar = document.getElementById('free-tts-bar');
    if (name === 'background') return bar;
    if (name === 'nested-toggle') {
      const child = document.createElement('span'); child.textContent = '⏸'; bar.querySelector('#free-tts-toggle').appendChild(child); return child;
    }
    if (name.startsWith('loop-') || name.startsWith('repeat-')) {
      const input = bar.querySelector(name.startsWith('loop-') ? '#free-tts-loop' : '#free-tts-repeat-current');
      if (name.endsWith('-label')) return input.parentNode;
      if (name.endsWith('-text')) return input.parentNode.lastChild;
      return input;
    }
    return bar.querySelector(`#free-tts-${name === 'double-toggle' ? 'toggle' : name}`);
  };
  const create = (deferred = false) => {
    owner = { tabId: 7, leaseId: 'fixture-lease', sessionId: `fixture-session-${lifecycle}`, lifecycleRevision: lifecycle };
    snapshot = { ...bundle.snapshot, sessionId: owner.sessionId, viewRevision: revision };
    run('install', [owner]);
    run('highlight', [['First sentence.', 'Second sentence.'], '#fff3cd', owner]);
    if (!deferred) run('show', [owner, snapshot, bundle.modes]);
  };
  const hide = () => run('hide', [owner, ++lifecycle]);
  run('claim');
  window.__fixture = {
    reset(deferred = false) {
      if (owner) hide();
      if (timers.size) throw new Error('Cleanup left fixture timers');
      if (documentListeners()) throw new Error('Cleanup left document/window listeners');
      lifecycle++; revision += 10; messages.length = 0; errors.length = 0;
      create(deferred); select(0); return state();
    },
    state, attrs, run, select, targets,
    retain() { select(0, 15); },
    pending() {
      select(0); mouse(paragraph, 'mousedown'); select(16, 32); mouse(paragraph, 'mouseup');
      if (timers.size !== 1) throw new Error('Pending-selection precondition: expected one genuine page-selection timer');
      window.__advance(50);
    },
    interact(name, synthetic = false) {
      const target = targets(name), before = attrs();
      const click = () => target.nodeType === Node.TEXT_NODE ? mouse(target, 'click') : target.click();
      if (!synthetic) { mouse(target, 'mousedown'); mouse(document, 'mousemove', { clientX: 70, clientY: 185 }); mouse(target, 'mouseup'); }
      click();
      if (name === 'double-toggle') { mouse(target, 'mousedown'); mouse(target, 'mouseup'); click(); mouse(target, 'dblclick', { detail: 2 }); }
      window.__advance(150);
      return { ...state(), before };
    },
    focus(name) { targets(name).focus(); },
    drag(cancellation = null) {
      select(0, 15);
      const bar = targets('background'), before = bar.getBoundingClientRect();
      mouse(bar, 'mousedown', { clientX: before.left + 2, clientY: before.top + 2 });
      mouse(paragraph, 'mousemove', { clientX: before.left + 52, clientY: before.top + 32 });
      const moved = { left: bar.style.left, top: bar.style.top };
      if (cancellation === 'pointercancel') document.dispatchEvent(new Event('pointercancel', { bubbles: true }));
      if (cancellation === 'blur') window.dispatchEvent(new Event('blur'));
      if (cancellation === 'hidden') {
        Object.defineProperty(document, 'hidden', { configurable: true, value: true });
        document.dispatchEvent(new Event('visibilitychange')); delete document.hidden;
      }
      mouse(paragraph, 'mouseup');
      mouse(paragraph, 'mousemove', { clientX: before.left + 102, clientY: before.top + 62 });
      window.__advance(150);
      return { ...state(), moved, before: { left: before.left, top: before.top } };
    },
    replace() {
      this.pending();
      const oldOwner = { ...owner }, oldSnapshot = { ...snapshot }, oldBar = targets('background');
      const oldToggle = targets('toggle'), oldCleanup = window.__freeTtsCleanupControlBar, beforeListeners = documentListeners();
      const oldClick = listeners.find(item => item.target === oldToggle && item.type === 'click').callback;
      const oldRender = window.__freeTtsView.renderControlBarSnapshot;
      hide();
      const afterHide = { timers: timers.size, listeners: documentListeners(),
        barListeners: listeners.filter(item => item.target === oldBar || (item.target.nodeType && oldBar.contains(item.target))).length };
      lifecycle++; revision += 10; create();
      const successor = targets('background'), before = attrs();
      oldCleanup(); oldCleanup(); window.__advance(150); oldToggle.click(); oldClick(); oldBar.dispatchEvent(new Event('dblclick', { bubbles: true }));
      oldRender(successor, { ...oldSnapshot, state: 'paused', viewRevision: revision + 100 }, bundle.modes);
      run('update', [oldOwner, { ...oldSnapshot, state: 'paused', viewRevision: revision + 100 }, bundle.modes]);
      run('hide', [oldOwner, oldOwner.lifecycleRevision + 1]);
      run('show', [oldOwner, oldSnapshot, bundle.modes]);
      run('show', [owner, snapshot, bundle.modes]);
      return { ...state(), afterHide, beforeListeners, before, sameBar: successor === targets('background') };
    },
    renderPending() {
      const paused = { ...snapshot, state: 'paused', viewRevision: snapshot.viewRevision + 2 };
      const deferred = run('update', [owner, paused, { loopEnabled: false, repeatCurrentEnabled: true }]);
      const shown = run('show', [owner, snapshot, bundle.modes]);
      const before = attrs();
      run('update', [owner, snapshot, bundle.modes]);
      return { ...state(), deferred, shown, before };
    },
    cleanup() { hide(); return state(); },
  };
  window.__fixtureReady = true;
}

class PipeCdp {
  constructor(browser) {
    this.browser = browser; this.nextId = 1; this.pending = new Map(); this.buffer = ''; this.failure = null; this.closing = false; this.stderr = '';
    browser.stderr.on('data', data => { this.stderr = (this.stderr + data).slice(-16000); });
    browser.on('error', error => this.fail(error));
    browser.on('exit', (code, signal) => { if (!this.closing) this.fail(new Error(`Chromium exited (${code ?? signal})`)); });
    browser.stdio[3].on('error', error => { if (!this.closing) this.fail(error); });
    browser.stdio[4].on('error', error => this.fail(error));
    browser.stdio[4].on('end', () => { if (!this.closing) this.fail(new Error('Chromium CDP pipe EOF')); });
    browser.stdio[4].setEncoding('utf8');
    browser.stdio[4].on('data', data => {
      this.buffer += data;
      for (let boundary; (boundary = this.buffer.indexOf('\0')) >= 0;) {
        const frame = this.buffer.slice(0, boundary); this.buffer = this.buffer.slice(boundary + 1);
        if (!frame) continue;
        let message;
        try { message = JSON.parse(frame); } catch (error) { this.fail(new Error(`Invalid CDP JSON: ${error.message}`)); return; }
        if (!message.id) continue;
        const request = this.pending.get(message.id);
        if (!request) continue;
        this.pending.delete(message.id); clearTimeout(request.timer);
        if (message.sessionId !== request.sessionId) request.reject(new Error(`CDP session mismatch for ${request.method}`));
        else if (message.error) request.reject(new Error(`${request.method}: ${JSON.stringify(message.error)}`));
        else request.resolve(message.result);
      }
    });
  }
  fail(error) {
    this.failure ||= error;
    for (const request of this.pending.values()) { clearTimeout(request.timer); request.reject(error); }
    this.pending.clear();
  }
  send(method, params = {}, sessionId) {
    if (this.failure) return Promise.reject(this.failure);
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id); const error = new Error(`CDP timeout: ${method}`); reject(error); this.fail(error);
      }, 8000);
      this.pending.set(id, { resolve, reject, timer, method, sessionId });
      this.browser.stdio[3].write(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }) + '\0');
    });
  }
  async evaluate(expression, sessionId) {
    const result = await this.send('Runtime.evaluate', { expression, returnByValue: true, awaitPromise: true }, sessionId);
    if (result.exceptionDetails) throw new Error(`Browser evaluation failed: ${result.exceptionDetails.exception?.description || result.exceptionDetails.text}`);
    return result.result.value;
  }
}

const controlCases = [
  ['toggle', [{ action: 'togglePlayback' }]], ['prev', [{ action: 'prevSentence' }]],
  ['next', [{ action: 'nextSentence' }]], ['close', [{ action: 'stopPlayback' }]],
  ['nested-toggle', [{ action: 'togglePlayback' }]],
  ['loop-label', [{ action: 'setLoopState', enabled: false }]], ['loop-text', [{ action: 'setLoopState', enabled: false }]],
  ['loop-input', [{ action: 'setLoopState', enabled: false }]],
  ['repeat-label', [{ action: 'setRepeatCurrentState', enabled: true }]], ['repeat-text', [{ action: 'setRepeatCurrentState', enabled: true }]],
  ['repeat-input', [{ action: 'setRepeatCurrentState', enabled: true }]],
  ['background', []], ['double-toggle', [{ action: 'togglePlayback' }, { action: 'togglePlayback' }]],
];

function assertMessages(result, actions, label) {
  assert.deepEqual(result.errors, [], `${label}: browser errors/rejections`);
  assert.deepEqual(result.messages, actions.map(action => ({ source: 'bar', leaseId: result.owner.leaseId,
    sessionId: result.owner.sessionId, lifecycleRevision: result.owner.lifecycleRevision, ...action })), `${label}: complete bound messages (no selection jump)`);
  assert.equal(result.timers, 0, `${label}: no pending selection timer`);
  assert.equal(result.attrs.loop && result.attrs.repeat, false, `${label}: exclusive modes`);
}

async function runGestureCases(cdp, sessionId) {
  const evaluate = expression => cdp.evaluate(expression, sessionId);
  for (const [name, actions] of controlCases) {
    const label = `G01 retained_selection_controls/${name}`;
    const result = await evaluate(`__fixture.reset(); __fixture.retain(); __fixture.interact(${JSON.stringify(name)})`);
    assertMessages(result, actions, label);
    if (name !== 'background') {
      assert.equal(result.attrs.left, result.before.left, `${label}: interactive descendants do not drag`);
      assert.equal(result.attrs.top, result.before.top, `${label}: interactive descendants do not drag`);
    }
  }
  console.log('PASS G01 retained_selection_controls');

  for (const [name, actions] of controlCases) for (const synthetic of [false, true]) {
    const label = `G02 pending_selection_invalidated/${name}/${synthetic ? 'click-only' : 'mouse'}`;
    const result = await evaluate(`__fixture.reset(); __fixture.pending(); __fixture.interact(${JSON.stringify(name)}, ${synthetic})`);
    assertMessages(result, actions, label);
  }
  const focus = await evaluate('__fixture.reset(); __fixture.pending(); __fixture.focus("toggle"); __advance(150); __fixture.state()');
  assertMessages(focus, [], 'G02 pending_selection_invalidated/focus');
  for (const [control, key, actions] of [
    ['toggle', 'Enter', [{ action: 'togglePlayback' }]],
    ['toggle', ' ', [{ action: 'togglePlayback' }]],
    ['loop-input', ' ', [{ action: 'setLoopState', enabled: false }]],
  ]) {
    await evaluate(`__fixture.reset(); __fixture.pending(); __fixture.focus(${JSON.stringify(control)})`);
    const enter = key === 'Enter';
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyDown', key, code: enter ? 'Enter' : 'Space',
      windowsVirtualKeyCode: enter ? 13 : 32, text: enter ? '\r' : ' ', unmodifiedText: enter ? '\r' : ' ' }, sessionId);
    await cdp.send('Input.dispatchKeyEvent', { type: 'keyUp', key, code: enter ? 'Enter' : 'Space', windowsVirtualKeyCode: enter ? 13 : 32 }, sessionId);
    assertMessages(await evaluate('__advance(150); __fixture.state()'), actions, `G02 pending_selection_invalidated/CDP-${control}-${enter ? 'Enter' : 'Space'}`);
  }
  const escape = await evaluate('__fixture.reset(); __fixture.pending(); document.dispatchEvent(new KeyboardEvent("keydown", { key: "Escape", bubbles: true })); __advance(150); __fixture.state()');
  assertMessages(escape, [{ action: 'stopPlayback' }], 'G02 pending_selection_invalidated/Escape');
  console.log('PASS G02 pending_selection_invalidated (including CDP Enter/Space default activation)');

  for (const cancellation of [null, 'pointercancel', 'blur', 'hidden']) {
    const label = `G03 bar_drag_release_outside/${cancellation || 'release'}`;
    const result = await evaluate(`__fixture.reset(); __fixture.drag(${JSON.stringify(cancellation)})`);
    assertMessages(result, [], label);
    assert.equal(result.moved.left, `${result.before.left + 50}px`, `${label}: horizontal movement`);
    assert.equal(result.moved.top, `${result.before.top + 30}px`, `${label}: vertical movement`);
    assert.equal(result.attrs.left, result.moved.left, `${label}: drag ended`);
    assert.equal(result.attrs.top, result.moved.top, `${label}: drag ended`);
    // Cancellation must also clear an already queued page-selection timer.
    if (cancellation) {
      const cancel = cancellation === 'pointercancel' ? 'document.dispatchEvent(new Event("pointercancel"))'
        : cancellation === 'blur' ? 'window.dispatchEvent(new Event("blur"))'
        : 'Object.defineProperty(document, "hidden", { configurable: true, value: true }); document.dispatchEvent(new Event("visibilitychange")); delete document.hidden';
      const cancelled = await evaluate(`__fixture.reset(); __fixture.pending(); ${cancel}; __advance(150); __fixture.state()`);
      assertMessages(cancelled, [], `${label}: pending page selection cancelled`);
    }
  }
  console.log('PASS G03 bar_drag_release_outside');

  const replacement = await evaluate('__fixture.reset(); __fixture.replace()');
  assertMessages(replacement, [], 'G04 teardown_replacement_timers');
  assert.deepEqual(replacement.afterHide, { timers: 0, listeners: 0, barListeners: 0 }, 'G04 removes timers and every document/window/bar listener');
  assert.equal(replacement.listeners, replacement.beforeListeners, 'G04 successor owns one listener set');
  assert.equal(replacement.sameBar, true, 'G04 repeated live show and old cleanup preserve successor identity');
  assert.deepEqual(replacement.attrs, replacement.before, 'G04 old controls/cleanup/render leave successor attributes unchanged');
  const rendered = await evaluate('__fixture.reset(true); __fixture.renderPending()');
  assertMessages(rendered, [], 'G04 deferred creation render');
  assert.equal(rendered.deferred, 'deferred'); assert.equal(rendered.shown, 'rendered');
  assert.deepEqual(rendered.attrs.toggle, ['▶', 'Resume', 'Resume'], 'newest pending snapshot renders all three attributes');
  assert.deepEqual(rendered.attrs, rendered.before, 'older update cannot overwrite pending paused render');
  const cleaned = await evaluate('__fixture.cleanup()');
  assert.equal(cleaned.timers, 0); assert.equal(cleaned.listeners, 0); assert.equal(cleaned.attrs, null);
  assert.deepEqual(cleaned.errors, []);
  console.log('PASS G04 teardown_replacement_timers (including deferred versioned creation)');
}

async function terminateBrowser(browser, cdp) {
  cdp.closing = true;
  cdp.fail(new Error('CDP driver shutting down'));
  if (browser.exitCode !== null || browser.signalCode !== null) return;
  await new Promise((resolve, reject) => {
    let killTimer, deadline;
    const done = () => { clearTimeout(killTimer); clearTimeout(deadline); resolve(); };
    browser.once('exit', done);
    browser.kill('SIGTERM');
    killTimer = setTimeout(() => browser.kill('SIGKILL'), 1500);
    deadline = setTimeout(() => { browser.removeListener('exit', done); reject(new Error('Chromium cleanup timeout after SIGKILL')); }, 5000);
  });
}

async function runDomSmoke(argv) {
  assert.equal(argv.length, 2, 'Usage: node tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium');
  assert.equal(argv[0], '--chromium');
  const executable = path.resolve(argv[1]);
  fs.accessSync(executable, fs.constants.X_OK);
  const bundle = await captureCallbacks();
  const temporary = fs.mkdtempSync(path.join(os.tmpdir(), 'free-tts-dom-'));
  let browser, cdp, failure, watchdog;
  try {
    const htmlPath = path.join(temporary, 'fixture.html');
    const serialized = JSON.stringify(bundle).replace(/</g, '\\u003c');
    fs.writeFileSync(htmlPath, `<!doctype html><meta charset="utf-8"><title>free-tts DOM fixture</title><style>body{margin:24px}p{margin-top:140px;font:20px monospace}</style><p id="fixture-text">First sentence. Second sentence.</p><script>(${fixtureBootstrap.toString()})(${serialized})</script>`);
    const flags = ['--headless=new', '--disable-gpu', '--remote-debugging-pipe', '--no-first-run', '--no-default-browser-check',
      '--disable-background-networking', '--disable-component-update', '--disable-extensions', `--user-data-dir=${path.join(temporary, 'profile')}`];
    // The test process may run as root or inside a namespace-restricted host.
    // This flag affects only the disposable browser and is always reported.
    flags.push('--no-sandbox');
    console.log('DOM smoke: Chromium --no-sandbox (test-only disposable profile)');
    browser = spawn(executable, flags, { stdio: ['ignore', 'ignore', 'pipe', 'pipe', 'pipe'] });
    cdp = new PipeCdp(browser);
    watchdog = setTimeout(() => { cdp.fail(new Error('DOM smoke process deadline (90s)')); browser.kill('SIGKILL'); }, 90000);
    const { targetId } = await cdp.send('Target.createTarget', { url: `file://${htmlPath}` });
    const { sessionId } = await cdp.send('Target.attachToTarget', { targetId, flatten: true });
    await cdp.send('Page.enable', {}, sessionId);
    await cdp.send('Runtime.enable', {}, sessionId);
    // Host readiness polling has a deadline independent of the fixture clock.
    const readyDeadline = Date.now() + 8000;
    while (!await cdp.evaluate('window.__fixtureReady === true', sessionId)) {
      assert.ok(Date.now() < readyDeadline, 'Fixture readiness timeout');
      await new Promise(resolve => setTimeout(resolve, 20));
    }
    await runGestureCases(cdp, sessionId);
    if (cdp.failure) throw cdp.failure;
    assert.doesNotMatch(cdp.stderr, /(?:FATAL|Received signal|ERROR:.*(?:zygote|sandbox|crash))/i, 'Chromium reported a fatal browser error');
  } catch (error) { failure = error; }
  finally {
    clearTimeout(watchdog);
    try { if (browser && cdp) await terminateBrowser(browser, cdp); } catch (error) { failure = failure ? new AggregateError([failure, error], 'DOM smoke and browser cleanup failed') : error; }
    try { fs.rmSync(temporary, { recursive: true, force: true }); } catch (error) { failure = failure ? new AggregateError([failure, error], 'DOM smoke and profile cleanup failed') : error; }
  }
  if (failure) {
    if (cdp?.stderr) console.error(`Chromium stderr:\n${cdp.stderr}`);
    throw failure;
  }
  console.log('PASS extension DOM smoke G01-G04; Chromium terminated and temporary files removed');
}

module.exports = { runDomSmoke };
if (require.main === module) runDomSmoke(process.argv.slice(2)).catch(error => { console.error(error); process.exitCode = 1; });
