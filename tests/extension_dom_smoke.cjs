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
  const messages = [], errors = [], timers = new Map(), listeners = [], pageEvents = [];
  let now = 0, nextTimer = 1, lifecycle = 1, revision = 1, owner, snapshot, heldSelectionTimer;
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
  for (const type of ['mousedown', 'mouseup', 'click', 'dblclick']) {
    add.call(paragraph, type, event => pageEvents.push({ type, detail: event.detail, trusted: event.isTrusted }));
  }
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
      buttons: [...bar.querySelectorAll('button')].map(button => ({ id: button.id, text: button.textContent,
        title: button.title, name: button.getAttribute('aria-label') })),
      inputs: [...bar.querySelectorAll('input')].map(input => ({ id: input.id, name: input.getAttribute('aria-label') })),
      loop: bar.querySelector('#free-tts-loop').checked, repeat: bar.querySelector('#free-tts-repeat-current').checked };
  };
  const selectionState = () => {
    const selection = window.getSelection();
    return { anchor: selection.anchorNode?.parentNode?.id || null, anchorOffset: selection.anchorOffset,
      focus: selection.focusNode?.parentNode?.id || null, focusOffset: selection.focusOffset,
      text: selection.toString(), collapsed: selection.isCollapsed };
  };
  const documentListeners = () => listeners.filter(item => item.target === document || item.target === window).length;
  const state = () => {
    const view = window.__freeTtsView;
    return { messages: structuredClone(messages), errors: [...errors], timers: timers.size,
      timerQueue: [...timers.values()].map(timer => ({ id: timer.id, remaining: timer.due - now })),
      events: [...pageEvents], selection: selectionState(), listeners: documentListeners(), owner: { ...owner }, attrs: attrs(),
      view: { leaseId: view.leaseId, sessionId: view.sessionId, lifecycleRevision: view.lifecycleRevision,
        retired: view.retired, barCreated: view.barCreated, viewRevision: view.viewRevision,
        pendingRevision: view.pendingRender?.snapshot.viewRevision ?? null } };
  };
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
      lifecycle++; revision += 10; messages.length = 0; errors.length = 0; pageEvents.length = 0;
      if (heldSelectionTimer) throw new Error('Unreleased selection callback');
      create(deferred); select(0); return state();
    },
    state, attrs, run, select, targets,
    pageMouse(type, detail = 1) { mouse(paragraph, type, { detail }); return state(); },
    reverseSelection() {
      window.getSelection().setBaseAndExtent(text, 32, text, 16);
      document.dispatchEvent(new Event('selectionchange'));
      return state();
    },
    holdSelectionTimer() {
      if (timers.size !== 1 || heldSelectionTimer) throw new Error('Expected one unheld selection callback');
      heldSelectionTimer = [...timers.values()][0];
      timers.delete(heldSelectionTimer.id);
      return state();
    },
    releaseSelectionTimer() {
      if (!heldSelectionTimer || heldSelectionTimer.due > now) throw new Error('Held selection callback is not due');
      const timer = heldSelectionTimer; heldSelectionTimer = null; timer.callback();
      return state();
    },
    replaceOwner(deferred = false) {
      const previousOwner = { ...owner };
      hide(); lifecycle++; revision += 10; create(deferred);
      return { ...state(), previousOwner };
    },
    wordPoint() {
      const range = document.createRange(); range.setStart(text, 16); range.setEnd(text, 22);
      const rect = range.getBoundingClientRect();
      return { x: rect.left + rect.width / 2, y: rect.top + rect.height / 2 };
    },
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
      return { ...state(), oldOwner, afterHide, beforeListeners, before, sameBar: successor === targets('background') };
    },
    renderPending() {
      const paused = { ...snapshot, state: 'paused', viewRevision: snapshot.viewRevision + 2 };
      const deferred = run('update', [owner, paused, { loopEnabled: false, repeatCurrentEnabled: true }]);
      const shown = run('show', [owner, snapshot, bundle.modes]);
      const before = attrs();
      run('update', [owner, snapshot, bundle.modes]);
      return { ...state(), deferred, shown, before };
    },
    renderOrder() {
      const stages = [];
      const record = name => stages.push({ name, ...state() });
      const older = { ...snapshot, viewRevision: snapshot.viewRevision + 1 };
      const paused = { ...snapshot, state: 'paused', viewRevision: snapshot.viewRevision + 2 };
      const pausedModes = { loopEnabled: false, repeatCurrentEnabled: true };
      record('before creation');
      const deferred = run('update', [owner, paused, pausedModes]); record('paused deferred');
      run('update', [owner, older, bundle.modes]); record('older deferred');
      const shown = run('show', [owner, snapshot, bundle.modes]); record('created paused');
      run('update', [owner, older, bundle.modes]); record('older update');
      run('show', [owner, older, bundle.modes]); record('older repeated show');
      const running = { ...snapshot, viewRevision: snapshot.viewRevision + 3 };
      run('update', [owner, running, bundle.modes]); record('newer running');
      run('update', [owner, paused, pausedModes]); record('old paused update');
      const newest = { ...paused, viewRevision: snapshot.viewRevision + 4 };
      run('update', [owner, newest, pausedModes]); record('newest paused');
      const oldOwner = { ...owner }, oldBar = targets('background');
      const oldRender = window.__freeTtsView.renderControlBarSnapshot, oldCleanup = window.__freeTtsCleanupControlBar;
      this.replaceOwner(true);
      const successorPaused = { ...snapshot, state: 'paused', viewRevision: snapshot.viewRevision + 1 };
      run('update', [owner, successorPaused, pausedModes]); record('successor deferred');
      run('show', [owner, snapshot, bundle.modes]); record('successor created paused');
      const successor = targets('background');
      oldRender(successor, { ...newest, viewRevision: revision + 100 }, bundle.modes); record('old renderer');
      run('update', [oldOwner, { ...snapshot, sessionId: oldOwner.sessionId, viewRevision: revision + 100 }, bundle.modes]); record('old owner update');
      run('hide', [oldOwner, oldOwner.lifecycleRevision + 1]); record('old owner hide');
      oldCleanup(); record('old cleanup');
      run('show', [oldOwner, { ...newest, viewRevision: revision + 100 }, bundle.modes]); record('old owner show');
      return { ...state(), stages, deferred, shown, oldOwner, replacedBar: oldBar !== successor, sameBar: successor === targets('background') };
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
    sessionId: result.owner.sessionId, lifecycleRevision: result.owner.lifecycleRevision, ...action })), `${label}: complete bound messages`);
  assert.equal(result.timers, 0, `${label}: no pending selection timer`);
  assert.deepEqual(result.timerQueue, [], `${label}: timer queue is empty`);
  assert.equal(result.attrs.loop && result.attrs.repeat, false, `${label}: exclusive modes`);
  assert.equal(result.view.leaseId, result.owner.leaseId, `${label}: exact live lease`);
  assert.equal(result.view.sessionId, result.owner.sessionId, `${label}: exact live session`);
  assert.equal(result.view.lifecycleRevision, result.owner.lifecycleRevision, `${label}: exact live lifecycle`);
  assert.equal(result.view.retired, false, `${label}: live view`);
}

function assertSelection(result, start, end, text, label) {
  assert.deepEqual(result.selection, { anchor: 'fixture-text', anchorOffset: start,
    focus: 'fixture-text', focusOffset: end, text, collapsed: start === end }, `${label}: exact page selection`);
}

function assertToggle(result, paused, label) {
  assert.deepEqual(result.attrs.toggle, paused ? ['▶', 'Resume', 'Resume'] : ['⏸', 'Pause', 'Pause'], `${label}: synchronized icon/title/accessible name`);
  assert.deepEqual(result.attrs.buttons, [
    { id: 'free-tts-prev', text: '⏮', title: 'Previous', name: 'Previous' },
    { id: 'free-tts-toggle', text: paused ? '▶' : '⏸', title: paused ? 'Resume' : 'Pause', name: paused ? 'Resume' : 'Pause' },
    { id: 'free-tts-next', text: '⏭', title: 'Next', name: 'Next' },
    { id: 'free-tts-close', text: '✕', title: 'Stop', name: 'Stop' },
  ], `${label}: compact icon controls retain explicit names`);
  assert.deepEqual(result.attrs.inputs, [
    { id: 'free-tts-loop', name: 'Repeat All' },
    { id: 'free-tts-repeat-current', name: 'Repeat Current Sentence' },
  ], `${label}: mode controls retain explicit names`);
}

async function runSelectionCases(cdp, sessionId) {
  const evaluate = expression => cdp.evaluate(expression, sessionId);
  const failures = [];
  const cases = [
    ['G05 genuine_page_selection', async label => {
      const queued = await evaluate('__fixture.reset(); __fixture.pageMouse("mousedown"); __fixture.select(16, 32); __fixture.pageMouse("mouseup")');
      assert.deepEqual(queued.messages, [], `${label}: mouseup defers jump`);
      assert.deepEqual(queued.timerQueue.map(timer => timer.remaining), [100], `${label}: one 100 ms timer`);
      assertSelection(queued, 16, 32, 'Second sentence.', label);
      const before = await evaluate('__advance(99); __fixture.state()');
      assert.deepEqual(before.messages, [], `${label}: no jump at 99 ms`);
      assert.deepEqual(before.timerQueue.map(timer => timer.remaining), [1], `${label}: one millisecond remains`);
      const selected = await evaluate('__advance(1); __fixture.state()');
      assertMessages(selected, [{ action: 'jumpToSentence', idx: 1 }], `${label}/100ms`);
      assertSelection(selected, 16, 32, 'Second sentence.', label);
      assertToggle(selected, false, label);
      // Neither an unmatched mouseup nor a full unchanged page gesture is proof
      // of a new selection. An intermediate selection change is proof.
      const retained = await evaluate('__fixture.pageMouse("mouseup"); __fixture.pageMouse("mousedown"); __fixture.pageMouse("mouseup"); __advance(100); __fixture.state()');
      assertMessages(retained, [{ action: 'jumpToSentence', idx: 1 }], `${label}/retained`);
      const reselected = await evaluate('__fixture.pageMouse("mousedown"); __fixture.select(0); __fixture.select(16, 32); __fixture.pageMouse("mouseup"); __advance(100); __fixture.state()');
      assertMessages(reselected, [{ action: 'jumpToSentence', idx: 1 }, { action: 'jumpToSentence', idx: 1 }], `${label}/reselected`);
      assertSelection(reselected, 16, 32, 'Second sentence.', label);
    }],
    ['G06 genuine_double_click_sequence', async label => {
      const first = await evaluate('__fixture.reset(); __fixture.pageMouse("mousedown"); __fixture.pageMouse("mouseup"); __fixture.pageMouse("click")');
      assertMessages(first, [], `${label}/first collapsed click`);
      assertSelection(first, 0, 0, '', label);
      const second = await evaluate('__fixture.pageMouse("mousedown", 2); __fixture.select(16, 22); __fixture.pageMouse("mouseup", 2); __fixture.pageMouse("click", 2)');
      assert.deepEqual(second.messages, [], `${label}: second mouseup has not jumped yet`);
      assert.deepEqual(second.timerQueue.map(timer => timer.remaining), [100], `${label}: second mouseup queues exactly one timer`);
      const doubled = await evaluate('__fixture.pageMouse("dblclick", 2)');
      const synthetic = await evaluate('__advance(150); __fixture.state()');
      assertMessages(synthetic, [{ action: 'jumpToSentence', idx: 1 }], `${label}/synthetic no duplicate jump`);
      assertMessages(doubled, [{ action: 'jumpToSentence', idx: 1 }], `${label}/synthetic immediate`);
      assertSelection(synthetic, 16, 22, 'Second', label);
      assert.deepEqual(synthetic.events, [
        { type: 'mousedown', detail: 1, trusted: false }, { type: 'mouseup', detail: 1, trusted: false }, { type: 'click', detail: 1, trusted: false },
        { type: 'mousedown', detail: 2, trusted: false }, { type: 'mouseup', detail: 2, trusted: false }, { type: 'click', detail: 2, trusted: false },
        { type: 'dblclick', detail: 2, trusted: false },
      ], `${label}: explicit first/second click sequence`);
      // A completed earlier drag selection and a later double-click are two
      // deliberate navigation attempts; only the second mouseup is suppressed.
      const dragged = await evaluate('__fixture.reset(); __fixture.pageMouse("mousedown"); __fixture.select(16, 32); __fixture.pageMouse("mouseup"); __advance(100); __fixture.state()');
      assertMessages(dragged, [{ action: 'jumpToSentence', idx: 1 }], `${label}/earlier drag`);
      const afterDrag = await evaluate('__fixture.select(0); __fixture.pageMouse("mousedown"); __fixture.pageMouse("mouseup"); __fixture.pageMouse("click"); __fixture.pageMouse("mousedown", 2); __fixture.select(16, 22); __fixture.pageMouse("mouseup", 2); __fixture.pageMouse("click", 2); __fixture.pageMouse("dblclick", 2); __advance(150); __fixture.state()');
      assertMessages(afterDrag, [{ action: 'jumpToSentence', idx: 1 }, { action: 'jumpToSentence', idx: 1 }], `${label}/fresh double-click after drag`);
      assertSelection(afterDrag, 16, 22, 'Second', label);
      await evaluate('__fixture.reset()');
      const point = await evaluate('__fixture.wordPoint()');
      for (const clickCount of [1, 2]) {
        await cdp.send('Input.dispatchMouseEvent', { type: 'mousePressed', ...point, button: 'left', buttons: 1, clickCount }, sessionId);
        await cdp.send('Input.dispatchMouseEvent', { type: 'mouseReleased', ...point, button: 'left', buttons: 0, clickCount }, sessionId);
        if (clickCount === 1) {
          const collapsed = await evaluate('__fixture.state()');
          assertMessages(collapsed, [], `${label}/CDP first collapsed click`);
          assert.equal(collapsed.selection.collapsed, true, `${label}: real first click is collapsed`);
        }
      }
      const actual = await evaluate('__advance(150); __fixture.state()');
      assertMessages(actual, [{ action: 'jumpToSentence', idx: 1 }], `${label}/CDP no duplicate jump`);
      assertSelection(actual, 16, 22, 'Second', `${label}/CDP`);
      assertToggle(actual, false, label);
      assert.deepEqual(actual.events, [
        { type: 'mousedown', detail: 1, trusted: true }, { type: 'mouseup', detail: 1, trusted: true }, { type: 'click', detail: 1, trusted: true },
        { type: 'mousedown', detail: 2, trusted: true }, { type: 'mouseup', detail: 2, trusted: true }, { type: 'click', detail: 2, trusted: true },
        { type: 'dblclick', detail: 2, trusted: true },
      ], `${label}: Chromium generates the complete trusted double-click sequence`);
    }],
    ['G07 selection_ticket_freshness', async label => {
      for (const boundary of ['new-gesture', 'selection-changed', 'endpoints-changed', 'replaced-owner']) {
        const original = await evaluate('__fixture.reset(); __fixture.pending(); __fixture.holdSelectionTimer()');
        const change = boundary === 'new-gesture' ? '__fixture.pageMouse("mousedown"); __fixture.select(0, 15); __fixture.pageMouse("mouseup")'
          : boundary === 'selection-changed' ? '__fixture.select(0, 15)'
          : boundary === 'endpoints-changed' ? '__fixture.reverseSelection()'
          : '__fixture.replaceOwner(); __fixture.pageMouse("mousedown"); __fixture.select(0, 15); __fixture.pageMouse("mouseup")';
        const changed = await evaluate(`${change}; __advance(50); __fixture.releaseSelectionTimer()`);
        assert.deepEqual(changed.messages, [], `${label}/${boundary}: released old callback cannot jump`);
        const hasSecondTicket = ['new-gesture', 'replaced-owner'].includes(boundary);
        if (hasSecondTicket) {
          assert.deepEqual(changed.timerQueue.map(timer => timer.remaining), [50], `${label}/${boundary}: second ticket retains its own timer`);
          const early = await evaluate('__advance(49); __fixture.state()');
          assert.deepEqual(early.messages, [], `${label}/${boundary}: second ticket cannot jump at 99 ms`);
          const current = await evaluate('__advance(1); __fixture.state()');
          assertMessages(current, [{ action: 'jumpToSentence', idx: 0 }], `${label}/${boundary}: only the second ticket jumps`);
          assertSelection(current, 0, 15, 'First sentence.', label);
          if (boundary === 'replaced-owner') {
            assert.deepEqual(current.owner, { tabId: 7, leaseId: original.owner.leaseId,
              sessionId: `fixture-session-${original.owner.lifecycleRevision + 2}`, lifecycleRevision: original.owner.lifecycleRevision + 2 }, `${label}: exact successor owner`);
          } else assert.deepEqual(current.owner, original.owner, `${label}: same session new gesture`);
        } else {
          assertMessages(changed, [], `${label}/${boundary}`);
          if (boundary === 'endpoints-changed') assertSelection(changed, 32, 16, 'Second sentence.', label);
          else assertSelection(changed, 0, 15, 'First sentence.', label);
          const next = await evaluate('__fixture.pageMouse("mousedown"); __fixture.select(0); __fixture.select(16, 32); __fixture.pageMouse("mouseup"); __advance(100); __fixture.state()');
          assertMessages(next, [{ action: 'jumpToSentence', idx: 1 }], `${label}/${boundary}: genuine next ticket still works`);
        }
      }
    }],
    ['G08 render_attributes_order', async label => {
      const result = await evaluate('__fixture.reset(true); __fixture.renderOrder()');
      assert.equal(result.deferred, 'deferred', `${label}: missing initial bar defers rendering`);
      assert.equal(result.shown, 'rendered', `${label}: delayed creation succeeds`);
      for (const stage of result.stages) {
        assert.deepEqual(stage.messages, [], `${label}/${stage.name}: render has no command effect`);
        assert.deepEqual(stage.errors, [], `${label}/${stage.name}: no errors`);
        assert.deepEqual(stage.timerQueue, [], `${label}/${stage.name}: no timers`);
        assertSelection(stage, 0, 0, '', `${label}/${stage.name}`);
        if (['before creation', 'paused deferred', 'older deferred', 'successor deferred'].includes(stage.name)) {
          assert.equal(stage.attrs, null, `${label}/${stage.name}: bar creation is pending`);
          assert.equal(stage.view.barCreated, false, `${label}/${stage.name}: not falsely rendered`);
          if (stage.name !== 'before creation') assert.ok(stage.view.pendingRevision > 0, `${label}/${stage.name}: newest pending snapshot retained`);
        } else {
          const paused = !['newer running', 'old paused update'].includes(stage.name);
          assertMessages(stage, [], `${label}/${stage.name}`);
          assertToggle(stage, paused, `${label}/${stage.name}`);
          assert.equal(stage.attrs.loop, !paused, `${label}/${stage.name}: accepted loop mode`);
          assert.equal(stage.attrs.repeat, paused, `${label}/${stage.name}: accepted repeat mode`);
          assert.equal(stage.view.pendingRevision, null, `${label}/${stage.name}: no obsolete pending render`);
        }
      }
      assert.deepEqual(result.owner, { tabId: 7, leaseId: result.oldOwner.leaseId,
        sessionId: `fixture-session-${result.oldOwner.lifecycleRevision + 2}`, lifecycleRevision: result.oldOwner.lifecycleRevision + 2 }, `${label}: exact successor owner`);
      assert.equal(result.replacedBar, true, `${label}: new session creates a new bar`);
      assert.equal(result.sameBar, true, `${label}: old update/hide/show cannot replace successor bar`);
      const successor = result.stages.find(stage => stage.name === 'successor created paused');
      for (const stage of result.stages.slice(result.stages.indexOf(successor) + 1)) {
        assert.deepEqual(stage.attrs, successor.attrs, `${label}/${stage.name}: all successor attributes preserved`);
        assert.deepEqual(stage.view, successor.view, `${label}/${stage.name}: successor ordering fences preserved`);
      }
    }],
  ];
  for (const [label, run] of cases) {
    try { await run(label); console.log(`PASS ${label}`); }
    catch (error) { console.error(`FAIL ${label}: ${error.message}`); failures.push(error); }
  }
  if (failures.length) throw new AggregateError(failures, 'Selection/render gesture cases failed');
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
  assert.deepEqual(replacement.owner, { tabId: 7, leaseId: replacement.oldOwner.leaseId,
    sessionId: `fixture-session-${replacement.oldOwner.lifecycleRevision + 2}`, lifecycleRevision: replacement.oldOwner.lifecycleRevision + 2 }, 'G04 exact successor owner');
  assertSelection(replacement, 16, 32, 'Second sentence.', 'G04 successor retains page selection');
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
  await runSelectionCases(cdp, sessionId);
  const final = await evaluate('__fixture.cleanup()');
  assert.equal(final.timers, 0); assert.deepEqual(final.timerQueue, []);
  assert.equal(final.listeners, 0); assert.equal(final.attrs, null); assert.deepEqual(final.errors, []);
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
  console.log('PASS extension DOM smoke G01-G08; Chromium terminated and temporary files removed');
}

module.exports = { runDomSmoke };
if (require.main === module) runDomSmoke(process.argv.slice(2)).catch(error => { console.error(error); process.exitCode = 1; });
