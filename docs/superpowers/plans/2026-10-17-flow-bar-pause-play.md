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
- `extension/background.js:320-540` owns the isolated-world compact bar, per-instance gesture lifecycle, selection tickets, drag cancellation, render fencing, and cleanup. Task 2 completes this region after Task 1 establishes its owner/snapshot contract.
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
- Both tasks are in the single sequential `extension-sequential` lane because worker/audio and bar gestures share `extension/background.js`. Task 2 consumes Task 1's canonical owner, message, snapshot, and render-call contracts and must not rename or weaken them.
- Do not add server changes, npm/Playwright/Puppeteer/jsdom infrastructure, dependencies, popup markup/CSS redesign, playback persistence, a generic playback engine, a persistent port, a global lock, a second generic DOM implementation, or physical hardware requirements. No real TTS endpoint or audible output is required.
- Required new entrypoints are exactly `tests/extension_playback.test.cjs`, `tests/extension_dom_smoke.cjs`, and `tests/test_extension_playback.py`. The Node tests use built-ins only and the Python wrapper must work with the repository's ordinary `python -m pytest` invocation.
- Required validation includes `node --check extension/background.js`, `node --check extension/popup.js`, `node --test tests/extension_playback.test.cjs`, `node tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium`, targeted pytest, and full pytest. Use `/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python` for documented local checks when available, while keeping tests standard-pytest compatible.
- Leave all plan checkboxes unticked. Each task ends with one scoped commit. Do not modify PM state, manifests, schedules, deliverable PM artifacts, or Git outside the child task commits. The only reports permitted are controller-assigned reports under the ignored per-plan SDD workspace and the external planning report assigned by the parent.

## Task 1: Worker and MAIN ordered playback with executable VM regressions

**Implementer tier:** Capable

**Lane:** `extension-sequential`

**Category:** `worker-main-ordering-and-canonical-snapshots`

**Ownership:** `extension/background.js`, `extension/popup.js`, `tests/extension_playback.test.cjs`, `tests/test_media_session.py`, `tests/test_extension_split_sentences.py`

**Depends on:** `[]`

**Resources:** `[]`

**Files:**

- Modify: `extension/background.js:4-219` for worker records, start tickets, bound runtime messages, canonical snapshots, intent admission, navigation, stop/error retirement, and command acknowledgments.
- Modify: `extension/background.js:221-318` for MAIN media/session injection, isolated relay, keepalive, page records, audio identity, media counters, and admission tokens.
- Modify: `extension/background.js:542-598` for owner-bound previous/next/jump navigation and fresh attempt selection.
- Modify: `extension/background.js:609-694` for owner-bound page setup/highlight/cleanup calls while preserving splitter and highlight algorithms.
- Modify: `extension/background.js:730-888` for start publication, one owned loader, guarded activation, fresh observation, completion polling, replay/advance policy, and terminal cleanup.
- Modify: `extension/popup.js:1-281` for canonical snapshot application, worker/view high-water handling, bounded transport refresh, and truthful command errors.
- Create: `tests/extension_playback.test.cjs` for the complete W01-W31 built-in Node behavioral suite.
- Modify: `tests/test_media_session.py:1-152` only where source-shape assertions are obsolete; preserve meaningful MAIN-world/static invariants and delegate dynamic semantics to the Node suite.
- Modify: `tests/test_extension_split_sentences.py:1-110` only to replace the extension's literal `startIdx` assertion with executable ownership cases; leave every splitter fixture and the `script.js` guard intact.

**Interfaces:**

- Consumes: existing `chrome.runtime.onMessage`, `chrome.commands.onCommand`, `chrome.scripting.executeScript`, `chrome.storage.sync.get`, `fetchSentenceAudio`, `splitSentences`, and the existing compact-bar call sites.
- Produces: `PageOwner = { tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number }` for every published page-bound operation.
- Produces: `PlaybackSnapshot = { workerId: string, state: "idle" | "playing" | "paused", sessionId: string | null, viewRevision: number, intentRevision: number | null, mediaRevision: number | null, phase: "idle" | "initializing" | "loading" | "ready" | "starting" | "playing" | "paused" | "ended", audioStatus: "none" | "starting" | "playing" | "paused" | "ended" | "error", error: { code: string, message: string } | null }`.
- Produces: `CommandResponse = PlaybackSnapshot & { ok: boolean, accepted: boolean, disposition: "applied" | "superseded" | "ignored" | "failed", pageApplied: boolean, commandError: { code: string, message: string } | null }`.
- Produces: `WorkerIntentCapability = { leaseId: string, sessionId: string, lifecycleRevision: number, attemptId: number | null, intentRevision: number, mediaBase: number, paused: boolean }` and `ActivationCapability = { leaseId: string, sessionId: string, lifecycleRevision: number, attemptId: number, intentRevision: number, mediaRevision: number, dataUrl: string }`.
- Produces: `PageReceipt = { leaseId: string, sessionId: string, lifecycleRevision: number, attemptId: number | null, intentRevision: number, mediaRevision: number, paused: boolean, audioId: string | null, audioStatus: string, startSerial: number | null, audioRevision: number, disposition: "applied" | "idempotent" | "stale" | "superseded-by-media" | "no-effect" }` from each synchronous MAIN reducer.
- Produces: `buildPlaybackSnapshot(pipeline: Pipeline | null): PlaybackSnapshot`, `acceptRuntimeCommand(msg: RuntimeMessage, sender: RuntimeSender): Promise<CommandResponse>`, `injectWorkerIntent(capability: WorkerIntentCapability): PageReceipt`, `injectActivation(capability: ActivationCapability): PageReceipt`, `injectFreshObservation(owner: PageOwner, attemptId: number): PageObservation`, `injectMediaSession(owner: PageOwner): void`, and `injectMediaRelay(): void`; these are local worker/page functions, not package exports.
- Produces: `showControlBar(owner: PageOwner, snapshot: PlaybackSnapshot, modes: { loopEnabled: boolean, repeatCurrentEnabled: boolean }): Promise<"rendered" | "deferred">` and `updateControlBar(owner: PageOwner, snapshot: PlaybackSnapshot, modes: { loopEnabled: boolean, repeatCurrentEnabled: boolean }): Promise<"rendered" | "deferred">` as the stable renderer seam Task 2 completes.
- Produces: `applyPopupSnapshot(snapshot: PlaybackSnapshot): boolean` and `handlePopupResponse(response: CommandResponse | null, command: string): Promise<void>` in `extension/popup.js`; popup code must use returned snapshots rather than requested-action state.

**Validation:**

- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/test_media_session.py", "tests/test_extension_split_sentences.py"]`

### Step 1: Build a real-source, three-phase VM harness before changing playback logic

- [ ] Add `tests/extension_playback.test.cjs` with only Node built-ins and a fresh worker VM per test. Read the complete `extension/background.js` from the repository, install a mocked `chrome` boundary before evaluating it, and append a VM-only accessor after the real source so tests can inspect current worker records without adding production exports.
- [ ] Make every `chrome.scripting.executeScript` request enter a `ScriptGate` in three separate phases: `dispatch` records `target`, `world`, `func.toString()`, and arguments without executing; `execute` serializes and evaluates that actual function in the selected tab's MAIN or isolated VM; `complete` resolves or rejects the worker-facing promise independently. The harness must permit a later request to execute/complete before an earlier request and must record every constructor, play, pause, src clear, currentTime write, timer, message, bar attribute, poll, and cleanup effect.
- [ ] Implement the following complete test-only scheduling primitives and use them instead of sleeps:

```js
class Deferred {
  constructor() {
    this.settled = false;
    this.promise = new Promise((resolve, reject) => {
      this._resolve = resolve;
      this._reject = reject;
    });
  }

  resolve(value) {
    if (this.settled) return;
    this.settled = true;
    this._resolve(value);
  }

  reject(error) {
    if (this.settled) return;
    this.settled = true;
    this._reject(error);
  }
}

class VirtualClock {
  constructor() {
    this.now = 0;
    this.nextId = 1;
    this.timers = new Map();
  }

  setTimeout(callback, delay = 0) {
    const id = this.nextId++;
    this.timers.set(id, { id, due: this.now + Math.max(0, delay), callback, repeat: 0 });
    return id;
  }

  setInterval(callback, delay = 0) {
    const id = this.nextId++;
    const period = Math.max(1, delay);
    this.timers.set(id, { id, due: this.now + period, callback, repeat: period });
    return id;
  }

  clearTimeout(id) {
    this.timers.delete(id);
  }

  clearInterval(id) {
    this.timers.delete(id);
  }

  async advance(milliseconds) {
    const target = this.now + milliseconds;
    while (true) {
      const due = [...this.timers.values()]
        .filter((timer) => timer.due <= target)
        .sort((left, right) => left.due - right.due || left.id - right.id)[0];
      if (!due) break;
      this.now = due.due;
      if (due.repeat) due.due += due.repeat;
      else this.timers.delete(due.id);
      await due.callback();
      await Promise.resolve();
    }
    this.now = target;
    await Promise.resolve();
  }
}

class ScriptGate {
  constructor() {
    this.nextId = 1;
    this.entries = [];
  }

  dispatch(details) {
    const entry = {
      id: this.nextId++,
      target: details.target,
      world: details.world || "ISOLATED",
      func: details.func,
      args: details.args || [],
      executed: false,
      result: undefined,
      error: null,
      completion: new Deferred(),
    };
    this.entries.push(entry);
    return entry.completion.promise;
  }

  pending(predicate) {
    const entry = this.entries.find((candidate) => !candidate.executed && predicate(candidate));
    assert.ok(entry, "expected a pending script request");
    return entry;
  }

  async complete(entry, result) {
    entry.completion.resolve(result);
    await Promise.resolve();
  }

  async fail(entry, error) {
    entry.completion.reject(error);
    await Promise.resolve();
  }
}
```

- [ ] Give the harness explicit methods `start(text, tabId = 7)`, `send(message, sender = { tab: { id: 7 } })`, `execute(entry)`, `complete(entry, result)`, `fail(entry, error)`, `drain()`, `page(tabId, world)`, and `assertNoUnintendedTimers()`. `start` and `send` must call the real listener/function paths; no helper may emulate a production transition by directly assigning `sentencePipeline`, `currentIdx`, `isPaused`, an Audio object, or a bar.
- [ ] Supply the default page fixture required by the specification: tab 7, sentences `['First sentence.', 'Second sentence.']`, normalized localhost settings, loop false/repeat false for boundary tests, data URLs `data:audio/mpeg;base64,AQ==` and `data:audio/mpeg;base64,Ag==`, default unfinished `currentTime = 3` and `ended = false`. Provide tab 8 for cross-tab cleanup tests and a controllable `Audio` constructor that is available only in MAIN contexts; constructing Audio in an isolated VM must fail the test.

### Step 2: Write the worker/audio regression cases with exact schedules

- [ ] Register one `node:test` case for every ID below. Parameterize only the explicitly named fulfillment/rejection/order variants; retain the ID in each test name so `--test-name-pattern` can select it. Each case must admit a real session and attempt through production start/selection/intent paths before it holds or releases a gate.

| ID and test name | Controlled schedule | Required executable assertions |
| --- | --- | --- |
| W01 `pause_loading_boundaries` | Hold settings storage, the first 200 ms cache tick, the 10,000 ms cache miss/fallback fetch, fetch body, and highlight execution/completion in separate runs. Accept pause before releasing each gate, then release and drain; resume afterward. | Worker `isPaused` changes before the held loader/highlight resolves; MAIN pause application is dispatched without releasing loading; zero Audio constructors and play calls while paused; the same loader/attempt reaches ready; one constructor/play after resume. |
| W02 `activation_executes_after_pause` | Load A, hold old running activation at dispatch, accept/execute/complete a newer pause, then execute/complete the old activation. | Old activation returns no-effect; constructor count and play count remain zero; page, worker, and rendered snapshot remain paused. This assertion records constructor/play calls, not only final paused state. |
| W03 `activation_executes_before_pause` | Execute old activation and hold its script completion and play settlement; execute/complete newer pause; settle the old play promise with both fulfill and reject variants. | Exactly one play before pause is allowed; the same Audio is paused at the pause boundary; no later play or playing Media Session assignment; old settlement cannot roll worker state back. |
| W04 `stale_resume_both_execution_orders` | With unfinished Audio, accept resume R then newer pause P. Run R-before-P but complete R last, and P-before-R in a separate schedule; delay old render. | R-before-P has at most the allowed pre-P play; P-before-R has no R play; both end with paused worker/page state and all three UI attributes from P. |
| W05 `rapid_toggle_parity` | From running, accept two and three bound toggles without render completion; execute newer intent/render requests first, older last, and reverse completion order. | Two toggles finish running and three finish paused; accepted intent revisions have parity; glyph/title/aria-label and page desired state agree; messages are `togglePlayback` regardless of stale glyph. |
| W06 `unfinished_resume_identity` | Pause default Audio at time 3; resume and issue a redundant explicit resume after acknowledgment. | Same Audio object, `audioId`, and `currentTime`; no seek/fetch/constructor; exactly one resume play and one completion observer; redundant resume does not call play again. |
| W07 `resume_during_pending_load` | Hold fallback fetch; accept pause, resume, pause, resume before releasing it; repeat with an ending paused before the next resume. | One fetch/load; one Audio construction and initial play using the latest running revision; one observer/advance; zero starts while the ending state is paused until a later resume. |
| W08 `ready_paused_resume` | Complete load and highlight under pause, leave the attempt ready without Audio, then resume twice. | Retained data URL is used; one Audio and one play; no obsolete preceding Audio is played; second resume is idempotent. |
| W09 `end_observed_paused_replay` | Pause A, mark its actual Audio ended, execute/complete an owned poll while paused, then resume. | Completion marker belongs to A; resume retires A, creates a distinct attempt at index 0, and performs one replay; duplicate old end samples have no effect. |
| W10 `end_observed_after_resume` | Mark A ended while paused, accept resume before delivering its owned ended poll, then complete the poll; run modes-off, repeat-current, and loop-at-last variants. | Resume never plays ended A; the later poll chooses idx 1 with modes off, idx 0 with repeat-current, and idx 0 with loop; no forced paused-end replay occurs. |
| W11 `pause_at_completion_decision` | Execute an ended poll but hold completion; accept pause and mode change before completion, then run completion-first-then-pause as a second schedule. | Pause-before-decision keeps current index with a paused-end marker and no advance; decision-before-pause may select the next/replay attempt, which becomes paused; no DOM mode read decides the worker path. |
| W12 `stop_initializer` | Hold start at tabs.query, keyboard selection extraction, storage, lease/setup, and highlight initialization in separate variants; stop; apply captured retirement fences before old gates resolve. | No pipeline resurrection, Audio/start/bar/media setup after the relevant fence; old continuations dispatch no new setup; no unhandled rejection; canonical state is idle. |
| W13 `replace_initializer_latest_wins` | Variant A holds S1 target lookup before publication, then publishes S2 before releasing S1. Variant B publishes S1 initializing, holds settings, publishes S2, then releases S1; run same tab/index and tabs 7/8. | Variant A never publishes S1. Variant B may retain already completed S1 effects but cannot republish or continue after replacement. Only S2 remains live and may start/render; old work cannot alter S2 cache/settings/owner. |
| W14 `replace_pending_attempt_boundaries` | Hold old cache/fetch/body/highlight/activation dispatch, replace S1 with S2 at idx 0 in tab 7, apply S1 retirement and S2 admission fences, release/execute S1; repeat with S2 on tab 8. | Old work cannot construct/play/modify S2, set its paused-end marker, or write its cache; already-dispatched old page effects are inert after the captured fence. |
| W15 `same_index_and_return_navigation` | Hold A at idx 0; jump to idx 0 for A2, or jump to 1 then 0 for A3; release old load/start/poll. | Attempt IDs are distinct; only the latest attempt owns Audio/observer/end advancement; index equality never revives A. |
| W16 `poll_identity_and_no_overlap` | Hold one 500 ms poll, advance virtual time by 2,000 ms, navigate/replay the same index, execute old poll and complete it last. | At most one old poll is in flight; old poll cannot inspect successor Audio, mutate successor end flags, or advance successor; missing/stale Audio is not completion. |
| W17 `late_failure` | After S2 starts in the same tab/index, reject old settings/fetch/script/play paths independently; include play rejection after pause. | S2 Audio/state/bar/metadata survive; old failure cannot clear or stop S2; invalidated play rejection is nonfatal; future preload rejection keeps the session alive. |
| W18 `terminal_cleanup_overlap` | Exercise stop, natural nonloop completion, and fatal error. Hold each media/audio/highlight/bar cleanup at dispatch and completion, publish S2 same tab/index, then run old cleanup; inject one failure in each cleanup step. | S2 survives; old finalization cannot clear it. Uncontended order is media handlers/metadata/keepalive, then pause/clear Audio, then highlights, bar, final worker clear. Every cleanup step is attempted after one fails. |
| W19 `media_direct_without_worker` | Execute actual MAIN play/pause handlers with relay delivery withheld, once with unfinished Audio and once loading/no Audio. | Page pause/play is immediate and same-identity/currentTime preserving; loading/ended play creates no Audio; media revisions/sequence advance before worker notification; no MAIN `chrome` access. |
| W20 `media_pause_fences_queued_start` | Dispatch old activation/resume, execute direct MAIN media pause, withhold notification, then execute old operation. | MAIN media revision rejects stale activation; no constructor/play after media pause even before worker notification. |
| W21 `media_notification_reordering` | Apply media pause M1 then resume M2; deliver M2 then M1 and duplicate M2; repeat with final pause ordering. | Worker/bar converge to highest admitted media revision; delayed lower notification and duplicate do not replay Audio or roll state back. |
| W22 `mixed_media_worker_intents` | (a) Adopt media pause, apply popup resume, deliver old pause. (b) Dispatch worker resume on old media base, execute media pause first, then apply worker resume and delay receipt. (c) Accept later bar toggle while receipt is delayed and deliver all out of order. | No rollback after popup intent; conflict receipt reports superseded actual pause and no play; later local intent wins by revision; no blind retry; a new post-reconciliation command can resume. |
| W23 `originating_session_messages` | Queue bound S1 bar/media messages, replace with S2 same tab/index, deliver S1 and tab-8 messages; issue unbound popup pause; separately deliver valid bound S1 stop while S2 is only a pending target lookup. | S2 survives stale messages and returns canonical S2 ignored snapshots; unbound popup intentionally targets published S2; bound S1 stop cannot cancel unrelated pending S2 start ticket. |
| W24 `current_errors` | Reject current settings/fetch/play/activation/poll/bar update; include no-receipt/missing-Audio and play pending at command response; reject obsolete activation/render after newer pause. | Current failures retire to idle/error and clean owned resources; responses distinguish failed page application from accepted/starting; obsolete errors leave newer pause intact; unsupported Media Session is nonfatal. |
| W25 `modes_and_splitting` | Run real extension splitter against retained fixtures; toggle loop/repeat via bound messages; run previous/next/jump and all end boundaries. | Sentence arrays are unchanged; mode defaults/exclusivity and boundary indices remain; one replay/advance occurs per end policy. |
| W26 `relay_and_world_invariants` | Install relay twice, set up media repeatedly, emit a full-token action, clean twice, and inspect every script target/world/timer. | One relay listener/forward; full tokens unchanged; Audio/media effects all MAIN; no MAIN `chrome`, port, or stale port name; one 25 s keepalive reset/clear; all five handlers install/clear. |
| W27 `snapshot_and_popup_responses` | Evaluate real `popup.js` with mocked DOM/storage/server and worker messages. Hold pause, accept resume/stop/replacement, deliver newer then older snapshots, ignored/superseded/failed/transport errors, then a fresh worker ID with lower view revision and an old-worker delayed response. | Popup uses canonical snapshot, worker ID, and view high-water; old worker cannot overwrite fresh state; one bounded `getPlaybackState` refresh on transport failure; no unconditional idle/playing guess. |
| W28 `lease_and_lifecycle_tombstones` | Queue old lease claim/install/show/highlight/retire, apply newer claim/session or stop fence, then execute old operations; include unchanged token with advanced lifecycle floor, CAS loss, and equal-lifecycle install after retirement. | Old work cannot reset/recreate media/UI/audio; fences survive idle; fresh worker can claim without restoring lost pipeline; claim retries are bounded. |
| W29 `early_audio_outcome` | Variant A completes play outcome before activation receipt. Variant B starts from paused unfinished R4/M2/S3, direct media play creates M3/S4, holds N/R4/O13, settles outcome O14, delivers F before N/read. Variant C accepts popup R5, holds conflict receipt, settles/delivers outcome, then completes receipt and old N. Variant D executes a pre-settlement read, delivers outcome first, then completes read and one coalesced resample; parameterize fulfill/reject and replacement/pause/navigation. | Early outcome can establish Audio/serial; old receipt cannot downgrade. Current rejection reaches idle/error; fulfillment reaches playing on same Audio/time with one play/no constructor/retry. Old N need not be admitted and cannot advance media sequence; post-outcome sample is guaranteed; max one in-flight read plus one resample. |
| W30 `navigation_resume_attempt_binding` | Hold new idx-1 selection, accept pause/resume and execute resume intent before selection, release new load, run old idx-0 start last. | Resume cannot play old idx-0 Audio; selected latest attempt activates once after its own selection/intent eligibility. |
| W31 `delayed_media_navigation` | Queue media next/previous/stop at base R, accept/apply bound pause at R+1, then deliver old commands. Separately issue two ordered media-next actions before first injection completes with at least three sentences, overtake first receipt with second notification, and interleave direct pause. | Delayed media cannot unpause/navigate/stop newer local intent; ordered next burst advances twice with distinct attempts and one surviving path; duplicate sequence has no effect; receipts do not consume actions or borrow newer pause revision. |

- [ ] Add an explicit W02 assertion of `audioSpy.constructors.length === 0` and `audioSpy.plays.length === 0` after old activation executes post-pause. Add W15/W16 assertions comparing successor `attemptId` and Audio identity, not merely non-null values. Add W29 assertions for `lastMediaSequence`, `audioRevision`, one observation request, and one `observationNeeded` follow-up.

### Step 3: Implement worker ownership, synchronous acceptance, and canonical responses

- [ ] Replace the two-field global state with a worker `workerId = crypto.randomUUID()`, monotonic `lifecycleRevision`, `startRequestSerial`, `pendingStart`, `activePlayback`, and enriched `sentencePipeline`. Allocate a unique `sessionId`, `leaseId`, `attemptId`, `intentRevision`, `localIntentFloor`, `mediaRevision`, `lastMediaSequence`, `viewRevision`, and `lastError` exactly at the ownership boundaries in the specification. Keep only live bounded records; do not create event histories or retired-session maps.
- [ ] Implement strict pipeline and attempt ownership checks (for example, `ownsPipeline(...)` and `ownsAttempt(...)`) using captured object identity and owner fields. The attempt rule must ensure the current pipeline/session and active owner match, including lifecycle/tab/lease, the captured attempt is exactly the live pipeline attempt, and neither pipeline nor attempt is cancelled/retired; equivalent centralized predicates and local field/helper spellings are allowed. The W15/W16 disposable mutation probe targets the complete attempt-ownership mechanism while retaining unrelated session/lease/lifecycle isolation.
- [ ] Implement `buildPlaybackSnapshot` and `makeCommandResponse` so idle has null session/intent/media revisions, `phase: "idle"`, `audioStatus: "none"`, and current worker ID/view revision; running/paused describe accepted desired state even when phase is initializing/loading/starting. Construct responses at send time, never from an earlier requested boolean.
- [ ] Rework the runtime listener into bound origin handling. Popup commands address the current published session at receipt; bar commands require `source: "bar"`, sender tab equality, lease/session/lifecycle equality; media commands require full validated page tokens; invalid jump indices return `ok: false`, `accepted: false`, `disposition: "failed"`, `pageApplied: false`, and `invalid-argument` without retiring the attempt. Add `togglePlayback` that reads `sentencePipeline.isPaused`, flips it synchronously, increments intent/view revision, raises `localIntentFloor`, and never reads a button glyph.
- [ ] Make pause/resume/toggle/mode/navigation acceptance synchronous after target resolution. A pause must change worker desired state and dispatch MAIN intent without waiting for settings, cache/fetch, highlight, prior play, poll, or render. A resume during loading reuses the current `loadPromise`; resume with unfinished Audio uses the same identity/currentTime; resume after `completionObservedWhilePaused` creates a fresh attempt at the same index. Mode changes are worker-owned and do not change local intent floor.
- [ ] Make start tickets precede `tabs.query`, keyboard selection extraction, and storage. Publish a valid session shell after target and nonempty sentences are known but before settings storage; initialize `isPaused: false`, default modes, `phase: "initializing"`, and a new attempt. Stop invalidates all popup/shortcut start tickets; session-bound bar/media stop retires only its addressed owner and preserves an unrelated unpublished newer ticket. Concurrent completions publish only the still-current ticket.

### Step 4: Implement page lease/lifecycle, intent, activation, media, and fresh observation

- [ ] Add self-contained MAIN/isolated lease functions that read `(leaseId, lifecycleFloor)`, compare-and-swap both values, retire the prior world synchronously before installing a new inert record, retry at most twice after the first claim, and serialize only lease establishment per tab. Complete MAIN retirement/claim before isolated retirement/claim. A callback with a different lease cannot claim implicitly or clean up.
- [ ] Install `window.__freeTtsPlayback` and the existing `window.__freeTtsAudio` alias with lease/worker/lifecycle floors, session/attempt ownership, `workerIntentRevision`, `mediaRevision`, `mediaSequence`, `desiredPaused`, `audioId`, `audioStatus`, `startSerial`, `audioRevision`, and retained `audioError`. Fresh installation starts with media counters zero and `desiredPaused: true`; installation is inert until explicit accepted intent.
- [ ] Implement `injectWorkerIntent` as a synchronous reducer. Reject wrong lease/session/lifecycle and lower intent; advance `workerIntentRevision` before effects; apply requested pause/running only when `mediaBase === state.mediaRevision`; return `superseded-by-media` with current desired/audio snapshot on conflict; invalidate play serial and pause Audio synchronously on pause; never resume a preceding attempt. Equal revision is idempotent and cannot repeat play.
- [ ] Implement `injectActivation` with the exact no-stale-start guard. It must verify owner, current attempt, exact intent/media revisions, `desiredPaused === false`, non-retired/non-completed eligibility, and data URL before constructing Audio. Publish the Audio reference and alias before invoking `play()`. No promise settlement, `.then`, `.catch`, `.finally`, timer, or worker receipt may call `play()` again. The implementation must retain loaded bytes when an old activation is rejected so a current running revision can schedule one new activation.
- [ ] Implement MAIN direct media handlers for play, pause, previous, next, and stop. Every action increments `mediaSequence`; only play/pause increments `mediaRevision` and commits desired state before notification. Pause invalidates play serial and pauses Audio; play resumes only owned unfinished Audio and otherwise notifies the worker; no-audio/loading/ended play must not construct Audio. Include all binding/observation tokens in `window.postMessage`; keep `event.source === window`, `__freeTtsMedia`, no `chrome.*` in MAIN, and an idempotent isolated relay forwarding the complete object in one runtime message.
- [ ] Implement worker media admission requiring sender tab, lease/session/lifecycle, `localIntentFloor <= notification.intentRevision <= pipeline.intentRevision`, and strictly increasing `mediaSequence`; advance `lastMediaSequence` only on admitted notifications, never on a script receipt or fresh read. Media navigation captures its originating `mediaRevision` as `mediaBase`, does not borrow a newer receipt revision, and cannot undo a newer local pause. Keep ordered next/previous commands as separate messages and ignore stale/reordered sequences without a resequencing queue.
- [ ] Implement `requestFreshObservation` with one `attempt.observationRequest` and one `attempt.observationNeeded` bit. It performs a synchronous no-effect MAIN read of the current owner and complete audio/error observation, adopts only current accepted revisions/identity/aRevision/serial, defers activation/replay while reconciliation is required, coalesces one follow-up read after a triggering read/outcome, and clears continuation authority on retirement. A read never calls `play`, constructs Audio, increments media sequence, or consumes a navigation message.
- [ ] Implement worker audio outcome admission for early fulfillment/rejection, exact lease/session/attempt/audio/start identity, current accepted revisions, nondecreasing serial, and increasing audio revision. Current rejection reaches idle/error through captured cleanup; an obsolete rejection is logged without touching a successor. A provisional `starting` sample cannot overwrite a later playing/paused/error observation.

### Step 5: Implement attempt loading, completion, navigation, and cleanup without stale global rereads

- [ ] Refactor `startSentenceTTS`, `playNextSentence`, `prevSentence`, `nextSentence`, and `jumpToSentence` to capture pipeline/attempt/owner locals. Settings, cache wait, fallback fetch, highlight, activation, and preload callbacks all check `ownsAttempt` after awaits and write only the captured live session cache. Resume never launches a second loader for one attempt.
- [ ] Give each attempt `phase`, `retired`, `loadPromise`, `dataUrl`, `activationRequest`, `audioId`, `startSerial`, `audioRevision`, `pollTimer`, `pollInFlight`, `observationRequest`, `observationNeeded`, and completion predicates. Manual navigation retires the old attempt before selecting a new one even when the index is unchanged; selecting an attempt never changes desired pause state. Previous at zero is a no-op; valid navigation requests running intent separately and can be superseded by a newer page pause.
- [ ] Replace the async interval with one non-overlapping 500 ms poll carrying owner and Audio identity. A poll returns owned/ended/audio status or missing/stale; missing is not completion. Accept an owned ended sample synchronously once, read current worker `isPaused`, and either mark paused completion without advancing or consume current mode and create exactly one successor. If the physical element ended before an end sample and resume arrives first, do not force paused replay; let the later poll choose normal repeat/advance/loop policy.
- [ ] Implement terminal retirement as synchronous worker cancellation followed by captured MAIN media teardown, Audio disposal, isolated highlight cleanup, bar cleanup, and owner-compared final clearing. Do not call unconditional global `clearPlayback()` from catches. Media handlers/metadata/keepalive clear before Audio, then highlights/bar, and every remaining cleanup is attempted after an individual failure. A successor published during cleanup survives all old operations.
- [ ] Keep current settings/load/play/poll/activation/bar failures distinct from obsolete failures. Use stable codes at least `invalid-argument`, `settings-failure`, `load-timeout`, `load-failure`, `page-operation-failure`, `play-rejection`, `missing-audio`, `poll-failure`, `render-failure`, and `cleanup-failure`; return command error only for the command's own failure while canonical `error` reflects current owner state.

### Step 6: Make popup responses canonical and repair only obsolete Python guards

- [ ] In `extension/popup.js`, add `acceptedWorkerId` and `renderedViewRevision`, implement the exact `applyPopupSnapshot(snapshot)` high-water rules, and use it for initialization, speak/pause/resume/stop responses, and explicit refresh. A new worker ID resets the view high-water; an older response from the former worker is ignored after the new worker is accepted.
- [ ] Change `speak`, `stop`, and initialization to consume `CommandResponse`/`PlaybackSnapshot`. An ignored or superseded response renders its actual state; a failed response renders its canonical snapshot and logs `commandError`; a transport failure performs one `getPlaybackState` request and otherwise retains the last confirmed state. Do not set idle on transport error or playing after any `{ok:true}` without a snapshot.
- [ ] Keep popup HTML/CSS and server/voice/speed behavior untouched. `getPlaybackState` must remain immediate and not await cache/fetch/play/poll/render.
- [ ] In `tests/test_media_session.py`, retain the static checks for MAIN injection, registered action names, no old port machinery, relay marker/idempotence, and 25-second keepalive. Replace brittle assertions that require the old `{ action: data.action }` shape, old `syncPausedState` body, DOM fallback ownership, or exact natural-cleanup source order with a helper that runs the real Node cases `W19`, `W20`, `W25`, `W26`, and `W29` and asserts zero exit status plus the named case in output.
- [ ] In `tests/test_extension_split_sentences.py`, keep all splitter parameterizations and the `script.js` `startIdx` guard. Remove only the extension `startIdx` source assertion and add an executable parametrized test that runs `node --test --test-name-pattern <case> tests/extension_playback.test.cjs` for `W15 same_index_and_return_navigation`, `W16 poll_identity_and_no_overlap`, and `W30 navigation_resume_attempt_binding`; assert each selected case passes.

### Step 7: Run the independent Task 1 checks and commit

- [ ] Run the four Validation argv vectors from the task metadata. The Node suite must report every W01-W31 label and no unintended timers/rejections; Python must report real passes and actual optional skips rather than converting skips into passes.
- [ ] Inspect the diff to confirm no `extension/manifest.json`, server, dependency, popup markup/CSS, PM state, external reproduction, or generic harness file was added. Confirm every new Node case executes production source paths and every worker/page assertion records the target identity/order.
- [ ] Commit only the Task 1 allowlist with:

```bash
git add extension/background.js extension/popup.js tests/extension_playback.test.cjs tests/test_media_session.py tests/test_extension_split_sentences.py
git commit -m "fix(extension): fence playback intent and page ownership"
```

## Task 2: Gesture lifecycle, versioned compact rendering, Chromium smoke, and pytest integration

**Implementer tier:** Capable

**Lane:** `extension-sequential`

**Category:** `gesture-render-and-browser-integration`

**Ownership:** `extension/background.js`, `tests/extension_dom_smoke.cjs`, `tests/test_extension_playback.py`

**Depends on:** `[1]`

**Resources:** `[]`

**Files:**

- Modify: `extension/background.js:320-540` for `showControlBar`, `updateControlBar`, `hideControlBar`, per-instance gesture state, event classification/cancellation, bound bar actions, and versioned render attributes. Preserve Task 1's `PageOwner`, `PlaybackSnapshot`, `showControlBar(owner, snapshot, modes)`, and `updateControlBar(owner, snapshot, modes)` signatures.
- Modify: `extension/background.js:57-115` only if the Task 1 runtime dispatch needs a narrow source/session binding correction for the bar payload; do not change the Task 1 worker acceptance or response schema.
- Create: `tests/extension_dom_smoke.cjs` for real Chromium G01-G08 and CDP pipe control.
- Create: `tests/test_extension_playback.py` for bounded pytest invocation of both Node suites and visible subprocess diagnostics.

**Interfaces:**

- Consumes: `PageOwner = { tabId: number, leaseId: string, sessionId: string, lifecycleRevision: number }`, `PlaybackSnapshot`, `CommandResponse`, and `showControlBar(owner, snapshot, modes)`/`updateControlBar(owner, snapshot, modes)` from Task 1.
- Consumes: bound bar runtime messages `{ source: "bar", leaseId: string, sessionId: string, lifecycleRevision: number, action: "togglePlayback" | "prevSentence" | "nextSentence" | "jumpToSentence" | "stopPlayback" | "setLoopState" | "setRepeatCurrentState", idx?: number, enabled?: boolean }`.
- Produces: a per-bar gesture record `{ instanceId: string, leaseId: string, sessionId: string, lifecycleRevision: number, alive: boolean, gestureRevision: number, origin: "bar" | "page" | null, lastCompletedOrigin: "bar" | "page" | null, selectionChanged: boolean, selectionTimer: number | null, selectionSnapshot: SelectionSnapshot | null, drag: DragState | null }` with all listener/timer handles owned by the instance.
- Produces: `renderControlBarSnapshot(bar: HTMLElement, snapshot: PlaybackSnapshot, modes: { loopEnabled: boolean, repeatCurrentEnabled: boolean }): void`, which synchronously applies the toggle's three attributes and the other controls' accessible names while rejecting retired/older owners/revisions.
- Produces: `runDomSmoke(argv: string[]): Promise<void>` through the executable `tests/extension_dom_smoke.cjs` entrypoint; it exits nonzero for any assertion, timeout, browser, cleanup, or missing-Chromium failure.
- Produces: standard pytest tests that invoke `node --test tests/extension_playback.test.cjs` and `node tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium` with 120-second subprocess timeouts and preserve stdout/stderr on failure.

**Validation:**

- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/background.js"]`
- `cwd: repository root; timeout: 30s; argv: ["node", "--check", "extension/popup.js"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "--test", "tests/extension_playback.test.cjs"]`
- `cwd: repository root; timeout: 120s; argv: ["node", "tests/extension_dom_smoke.cjs", "--chromium", "/usr/bin/chromium"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q", "tests/test_media_session.py", "tests/test_extension_split_sentences.py", "tests/test_extension_playback.py"]`
- `cwd: repository root; timeout: 120s; argv: ["/data/home/henry-arch/.local/state/pi/project-manager/runs/3feb48d1239aabaa64f716b74cb0652340d75c47efd854fad17b7270a3e22eb3/pm-run-20261017-032149-a3b5d4e1/verify-venv/bin/python", "-m", "pytest", "-p", "no:cacheprovider", "-q"]`


### Step 1: Preserve the Task 1 renderer seam and add per-instance gesture ownership

- [ ] Change `showControlBar` to create an owner-bound instance without deleting a successor. On creation set `window.__freeTtsControlBarOwner` to the live owner, keep a cleanup tombstone after teardown, and retain `{barCreated, highestRenderedViewRevision, pendingRender, cleanup}` in the isolated page record. Repeated show for the same owner is idempotent; a retired owner cannot recreate a bar; a delayed initial creation materializes the newer of its supplied snapshot and pending snapshot.
- [ ] Implement the required per-instance record exactly as a closure-owned object. Keep `alive`, `gestureRevision`, `origin`, `lastCompletedOrigin`, selection baseline/snapshot, timer, drag state, every document listener, and every timeout. Cleanup must first set `alive = false`, increment `gestureRevision`, clear timer, and end drag; only then remove listeners/bar. Cleanup is idempotent and cannot delete a successor.
- [ ] Add target normalization using `event.composedPath()` and containment, normalizing text nodes to parents. `isInsideBarTarget(event)` must classify nested button children, labels and label text, inputs, and bar background as bar interaction. Do not rely on a literal target tag name and do not stop propagation or prevent default merely to suppress selection.
- [ ] Add a capture-phase `mousedown` classifier before document selection work. A down inside the bar cancels the selection ticket, increments `gestureRevision`, records `origin: "bar"`, and keeps that origin through mouseup outside the bar. Only a noninteractive bar background starts `drag`; button/input/label descendants never start drag. Window blur, hidden visibilitychange, and document pointercancel cancel drag and pending selection, increment the ticket, and reset active origin without adding a second pointer lifecycle.
- [ ] Add bar capture listeners for `click`, `change`, `keydown`, and `focusin`. Each calls the cancellation helper before any command/checkbox behavior and checks `alive`; it must preserve default button/checkbox activation. Escape cancels pending selection first and then sends bound stop. Bar messages must use the stable helper below and must never inspect toggle text:

```js
function sendBarAction(action, payload = {}) {
  if (!gesture.alive) return;
  chrome.runtime.sendMessage({
    source: "bar",
    leaseId: gesture.leaseId,
    sessionId: gesture.sessionId,
    lifecycleRevision: gesture.lifecycleRevision,
    action,
    ...payload,
  }).catch(() => {});
}
```

- [ ] Bind the four buttons to `prevSentence`, `togglePlayback`, `nextSentence`, and `stopPlayback`; bind checkboxes to mode actions with the existing mutual-exclusion UI. The toggle always sends `togglePlayback`; previous/next/stop never send a selection jump. Keep compact widths, icons, placement, drag movement, and loop/repeat labels.

### Step 2: Implement fresh page selection, double-click, drag cancellation, and render fencing

- [ ] On a page-origin mousedown, increment the ticket, clear `selectionChanged`, and capture a baseline `{anchorNode, anchorOffset, focusNode, focusOffset, text}`. During the live gesture, a `selectionchange` listener sets `selectionChanged` only when endpoints/text differ from baseline. On mouseup, require live page origin and a target outside the bar; unmatched mouseup schedules nothing. End active origin but retain `lastCompletedOrigin` through click/dblclick.
- [ ] At mouseup snapshot the selected endpoints/text and schedule exactly the existing 100 ms defer only when the snapshot is noncollapsed/nonempty and differs from baseline or changed during the gesture. The callback must require `alive`, exact instance/owner, exact gesture revision/ticket, matching current selection snapshot, and matching session. It uses only captured trimmed text and first-match sentence order; it never reads an unrelated retained selection. A failed check has no message/effect.
- [ ] Make the document `dblclick` handler reject bar targets and active/last-completed bar origin, invalidate pending mouseup work, and send exactly one immediate bound `jumpToSentence` for the genuine page selection. For the real first-click/second-click sequence, cancel the queued second mouseup before it can send a duplicate. A genuine later double-click may supersede a previous completed selection through Task 1's fresh-attempt ownership.
- [ ] Keep drag movement on noninteractive bar background, release outside without a jump, and cancel on pointercancel/blur/hidden visibility. Store and remove `mousemove`, `mouseup`, `selectionchange`, `dblclick`, `keydown`, `focusin`, `click`, `change`, `mousedown`, `pointercancel`, visibility, and blur listeners explicitly.
- [ ] Implement one version-guarded renderer. For a running snapshot set toggle `textContent = "⏸"`, `title = "Pause"`, and `aria-label = "Pause"`; for paused set `"▶"`, `"Resume"`, and `"Resume"`. Set `aria-label` for previous/next/stop and mode controls from their existing action titles. Assign all three toggle attributes in one synchronous block before returning; no await or message may split them.
- [ ] Reject wrong lease/session/lifecycle and revisions below the isolated high-water. Equal revisions are idempotent. While `barCreated` is false, retain only the newest `pendingRender` and report `deferred`; creation must use the newer pending snapshot even if its supplied snapshot is older. Once live, a missing bar/current update is a current render failure and must follow Task 1's owner error path; a stale update/hide/show cannot delete or recreate a successor.
- [ ] Use the following render logic as the required algorithm, adapting local field names only without changing its ordering:

```js
function renderControlBarSnapshot(bar, snapshot, modes) {
  if (!bar || snapshot.viewRevision < isolated.highestRenderedViewRevision) return false;
  if (snapshot.viewRevision === isolated.highestRenderedViewRevision &&
      snapshot.sessionId !== isolated.owner.sessionId) return false;
  const paused = snapshot.state === "paused";
  const toggle = bar.querySelector("#free-tts-toggle");
  if (!toggle) return false;
  toggle.textContent = paused ? "▶" : "⏸";
  toggle.title = paused ? "Resume" : "Pause";
  toggle.setAttribute("aria-label", paused ? "Resume" : "Pause");
  bar.querySelector("#free-tts-prev").setAttribute("aria-label", "Previous");
  bar.querySelector("#free-tts-next").setAttribute("aria-label", "Next");
  bar.querySelector("#free-tts-close").setAttribute("aria-label", "Stop");
  bar.querySelector("#free-tts-loop").setAttribute("aria-label", "Repeat All");
  bar.querySelector("#free-tts-repeat-current").setAttribute("aria-label", "Repeat Current Sentence");
  bar.querySelector("#free-tts-loop").checked = modes.loopEnabled;
  bar.querySelector("#free-tts-repeat-current").checked = modes.repeatCurrentEnabled;
  isolated.highestRenderedViewRevision = snapshot.viewRevision;
  return true;
}
```

### Step 3: Exercise G01-G08 in real Chromium using production injected functions

- [ ] Create `tests/extension_dom_smoke.cjs` with `node:assert/strict`, `node:fs`, `node:os`, `node:path`, `node:vm`, and `node:child_process`. Parse `--chromium`, defaulting to `/usr/bin/chromium`; fail rather than skip if it is absent or not executable.
- [ ] Capture the actual injected callback functions from the Task 1 worker VM by invoking the real `showControlBar`, `updateControlBar`, `initPageHighlighting`, and `hideControlBar` with a `chrome.scripting.executeScript` recorder. The temporary HTML must embed those captured `func.toString()` bodies and a mock `chrome.runtime.sendMessage` that records complete messages. Do not hand-copy a model of the gesture logic and do not depend on `selection-repro.html` or any PM report artifact.
- [ ] Use a real fixture DOM containing a paragraph with exact text `First sentence. Second sentence.`, real text nodes, `document.createRange`, `window.getSelection`, and the production-created bar. Set `window.__freeTtsSentences` through the captured production initialization callback. Install a fixture-only timer queue with `window.__advance(ms)` so the 100 ms selection callback and event ordering are deterministic without sleeps; keep host-side CDP request deadlines bounded.
- [ ] Launch Chromium with `--headless=new`, `--disable-gpu`, `--remote-debugging-pipe`, a temporary user-data directory, and a temporary HTML file. Use `child_process.spawn` with fd 3 for CDP writes and fd 4 for null-delimited CDP reads; maintain request IDs and session IDs, attach to the fixture target, use `Runtime.evaluate` for assertions/events, and close/kill the browser in `finally`.
- [ ] Implement the CDP pipe primitive with this concrete framing: write `JSON.stringify({id, method, params}) + "\0"` to `child.stdio[3]`, buffer `child.stdio[4]` until `\0`, parse each JSON message, and resolve the matching request. Apply a per-command deadline and a process-level deadline; a browser EOF, stderr failure, timeout, assertion, or cleanup error exits nonzero and includes captured stdout/stderr.
- [ ] For G06, obtain the `Range.getBoundingClientRect()` for the word `Second`, then send `Input.dispatchMouseEvent` through CDP for the first click (`clickCount: 1`) and second click (`clickCount: 2`) at that coordinate. Use CDP keyboard input with Enter or Space for at least one control/checkbox case; for Enter send `keyDown` with carriage-return text because rawKeyDown alone does not activate the browser default. Use programmatic dispatch/`.click()` for the retained-selection and synthetic-key invalidation cases as additional coverage.

### Step 4: Register exact gesture/render cases

- [ ] Add one executable case for every ID below. Each case must query complete messages, selection state, bar DOM, and timer queue; a passing case must assert the absence of a jump, not merely the presence of a control action.

| ID and test name | Events/timers | Required executable assertions |
| --- | --- | --- |
| G01 `retained_selection_controls` | Retain first-sentence range; run mousedown/up/click toggle and advance 150 ms. Parameterize previous/next/close, nested button child, label/text/input, bar background, and control double-click. | Only intended bound action(s), no `jumpToSentence`; toggle is `togglePlayback`; modes remain mutually exclusive; interactive descendants do not drag. |
| G02 `pending_selection_invalidated` | Make a genuine page selection schedule the 100 ms callback; at 50 ms interact with each G01 target, focus a control, press Space/Enter, and invoke synthetic `.click()` without mousedown. Advance beyond 150 ms. | No delayed jump; actual control action still occurs; mutation-removing the bar cancellation makes this case fail with an extra jump. |
| G03 `bar_drag_release_outside` | Start background drag with retained text, move over page, release on paragraph, advance 150 ms; repeat pointercancel/lost gesture. | Bar position changes; drag ends; no selection jump; cancellation leaves no active drag/timer. |
| G04 `teardown_replacement_timers` | Schedule page-selection callback, clean up at 50 ms, create successor bar, advance old timer, and invoke an old control reference. | No old message/listener; successor bar and attributes remain unchanged; repeated cleanup is safe. |
| G05 `genuine_page_selection` | Page mousedown with collapsed selection, change to second-sentence range, page mouseup, assert no jump at 99 ms and advance exactly 100 ms. Then retain unchanged selection and mouseup, and reselect same text after an intermediate selection change. | Exactly one bound `jumpToSentence` idx 1 for the genuine selection; no retained-selection jump; re-selection after an intermediate change works. |
| G06 `genuine_double_click_sequence` | First collapsed mousedown/up/click; second mousedown selecting `Second`, mouseup/click detail 2, dblclick detail 2, advance 150 ms; also execute the CDP-generated sequence. | Exactly one idx 1 jump; queued second mouseup is canceled; mutation-removing double-click cancellation produces a failing duplicate-jump assertion. |
| G07 `selection_ticket_freshness` | Schedule first selection callback, begin a different page gesture/change selection or replace sentence owner before 100 ms, then release old callback; run a genuine second selection. | Old ticket cannot read newer selection/session or jump; second ticket sends exactly its own jump. |
| G08 `render_attributes_order` | Queue newer paused update before initial creation; create using older running snapshot; apply older running after paused; replace session and run old update/hide/show last. | Deferred creation materializes paused; exact `▶`/`Resume`/`Resume` attributes agree; old updates/cleanup are ignored; compact controls and explicit accessible names remain. |

- [ ] Make G01/G02 check every descendant path with `composedPath`, G03 check release outside and cancellation, G04/G07 check the exact successor owner, and G08 check all three toggle attributes after every update. Do not treat a DOM fixture as evidence for physical media keys, installed-extension permissions, real audio, or service-worker eviction.

### Step 5: Add the standard pytest integration and mutation sensitivity procedure

- [ ] Create `tests/test_extension_playback.py` with no new dependency. Use `subprocess.run` with `cwd=ROOT`, `capture_output=True`, `text=True`, `timeout=120`, and `check=False`; assert return code zero and include command/stdout/stderr in the failure. Use two tests with exact argv vectors:

```python
from pathlib import Path
import subprocess

ROOT = Path(__file__).resolve().parents[1]


def run_node(argv):
    completed = subprocess.run(
        argv,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    assert completed.returncode == 0, (
        f"command failed ({completed.returncode}): {argv}\n"
        f"stdout:\n{completed.stdout}\n"
        f"stderr:\n{completed.stderr}"
    )
    return completed.stdout


def test_extension_playback_node_suite():
    output = run_node(["node", "--test", "tests/extension_playback.test.cjs"])
    assert "W01" in output and "W31" in output


def test_extension_dom_smoke():
    output = run_node([
        "node",
        "tests/extension_dom_smoke.cjs",
        "--chromium",
        "/usr/bin/chromium",
    ])
    assert "G01" in output and "G08" in output
```

- [ ] Run mutation sensitivity only in disposable copies outside the delivery worktree; do not add a mutator or mutation dependency. First run each unmutated selected case and require PASS: W02, W15, W16, G02, and G06. In each copy inspect the actual authored production implementation (not tests, specs, or external reproductions), record the exact file/function/line locations enforcing the target mechanism, and use asserted source replacements without requiring a particular helper or field spelling. Disable the complete relevant mechanism, including every redundant predicate/branch/call that independently enforces it, while preserving unrelated session, lease, lifecycle, and owner isolation:
  - W02: disable every part of the actual activation-staleness mechanism that independently keeps this pre-pause activation from constructing Audio or calling `play()` after the applied pause. Cover redundant intent-revision, media-revision, desired-pause, start-serial, page-attempt eligibility/floor, or equivalent centralized checks when they fence this exact operation. Preserve unrelated lease/session/lifecycle isolation. The mutated W02 must fail specifically at the zero-constructor/zero-play assertion after the pause boundary.
  - W15/W16: disable all checks that prevent a displaced attempt's actual stale continuation/poll from inspecting or mutating its successor, including strict object identity and equivalent attempt-ID/floor, start-serial, or centralized checks when they enforce that same attempt-ownership decision. Preserve unrelated current session/lease/lifecycle binding. The mutated W15 and W16 must each fail at an explicit successor identity/index/end-marker/advance assertion, not merely at a generic state or interface assertion.
  - G02: disable the complete bar-interaction cancellation/invalidation mechanism for the pending selection callback, including all relevant timer/ticket invalidations and equivalent admission checks. Preserve callback liveness/session/owner checks and the intended control action. The mutated G02 must fail because the still-pending selection produces an extra jump after bar interaction.
  - G06: disable the complete double-click-specific suppression of the queued second mouseup selection callback, including all redundant ticket/timer invalidations or equivalent checks. Preserve ordinary page-selection and owner/liveness checks. The mutated G06 must fail on the explicit duplicate-jump count assertion.
  If multiple source locations enforce one mechanism, enumerate each targeted location and assert the expected replacement count separately. Do not weaken unrelated fences or edit tests to manufacture a failure.
- [ ] Use a concrete disposable-copy probe with `tempfile.TemporaryDirectory`, `shutil.copytree(ROOT, copy)`, exact `Path.read_text`/`replace` operations, and `subprocess.run(..., timeout=120)`. Before each mutation, assert that the named baseline case passes; assert every expected source/replacement count for each production-file edit; then require the mutated selected case to return nonzero with its case name and target-observable assertion text in output. A syntax/interface failure or failure before reaching the target assertion is not sensitivity evidence. Remove every temporary copy in `finally`; leave no permanent mutation subsystem.
- [ ] Record Chromium availability and real optional pytest skip reasons. Do not mark DOM smoke as skipped on this host, where `/usr/bin/chromium` is required; do not describe ffmpeg-dependent full-suite skips as passes.

### Step 6: Run all checks, inspect coverage, and commit

- [ ] Run every Validation argv vector in this task, including both targeted and full pytest. Confirm syntax checks pass, the Node suite reports W01-W31, the Chromium driver reports G01-G08, and the wrapper propagates failures rather than hiding them. Record actual optional skip reasons and the mutation sensitivity results in the child report.
- [ ] Confirm R1-R6 are covered by executable cases: R1 by W12-W18/W23/W28 and G04/G07/G08; R2 by W02-W05/W19-W22/W27/W29-W31 and G08; R3 by W01/W06-W11/W15/W16/W25/W30; R4 by G01-G07 plus W15/W23; R5 by W03/W04/W24/W27 and G02/G08; R6 by W01-W31, G01-G08, syntax, targeted/full pytest, CDP smoke, and mutation probes.
- [ ] Inspect the final diff for allowlist compliance. Confirm no generic DOM library, npm file, server file, manifest permission, popup layout, hardware dependency, PM state, or external evidence file changed. Confirm `extension/background.js` still has all Audio/media effects in MAIN and no `chrome.*` in serialized MAIN functions.
- [ ] Commit only the Task 2 allowlist with:

```bash
git add extension/background.js tests/extension_dom_smoke.cjs tests/test_extension_playback.py
git commit -m "fix(extension): fence bar gestures and canonical rendering"
```
