# Flow bar pause/play repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Repair the extension's flow-bar gesture handling and sentence playback ordering so pause/resume is synchronous at the accepted boundary, stale asynchronous work cannot start or mutate successor playback, and the compact bar and popup truthfully render canonical worker state.

**Architecture:** Keep the service worker as the owner of accepted playback intent, session identity, attempt identity, modes, and public snapshots; keep the page MAIN world as the owner of Audio identity, media-key effects, play outcomes, and page-side lifecycle fences. Add bounded lease/lifecycle/intent/media/audio revisions and one-at-a-time fresh observation to the existing pipeline rather than introducing a playback framework. The isolated bar remains a thin, versioned renderer and gesture source; its controls send a bound `togglePlayback` or explicit action and never infer a command from a glyph.

**Tech Stack:** Chrome MV3 JavaScript service worker and injected MAIN/isolated-world functions; Node v24.16.0 built-ins (`node:test`, `node:assert/strict`, `node:vm`, `node:fs`, `node:child_process`); headless `/usr/bin/chromium` controlled by null-delimited Chrome DevTools Protocol; Python `pytest` from the repository requirements.

## File Responsibility Map

- `extension/background.js:4-219` owns worker identity, session/start tickets, canonical snapshots, bound runtime commands, intent acceptance, navigation, stop/error retirement, and the no-await command-acceptance boundary.
- `extension/background.js:221-318` owns MAIN-world media handlers, the isolated relay, keepalive, media/action tokens, and page-side lease/lifecycle/audio fences. Audio construction and all direct media effects remain here in MAIN-world injected functions.
- `extension/background.js:320-540` owns the isolated-world compact bar, per-instance gesture lifecycle, selection tickets, drag cancellation, render fencing, and cleanup. Tasks 8-9 complete this region after Tasks 1-7 establish its owner/snapshot contract.
- `extension/background.js:542-694` owns attempt navigation, splitter preservation, page highlight setup/cleanup, and owner-fenced page operations.
- `extension/background.js:730-888` owns start-ticket publication, settings/cache/fetch/highlight preparation, one attempt loader, guarded activation, non-overlapping completion polling, qualified end/replay/advance policy, and terminal cleanup.
- `extension/popup.js:1-281` consumes canonical versioned snapshots and sends popup commands without guessing state from the requested action. The existing popup layout and server/voice/speed controls remain unchanged.
- `tests/extension_playback.test.cjs` is the built-in Node behavioral suite. It evaluates the complete real worker source, executes real serialized injected functions in separate mocked MAIN/isolated contexts, and covers W01-W31 with independently controlled dispatch, execution, completion, fetch, storage, timer, relay, and play gates.
- `tests/extension_dom_smoke.cjs` is the real Chromium DOM/CDP smoke driver. It embeds captured production injected functions into a temporary HTML fixture and covers G01-G08 without an extension package, npm package, generic second DOM implementation, server, or physical media keys.
- `tests/test_extension_playback.py` is the standard pytest integration wrapper for the two Node entrypoints. It uses bounded subprocesses, propagates stdout/stderr, and makes missing Chromium or a failed smoke run a visible failure.
- `tests/test_media_session.py:1-152` retains static invariants that are genuinely source-level and delegates token forwarding, direct MAIN effects, ownership, and cleanup semantics to executable Node cases rather than brittle source-shape strings.
- `tests/test_extension_split_sentences.py:1-110` retains all splitter fixtures and the web-preview guard, while replacing only the extension's literal `startIdx` assertion with executable attempt-ownership cases W15/W16/W30.
- `extension/manifest.json`, `extension/popup.html`, `extension/popup.css`, `requirements.txt`, `script.js`, server code, and external PM artifacts are not changed.

## Global Constraints

- Read the complete approved design at `docs/superpowers/specs/2026-10-17-flow-bar-pause-play-design.md` and the complete approved specification at `docs/superpowers/specs/2026-10-17-flow-bar-pause-play-spec.md`; the approved specification and its exact W01-W31/G01-G08/R1-R6 acceptance rules are authoritative.
- The implementation baseline is commit `30c025138d4cb2c51b6fd00f266ae79deac3a86e`; do not broaden the repair beyond the approved files and behavior.
- Keep `PRELOAD_AHEAD` at 2, cache waiting at most 10,000 ms with 200 ms checks, direct-fetch timeout at 120,000 ms, keepalive at 25,000 ms, and completion polling non-overlapping at 500 ms.
- Preserve the existing splitter, first matching `sentence.includes(trimmedSelection)` rule, XML escaping, normalized localhost server URL, voice/speed behavior, prefetching, and highlighting behavior except for owner fences and nonfatal highlight failures required by the specification.
- Preserve loop-on/repeat-current-off defaults, mutual exclusivity, previous-at-zero no-op, valid navigation creating a fresh attempt even at the same index, loop/repeat/end-of-list policies, and no audible start while paused.
- The worker is authoritative for accepted desired intent and session/attempt ownership; MAIN world is authoritative for Audio identity, currentTime, direct media effects, play outcomes, audio observations, and the exact synchronous pause/no-stale-start boundary.
- A session identity is `{tabId, leaseId, sessionId, lifecycleRevision}` and an attempt identity is `{sessionId, lifecycleRevision, attemptId, idx}`. Index equality or tab equality alone is never an ownership check.
- Allocate worker/session/start/attempt identifiers synchronously before an await that can outlive the owner. A start ticket exists before target lookup, selection extraction, or settings storage; a session shell publishes before settings storage; a newer valid start retires the predecessor before publishing its successor.
- Every continuation checks captured session and attempt ownership before and after relevant awaits, before effects, before cache/index/end mutations, before timers, and in catch/finally paths. Retired cleanup and fatal errors must compare their captured owner before touching worker state or a successor.
- All injected functions are self-contained and serializable. No injected MAIN function may access `chrome.*`; Audio and every direct media effect are MAIN-world only. The relay is isolated-world, idempotent, source-filtered, and forwards complete tokens unchanged.
- A worker intent carries `{leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaBase, paused}`. Activation carries `{leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaRevision, dataUrl}` and must reject stale owner, attempt, intent, media, or paused state before constructing Audio or calling `play()`.
- Direct media play/pause commits in MAIN before relay delivery, increments media counters, fences old starts, and sends full `{action, leaseId, sessionId, lifecycleRevision, intentRevision, mediaSequence, mediaRevision, paused, attemptId, audioId, audioStatus, startSerial, audioRevision}` tokens. Previous/next/stop are worker requests and do not preemptively rewrite desired state.
- Fresh observation is one bounded no-effect MAIN read per live attempt with one coalesced `observationNeeded` follow-up bit. It is required for admitted media actions, newly learned media/audio/start observations, and structurally current early outcomes whose identity is not yet known; it never consumes a media action or retries `play()` blindly.
- Public snapshots are `{workerId, state, sessionId, viewRevision, intentRevision, mediaRevision, phase, audioStatus, error}`. Command responses add `{ok, accepted, disposition, pageApplied, commandError}` and are constructed from current state at response time. `accepted` is not audible success; `pageApplied` is not dispatch success.
- Render Option A only: compact icons; running maps synchronously to `⏸`/`Pause`/`Pause`, paused maps to `▶`/`Resume`/`Resume`. `textContent`, `title`, and `aria-label` are written in one synchronous render. The popup uses the same canonical snapshot and high-water ordering, without continuous unsolicited synchronization.
- All ten tasks are in the single sequential `extension-sequential` lane because worker/audio and bar gestures share `extension/background.js`. Each task consumes its predecessor's owner, message, snapshot, and render-call contracts and must not rename or weaken them.
- Do not add server changes, npm/Playwright/Puppeteer/jsdom infrastructure, dependencies, popup markup/CSS redesign, playback persistence, a generic playback engine, a persistent port, a global lock, a second generic DOM implementation, or physical hardware requirements. No real TTS endpoint or audible output is required.
- Required new entrypoints are exactly `tests/extension_playback.test.cjs`, `tests/extension_dom_smoke.cjs`, and `tests/test_extension_playback.py`. The Node tests use built-ins only and the Python wrapper must work with the repository's ordinary `python -m pytest` invocation.
- Required validation includes `node --check extension/background.js`, `node --check extension/popup.js`, `node --test tests/extension_playback.test.cjs`, `node tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium`, targeted pytest, and full pytest. Use `/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python` for documented local checks when available, while keeping tests standard-pytest compatible.
- Leave all plan checkboxes unticked. Each task ends with one scoped commit. Do not modify PM state, manifests, schedules, deliverable PM artifacts, or Git outside the child task commits. The only reports permitted are controller-assigned reports under the ignored per-plan SDD workspace and the external planning report assigned by the parent.
- Every task must leave the repository green: the existing full pytest suite plus every Node suite written so far must pass. Each task must commit only its own explicit allowlist.

## Task 1: VM harness scaffolding and pause-before-activation ordering

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `vm-scaffold-and-initial-ordering`
**Ownership:** `tests/extension_playback.test.cjs`, `extension/background.js`
**Depends on:** `[]`
**Resources:** `[]`

**Files:**
- Create: `tests/extension_playback.test.cjs` for the real-source harness and W01-W03 only.
- Modify: `extension/background.js:4-219,221-318,320-540,609-694,730-888` only for the session/attempt shell, short install barrier, pause, loader, activation, and renderer seam needed by W01-W03.

**Interfaces:**
- Consumes: real `chrome.runtime.onMessage`, `chrome.commands.onCommand`, `chrome.scripting.executeScript`, `chrome.storage.sync.get`, `fetchSentenceAudio`, and `splitSentences`; evaluate their complete production source, without production test exports.
- Produces: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, `WorkerIntentCapability = {leaseId, sessionId, lifecycleRevision, attemptId: number | null, intentRevision, mediaBase, paused: boolean}`, `ActivationCapability = {leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaRevision, dataUrl}`; all unannotated revisions/IDs here are numbers except opaque string lease/session IDs and string dataUrl.
- Produces: local `buildPlaybackSnapshot(pipeline: Pipeline | null): PlaybackSnapshot`, `acceptRuntimeCommand(msg, sender): Promise<CommandResponse>`, `injectWorkerIntent(capability: WorkerIntentCapability): PageReceipt`, and `injectActivation(capability: ActivationCapability): PageReceipt`.
- Produces: `PlaybackSnapshot = {workerId: string, state: "idle" | "playing" | "paused", sessionId: string | null, viewRevision: number, intentRevision: number | null, mediaRevision: number | null, phase: "idle" | "initializing" | "loading" | "ready" | "starting" | "playing" | "paused" | "ended", audioStatus: "none" | "starting" | "playing" | "paused" | "ended" | "error", error: {code: string, message: string} | null}`; `CommandResponse = PlaybackSnapshot & {ok: boolean, accepted: boolean, disposition: "applied" | "superseded" | "ignored" | "failed", pageApplied: boolean, commandError: {code: string, message: string} | null}`.
- Produces: `PageReceipt = {leaseId, sessionId, lifecycleRevision, attemptId: number | null, intentRevision, mediaRevision, paused, audioId: string | null, audioStatus, startSerial: number | null, audioRevision, disposition: "applied" | "idempotent" | "stale" | "superseded-by-media" | "no-effect"}` and `showControlBar(owner: PageOwner, snapshot: PlaybackSnapshot, modes: {loopEnabled: boolean, repeatCurrentEnabled: boolean}): Promise<"rendered" | "deferred">` / `updateControlBar(owner, snapshot, modes)` with the same signature.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Scaffold only the scheduling and fixture paths needed by W01-W03

- [ ] Read the approved design/specification; create a fresh worker VM per case, mock Chrome before evaluating the complete `background.js`, and append a VM-only lexical accessor. Implement these approved scheduling primitives (compact formatting below preserves their operations):

```js
class Deferred {
  constructor() { this.settled = false; this.promise = new Promise((resolve, reject) => { this._resolve = resolve; this._reject = reject; }); }
  resolve(value) { if (this.settled) return; this.settled = true; this._resolve(value); }
  reject(error) { if (this.settled) return; this.settled = true; this._reject(error); }
}
class VirtualClock {
  constructor() { this.now = 0; this.nextId = 1; this.timers = new Map(); }
  setTimeout(callback, delay = 0) { const id = this.nextId++; this.timers.set(id, {id, due: this.now + Math.max(0, delay), callback, repeat: 0}); return id; }
  setInterval(callback, delay = 0) { const id = this.nextId++; const period = Math.max(1, delay); this.timers.set(id, {id, due: this.now + period, callback, repeat: period}); return id; }
  clearTimeout(id) { this.timers.delete(id); }
  clearInterval(id) { this.timers.delete(id); }
  async advance(milliseconds) {
    const target = this.now + milliseconds;
    while (true) {
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
    const entry = {id: this.nextId++, target: details.target, world: details.world || "ISOLATED", func: details.func, args: details.args || [], executed: false, result: undefined, error: null, completion: new Deferred()};
    this.entries.push(entry); return entry.completion.promise;
  }
  pending(predicate) { const entry = this.entries.find(e => !e.executed && predicate(e)); assert.ok(entry, "expected a pending script request"); return entry; }
  async complete(entry, result) { entry.completion.resolve(result); await Promise.resolve(); }
  async fail(entry, error) { entry.completion.reject(error); await Promise.resolve(); }
}
```

- [ ] Add harness methods `start(text, tabId = 7)`, `send(message, sender = {tab: {id: 7}})`, `execute(entry)`, `complete(entry, result)`, `fail(entry, error)`, `drain()`, `page(tabId, world)`, and `assertNoUnintendedTimers()`. Start/send call real production listener/function paths; no test helper assigns pipeline/index/pause/Audio/bar state to simulate a transition. Dispatch records target/world/`func.toString()`/args; execute serializes that actual function into the selected world's VM and captures return/throw; complete settles the worker-facing promise independently, including later-before-earlier schedules.
- [ ] Use Node built-ins only, separate MAIN/isolated contexts per tab, no MAIN `chrome`, and an isolated Audio constructor that fails. Record constructor URL/identity, play identity/time/revisions, pause/src-clear/currentTime writes, timers, messages, bar attributes, polls, and cleanup. Gate storage, 200 ms cache checks/10,000 ms fallback, response/body/highlight, injection execution/completion, and play settlement independently; use virtual time/microtask draining with a finite step bound, never sleeps. Add other harness adapters only as their task's cases need them.
- [ ] Fixture: tab 7, `['First sentence.', 'Second sentence.']`, normalized localhost/default voice/speed, loop=false/repeat=false for boundary cases, URLs `data:audio/mpeg;base64,AQ==` / `data:audio/mpeg;base64,Ag==`, unfinished Audio time 3/ended=false; tab 8 is available for later cross-tab cases. Admit real session/attempt/install/selection/intent paths before any focused injection check.

### Step 2: Pin and implement the first ordering slice (R2, R3, R5, R6)

- [ ] Add one named `node:test` per row, retain IDs for `--test-name-pattern`, run to the behavioral failure, then implement only what these cases require. Record effects at the boundary; final paused state alone is insufficient.

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W01 `pause_loading_boundaries` | Hold settings storage, the first 200 ms cache tick, the 10,000 ms cache miss/fallback fetch, fetch body, and highlight execution/completion in separate runs. Accept pause before releasing each gate, then release and drain; resume afterward. | Worker `isPaused` changes before the held loader/highlight resolves; MAIN pause application is dispatched without releasing loading; zero Audio constructors and play calls while paused; the same loader/attempt reaches ready; one constructor/play after resume. |
| W02 `activation_executes_after_pause` | Load A, hold old running activation at dispatch, accept/execute/complete a newer pause, then execute/complete the old activation. | Old activation returns no-effect; constructor count and play count remain zero; page, worker, and rendered snapshot remain paused. This assertion records constructor/play calls, not only final paused state. |
| W03 `activation_executes_before_pause` | Execute old activation and hold its script completion and play settlement; execute/complete newer pause; settle the old play promise with both fulfill and reject variants. | Exactly one play before pause is allowed; the same Audio is paused at the pause boundary; no later play or playing Media Session assignment; old settlement cannot roll worker state back. |

- [ ] Allocate worker UUID, captured session/attempt identity and accepted intent before relevant awaits; publish the initializing shell before settings storage, keep one loader, and check captured ownership after cache/fetch/body/highlight. Pause changes worker desired state before awaiting MAIN application and never waits for loading, play, poll, or render. Keep preparation/bytes ready while paused; resume uses that attempt.
- [ ] Establish the short inert page installation barrier and bounded live MAIN record/Audio alias, initially workerIntentRevision=0, mediaRevision=0, mediaSequence=0 and desiredPaused=true. Use increasing safe integers within their specified worker/session scopes; random worker IDs are opaque, never ordered. Apply worker intent synchronously, reject lower revisions, make equal revision idempotent, advance revision before effects, and invalidate the play serial/pause owned Audio on pause. Activation must check exact lease/session/lifecycle/attempt/intent/media, running desired state and eligibility before construction; publish Audio/alias before synchronous play. Never replay from promise settlements. Assert `audioSpy.constructors.length === 0` and `audioSpy.plays.length === 0` when W02 releases old activation after pause.
- [ ] Return truthful current snapshots/receipts and separate accepted/application/starting from audible success. Preserve the `showControlBar(owner, snapshot, modes)` / `updateControlBar(owner, snapshot, modes)` seam and synchronous three-attribute mapping used by these assertions. Keep this the smallest useful production slice, aiming below roughly 300-400 production and 300-400 test lines; do not build the W04-W31 suite here.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add tests/extension_playback.test.cjs extension/background.js
git commit -m "fix(extension): honor pause before audio activation"
```

## Task 2: Resume identity, ordered toggles, and pending-load continuation

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `resume-and-intent-identity`
**Ownership:** `extension/background.js`, `tests/extension_playback.test.cjs`
**Depends on:** `[1]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:4-219,221-318,320-540,730-888` for synchronous toggle/resume, live play identity, activation coalescing, and owner/versioned render-call handling.
- Modify: `tests/extension_playback.test.cjs` for W04-W06 and the required render/play gates.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`; `injectWorkerIntent({leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaBase, paused}): PageReceipt`, `injectActivation({leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaRevision, dataUrl}): PageReceipt`, and `showControlBar(owner, snapshot, modes)` / `updateControlBar(owner, snapshot, modes): Promise<"rendered" | "deferred">`.
- Produces: bound `togglePlayback` acceptance, explicit idempotent pause/resume, and attempt `activationRequest: null | {intentRevision: number, mediaRevision: number, promise: Promise<unknown>}`; duplicate tuples coalesce, newer tuples replace the reference without awaiting old completion.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Add identity and intent-order regressions (R2, R3, R5, R6)

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W04 `stale_resume_both_execution_orders` | With unfinished Audio, accept resume R then newer pause P. Run R-before-P but complete R last, and P-before-R in a separate schedule; delay old render. | R-before-P has at most the allowed pre-P play; P-before-R has no R play; both end with paused worker/page state and all three UI attributes from P. |
| W05 `rapid_toggle_parity` | From running, accept two and three bound toggles without render completion; execute newer intent/render requests first, older last, and reverse completion order. | Two toggles finish running and three finish paused; accepted intent revisions have parity; glyph/title/aria-label and page desired state agree; messages are `togglePlayback` regardless of stale glyph. |
| W06 `unfinished_resume_identity` | Pause default Audio at time 3; resume and issue a redundant explicit resume after acknowledgment. | Same Audio object, `audioId`, and `currentTime`; no seek/fetch/constructor; exactly one resume play and one completion observer; redundant resume does not call play again. |

### Step 2: Preserve one unfinished/pending attempt across accepted commands

- [ ] Run each case above to its behavioral failure before repair. Resolve bar origin against sender tab and lease/session/lifecycle; tab-origin commands cannot fall through as popup. Popup/untagged no-tab commands intentionally target the current published owner. Accept toggle synchronously from `pipeline.isPaused`, increment intent/view revisions and `localIntentFloor`; explicit pause/resume are idempotent. No glyph read chooses the command.
- [ ] Resume unfinished Audio on the same attempt/audioId/object at unchanged currentTime, with no fetch/seek/constructor or extra observer. A resume while loading retains its `loadPromise`; a ready/current running tuple can schedule activation while an obsolete activation's response is held. Equal running intent or already playing/starting Audio never issues a second play. Retain only the latest activation request, not a map/history.
- [ ] Fence every live play outcome by captured owner/attempt/reference/audioId/startSerial/running revisions; pause/disposal invalidates the serial. Fulfillment alone establishes playing/Media Session playing; running buffering/starting uses Media Session paused. Old fulfillment/rejection cannot play again or overwrite a newer pause. Render acceptance is independent of audio and compares owner/view revision; a delayed old render cannot change the toggle's three attributes. Responses use state at send time, after actual MAIN application or truthful supersession.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_playback.test.cjs
git commit -m "fix(extension): preserve resume identity and toggle ordering"
```

## Task 3: Ready-paused resume and qualified completion observation

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `ready-and-end-observation`
**Ownership:** `extension/background.js`, `tests/extension_playback.test.cjs`, `tests/test_media_session.py`
**Depends on:** `[2]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:4-219,542-598,730-888` for owned phases, ready/resume continuation, completion polling and qualified replay decisions.
- Modify: `tests/extension_playback.test.cjs` for W07-W11.
- Modify: `tests/test_media_session.py:122-152` only if its natural-cleanup source-shape guard needs the captured-owner signature; preserve the media-before-clear invariant until its executable replacement in Task 6.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, the single `attempt.loadPromise`, `attempt.dataUrl: string | null`, and `activationRequest: null | {intentRevision: number, mediaRevision: number, promise: Promise<unknown>}`.
- Produces: attempt `phase`, `retired`, `pollTimer`, `pollInFlight`, `completionObserved`, `completionObservedWhilePaused`, `completionConsumed` (equivalent derived predicates allowed), and a fenced poll returning owned/ended/audio status or missing/stale; one 500 ms timeout and at most one poll in flight.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Exercise loading, ready, and both completion-decision orders (R3, R6)

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W07 `resume_during_pending_load` | Hold fallback fetch; accept pause, resume, pause, resume before releasing it; repeat with an ending paused before the next resume. | One fetch/load; one Audio construction and initial play using the latest running revision; one observer/advance; zero starts while the ending state is paused until a later resume. |
| W08 `ready_paused_resume` | Complete load and highlight under pause, leave the attempt ready without Audio, then resume twice. | Retained data URL is used; one Audio and one play; no obsolete preceding Audio is played; second resume is idempotent. |
| W09 `end_observed_paused_replay` | Pause A, mark its actual Audio ended, execute/complete an owned poll while paused, then resume. | Completion marker belongs to A; resume retires A, creates a distinct attempt at index 0, and performs one replay; duplicate old end samples have no effect. |
| W10 `end_observed_after_resume` | Mark A ended while paused, accept resume before delivering its owned ended poll, then complete the poll; run modes-off, repeat-current, and loop-at-last variants. | Resume never plays ended A; the later poll chooses idx 1 with modes off, idx 0 with repeat-current, and idx 0 with loop; no forced paused-end replay occurs. |
| W11 `pause_at_completion_decision` | Execute an ended poll but hold completion; accept pause and mode change before completion, then run completion-first-then-pause as a second schedule. | Pause-before-decision keeps current index with a paused-end marker and no advance; decision-before-pause may select the next/replay attempt, which becomes paused; no DOM mode read decides the worker path. |

### Step 2: Commit the completion decision synchronously against current intent/modes

- [ ] Run each case above to its behavioral failure before repair. Enforce phase priority: initializing until settings/setup finish; loading during cache/fetch/highlight; ready when URL/highlight are ready with no Audio; starting for a live pending play; playing on live success; paused for unfinished Audio with accepted pause; ended after accepted completion without advancement. Pausing initializing/loading/ready retains that phase. Missing Audio in loading/ready is legitimate.
- [ ] Replace the async interval with nonoverlapping 500 ms timeouts carrying captured owner/attempt/audio identity; revalidate after receipt. Missing/stale is not ended. At the worker's synchronous acceptance of an owned ended sample, consume it once and read current `isPaused`; paused marks ended-with-paused-completion without advance, running commits repeat/advance/loop using worker modes with no awaited DOM mode read.
- [ ] Resume after observed paused completion retires that attempt and creates a new attempt at the same index from retained/cache/fetched bytes. Resume before the ended sample is accepted never plays ended Audio and lets the later poll apply normal policy. A pause after the synchronous decision can pause the new attempt. Keep exactly one loader/activation/observer path, reject duplicate old end samples, and preserve the unchanged cleanup-order invariant while adapting any obsolete literal signature guard.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_playback.test.cjs tests/test_media_session.py
git commit -m "fix(extension): qualify paused completion and replay"
```

## Task 4: Stop, replacement, and teardown ownership fences

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `session-retirement-and-replacement`
**Ownership:** `extension/background.js`, `tests/extension_playback.test.cjs`, `tests/test_media_session.py`
**Depends on:** `[3]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:4-318,320-540,609-694,730-888` for start tickets, lease/install/retirement fencing, and captured cleanup ownership; bar edits are limited to owner admission/teardown fences.
- Modify: `tests/extension_playback.test.cjs` for W12-W14 and target-lookup/selection/lease/cleanup gates.
- Modify: `tests/test_media_session.py:1-152` only for changed owner/helper signatures in still-meaningful static world/cleanup invariants; full semantic conversion belongs to Task 6.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}` and real start/stop/setup/highlight/render functions; each operation captures its owner before awaits.
- Produces: `pendingStart: null | {requestId: string, requestSerial: number, cancelled: boolean}`, one owned `initializationPromise`, bounded per-tab shared lease-claim/install barriers, and strict `ownsPipeline(pipeline): boolean` / `ownsAttempt(pipeline, attempt): boolean` (equivalent local helper spellings allowed).

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Pin initializer and replacement ownership (R1, R6)

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W12 `stop_initializer` | Hold start at tabs.query, keyboard selection extraction, storage, lease/setup, and highlight initialization in separate variants; stop; apply captured retirement fences before old gates resolve. | No pipeline resurrection, Audio/start/bar/media setup after the relevant fence; old continuations dispatch no new setup; no unhandled rejection; canonical state is idle. |
| W13 `replace_initializer_latest_wins` | Variant A holds S1 target lookup before publication, then publishes S2 before releasing S1. Variant B publishes S1 initializing, holds settings, publishes S2, then releases S1; run same tab/index and tabs 7/8. | Variant A never publishes S1. Variant B may retain already completed S1 effects but cannot republish or continue after replacement. Only S2 remains live and may start/render; old work cannot alter S2 cache/settings/owner. |
| W14 `replace_pending_attempt_boundaries` | Hold old cache/fetch/body/highlight/activation dispatch, replace S1 with S2 at idx 0 in tab 7, apply S1 retirement and S2 admission fences, release/execute S1; repeat with S2 on tab 8. | Old work cannot construct/play/modify S2, set its paused-end marker, or write its cache; already-dispatched old page effects are inert after the captured fence. |

### Step 2: Fence every world and terminal continuation against its captured owner

- [ ] Run each case above to its behavioral failure before repair. Allocate starter serial/ticket before tabs.query, shortcut selection extraction, or storage; invalid input never replaces valid playback. Newer valid start invalidates earlier tickets immediately; only the current ticket publishes after target/nonempty splitting. Publish shell/default modes/initializing before settings. At replacement, synchronously allocate predecessor retirement first, then the strictly larger successor lifecycle. Popup/shortcut stop cancels all pending starts; bound stop/natural completion/error retires only its addressed owner and preserves an unrelated newer unpublished ticket.
- [ ] Check captured pipeline object, active tab/lease/session/lifecycle, cancellation, exact live attempt object and retirement before effects, after awaits, cache/index/end writes, scheduling, and catch/finally. Preloads write only their captured live cache. Cancel attempt/initializer/timer authority synchronously; no cleanup target is rediscovered from activePlayback after await and no catch calls unconditional global clearPlayback.
- [ ] Claim page worlds with CAS against both `(leaseId, lifecycleFloor)`; losing claims do nothing, including when token is unchanged but floor advanced. Share/serialize establishment only per worker/tab, retry at most twice after first claim, and remove closed-tab entries. Retire/claim MAIN before isolated; an abandoned inert partial claim can be reclaimed by the next live owner. Different leases cannot claim implicitly or clean. Keep only current bounded floors/tombstones, with no lock held across loading/play/poll.
- [ ] In each injected function recheck lease/session/lifecycle inside its world. Install below-floor rejects; equal only same session/idempotent; newer retires predecessor and installs inert/paused without resetting live revisions/attempt. Setup, intent, selection and activation await only the short shared install barrier; pause updates worker immediately. Retirement advances a captured floor even before setup finishes, preserves newer resources, and keeps the fence after teardown so delayed show/highlight/install cannot recreate old state.
- [ ] Cleanup attempts media handlers/metadata/keepalive before owned Audio pause/clear, then isolated highlights, bar/listeners/timers, and owner-compared worker clearing; Media Session playbackState is none after retirement. Continue all cleanup steps after one fails; old terminal/failure paths cannot clear a successor. Keep any affected Python static invariant meaningful while adjusting only its changed signature.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_playback.test.cjs tests/test_media_session.py
git commit -m "fix(extension): fence initializer replacement and teardown"
```

## Task 5: Navigation attempts, poll identity, and late failures

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `attempt-navigation-and-stale-failures`
**Ownership:** `extension/background.js`, `tests/extension_playback.test.cjs`, `tests/test_extension_split_sentences.py`
**Depends on:** `[4]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:4-219,221-318,542-598,609-694,730-888` for fresh navigation attempts, page attempt floors, poll identity and obsolete failure fencing.
- Modify: `tests/extension_playback.test.cjs` for W15-W17.
- Modify: `tests/test_extension_split_sentences.py:78-110` only to replace the extension literal startIdx guard with executable W15/W16; preserve all splitter fixtures and the script.js guard.

**Interfaces:**
- Consumes: `ownsPipeline(pipeline): boolean`, `ownsAttempt(pipeline, attempt): boolean`, `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, and the single owned 500 ms poll.
- Produces: owner-bound `prevSentence(owner)`, `nextSentence(owner)`, `jumpToSentence(owner, idx: number)`; every valid selection/replay has a fresh monotonically increasing attemptId and page attemptFloor, including same-index replay/return.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Pin actual attempt identity rather than index equality (R1, R3, R4, R6)

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W15 `same_index_and_return_navigation` | Hold A at idx 0; jump to idx 0 for A2, or jump to 1 then 0 for A3; release old load/start/poll. | Attempt IDs are distinct; only the latest attempt owns Audio/observer/end advancement; index equality never revives A. |
| W16 `poll_identity_and_no_overlap` | Hold one 500 ms poll, advance virtual time by 2,000 ms, navigate/replay the same index, execute old poll and complete it last. | At most one old poll is in flight; old poll cannot inspect successor Audio, mutate successor end flags, or advance successor; missing/stale Audio is not completion. |
| W17 `late_failure` | After S2 starts in the same tab/index, reject old settings/fetch/script/play paths independently; include play rejection after pause. | S2 Audio/state/bar/metadata survive; old failure cannot clear or stop S2; invalidated play rejection is nonfatal; future preload rejection keeps the session alive. |

### Step 2: Select a fresh attempt without importing old intent or Audio

- [ ] Run each case above to its behavioral failure before repair; for W15/W16 compare successor attemptId and Audio identity, never merely non-null values. Capture owner/pipeline/attempt at startSentenceTTS/playNextSentence/navigation entry; retire old observers/attempt before selecting even the same index. Selecting advances the page attempt floor, pauses/clears only displaced attempt Audio, and never writes desiredPaused. Apply navigation's accepted running intent separately; newer page pause may supersede it. Previous at 0 is a no-op; invalid/noninteger/out-of-range jump returns invalid-argument with no retirement or other side effect.
- [ ] Reject stale attempt selection/start/poll both in worker and MAIN; never read successor Audio via the global alias as if owned by an old poll. After a held poll resolves recheck ownership before end flags/index/advancement or scheduling; retain at most one in-flight poll. A missing/stale report is not completion. Keep cache/loader writes captured, and treat obsolete settings/fetch/script/play rejection as no current-state mutation; invalidated play rejection and future preload failure are nonfatal.
- [ ] Replace only the extension's startIdx source assertion with subprocess-backed parameterized selectors `W15 same_index_and_return_navigation` and `W16 poll_identity_and_no_overlap`; invoke `node --test --test-name-pattern <case> tests/extension_playback.test.cjs`, timeout 120, and assert return code zero plus the named case actually ran/passed. Keep every splitter fixture, XML/splitting algorithm and the web-preview script.js startIdx guard. Task 7 adds W30 to this existing helper after that case exists; never count zero selected cases as evidence.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_playback.test.cjs tests/test_extension_split_sentences.py
git commit -m "fix(extension): bind navigation and polls to attempts"
```

## Task 6: Media ownership, relay tokens, canonical responses, and popup

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `media-relay-and-canonical-consumers`
**Ownership:** `extension/background.js`, `extension/popup.js`, `tests/extension_playback.test.cjs`, `tests/test_media_session.py`, `tests/test_extension_split_sentences.py`
**Depends on:** `[5]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:4-318,320-540,609-694,730-888` for full media/source binding, terminal error/cleanup responses and stable renderer calls; no gesture redesign here.
- Modify: `extension/popup.js:1-281` for canonical snapshots, request epochs and bounded transport refresh.
- Modify: `tests/extension_playback.test.cjs` for W18-W24, W26 and W27 with real media/popup adapters.
- Modify: `tests/test_media_session.py:1-152` for remaining obsolete source-shape replacements.
- Modify: `tests/test_extension_split_sentences.py:78-110` only to finalize the W15/W16 executable-helper diagnostics, retaining all fixtures/web guard.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, `buildPlaybackSnapshot(pipeline: Pipeline | null): PlaybackSnapshot`, and `acceptRuntimeCommand(msg, sender): Promise<CommandResponse>` with the exact snapshot/response fields in Global Constraints and Task 1; renderer signatures remain `showControlBar(owner, snapshot, modes)` / `updateControlBar(owner, snapshot, modes)`.
- Produces: `injectMediaSession(owner: PageOwner): void`, `injectMediaRelay(): void`, and full media notifications `{action, leaseId, sessionId, lifecycleRevision, intentRevision, mediaSequence, mediaRevision, paused, attemptId, audioId, audioStatus, startSerial, audioRevision}`; isolated runtime forwarding adds `source: "media"` without changing tokens.
- Produces: `applyPopupSnapshot(snapshot: PlaybackSnapshot): boolean` and `handlePopupResponse(response: CommandResponse | null, command: string): Promise<void>`; popup tracks acceptedWorkerId, renderedViewRevision and each request's issuing connection epoch.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Add the media/cleanup/response cases and real popup consumer (R1, R2, R4, R5, R6)

- [ ] Extend the harness with independent relay-delivery, direct media, cleanup-failure and real-popup VM adapters; add each row and run its red case. W26/W27 are established here and remain part of Task 7's W25-W31 sweep.

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W18 `terminal_cleanup_overlap` | Exercise stop, natural nonloop completion, and fatal error. Hold each media/audio/highlight/bar cleanup at dispatch and completion, publish S2 same tab/index, then run old cleanup; inject one failure in each cleanup step. | S2 survives; old finalization cannot clear it. Uncontended order is media handlers/metadata/keepalive, then pause/clear Audio, then highlights, bar, final worker clear. Every cleanup step is attempted after one fails. |
| W19 `media_direct_without_worker` | Execute actual MAIN play/pause handlers with relay delivery withheld, once with unfinished Audio and once loading/no Audio. | Page pause/play is immediate and same-identity/currentTime preserving; loading/ended play creates no Audio; media revisions/sequence advance before worker notification; no MAIN `chrome` access. |
| W20 `media_pause_fences_queued_start` | Dispatch old activation/resume, execute direct MAIN media pause, withhold notification, then execute old operation. | MAIN media revision rejects stale activation; no constructor/play after media pause even before worker notification. |
| W21 `media_notification_reordering` | Apply media pause M1 then resume M2; deliver M2 then M1 and duplicate M2; repeat with final pause ordering. | Worker/bar converge to highest admitted media revision; delayed lower notification and duplicate do not replay Audio or roll state back. |
| W22 `mixed_media_worker_intents` | (a) Adopt media pause, apply popup resume, deliver old pause. (b) Dispatch worker resume on old media base, execute media pause first, then apply worker resume and delay receipt. (c) Accept later bar toggle while receipt is delayed and deliver all out of order. | No rollback after popup intent; conflict receipt reports superseded actual pause and no play; later local intent wins by revision; no blind retry; a new post-reconciliation command can resume. |
| W23 `originating_session_messages` | Queue bound S1 bar/media messages, replace with S2 same tab/index, deliver S1 and tab-8 messages; issue unbound popup pause; separately deliver valid bound S1 stop while S2 is only a pending target lookup. | S2 survives stale messages and returns canonical S2 ignored snapshots; unbound popup intentionally targets published S2; bound S1 stop cannot cancel unrelated pending S2 start ticket. |
| W24 `current_errors` | Reject current settings/fetch/play/activation/poll/bar update; include no-receipt/missing-Audio and play pending at command response; reject obsolete activation/render after newer pause. | Current failures retire to idle/error and clean owned resources; responses distinguish failed page application from accepted/starting; obsolete errors leave newer pause intact; unsupported Media Session is nonfatal. |
| W26 `relay_and_world_invariants` | Install relay twice, set up media repeatedly, emit a full-token action, clean twice, and inspect every script target/world/timer. | One relay listener/forward; full tokens unchanged; Audio/media effects all MAIN; no MAIN `chrome`, port, or stale port name; one 25 s keepalive reset/clear; all five handlers install/clear. |
| W27 `snapshot_and_popup_responses` | Evaluate real `popup.js` with mocked DOM/storage/server and worker messages. Hold pause, accept resume/stop/replacement, deliver newer then older snapshots, ignored/superseded/failed/transport errors, then a fresh worker ID with lower view revision and an old-worker delayed response. | Popup uses canonical snapshot, worker ID, and view high-water; old worker cannot overwrite fresh state; one bounded `getPlaybackState` refresh on transport failure; no unconditional idle/playing guess. |

### Step 2: Apply direct effects, truthful snapshots, and bounded popup refresh

- [ ] Keep MAIN handlers bound to installed owner; increment mediaSequence before every play/pause/previous/next/stop effect/notification. Only play/pause advances mediaRevision/desiredPaused. Play immediately resumes only unfinished owned Audio; absent/ended Audio merely asks for continuation. Pause immediately invalidates serial/pauses; previous/next/stop do not preemptively clear or change running intent. Relay is source-filtered (`event.source === window`), marker-preserving (`__freeTtsMedia`), idempotent, and forwards full tokens in one runtime message. Keep all effects MAIN, no MAIN chrome/ports, all five handlers and one reset/cleared 25 s keepalive; ping is a bound no-op.
- [ ] Share media-action admission: validate tab/lease/session/lifecycle, `localIntentFloor <= notification.intentRevision <= pipeline.intentRevision`, strictly increasing mediaSequence; only admitted actions advance lastMediaSequence. Adopt nondecreasing media observations; duplicates/lower messages cannot replay. Worker intent with mismatched mediaBase advances seen worker revision but preserves page intent/effect, returning superseded-by-media; no blind requested-state retry. Originating bar/media commands cannot mutate a successor, and bound stop cannot cancel unrelated pending start tickets.
- [ ] Canonical snapshots are constructed at response-send time; getPlaybackState returns immediately through every held long boundary. Idle has null session/intent/media, phase idle/audio none and retained workerId/viewRevision/error. Increment viewRevision on publication, desired/mode changes, owned phase changes and retirement/failure; clear lastError on valid new session. Responses add boolean ok/accepted/pageApplied, applied/superseded/ignored/failed disposition, commandError distinct from canonical error. Stale/no-session is ok/ignored/not accepted; invalid arguments fail without side effects. Pause success requires a real receipt; start/resume acknowledges setup/intent/scheduled continuation without cache/play completion or invented audible success.
- [ ] Rendering is independent: try a current render before response, retaining only newest deferred snapshot while initial creation is pending; absence before creation is deferred, absence/current failure after creation is fatal. Superseded render errors cannot retire newer intent. Stable terminal codes include invalid-argument, settings-failure, load-timeout, load-failure, page-operation-failure, play-rejection, missing-audio, poll-failure, render-failure, cleanup-failure. Current errors retire captured owner and attempt all remaining cleanup, obsolete errors do nothing current; highlight lookup/rect/scroll exceptions and future preload failure are nonfatal. Unsupported media is nonfatal; inability to establish MAIN ownership is fatal. Stop cleanup failure returns truthful failure with idle/successor snapshot.
- [ ] Popup consumes canonical snapshots for initialization, speak/pause/resume/stop and explicit refresh. Same worker compares viewRevision; a newly contacted worker resets high-water, and later-issued request/connection epochs prevent old-worker delayed responses undoing it. Ignored/superseded/failed responses render actual state and log commandError. Transport failure performs one getPlaybackState refresh, then retains last confirmed state; never guess idle/playing from failure/request/ok. Keep layout/settings/server controls and no unsolicited live synchronization.
- [ ] In test_media_session.py retain meaningful MAIN-world/action-name/no-port/relay-marker/idempotence/25 s invariants, replacing obsolete forwarding/syncPausedState/DOM-fallback/natural-cleanup literal bodies with executable W18/W19/W20/W26 checks. The helper selects each real named case and requires zero exit plus the named pass, with bounded subprocess diagnostics. Keep splitter fixtures/web guard and W15/W16 helper intact. Task 7 extends selectors to W25/W29 and W30 when registered, completing the original approved Python coverage without a zero-match intermediate pass.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js extension/popup.js tests/extension_playback.test.cjs tests/test_media_session.py tests/test_extension_split_sentences.py
git commit -m "fix(extension): reconcile bound media and canonical popup state"
```

## Task 7: Loop/repeat, retained errors, and worker/media command reconciliation

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `modes-and-fresh-observation`
**Ownership:** `extension/background.js`, `tests/extension_playback.test.cjs`, `tests/test_media_session.py`, `tests/test_extension_split_sentences.py`
**Depends on:** `[6]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:4-318,542-598,730-888` for mode/end boundaries, bounded fresh observation and media navigation/outcome reconciliation.
- Modify: `tests/extension_playback.test.cjs` for W25/W28-W31 and the complete W25-W31 regression sweep including existing W26/W27.
- Modify: `tests/test_media_session.py:1-152` only to extend existing executable case selectors with W25/W29.
- Modify: `tests/test_extension_split_sentences.py:78-110` only to extend existing executable case selectors with W30.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, full media tokens from Task 6, live page audioId/startSerial/audioRevision, `ownsAttempt(pipeline, attempt): boolean`, and canonical snapshots.
- Produces: `injectFreshObservation(owner: PageOwner, attemptId: number): PageObservation` (synchronous no-effect full intent/audio/audioError read), `requestFreshObservation` with one `observationRequest` and one `observationNeeded` bit per live attempt, and exact-revision audio-outcome admission.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Finish W25-W31 with all early-outcome and navigation schedules (R1, R2, R3, R5, R6)

- [ ] Register the missing rows, run red, and rerun existing W26 relay/world and W27 snapshot/popup cases from Task 6. Preserve every W29 crossing and fulfill/reject/pause/navigation/replacement variant, including these exact revision/serial samples: from paused R4/M2/S3, direct play M3/S4, hold N/R4/O13 starting, settle F/O14; in receipt-triggered crossing accept popup R5 on M2 and execute/hold C/R5/M3/S4/O13, deliver F/R5/M3/S4/O14 first, then C and rejected N/R4. W31 uses at least three sentences.

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W25 `modes_and_splitting` | Run real extension splitter against retained fixtures; toggle loop/repeat via bound messages; run previous/next/jump and all end boundaries. | Sentence arrays are unchanged; mode defaults/exclusivity and boundary indices remain; one replay/advance occurs per end policy. |
| W28 `lease_and_lifecycle_tombstones` | Queue old lease claim/install/show/highlight/retire, apply newer claim/session or stop fence, then execute old operations; include unchanged token with advanced lifecycle floor, CAS loss, and equal-lifecycle install after retirement. | Old work cannot reset/recreate media/UI/audio; fences survive idle; fresh worker can claim without restoring lost pipeline; claim retries are bounded. |
| W29 `early_audio_outcome` | Variant A completes play outcome before activation receipt. Variant B starts from paused unfinished R4/M2/S3, direct media play creates M3/S4, holds N/R4/O13, settles outcome O14, delivers F before N/read. Variant C accepts popup R5, holds conflict receipt, settles/delivers outcome, then completes receipt and old N. Variant D executes a pre-settlement read, delivers outcome first, then completes read and one coalesced resample; parameterize fulfill/reject and replacement/pause/navigation. | Early outcome can establish Audio/serial; old receipt cannot downgrade. Current rejection reaches idle/error; fulfillment reaches playing on same Audio/time with one play/no constructor/retry. Old N need not be admitted and cannot advance media sequence; post-outcome sample is guaranteed; max one in-flight read plus one resample. |
| W30 `navigation_resume_attempt_binding` | Hold new idx-1 selection, accept pause/resume and execute resume intent before selection, release new load, run old idx-0 start last. | Resume cannot play old idx-0 Audio; selected latest attempt activates once after its own selection/intent eligibility. |
| W31 `delayed_media_navigation` | Queue media next/previous/stop at base R, accept/apply bound pause at R+1, then deliver old commands. Separately issue two ordered media-next actions before first injection completes with at least three sentences, overtake first receipt with second notification, and interleave direct pause. | Delayed media cannot unpause/navigate/stop newer local intent; ordered next burst advances twice with distinct attempts and one surviving path; duplicate sequence has no effect; receipts do not consume actions or borrow newer pause revision. |

### Step 2: Reconcile from retained current observations without replay retries

- [ ] Preserve loop-on/repeat-off defaults/exclusivity, disabling one never enables the other, previous-at-zero and invalid jump no-op policy. End/repeat/loop/manual navigation/paused-end replay each allocate fresh attempts; next beyond last starts index 0 for repeat or loop, otherwise completes. Run real splitter fixtures unchanged and reuse normalized session URL/voice/speed for preload/fallback. No DOM mode read or mode-only localIntentFloor increase.
- [ ] Retain MAIN audioError `{code,message}` with audioStatus error and exact audio/serial/revision until pause/disposal/new live play clears it. Increment audioRevision on creation/disposal, accepted play/pause, live outcome and first ended observation; every receipt/notification carries it. Outcome checks captured owner/attempt/reference/audioId, serial and running intent; no then/catch/finally invokes play. Current exact outcome/read may establish previously unknown audioId/serial, then reject different Audio/lower serial and adopt newer serial only with greater audioRevision. Old activation receipts never downgrade newer observations.
- [ ] Fresh-read triggers: admitted media play/pause; current receipt learning unknown media revision/live invocation including conflict/read receipts; structurally owned higher early outcome whose revision/serial is not yet known. Reject wrong owner/attempt/different established Audio/lower media/serial/non-new observations first. Early outcome requests a harmless read, never proposes intent; stale worker revision cannot authorize adoption/failure. Reads do not advance lastMediaSequence, consume delayed navigation, create Audio or play, and never retry failed injection.
- [ ] One observation in flight plus observationNeeded coalesces triggers, including outcomes arriving after an earlier read executes but before its receipt. Process receipt, then one needed fresh sample for the still-current owner; learning a new invocation may trigger one follow-up, unchanged read cannot self-trigger. While reconciliation is required, let loading finish but defer worker activation/replay from provisional running samples. Pause/new local intent acknowledges independently; an obsolete sample has no continuation authority. Adoption requires current worker revision/nondecreasing media and exact resulting accepted audio revisions/attempt/Audio, nondecreasing serial and greater audioRevision. Current retained error retires owner; playing/starting needs no extra play; none/running can continue single loader; owned ended uses qualified worker completion decision.
- [ ] Media navigation captures originating notification.mediaRevision as mediaBase, never a newer receipt value. A later known media state preserves desired pause through selection; admitted media navigation increments worker intent without raising localIntentFloor. Ordered next/next remain two distinct commands even when a receipt overtakes the second notification; higher sequence suppresses reordered lower undelivered actions, with no resequencing queue. Assert W29 lastMediaSequence/audioRevision/one read/one follow-up, W30 never resumes the old attempt before selection, and all W28 CAS/tombstone/bounded-retry cases.
- [ ] Extend existing Python delegates to cover W19/W20/W25/W26/W29 and W15/W16/W30 with exact named passing cases; keep all source-level media invariants, splitter fixtures and web-preview guard. This is a selector extension over Task 6's completed source-shape replacements, with no new test entrypoint or dependency.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_playback.test.cjs tests/test_media_session.py tests/test_extension_split_sentences.py
git commit -m "fix(extension): reconcile media outcomes and end policies"
```

## Task 8: Bar gesture lifecycle, cancellation, drag, and versioned rendering

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `bar-lifecycle-and-dom-fixture`
**Ownership:** `extension/background.js`, `tests/extension_dom_smoke.cjs`
**Depends on:** `[7]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:320-540` for show/update/hide, per-instance gesture ownership, bar event cancellation, drag and renderer; `extension/background.js:57-115` only for a narrow bar source/session binding correction.
- Create: `tests/extension_dom_smoke.cjs` for the real Chromium fixture/CDP pipe and G01-G04.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, canonical PlaybackSnapshot fields in Global Constraints, and `showControlBar(owner, snapshot, modes)` / `updateControlBar(owner, snapshot, modes): Promise<"rendered" | "deferred">`; keep all worker/MAIN contracts intact.
- Produces: closure-owned `{instanceId: string, leaseId: string, sessionId: string, lifecycleRevision: number, alive: boolean, gestureRevision: number, origin: "bar" | "page" | null, lastCompletedOrigin: "bar" | "page" | null, selectionChanged: boolean, selectionTimer: number | null, selectionSnapshot: SelectionSnapshot | null, drag: DragState | null}` plus listener/timeout handles, and `renderControlBarSnapshot(bar: HTMLElement, snapshot: PlaybackSnapshot, modes: {loopEnabled: boolean, repeatCurrentEnabled: boolean}): void`.
- Produces: `runDomSmoke(argv: string[]): Promise<void>` at `tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium`, nonzero for missing Chromium/assertion/timeout/browser/cleanup failure; bound bar messages `{source: "bar", leaseId, sessionId, lifecycleRevision, action, idx?: number, enabled?: boolean}`.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "tests/extension_dom_smoke.cjs", "--chromium", "/usr/bin/chromium"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Build the real-browser fixture and first gesture cases (R1, R2, R4, R5, R6)

- [ ] Use Node assert/fs/os/path/vm/child_process built-ins. Capture production injected callbacks by invoking real showControlBar/updateControlBar/initPageHighlighting/hideControlBar through a worker executeScript recorder; embed their actual `func.toString()` and arguments plus a complete-message mock runtime in temporary HTML. No copied predicate/model, external reproduction file, server, extension package or generic second DOM implementation.
- [ ] Fixture paragraph is exactly `First sentence. Second sentence.` with real Range/Selection/text nodes and production-created controls; populate sentences through actual owned highlight initialization. Ranges: first 0..15, second 16..32, word Second 16..22. Install fixture-only timer queue `window.__advance(ms)` for deterministic 100 ms callbacks, separate from bounded host CDP deadlines.
- [ ] Launch executable `/usr/bin/chromium` (absence fails) with `--headless=new --disable-gpu --remote-debugging-pipe` and a temporary profile/HTML. Spawn fd 3 writes and fd 4 reads; frame `JSON.stringify({id, method, params}) + "\0"`, buffer/split null-delimited JSON, correlate IDs/session IDs, create/attach target, and use Runtime.evaluate. Enforce per-step/process deadlines; EOF/stderr failure/assertion/timeout/cleanup failure exits nonzero with diagnostics. Always terminate/kill and delete profile/HTML in finally. A required sandbox-disable flag is test-only and reported.
- [ ] Add these cases against the actual injected handlers, assert absence of jump as well as intended complete bound messages, and run red before repairs. G02 includes CDP Enter/Space default activation for a button/checkbox; use Enter `keyDown` with carriage-return text, since rawKeyDown alone is insufficient.

| ID and test name | Events/timers | Required executable assertions |
| --- | --- | --- |
| G01 `retained_selection_controls` | Retain first-sentence range; run mousedown/up/click toggle and advance 150 ms. Parameterize previous/next/close, nested button child, label/text/input, bar background, and control double-click. | Only intended bound action(s), no `jumpToSentence`; toggle is `togglePlayback`; modes remain mutually exclusive; interactive descendants do not drag. |
| G02 `pending_selection_invalidated` | Make a genuine page selection schedule the 100 ms callback; at 50 ms interact with each G01 target, focus a control, press Space/Enter, and invoke synthetic `.click()` without mousedown. Advance beyond 150 ms. | No delayed jump; actual control action still occurs; mutation-removing the bar cancellation makes this case fail with an extra jump. |
| G03 `bar_drag_release_outside` | Start background drag with retained text, move over page, release on paragraph, advance 150 ms; repeat pointercancel/lost gesture. | Bar position changes; drag ends; no selection jump; cancellation leaves no active drag/timer. |
| G04 `teardown_replacement_timers` | Schedule page-selection callback, clean up at 50 ms, create successor bar, advance old timer, and invoke an old control reference. | No old message/listener; successor bar and attributes remain unchanged; repeated cleanup is safe. |

### Step 2: Own gestures, timers, cleanup, and each synchronous render

- [ ] Keep one closure record per actual bar, independently tracking origin from drag and retaining lastCompletedOrigin through click/dblclick. Normalize text nodes and classify composedPath/containment against this bar, including nested button children, labels/text/input/background. Capture mousedown once before document selection; bar origin advances ticket/cancels selection and remains bar-origin on outside release. Only noninteractive background drags; button/input/label descendants never drag. Do not register compatibility pointer/mouse down/up pairs or preventDefault/stop propagation merely to filter selection.
- [ ] Capture click/change/keydown/focusin on bar to invalidate pending selection before actions and preserve default activation, including synthetic .click without mousedown. Toggle always sends togglePlayback; previous/next/close/modes send explicit bound actions. Every captured control checks alive. Escape cancels ticket first then sends bound stop. Blur/hidden visibilitychange/pointercancel clear timer/drag, advance revision/reset active origin; keep movement/compact widths/icons/placement/mode labels.
- [ ] Cleanup first sets alive=false, advances ticket, clears timeout/drag, then removes every mousedown/mousemove/mouseup/selectionchange/dblclick/keydown/focusin/click/change/pointercancel/visibility/blur listener and this bar. Repeated cleanup is safe and cannot remove a successor's cleanup function/bar; retain lifecycle tombstone and window.__freeTtsControlBarOwner. Repeated live show is idempotent. Store isolated `{barCreated, highestRenderedViewRevision, pendingRender, cleanup}`; reject retired/wrong owner/lower revision, equal is idempotent, pending update keeps only highest revision and defers. Creation materializes newer supplied/pending snapshot, clears pending, records revision/created before attaching handlers. Missing live bar/current update fails through owned render error path.
- [ ] Apply this bound action helper; it preserves control behavior and ignores transport rejection after the liveness check:

```js
function sendBarAction(action, payload = {}) {
  if (!gesture.alive) return;
  chrome.runtime.sendMessage({source: "bar", leaseId: gesture.leaseId, sessionId: gesture.sessionId, lifecycleRevision: gesture.lifecycleRevision, action, ...payload}).catch(() => {});
}
```

- [ ] Apply this approved synchronous renderer after lease/session/lifecycle admission (local names may adapt); keep title/aria/icon assignments together and modes worker-owned:

```js
function renderControlBarSnapshot(bar, snapshot, modes) {
  if (!bar || snapshot.viewRevision < isolated.highestRenderedViewRevision) return false;
  if (snapshot.viewRevision === isolated.highestRenderedViewRevision && snapshot.sessionId !== isolated.owner.sessionId) return false;
  const paused = snapshot.state === "paused", toggle = bar.querySelector("#free-tts-toggle");
  if (!toggle) return false;
  toggle.textContent = paused ? "▶" : "⏸";
  toggle.title = paused ? "Resume" : "Pause";
  toggle.setAttribute("aria-label", paused ? "Resume" : "Pause");
  for (const [id, name] of [["prev", "Previous"], ["next", "Next"], ["close", "Stop"], ["loop", "Repeat All"], ["repeat-current", "Repeat Current Sentence"]]) bar.querySelector(`#free-tts-${id}`).setAttribute("aria-label", name);
  bar.querySelector("#free-tts-loop").checked = modes.loopEnabled;
  bar.querySelector("#free-tts-repeat-current").checked = modes.repeatCurrentEnabled;
  isolated.highestRenderedViewRevision = snapshot.viewRevision; return true;
}
```

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_dom_smoke.cjs
git commit -m "fix(extension): own bar gestures and render lifecycle"
```

## Task 9: Genuine selection, double-click, stale tickets, and render ordering

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `selection-tickets-and-browser-ordering`
**Ownership:** `extension/background.js`, `tests/extension_dom_smoke.cjs`
**Depends on:** `[8]`
**Resources:** `[]`

**Files:**
- Modify: `extension/background.js:320-540` for page selection baseline/snapshot/ticket, dblclick suppression and render-order completion.
- Modify: `tests/extension_dom_smoke.cjs` for G05-G08 including actual CDP first/second click sequence.

**Interfaces:**
- Consumes: `PageOwner = {tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number}`, Task 8's closure gesture fields, `renderControlBarSnapshot(bar, snapshot, modes)`, and real Chromium CDP/timer fixture.
- Produces: `SelectionSnapshot = {anchorNode: Node | null, anchorOffset: number, focusNode: Node | null, focusOffset: number, text: string}` with exact live instance/owner/gesture ticket admission, and complete G01-G08 browser evidence.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "tests/extension_dom_smoke.cjs", "--chromium", "/usr/bin/chromium"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Add genuine gestures, stale tickets, and delayed render cases (R1, R2, R4, R5, R6)

| ID and test name | Events/timers | Required executable assertions |
| --- | --- | --- |
| G05 `genuine_page_selection` | Page mousedown with collapsed selection, change to second-sentence range, page mouseup, assert no jump at 99 ms and advance exactly 100 ms. Then retain unchanged selection and mouseup, and reselect same text after an intermediate selection change. | Exactly one bound `jumpToSentence` idx 1 for the genuine selection; no retained-selection jump; re-selection after an intermediate change works. |
| G06 `genuine_double_click_sequence` | First collapsed mousedown/up/click; second mousedown selecting `Second`, mouseup/click detail 2, dblclick detail 2, advance 150 ms; also execute the CDP-generated sequence. | Exactly one idx 1 jump; queued second mouseup is canceled; mutation-removing double-click cancellation produces a failing duplicate-jump assertion. |
| G07 `selection_ticket_freshness` | Schedule first selection callback, begin a different page gesture/change selection or replace sentence owner before 100 ms, then release old callback; run a genuine second selection. | Old ticket cannot read newer selection/session or jump; second ticket sends exactly its own jump. |
| G08 `render_attributes_order` | Queue newer paused update before initial creation; create using older running snapshot; apply older running after paused; replace session and run old update/hide/show last. | Deferred creation materializes paused; exact `▶`/`Resume`/`Resume` attributes agree; old updates/cleanup are ignored; compact controls and explicit accessible names remain. |

### Step 2: Snapshot genuine selection and cancel the second mouseup before it jumps

- [ ] Run each case above to its behavioral failure before repair. Page mousedown invalidates old tickets, clears selectionChanged and captures baseline endpoints/text. Live selectionchange marks change only when baseline differs. Mouseup requires live page origin/outside bar; unmatched mouseup schedules nothing; reset active origin while retaining completed origin. Schedule the existing 100 ms callback only for noncollapsed/nonempty changed selection or a gesture that changed through an intermediate selection. Snapshot at mouseup; callback checks alive, exact current instance/owner/session, same revision and matching endpoints/text, then uses captured trimmed text and first sentence.includes match in existing order. Retained unchanged selection alone is never evidence.
- [ ] Dblclick rejects inside-bar targets and active/last-completed bar origin, invalidates pending mouseup callback, and sends immediate genuine selection jump. The first collapsed click creates no callback; second mouseup's callback is suppressed by dblclick. An earlier completed genuine drag-selection may already have jumped; a later double-click is a new fresh attempt under the existing navigation fence, with no duplicate surviving Audio/advance.
- [ ] For G06 obtain Second word coordinates with Range.getBoundingClientRect(), then CDP Input.dispatchMouseEvent first mousePressed/mouseReleased `clickCount: 1`, second mousePressed/mouseReleased `clickCount: 2`; assert the Chromium-generated click/dblclick results, exactly one idx 1 jump after 150 ms. Also retain the explicit synthetic down/up/click, down/up/click(detail 2)/dblclick(detail 2) sequence. G05 asserts none at 99 ms and exactly one at 100 ms; G07 second ticket works on its own timer; G08 checks all three attributes after each deferred/old/new-owner injection.
- [ ] Rerun G01-G04 and W01-W31; assert exact successor owner for G04/G07 and query complete messages, selection/bar DOM and timer queue for every G case. A real DOM fixture does not establish installed-extension permissions, physical media keys, audible output or worker eviction; record available manual installed-extension smoke for retained-selection pause, loading pause, resume position, keyboard activation and hardware play/pause/next/stop, with actual limits. Controlled VM/MAIN/browser evidence remains mandatory, and DOM smoke is not skipped on this Chromium host.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add extension/background.js tests/extension_dom_smoke.cjs
git commit -m "fix(extension): fence selection tickets and double clicks"
```

## Task 10: Pytest wrapper, mutation sensitivity, and final validation

**Implementer tier:** Capable
**Lane:** `extension-sequential`
**Category:** `integration-and-sensitivity-evidence`
**Ownership:** `tests/test_extension_playback.py`
**Depends on:** `[9]`
**Resources:** `[]`

**Files:**
- Create: `tests/test_extension_playback.py` for bounded ordinary pytest integration of both Node entrypoints.

**Interfaces:**
- Consumes: exact executable argv `node --test tests/extension_playback.test.cjs` and `node tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium`, W01-W31/G01-G08 labels, and authored production fences (read-only in the delivery worktree).
- Produces: standard pytest subprocess tests with 120-second timeouts and stdout/stderr propagation; external assigned child-report evidence for disposable-copy W02/W15/W16/G02/G06 probes and final R1-R6 coverage. Sensitivity probes never mutate delivery production/tests or add a permanent mutator/dependency.

**Validation:**
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "tests/extension_dom_smoke.cjs", "--chromium", "/usr/bin/chromium"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/test_media_session.py", "tests/test_extension_split_sentences.py", "tests/test_extension_playback.py"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`

### Step 1: Integrate both real Node suites with ordinary pytest

- [ ] Add exactly this wrapper with no dependency and the repository ROOT as cwd. A missing browser/failed entrypoint remains a visible failure; run both new tests to their actual result:

```python
from pathlib import Path
import subprocess
ROOT = Path(__file__).resolve().parents[1]

def run_node(argv):
    completed = subprocess.run(argv, cwd=ROOT, capture_output=True, text=True, timeout=120, check=False)
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {argv}\n"
        f"stdout:\n{completed.stdout}\nstderr:\n{completed.stderr}"
    )
    return completed.stdout

def test_extension_playback_node_suite():
    output = run_node(["node", "--test", "tests/extension_playback.test.cjs"])
    assert "W01" in output and "W31" in output

def test_extension_dom_smoke():
    output = run_node(["node", "tests/extension_dom_smoke.cjs", "--chromium", "/usr/bin/chromium"])
    assert "G01" in output and "G08" in output
```

### Step 2: Prove mechanism sensitivity and finish the acceptance sweep (R1, R2, R3, R4, R5, R6)

- [ ] Use only disposable copies outside the delivery worktree: `tempfile.TemporaryDirectory`, `shutil.copytree(ROOT, copy)`, asserted exact Path.read_text/replace operations against actual authored production code, and subprocess.run with timeout=120. First require each selected unmutated W02, W15, W16, G02 and G06 to pass at its named observable; inspect/record exact production file/function/line locations and replacement counts, then independently disable each complete mechanism below. Enumerate every redundant predicate/branch/call enforcing that mechanism and assert each replacement count separately. Never alter tests/specs/reproductions or unrelated owner fences to manufacture failure; baseline interface/syntax failure is not evidence. Remove all copies in finally and add no permanent subsystem.
- [ ] W02: disable all activation-staleness checks independently preventing the pre-pause activation's construction/play (intent/media revisions, desired pause, start serial, attempt eligibility/floor or equivalent centralized enforcement when they fence this operation), preserving unrelated lease/session/lifecycle isolation. Require nonzero W02 at its explicit post-pause zero-constructor/zero-play assertion, not a final paused flag or earlier failure.
- [ ] W15 and W16: disable every displaced-attempt continuation/poll ownership guard, including strict object identity and equivalent attempt-ID/floor/start-serial/centralized enforcement for that decision, preserving unrelated current session/lease/lifecycle binding. Require each named case to fail at explicit successor identity/index/end-marker/advance assertions; generic state or interface failure is insufficient.
- [ ] G02: disable all bar-interaction cancellation/invalidation of the pending selection callback, including redundant timer/ticket invalidation/admission, while preserving callback liveness/session/owner and intended control action. Require failure from the extra delayed jump after bar interaction. G06: independently disable all double-click-specific suppression of queued second mouseup, including redundant timer/ticket/equivalent checks, preserving ordinary page selection/owner/liveness; require the explicit duplicate-jump count to fail. Each mutated run must be nonzero and include its case name and target-observable assertion text.
- [ ] Run every Validation vector: JS syntax, all W01-W31, all G01-G08 in real Chromium, targeted/full pytest. Record real counts, browser availability/flags, optional ffmpeg skip reasons, sensitivity locations/observables, and available manual-smoke limits in the assigned child report. Skips are not passes; this host's DOM smoke cannot silently skip. No real Edge TTS endpoint or audible output is required.
- [ ] Trace full acceptance: R1 = W12-W18/W23/W28 plus G04/G07/G08; R2 = W02-W05/W19-W22/W27-W31 plus G08; R3 = W01/W06-W11/W15/W16/W25/W30; R4 = G01-G07 plus W15/W23; R5 = W03/W04/W19/W24/W27 plus G02/G08; R6 = every W01-W31/G01-G08, retained splitter/media checks, syntax, targeted/full pytest, CDP and mutation evidence. Read every normative spec section, not IDs alone; confirm one loader/live attempt/observer/read plus follow-up, no stale audible start, and complete current-owner error/cleanup behavior.
- [ ] Inspect final allowlist/world boundaries: no manifest/permissions, npm/generic DOM library, server/dependency/web-preview, popup markup/CSS/layout, hardware requirement, PM state/manifest/schedule or external evidence edits. All Audio/media effects are MAIN with no chrome in serialized MAIN functions. Preserve final render/source/message signatures and compact UI. Parent PM workflow still requires independent frontier implementation audit; this task supplies evidence without delivery/merge/push or PM ledger changes.

### Step 3: Validate and commit

- [ ] Run every Validation vector, require all existing and newly added cases to pass with no unintended timers/rejections, inspect the diff against this task's allowlist, and commit only these files:

```bash
git add tests/test_extension_playback.py
git commit -m "test(extension): integrate playback and prove regression sensitivity"
```
