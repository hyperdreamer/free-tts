# Flow bar pause/play technical specification

Date: 2026-10-17  
Status: Approved by the user after independent review (PASS); authorized for implementation under the PM workflow.  
Approved design: [2026-10-17-flow-bar-pause-play-design.md](2026-10-17-flow-bar-pause-play-design.md), commit `95e10aabfdd8cfc07467961177d27287e83d58a2`.  
Implementation baseline: `30c025138d4cb2c51b6fd00f266ae79deac3a86e`.  
UI decision: Option A, compact icons.

Normative terms: **must** specifies an acceptance requirement. Identifiers below specify logical fields and contracts; JavaScript objects and local helpers implement them without introducing a framework. No TypeScript build or generated types are required.

## 1. Outcome and scope

Repair the existing extension service worker, injected MAIN-world audio/media functions, and isolated-world floating bar. Keep sentence splitting, server requests, prefetching, highlighting, media-key latency, loop/repeat semantics, and the popup layout. The worker remains the authority for accepted playback intent and sentence/attempt ownership. MAIN world remains the authority for the Audio object, playback position, and the instant at which an audio operation applies.

The repair must provide all of the following:

- Bar controls, their descendants, dragging, and keyboard activation cannot turn retained selection or a pending selection callback into a sentence jump.
- Pause remains available while initialization, cache waiting, fetching, highlighting, audio activation, or completion polling is pending.
- A start/resume carrying an obsolete intent cannot call `Audio.play()` after a newer pause has applied in MAIN world. Obsolete activation must also avoid constructing an Audio object.
- One attempt owns loading, activation, the intended Audio identity, completion observation, and advancement. Replacing a session at the same index/tab, replaying the same index, or navigating away and back creates distinct ownership.
- Resume preserves the identity and `currentTime` of unfinished audio; resumes pending loading once; and restarts the current sentence only when its completion was already observed while the worker was paused.
- State and rendering converge in accepted order. The bar's `textContent`, `title`, and `aria-label` describe the same next action in one synchronous render.
- Failures, terminal cleanup, and already-sent bar/media messages cannot mutate a successor session.

No server changes, npm setup, dependencies, popup markup/CSS redesign, playback persistence, generic playback engine, or general bridge security project belongs in this repair. An already-open popup does **not** acquire continuous synchronization with unsolicited bar/media changes. Its initialization, command responses, and explicit `getPlaybackState` reads must use canonical worker snapshots (§8).

The guarantee begins at page application, not at the user's physical input time or worker receipt. Audio can begin if a start runs before pause reaches MAIN world. Once pause applies, no older start may run, even transiently. A later compensating pause does not satisfy this contract.

## 2. Evidence and retained behavior

The complete baseline sources for this specification are `extension/background.js`, `extension/popup.js`, `extension/manifest.json`, `extension/popup.html`, `extension/popup.css`, `tests/test_media_session.py`, `tests/test_extension_split_sentences.py`, and `requirements.txt`. The approved design and complete independent design review were read. External evidence resides in the PM run's `reports/` directory and is not deliverable source:

- `selection-repro.html` and `selection-repro-browser-output.html` show a Pause click producing `pausePlayback` followed by `jumpToSentence(0)` with a retained selection. With selection cleared it produces only pause. The saved browser stderr log is empty.
- `playback-repro.cjs` and `playback-repro-output.txt` show new audio playing with worker `isPaused === true`, preserved `currentTime === 3` for normal pause/resume, and resume-before-end-poll leaving an ended element for the poll to process.
- `design-review-1.md` reports same-index replacement, stale resume, initializer/cleanup ownership gaps, and requirements R1–R6. Those findings are resolved in §13.

These probes establish the defect and test inputs; they do not establish the repaired implementation's correctness. This drafting task does not run implementation tests or the full suite.

Retain these baseline rules:

1. `PRELOAD_AHEAD` stays 2; the cache wait stays at most 10,000 ms with 200 ms checks; direct-fetch timeout stays 120,000 ms. Use the session's normalized server URL, voice, and speed consistently for preload and fallback.
2. Selection matching uses trimmed nonempty selected text, the first sentence for which `sentence.includes(text)` is true, and existing sentence order. Do not change the splitter or XML escaping.
3. Previous at index 0 is a no-op. Valid previous/next/jump explicitly select a new attempt and request running state; jumping to the same index intentionally replays it. Invalid jump indices have no side effect.
4. Repeat-current prevents normal end advancement. Loop restarts at index 0 at the end of the list. Loop defaults on and repeat-current defaults off. Enabling either disables the other; disabling one does not enable the other. Next beyond the last sentence follows the existing boundary policy: repeat-current or loop starts index 0; neither enabled completes the session. No audible start occurs while paused.
5. MAIN-world Media Session handlers, MAIN-world Audio, an idempotent isolated relay, no `chrome.*` in MAIN world, a 25,000 ms keepalive, and media teardown before final playback clearing remain invariants.

## 3. Ownership and concrete state

### 3.1 Worker records

Keep `sentencePipeline` and `activePlayback`, enriching their existing records. Allocate identifiers synchronously, before asynchronous work that could outlive its owner.

| Record/field | Required type and meaning |
| --- | --- |
| `workerId` | Opaque `crypto.randomUUID()` created once per worker lifetime. It is an identity, not a sortable timestamp. |
| `lifecycleRevision` | Worker-wide increasing safe integer. Allocate when publishing a session and on every stop/retirement. Never reuse a value in that worker. |
| `startRequestSerial`, `pendingStart` | Increasing starter serial and at most one `{requestId, requestSerial, cancelled}` ticket for target-tab lookup/selection extraction before a session can be published. Allocate the ticket serial before the first await; allocate the session lifecycle only at publication. |
| `sentencePipeline.sessionId` | Unique opaque string for one published session. |
| `sentencePipeline.lifecycleRevision`, `tabId`, `leaseId` | Immutable session identity and target binding. `leaseId` is populated by §4's page handshake. |
| `sentences`, `currentIdx`, `cache`, `voice`, `serverUrl`, `speed` | Existing fields. Settings may be uninitialized until storage resolves; no audio fetch starts before settings are valid. |
| `isPaused` | Accepted worker desired state. `false` includes initialization/buffering; it does not assert audible playback. |
| `intentRevision` | Session-local increasing safe integer, initially 1. Increment synchronously for each toggle and each state-changing explicit pause/resume/navigation. A redundant explicit pause/resume is idempotent. |
| `localIntentFloor` | Latest intent revision introduced by a bar/popup/shortcut toggle, explicit pause/resume, or manual navigation, initially 1. Worker revisions generated to execute admitted media navigation do not raise this floor. Mode-only changes do not raise it. It separates delayed media from newer local playback intent without rejecting an ordered burst of media navigation. |
| `mediaRevision`, `lastMediaSequence` | Latest validated page play/pause revision known to this worker session, initially 0; and highest admitted media-action sequence, initially 0. A page snapshot may advance `mediaRevision`, but only an admitted media-action notification advances `lastMediaSequence`. A script receipt cannot consume a not-yet-delivered navigation command. |
| `loopEnabled`, `repeatCurrentEnabled` | Worker-owned booleans with §2's exclusivity rule. |
| `nextAttemptId`, `attempt` | Increasing session-local attempt counter and at most one current attempt record. |
| `cancelled`, `initializationPromise` | Session invalidation and one owned initializer. |
| `activePlayback` | At least `{tabId, sessionId, lifecycleRevision}` for the published owner, or null fields for idle. Never rediscover a cleanup target from this mutable global after an await. |
| `viewRevision` | Worker-wide increasing safe integer for canonical public snapshots and UI rendering. Increment on publication, accepted desired/mode changes, owned phase changes, and retirement/failure. |
| `lastError` | Null or the most recent current-owner terminal error's code/message for the current idle snapshot. Clear on a new valid session. |

A session shell must be published after a valid target and nonempty split sentences are known, **before** awaiting settings storage. It has `isPaused: false`, default modes, and phase `initializing`. A pause can then affect this initializer. A start ticket must already exist before `tabs.query`, keyboard selection extraction, or storage is awaited. Stop and a newer start invalidate earlier tickets, including tickets that never published a pipeline. While only target lookup is pending, popup pause/resume addresses the currently published session, if any; it does not configure an as-yet unpublished start.

A newer valid start invalidates older pending start tickets immediately. At publication it synchronously retires the former session and installs its shell. Allocate the former session's retirement fence first, then the new session's strictly larger lifecycle; both occur in that synchronous publication step. This prevents a retirement fence from outranking the successor it was created for. Popup/shortcut stop invalidates all pending start tickets. A session-bound bar/media stop retires its addressed session/initializer, preserving a newer unrelated start ticket awaiting target lookup; natural completion/error follows that same ownership limit. Concurrent start lookups/storage completions cannot publish in completion order. Every continuation checks its original ticket/session. Invalid input does not replace valid playback.

### 3.2 Attempt record

An attempt is one deliberate playing/replaying of one sentence, not merely a sentence index:

| Field | Required meaning |
| --- | --- |
| `attemptId`, `idx` | Immutable increasing ID and selected sentence index. |
| `phase` | `initializing`, `loading`, `ready`, `starting`, `playing`, `paused`, or `ended`. Idle/failed/retired have no active attempt. |
| `retired` | Becomes true before replacement work is scheduled. |
| `loadPromise`, `dataUrl` | At most one current-sentence loader and its retained result. Resume never launches a second cache-wait/fallback loader for this attempt. |
| `activationRequest` | Null or the latest pending `{intentRevision, mediaRevision, promise}`. Duplicate requests for its tuple coalesce. A newer tuple replaces this reference without awaiting the old promise; old closures remain fenced. Do not retain a history/map of superseded requests. |
| `audioId`, `startSerial`, `audioRevision` | Page-issued Audio identity, latest play-invocation serial, and latest accepted audio observation revision, or null before creation. |
| `pollTimer`, `pollInFlight` | At most one completion timeout and one injected poll in flight. |
| `observationRequest`, `observationNeeded` | At most one fenced MAIN observation request in flight and one coalesced request for a fresh sample after a triggering page observation; see §6.1. No outcome/message history. |
| `completionObserved`, `completionObservedWhilePaused`, `completionConsumed` | Initially false. Owned completion is accepted once. A paused completion remains on this attempt until replay/retirement. These are logical predicates and may be derived from phase/retirement rather than stored as three independent latches. |

Allocate a new attempt for initial playback, manual navigation, automatic advance, loop restart, repeat-current replay, and restart after observed paused completion. Reusing cached bytes is allowed; reusing an ended attempt/Audio as a new playback attempt is not. Resume of unfinished audio or loading keeps its attempt ID.

The worker ownership predicate for attempt continuations is: the captured pipeline is the current object; its session identity/lifecycle match `activePlayback`; it is not cancelled; the captured attempt is exactly `pipeline.attempt`; and that attempt is not retired. Index equality or tab equality alone is never sufficient.

Check ownership before dispatching effects, after every relevant await, before mutating cache/current index/end markers, before scheduling another callback, and in catch/finally/terminal paths. Intent-sensitive operation failures additionally require the operation's intent/media revisions to remain current; render failures require its `viewRevision` still to be current. An obsolete activation/render error within the same session/attempt cannot terminate a newer intent. The still-owned loader/settings operation remains necessary across pause/resume, so its genuine failure can retire that owner. Capture tab/session/attempt locals at entry. Preload callbacks may write only their captured live session cache; they never acquire the current global cache. Obsolete failures may be logged with their old owner but cannot clear or stop anything current.

### 3.3 MAIN and isolated records

Maintain one bounded MAIN-world record, for example `window.__freeTtsPlayback`, plus the existing `window.__freeTtsAudio` alias. Audio must only be created/accessed in MAIN world. Its fields are:

- `leaseId`, `workerId`, `lifecycleFloor`, and the current `sessionId`/session lifecycle or null.
- `workerIntentRevision`, `mediaRevision`, `mediaSequence`, `desiredPaused`. Fresh installation uses `workerIntentRevision=0`, both media counters 0, and `desiredPaused=true`, so the worker's first revision 1 can apply explicitly. `mediaRevision` counts direct media play/pause applications; `mediaSequence` counts all five media action invocations. They are independent of `audioRevision`, which orders audio observations.
- `attemptFloor`, `attemptId`, `audioId`, the owned Audio reference, `audioStatus`, `startSerial`, `audioRevision`, and the last accepted worker intent's compact result. No historical arrays/maps of retired sessions or commands.
- `audioStatus` is `none`, `starting`, `playing`, `paused`, `ended`, or `error`. Missing audio is not equivalent to ended audio. Increment `audioRevision` on audio creation/disposal, an accepted play/pause effect, a live play outcome, and first observation of ended audio. Include it in every page receipt/audio notification, so delayed starting receipts cannot overwrite newer playing/paused/error observations with identical intent revisions.
- `audioError` is null or `{code, message}` for the current live play failure. Set it with `audioStatus: error` and the corresponding audio/serial/revision identity before emitting the failure outcome. Retain that observation for a fenced read; clear it on pause, disposal, or a new live play invocation, which invalidate/supersede that failure.

Counter reference (the protocol sections below define their authoritative admission rules):

| Counter/floor | Scope and advancing writer | Admission purpose |
| --- | --- | --- |
| `lifecycleRevision` / `lifecycleFloor` | Worker lifetime; worker allocates, each world retains its applied floor within a lease | Reject replaced/retired session effects (§4) |
| `attemptId` / `attemptFloor` | Session; worker allocates, MAIN retains selection floor | Reject displaced playback attempts (§3.2/§4) |
| `intentRevision` / `workerIntentRevision` | Session; worker accepts, MAIN records seen revision | Order local intent and reject stale worker effects (§5.1) |
| `localIntentFloor` | Session; worker advances on local playback intent only | Reject media commands predating a local command (§5.3) |
| `mediaRevision` | Session; MAIN play/pause advances, worker observes | Fence starts against direct media intent (§5.1–§5.3) |
| `mediaSequence` / `lastMediaSequence` | Session; MAIN actions advance, worker admits notifications | Deduplicate/order media actions without consuming them through receipts (§5.3) |
| `startSerial` / `audioRevision` | Current MAIN session; MAIN advances on play invalidation/effects/observations | Identify a live invocation and order observations (§6.1) |
| `viewRevision` | Worker lifetime; worker advances | Reject stale public/UI snapshots (§5.4/§7.2) |

The isolated world retains an analogous lease/lifecycle fence and current bar/highlight ownership, the highest rendered `viewRevision`, one optional latest `pendingRender` snapshot, a session-local `barCreated` flag, and one bar-instance gesture record (§7). A render snapshot contains the canonical view plus the session's current `loopEnabled`/`repeatCurrentEnabled`; mode rendering uses these worker values. Keep the fence/tombstone after teardown so late show/highlight injections cannot recreate retired UI. Old tombstones need not accumulate.

## 4. Page admission and lifecycle fencing

Numeric lifecycle counters are meaningful only within their worker lease. Establish a per-tab lease lazily using existing `executeScript` calls; no ports, persistent service, dependency, or network request is required:

1. Read the current `(leaseId, lifecycleFloor)` in each execution world. A fresh page has `(null, 0)`. Generate a new lease token for this worker/tab.
2. Claim each world with a synchronous compare-and-swap against both observed fields. A stop advancing the floor must make an earlier claim lose even when the token did not change. A claim retires the prior world's owned resources before installing the new token and empty fences. Complete MAIN retirement/claim before dispatching isolated retirement/claim, preserving media/audio cleanup before UI teardown. A losing claim changes nothing. Read/retry only while the initiating request/session is still current. Serialize lease establishment per worker/tab, share its promise, and never serialize loading/playback behind it. On an abandoned partial claim, its inert lease may remain; the next live request can finish/reclaim it.
3. A callback carrying a different lease cannot claim implicitly, install a session, or perform cleanup. Reusing a worker's established lease does not reset fences. This also allows a newly started worker to supersede old page resources without comparing random worker IDs or wall clocks. Restoring a lost in-memory pipeline is out of scope.

Install a session with `(leaseId, lifecycleRevision, sessionId)`. Within the lease:

- A lifecycle below the floor is rejected. Equal lifecycle is accepted only for the same session and is idempotent. A strictly newer lifecycle retires the former session's resources, advances the floor, and installs the new owner.
- Installation starts inert/paused until an explicit accepted intent is applied. Repeated installation cannot reset intent/media revisions or an existing attempt.
- Initial UI/media/highlight setup, attempt selection, intent application, and audio activation must await the shared short session-install barrier. They must not await settings, fetching, a preceding command's completion, UI rendering, or sentence completion merely to dispatch pause. There is no audio before installation; pause during this barrier updates worker intent immediately and is sent once the barrier is available.
- Stop/terminal cleanup captures its owner and allocates a newer retirement lifecycle synchronously. The MAIN retirement injection advances the floor even if the old session never finished setup. It clears only resources owned by the retired session or an older session, never a newer lifecycle. A late old install/activate is rejected against the retained floor.
- MAIN retirement clears owned Media Session handlers/metadata/keepalive **before** pausing and clearing owned audio. Isolated cleanup then removes owned highlights and the owned bar/listeners/timers, in that order. Final worker clearing uses an owner comparison and cannot clear a successor. Cancellation of worker timers/attempts happens synchronously before page cleanup awaits.

Setup and cleanup can overlap with replacement. Each injected function must recheck its own lease/session/lifecycle (and attempt where applicable) **inside its execution world** before DOM/media/audio changes. A worker precheck alone cannot protect an already dispatched injection. Installation, highlight initialization, individual highlights, show/update/hide bar, media setup/cleanup, and audio disposal all obey this rule.

The ordering records are constant in size per live tab/session/attempt: floors and last-observation counters, not an append-only event log or a set of old tokens. Keep only live tab lease entries and remove closed-tab entries. A shared per-tab lease claim may retry compare-and-swap at most twice after its first attempt (three claims total); continued contention is a current-owner setup error under §9. No worker lock/queue is held across playback loading or polling.

For an attempt change, advance a per-session attempt floor and pause/clear only the displaced attempt's Audio. An old attempt selection cannot replace a newer attempt. Selecting an attempt never writes desired paused state. In particular, an old selection arriving after pause cannot unpause the page. A later attempt can still be selected/loaded while the latest desired state remains paused.

## 5. Intent ordering, application, and acknowledgment

### 5.1 Worker acceptance and separate page execution

A valid toggle reads `pipeline.isPaused` and flips it synchronously, increments `intentRevision` and `viewRevision`, raises `localIntentFloor` to that intent revision, then launches bounded page intent application. Two/three toggles accepted while rendering lags must have even/odd parity against accepted state. The button glyph is never read to choose a command. Explicit pause/resume set the requested state idempotently. Navigation accepts its selected index/new attempt and running intent synchronously; completion/replay creates attempts only through the current owner. A state-changing bar/popup/shortcut playback-intent command raises `localIntentFloor`; an admitted media navigation increments worker intent for its page application but does not raise that local floor.

No await may precede these accepted worker changes once the target session is resolved. No long command queue may hold pause behind initialization, cache/fetch/highlight, an Audio play promise, or the completion observer. Page application and rendering are separate operations. Rendering must not be an audio-control prerequisite.

A worker intent injection carries:

`{leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaBase, paused}`

`attemptId` is the worker's selected attempt at acceptance (or null before one exists). A running intent may play existing audio only if that same attempt is already selected in MAIN world. An attempt mismatch still permits desired-state application, but never permits resuming the preceding attempt. Once the matching selection applies, the normal guarded activation continues it. Pause can always pause the session's currently owned audio, including an audio element awaiting displacement.

`mediaBase` is the worker's last validated `mediaRevision` at acceptance. The page's synchronous intent reducer must:

1. Reject wrong lease/session/lifecycle.
2. Reject an intent revision below its `workerIntentRevision`. Equal revision is idempotent and cannot reapply a contradictory payload or repeat a play call.
3. For a newer revision, advance `workerIntentRevision` before effects. If `mediaBase` equals current page `mediaRevision`, apply `desiredPaused = paused`. On pause, invalidate any pending play outcome serial and call `pause()` on the owned Audio if present. On resume, use the owned unfinished Audio if one exists, with the play rules in §6; never play a preceding attempt's element.
4. If `mediaBase` differs, keep the page's newer media intent and audio effect; record the worker revision as seen but **do not apply** its requested paused/running value. Return a `superseded-by-media` receipt with current page state. This also blocks lower worker revisions from applying afterward.
5. Return a synchronous application receipt containing `{leaseId, sessionId, lifecycleRevision, attemptId, intentRevision: workerIntentRevision, mediaRevision, paused: desiredPaused, audioId, audioStatus, startSerial, audioRevision, disposition}`. Use null audio identity when absent. MAIN dispositions distinguish `applied`, `idempotent`, `stale`, and `superseded-by-media`; the worker maps these to §5.4's public dispositions. Other page receipts use the same observation fields plus their operation result. A play promise must not delay this receipt.

This conflict rule is intentional: a direct media change and a worker command based on a page state predating that change overlap until reconciliation. Media wins that overlap. The worker command's response must reveal supersession and actual accepted state. After a validated media receipt/notification is adopted, a new worker command carries its new `mediaBase` and can change the state normally. Do not blindly retry the superseded requested state; that would undo the media action. A manual navigation's selected attempt remains selected, but its request to unpause loses to the newer page media state in this overlap.

The worker adopts a page receipt's desired state only if its session still matches, its worker intent revision equals the current accepted worker intent revision, and its media revision is not older than the worker's. Otherwise it may collect identity information for a still-current attempt without changing desired state, or discard the receipt. Any newly adopted media intent increments `viewRevision`. Render a fresh snapshot after adoption. A stale completion of `executeScript` cannot assign `isPaused` from the command's old requested value. A current receipt that first introduces an unknown media revision/live invocation also triggers §6.1's bounded fresh observation, including a `superseded-by-media` receipt; its earlier captured `starting` sample is provisional even if the introducing media-action notification is rejected.

### 5.2 The no-stale-start boundary

Audio activation is a separate operation carrying an exact capability:

`{leaseId, sessionId, lifecycleRevision, attemptId, intentRevision, mediaRevision, dataUrl}`

Activation does **not** install a session, select an attempt, advance an intent revision, or write `desiredPaused`. Before constructing Audio or calling `play()`, its synchronous MAIN function must verify:

- lease, lifecycle, session, and current attempt all match;
- both revisions exactly match the page's current intent/media revisions;
- `desiredPaused === false`;
- the attempt is not completed/retired and is eligible to activate.

A failed check returns a no-effect receipt/snapshot. Once these checks pass, creation/assignment/play invocation are synchronous, without an intervening await or timer. If this attempt already owns audio, return/use that same identity according to §6; duplicate activation cannot construct a second element. Publish the owned reference and `window.__freeTtsAudio` before calling `play()` so immediate handlers/polls address the same object.

Every delayed continuation that could call `play()` must pass the same checks at its own execution time. In particular, resolving fetch/highlight/script promises cannot themselves grant permission to start. If an activation was rejected only because its old intent was superseded, the current attempt retains its loaded bytes; the latest running intent schedules activation with the current receipt. It cannot be stranded by an obsolete in-flight activation promise. A new revision may schedule its guarded activation while an older injection response is still pending; the page's attempt identity/idempotency prevents duplicate creation.

A pause **applies** at the synchronous MAIN step that commits the newer revision/paused value, invalidates pending play outcomes, and pauses the owned element. Its receipt records that boundary. Therefore:

| Page execution order | Required result |
| --- | --- |
| Old start executes, then newer pause executes | A play call before pause is allowed; the same owned Audio is paused at pause application. No old callback may start it afterward. |
| Newer pause executes, then old start executes | Old start creates no Audio and makes no play call. |
| Old resume executes, then newer pause executes; old resume's script/play promises settle last | No new play call on either settlement; pause remains applied. |
| Newer pause executes, then old resume executes | Old resume revision is rejected before play. |

Dispatch order, page execution order, and `executeScript` promise completion order are three distinct things. Tests must control all three. A final paused flag after an illicit transient stale play is a failure.

### 5.3 Direct media and delayed bridge messages

Keep media action handlers in MAIN world. Each handler captures its installed lease/session/lifecycle and checks that binding before acting or sending. Every media action (play, pause, previous, next, stop) increments `mediaSequence` before effects/notification, including repeated explicit actions. Only play/pause also increments `mediaRevision` and updates the shared desired-state record synchronously. Previous/next/stop initially only request worker action; they do not preemptively clear audio or set desired running state. Keepalive/audio outcome notifications allocate neither counter. No action waits for the relay/worker merely to perform its direct play/pause effect:

- Pause commits paused intent, invalidates pending play outcomes, and pauses the owned audio if present.
- Play commits running intent, resumes only owned unfinished audio, and otherwise requests worker continuation. It cannot replay an ended element or construct a new one on its own.
- During loading/no audio, both actions still update the record and notify the worker. Absence of Audio must not suppress media intent.

The bridged media-action notification includes `{action, leaseId, sessionId, lifecycleRevision, intentRevision: workerIntentRevision, mediaSequence, mediaRevision, paused, attemptId, audioId, audioStatus, startSerial, audioRevision}`. The isolated relay forwards these fields unchanged in one runtime message and stays idempotent. Retain the `__freeTtsMedia` marker and `event.source === window` filtering. No `chrome.*` call is introduced in MAIN world.

A shared media-action admission check validates sender tab/session/lease, requires `localIntentFloor <= notification.intentRevision <= pipeline.intentRevision`, and requires `mediaSequence > lastMediaSequence`; admission synchronously advances `lastMediaSequence`. Duplicates and delayed lower media sequences are ignored. A notification based on a revision below the latest local floor cannot undo a newer bar/popup/shortcut playback command. An older worker base may still be admitted after earlier commands from the same ordered media burst advanced the worker revision; these are not newer local intent. Audio-outcome messages use §6's separate exact-revision/identity checks and are not media-action proposals.

`syncPausedState` uses that admission check for mediaPause/mediaResume. It adopts desired/audio observations only if their `mediaRevision` is at least the worker's known media revision, with audio observations additionally guarded by `audioRevision`. Equal media revision can supply an already-observed action without repeating its Audio effect. After admitting either action, request the fresh fenced observation specified in §6.1 before its worker-side activation/replay continuation. The notification's captured `starting` observation is provisional: its play outcome may already have settled and reached the worker before the action itself. If a newer local operation had an obsolete media base, its own page receipt supplies the conflict snapshot as specified above; accepting a delayed notification is not used to guess ordering.

Previous/next/stop use the same admission check before normal worker action. Media navigation captures `notification.mediaRevision` as its page command's `mediaBase`; it cannot substitute a newer media revision learned from a receipt and thereby overwrite a later direct media pause. If a newer media revision is already known, select the requested attempt but preserve the latest known desired state until reconciliation. Ordered quick next/next messages remain two commands, including when a page receipt overtakes the second notification; a next message delayed past a newer local pause is ignored. For an intentionally reordered media burst, the highest admitted media sequence supersedes lower undelivered requests; no resequencing queue is introduced.

A valid media resume may request the existing attempt's pending activation or paused-at-end replay through the normal worker continuation helper. It must not issue a duplicate Audio play for a direct play already in progress/successful in MAIN world. Previous/next/stop notifications carry the same originating session binding; delayed old-session handlers/messages are ignored. They continue through the existing worker navigation/stop paths. Keepalive `ping` carries a binding but remains a no-op message without a dedicated worker state mutation.

### 5.4 Responses and realistic acknowledgment

Use a canonical snapshot containing:

`{workerId, state, sessionId, viewRevision, intentRevision, mediaRevision, phase, audioStatus, error}`

`workerId` scopes `viewRevision`; a consumer accepting a response from a newly contacted worker starts a fresh view high-water mark. Each popup/runtime request callback retains its issuing connection epoch: after accepting a different worker ID from a later-issued state/command request, ignore responses from earlier-issued requests for a previous worker ID. A worker restart yields idle in-memory state; session restoration remains out of scope. Same-worker responses compare `viewRevision` normally.

`state` is `idle`, `playing`, or `paused`; playing/paused describe accepted desired state. Idle has null session/intent/media revisions, phase `idle`, and `audioStatus: none`, while retaining its worker ID and numeric view revision. `error` is null or `{code, message}` for the most recent terminal playback error. `phase`/`audioStatus` explicitly distinguish buffering/starting from audible success.

`getPlaybackState` returns that snapshot object. A command response returns those same fields at the top level plus `{ok, accepted, disposition, pageApplied, commandError}`. `ok`, `accepted`, and `pageApplied` are booleans; `commandError` is null or `{code, message}` for this command's failure. Invalid arguments or cleanup failure can set `commandError` while the canonical `error` field still describes actual playback state. Use stable error codes at least for invalid arguments, settings/load timeout or failure, page operation failure, play rejection, missing audio, poll failure, render failure, and cleanup failure; messages may retain the underlying exception's useful text.

- `accepted` means the worker accepted a valid command against its intended session. It does not mean a sentence finished or sound started.
- `disposition` is `applied`, `superseded`, `ignored`, or `failed`. A stale-origin/no-session no-op is `ok: true, accepted: false, disposition: ignored` with the current snapshot. Invalid arguments are `ok: false, accepted: false, disposition: failed` with an error and current snapshot.
- `pageApplied` describes whether that command crossed its MAIN application boundary, independently of whether a later command superseded it. It is false for superseded-before-application/ignored/failed application. No page application is claimed from dispatch alone.
- A successful pause response requires its MAIN pause receipt, or a truthful superseded response if overtaken. Resume/start responds after owned setup/intent application and scheduling the one current continuation, without awaiting cache/fetch/audio completion. Its snapshot can report loading/starting; it must not report Audio play success solely because injection resolved.
- Page UI rendering is scheduled from accepted snapshots independently. The command attempts a current render before responding, but must not wait for loading/play/poll. While initial bar creation is still pending, retain only the newest snapshot for that creation and classify rendering as deferred, rather than treating expected absence as a failed update. Once creation has succeeded, a missing bar/current render failure follows §9. If a newer operation supersedes its render, return the latest snapshot. A render result never grants permission to issue audio again.

Construct the public snapshot at response-send time from current worker state, not from an earlier command's requested state. `getPlaybackState` returns that same snapshot immediately without waiting for loading, audio promises, polls, or UI. The browser can still delay delivery after construction; consumers ignore a snapshot below the greatest `viewRevision` they have rendered.

## 6. Playback lifecycle and transitions

### 6.1 Loading and activation

The session initializer owns settings, guarded bar/media/highlight setup, initial preloads, and the first attempt. After each await it checks ownership. Pause does not cancel data preparation: cache waiting/fallback/highlighting may finish while paused, but the result becomes `ready` without Audio construction or play. Resume while loading clears paused intent and lets that same `loadPromise` finish. Resume from `ready` schedules activation once. Initialization can finish in either desired state.

The public phase has an exact priority: `initializing` until settings and initial page setup finish; `loading` during current-sentence cache/fetch/highlight preparation; `ready` once its URL/highlight is ready but no owned Audio exists; `starting` while a live running play invocation is pending; `playing` after its live play success; `paused` when unfinished owned Audio has accepted paused intent; `ended` after owned completion is accepted without advancement. Pausing an initializing/loading/ready attempt retains that phase and changes `isPaused`; pausing a starting/playing attempt sets phase paused and invalidates its play invocation. `audioStatus` reports MAIN observations separately. Retired/failed sessions expose idle as specified in §9.

Manual navigation retires the former attempt and all its observers immediately, allocates a new attempt even at the same index, resets its completion flags, applies the new desired intent separately, and selects the new page attempt. No old Audio can be resumed during the new attempt's loading. Cache is session-scoped, so valid cached bytes can be reused. Navigation and later pause may execute out of order in the page: attempt selection never changes desired state; stale intent application/start is separately rejected.

The page serializes an actual play invocation with `startSerial`. Pause, disposal, or a new conflicting play invalidates its predecessor's serial. A redundant running action/duplicate activation does not call `play()` again on audio already playing/starting. A play outcome checks captured lease/session/attempt, exact Audio reference/audioId, live serial, and current running intent before changing `audioStatus` or emitting success/failure. It never calls `play()` from `.then`, `.catch`, or `.finally`. Emit outcomes through the existing relay with their complete current page snapshot, `audioError`, and audio/serial/revision identity. A late outcome's worker handler also checks current session/attempt and accepted revisions. A valid exact-revision outcome or fenced observation can establish `attempt.audioId` and the corresponding `startSerial` before an activation receipt/action notification has established them; do not require an unknown serial to match first. Once established, reject a different Audio identity for that attempt and any lower serial. Adopt a newer serial only from an otherwise current owned observation with a greater `audioRevision`. Adopt audio observations only when `audioRevision` exceeds the last accepted observation; a delayed activation receipt cannot downgrade a more recent outcome. Expected rejection of a play invalidated by pause/disposal is not a current-owner failure.

**Fresh observation for newly learned page state.** The following are triggers for one bounded reconciliation helper: an admitted media play/pause action; a current page receipt that introduces a previously unknown media revision or live Audio/invocation (including `superseded-by-media` and observation receipts); and an otherwise structurally owned audio outcome whose higher media/audio observation or serial cannot yet be admitted because its introducing revision/invocation is not known. An early outcome is not an intent proposal: it requests a read rather than changing desired state or being forgotten without a guaranteed resample. Reject wrong lease/session/attempt, different established Audio, lower media/serial, and non-new audio observations before triggering. A stale worker revision cannot authorize adoption or failure; a structurally current higher observation may still request a harmless fresh read of the current owner. No trigger admits a rejected media action or advances `lastMediaSequence`.

The helper requests a synchronous, no-effect MAIN read of the live record. Return the full current intent/audio snapshot including `audioError`, after checking lease/session/attempt and the owned Audio reference; do not call `play()`, allocate Audio, or advance `mediaSequence`. MAIN retains settled status/error as specified in §3.3. A read executed after the result observes that result or a newer superseding observation. A read executed before settlement is an earlier sample; if the result then arrives before that read's receipt, the early-outcome trigger requests a resample instead of losing it. Recovery never depends on eventual admission of the introducing action notification.

Keep at most one request in flight per live attempt and one `observationNeeded` bit. While reconciliation is required, loading may finish but no worker continuation may schedule activation/replay from the provisional running sample. A trigger received while a request is in flight sets the bit even when its new media revision/serial is not yet adoptable. After processing the in-flight receipt, issue one coalesced fresh read if needed and the owner is still current; defer the pending continuation until the required observation completes. A read introducing new revision/invocation can cause one follow-up sample; an unchanged read is not itself a trigger. Do not accumulate per-action promises/outcome history or retry failed reads. An injection failure follows §9; retirement cancels continuation authority and clears the bit. MAIN execution and worker receipt may be delayed independently.

Pause and newer local commands apply and acknowledge independently of an older read. Their current receipts obey these same triggers: a new local command does not bypass reconciliation merely because its conflict receipt is newer. Acknowledgment may still return accepted/superseded with loading/starting state without waiting for play settlement. A newer intent/attempt invalidates an older sample's continuation; a fresh read requested by that newer current receipt observes its own owner rather than reviving the old command.

On a read's completion, revalidate the captured owner. Adopt desired intent only under §5.1's current-worker-revision and nondecreasing-media-revision rules; adopt audio only for the resulting exact accepted revisions, same attempt/Audio, nondecreasing serial, and greater `audioRevision`. A current `error` observation retires the owner under §9; `playing`/`starting` on existing unfinished Audio requires no extra play; `none` with current running intent may continue the single loader/activation after required reconciliation; an owned ended observation uses §6.2's qualified completion boundary. A newer pause, navigation, replacement, or higher observation defeats the obsolete read/outcome and its continuation. A lower-media action notification admitted after a fresh snapshot cannot regress it. These rules cover fulfillment/rejection learned through actions, conflict receipts, and early outcomes without a play retry or consuming a delayed navigation action.

Set Media Session `playbackState` to `playing` only after a still-owned play promise fulfills. Set it to `paused` on accepted pause; use `paused` while running intent is buffering/starting, since Media Session has no buffering value. After retirement use `none`. Unsupported Media Session is optional (§9); Audio ownership is not optional.

### 6.2 Completion and qualified restart policy

Use a nonoverlapping 500 ms completion timeout loop, not an async interval that can issue overlapping polls. The injected poll receives session/attempt/audio identity. It reports `owned` plus `ended`/audio status, or `missing`/`stale`; missing/stale is **not** completion. A response from a displaced Audio cannot observe or advance a successor through the global alias. Revalidate worker ownership after the injected poll resolves, including responses that executed earlier but resolved after replacement.

The completion observation boundary is the worker's synchronous acceptance of an owned `ended: true` sample. Set `completionObserved` once. Read **current accepted** worker `isPaused` at that boundary:

- If paused, mark `completionObservedWhilePaused`, phase `ended`, and do not advance. A later resume retires this attempt and starts a new attempt at the same index from cached/fetched bytes.
- If running, process repeat-current/advance/loop using current worker-owned mode fields and consume completion once. Do not await a DOM mode read between checking pause and committing the next attempt. An active pipeline never uses DOM fallback as command authority. If helpers retain a fallback for no-pipeline callers, it is outside the owned completion path.
- If the element ended physically while paused, but resume is accepted **before** an end sample is accepted, no paused-completion marker exists. Resume does not play the ended element; the later poll observes running state and follows normal repeat/advance/loop. Do not impose an always-restart or always-skip policy.

A pause accepted before the completion decision commits prevents advancement and establishes the paused-end marker when completion is observed. A pause accepted after the synchronous decision can pause the newly selected attempt. Direct media application can precede its worker notification; no audio starts after its page pause, and the worker's end-policy decision uses the desired state it has actually accepted at observation time. Do not claim knowledge of an undelivered media event.

### 6.3 Transition table

`P` denotes accepted desired pause, `A` the one current attempt. Every row is subject to the ownership/revision checks above.

| Event and precondition | Worker transition | Permitted page/continuation effects |
| --- | --- | --- |
| Valid start publishes | Retire predecessor; new session, `P=false`, new A/initializing | Guarded setup; loading once; start only after current running page receipt |
| Pause/toggle to pause while initializing/loading | `P=true`; keep A and loader | Apply page pause promptly; loading may become ready; no Audio creation/start |
| Pause while ready/starting/playing | `P=true`; preserve A/audio | Fence old starts; invalidate play outcome; pause current owned audio |
| Resume/toggle to running while loading | `P=false`; same A/loadPromise | Apply intent; existing loader activates once when ready |
| Resume while ready/no Audio | `P=false`; same A | Guarded activation; one Audio for A |
| Resume with unfinished owned Audio | `P=false`; same A/audioId | Play same object at unchanged `currentTime`; no fetch, new Audio, seek, or extra completion observer |
| Resume after `completionObservedWhilePaused` | `P=false`; retire A; new A at same idx | Replay current sentence from start; old end marker cannot affect new A |
| Resume ended Audio before end observation | `P=false`; same A until poll | No play of dead element; later normal completion decision |
| Valid previous/next/jump | Retire A; new A at selected/boundary idx; `P=false` | Dispose displaced audio, load selected sentence; concurrent newer page pause may supersede unpause |
| Owned ended sample with `P=true` | A becomes ended/observed-while-paused | No advance or replay until resume |
| Owned ended sample with `P=false`, repeat-current | Consume once; new A, same idx | Replay once; new identity despite same index |
| Owned ended sample with `P=false`, more sentences | Consume once; new A at idx+1 | One advance/activation path |
| End of list with loop or boundary repeat policy | Consume once; new A at 0 | Preserve mode exclusivity; obey latest pause |
| End of list with neither mode | Retire session; idle after accepted terminal transition | Media teardown, audio disposal, highlight cleanup, bar cleanup, owner-compared final clearing |
| Valid direct media intent | Page applies first; worker adopts only current receipt/notification | Low-latency owned play/pause, or worker pending/replay continuation |
| Stop | Invalidate addressed session/A/initializer/loader authority/polls; popup/shortcut also cancels all pending starter tickets; public idle unless a successor is already active | Captured/fenced retirement; no global target rereads |
| Replacement | New shell/identity; predecessor cancelled | Old initializers, starts, polls, failure handlers, and cleanup cannot touch successor |
| Owned fatal error | Retire owner, public idle/error | Fenced cleanup; truthful failure/snapshot |
| Stale callback/message/receipt/error | No current-state mutation | No start, seek, advancement, successor cleanup, or stale render |

## 7. Gesture lifecycle and compact bar

### 7.1 Per-instance gesture record

Each bar creation has `{instanceId, leaseId, sessionId, lifecycleRevision, alive, gestureRevision, origin, lastCompletedOrigin, selectionChanged, selectionTimer, selectionSnapshot, drag}` in its closure. `origin` is independently tracked as `bar`, `page`, or null; it must not be inferred from `drag` after a mouseup listener resets dragging. Retain `lastCompletedOrigin` through click/dblclick after mouseup; reset it when a new gesture begins. Store every document listener and timeout handle for teardown.

Classify event targets with `composedPath()`/containment of the actual bar instance; normalize text nodes to their parent when needed. Filtering must include buttons, nested button children, labels, label text, inputs, and bar background. Do not filter by the literal target tag name alone.

Required event behavior:

1. Use the existing mouse event lifecycle, with a capture-phase mousedown classifier before document selection work; do not register compatibility pointer/mouse pairs that reset the same gesture twice. A down on/inside the bar marks bar origin, advances `gestureRevision`, and cancels pending selection timeout. A bar-origin drag stays bar-origin through its release, even if released over page text.
2. Capture `click`, `change`, `keydown`, and `focusin` on the bar to invalidate pending selection work too. This covers keyboard Space/Enter activation, checkbox label activation, focus moved into a control, and programmatic click without mouse down. Preserve default button/checkbox behavior. Do not `preventDefault` merely to filter selection.
3. Only noninteractive bar background starts dragging. Interactive descendants (`button`, `input`, `label` and their descendants) never start a drag. Background drag preserves existing movement; release outside ends drag without a jump. Window `blur`, document `visibilitychange` to hidden, and document `pointercancel` cancel an active gesture/drag, clear its selection timeout, advance its ticket, and reset its active origin. These cancellation listeners do not add a second pointer-down/up lifecycle and are removed on cleanup.
4. On a page-origin mousedown, invalidate older tickets, clear `selectionChanged`, and capture the baseline selection (anchor/focus nodes/offsets and text). A document `selectionchange` during this live page gesture marks `selectionChanged` when selection differs from baseline; remove this listener on cleanup too. On mouseup, require a live page-origin gesture and reject bar targets; an unmatched mouseup schedules nothing. Then end/reset active gesture state, retaining completed origin through click/dblclick. Schedule a selection callback only for a noncollapsed nonempty page selection that differs from baseline or changed during the gesture. This permits a genuine re-selection of the same text after an intermediate collapsed/different selection. Snapshot selection endpoints/text at mouseup; do not discover an unrelated retained selection later.
5. Keep the existing 100 ms defer for page selection. Its callback must require `alive`, the exact current bar instance, the same `gestureRevision`/ticket, a still-matching selection snapshot, and the same session. It uses the captured selection text and first-match sentence rule. A callback that fails a check has no message/effect.
6. The document double-click handler rejects when the target is inside the bar or the active/last-completed origin is `bar`, invalidates pending mouseup callbacks from the gesture, and sends the genuine page selection jump immediately. For the real `mousedown/up/click`, `mousedown/up/click/dblclick` sequence, the queued second mouseup must not send another jump after dblclick. First-click collapsed selection creates no callback. If a genuine earlier completed drag-selection had already sent a jump before a later double-click, the later jump is a new navigation attempt and supersedes it safely; no duplicate live audio/advancement is allowed.
7. Escape first cancels pending selection work/advances its ticket, then sends bound stop while this bar is live. All bar actions send an explicit source/session binding. The toggle always sends `togglePlayback`; no handler infers pause/resume from `textContent`.
8. Cleanup/replacement first sets `alive=false`, advances the ticket revision, clears the selection timeout, and ends dragging; then removes document listeners and this bar. A previously captured/removed DOM control's handler must also check `alive` before sending. Cleanup is idempotent. Old cleanup cannot delete a successor's cleanup function/bar.

A retained selection, by itself, never supplies proof of a new selection gesture. Filtering by target only, stopping just `click` propagation, or merely removing the mouseup listener without clearing its timer is insufficient.

### 7.2 Rendering

Initial creation and every accepted update run one synchronous, version-guarded renderer with the following mapping:

| Accepted worker intent | `textContent` | `title` | `aria-label` |
| --- | --- | --- | --- |
| Running, including loading/starting | `⏸` | `Pause` | `Pause` |
| Paused, including ready/ended | `▶` | `Resume` | `Resume` |

Apply all three attributes together without an await. Set explicit accessible names for the other existing icon buttons from their existing action titles as well. Keep Option A's widths, icons, modes, drag behavior, and placement; no new visible button text.

Render messages include lease/session/lifecycle and `viewRevision`. The isolated renderer rejects wrong/retired owners and revisions below its high-water mark. Repeated equal snapshots are idempotent. While `barCreated` is false, an update retains the highest-revision `pendingRender` without claiming to have rendered it; initial creation materializes the newer of its own snapshot and that pending snapshot, then sets `barCreated=true`, clears pending data, and records the rendered revision before attaching handlers. A delayed creation call must use a newer pending snapshot rather than dropping creation solely because its supplied snapshot is older. Once a live bar exists, repeated show is idempotent and cannot replace/reset it; once that session is retired, show/update cannot recreate it. Mode rendering follows the same ownership/version fence and preserves checkbox exclusivity.

## 8. Runtime interfaces, origin binding, and popup

Retain the existing action names except add `togglePlayback` and an internal audio-outcome action such as `audioPlaybackEvent`. The existing pause/resume actions remain explicit for popup/other existing callers.

| Origin/action | Required addressing and response behavior |
| --- | --- |
| Bar: toggle, previous, next, jump, close/Escape, mode changes | `{source: "bar", sessionId, lifecycleRevision, action, ...payload}`; sender tab must equal session tab. Bind the target at receipt; stale/no-longer-active owner is ignored. |
| MAIN relay: mediaPause/mediaResume, previous/next/stop, audio outcomes | Forward full §5 tokens with `{source: "media"}`; sender tab, lease/session/lifecycle must match. Audio outcomes additionally match attempt/audio/startSerial and current accepted revisions. |
| Popup: speak/pause/resume/stop/getPlaybackState | `{source: "popup", action, ...payload}`; intentionally address the currently published session at command receipt. A popup does not acquire an old session binding from its last displayed state. |
| Keyboard shortcut | Existing start/stop toggle, but create/invalidate tickets before asynchronous tab/selection operations. Internal commands bind captured owner. |
| Mode changes | Validate booleans and originating session, change worker mode flags synchronously; disabling sets false and enabling enforces exclusivity. Return canonical snapshot. |
| Jump | Require an integer within sentence bounds; invalid indices return invalid-argument response without retiring A. |

For existing untagged popup-style commands with no `sender.tab`, treat the extension popup caller as popup/current-session addressing. A tab-origin command must have a valid session binding; it cannot fall through as an unbound popup command. This is lifecycle isolation, without adding a new authentication scheme.

Adapt local function signatures to carry captured owner records rather than rediscover globals: `startSentenceTTS` accepts/creates a start ticket; `pausePlayback`, `resumePlayback`, `stopPlayback`, navigation, setup/cleanup, and rendering accept the resolved owner; `playNextSentence` operates on a captured pipeline/attempt. They may use small local helpers for ownership, snapshots, and page operations. Keep all injection bodies self-contained/serializable: closures in worker memory are unavailable in the page. MAIN media handlers may share the page's installed record/helper. No exported package or engine abstraction is required.

Popup changes are limited to response consumption and error handling in `extension/popup.js`:

- Keep `popupState` and existing buttons/layout, but set state from returned canonical snapshots, not from the requested action (`paused`, `playing`, or `idle`) guessed after `{ok:true}`.
- Track the greatest rendered `viewRevision`. Use the same snapshot application for init, speak/pause/resume/stop responses, and state refresh. Ignore an older delayed response.
- An ignored/superseded command renders its response's actual state. A failed response also renders its canonical state and records/logs the error through existing console paths. A transport failure triggers one `getPlaybackState` attempt; if that also fails, retain the last confirmed state and log failure. Do not unconditionally display idle on a transport error or unconditionally display playing after start.
- A start response can show playing while initializing/loading because the next action is Pause. Terminal failure removes the owned bar and makes `getPlaybackState` idle/error. Continuous unsolicited popup updates remain out of scope.

## 9. Errors and cleanup authority

| Condition | Required behavior |
| --- | --- |
| Obsolete initializer/load/highlight/script/play/poll fails | No current-state mutation or successor cleanup. Cancel its local timers; log context if useful. |
| Preload for a future sentence fails | Log, keep session alive, allow owned current-sentence fallback later. |
| Current owned settings read or current-sentence fallback fails/timeouts | Retire owner to idle/error and perform captured fenced cleanup. A still-waiting command returns `ok:false`; an already acknowledged pending start is corrected through state/bar teardown and logged error. |
| MAIN install/intent/activation injection fails or returns no valid receipt | Do not claim page application/play success. If still current, retire to idle/error, attempt cleanup; return failure to the waiting command. |
| Current owned `Audio.play()` rejects | Mark page audio status/error only for the live invocation; emit owned failure and retain it for a fresh fenced observation. Either the admitted outcome or §6.1's read must retire the current owner to idle/error and clean up. Outcome-before-media-action delivery cannot lose the failure. An earlier command that already reported accepted/starting is not retroactively rewritten; subsequent state reads show failure. |
| Current completion poll/fresh-observation scripting fails or unexpected owned audio is missing | Fail current owner to idle/error; do not treat failure/missing as ended and advance. A legitimate no-audio loading/ready snapshot is not unexpected missing audio. A superseded observation cannot fail a newer intent. |
| Highlight lookup finds no text/rects | Nonfatal; audio may continue. Existing highlight rendering/scroll exceptions are logged and must not alter intent. |
| Bar creation/current update injection fails | Log and fail current owner to idle/error with best-effort cleanup, so successful command responses do not claim a usable synchronized bar. |
| Media Session unsupported or metadata/action registration unavailable | Log unsupported API/setup error; continue ordinary bar/audio playback. If MAIN session lease/record cannot be established, that is fatal as above. |
| Cleanup injection fails, tab closed, or page navigated | Log and continue remaining cleanup attempts. Retain worker invalidation/fences where available; never resurrect the retired session or clear a successor. Stop reports failure for an actual page-cleanup failure, with canonical idle/successor snapshot. |

Fatal handling must itself compare the captured owner before initiating retirement. Never call an unconditional global `clearPlayback()` from a catch. For natural completion, stop, and fatal error: cancel worker continuation authority first; perform owned media teardown, audio disposal, highlight cleanup, bar teardown; then finalize clearing only if the retired owner still occupies the corresponding worker record. A replacement can already be active by finalization; each page operation and the final compare must then leave it alone. Cleanup failures must not prevent attempting the remaining owned cleanup steps.

A failed media playback notification is not an explicit pause/resume proposal and cannot roll back newer desired state. On all paths, async play settlement is separate from intent application, and Media Session state changes must be guarded by the same live Audio invocation.

## 10. Affected files and implementation boundaries

| File | Permitted change |
| --- | --- |
| `extension/background.js` | Local ownership/revisions/lease helpers; runtime contracts; existing initializer, pipeline/navigation, completion observer, MAIN audio/media, isolated relay/bar/gesture/highlight cleanup functions. Keep existing architecture and splitting/network contracts. |
| `extension/popup.js` | Canonical versioned response consumption and bounded error refresh; retain layout and server/voice/speed functionality. |
| `tests/extension_playback.test.cjs` | New built-in Node executable behavioral regression suite for worker and injected functions; no npm dependencies. |
| `tests/extension_dom_smoke.cjs` | New Chromium DOM smoke driver/fixture generation using Node built-ins and `child_process`. |
| `tests/test_extension_playback.py` | New pytest integration that invokes the Node behavioral suite and DOM smoke, propagates failure/stdout, and has bounded subprocess timeouts. |
| `tests/test_media_session.py` | Retain useful invariants; replace brittle source-shape assumptions affected by token forwarding/helper signatures with executable semantic guards. Do not retain an ineffective string solely to make a test pass. |
| `tests/test_extension_split_sentences.py` | Keep all splitter fixtures and the web-preview guard. Replace only the extension's literal `startIdx` guard with a reference to the executable ownership cases; the stronger session/attempt predicate supersedes that literal. |

`extension/manifest.json` needs no permission, version, or content-script change. `requirements.txt`, server code, web-preview `script.js`, popup HTML/CSS, and external PM state/reproductions need no change. Node's built-ins and installed Chromium make a new package demonstrably unnecessary.

## 11. Deterministic acceptance fixtures

### 11.1 Harness contract

Use `node:test`, `node:assert/strict`, `node:vm`, `node:fs`, and other built-ins. Evaluate the complete real `extension/background.js` in a fresh worker VM per case with mocked Chrome boundaries. Invoke its registered runtime listener or real local functions. Do not rewrite the logic under test, replace real playback/control functions with model implementations, or assert only source text. Append a VM-only accessor if needed to inspect lexical records; production test exports are unnecessary.

Use separate MAIN and isolated VM contexts **per tab**, with shared/mock DOM only where required. Serialize actual injected `func.toString()` and arguments into the requested context, as Chromium does. MAIN has no `chrome`; Audio exists only in MAIN; isolated Audio construction is a test failure. Store each scripting request as three separately controllable steps:

1. `dispatch`: record target/world/arguments without executing.
2. `execute`: evaluate the actual function, capture its return/throw and any created play promises/effects.
3. `complete`: resolve/reject the worker-facing scripting promise with that captured result, independently of later/earlier request execution or completion.

Controls must allow completing a later request before an earlier one without executing the earlier one first. Play promises, storage, target lookups, fetch responses/body reads, bridge delivery, and UI injections have independent deferred gates. Use a virtual clock for 100/200/500/10,000/25,000/120,000 ms timers, with explicit microtask draining and a finite step limit; no real sleeps in concurrency fixtures. Tests must finish with no unintended observers/timeouts remaining after disposal.

The Audio spy records every constructor URL/identity, every `play()` invocation's identity/time/revisions, every pause/src-clear/currentTime write, and controllable ended/paused/play outcomes. Do not infer no stale start from final paused state. Observe runtime/window messages, accepted worker snapshot/index/end markers, poll count, media handlers/state/cleanup order, bar presence and all three toggle attributes. Failure assertions include identity of the successor, not just non-null state.

Default worker fixture: tab 7; sentences `['First sentence.', 'Second sentence.']`; normalized localhost server/default voice/speed; modes loop=false/repeat=false; data URLs `data:audio/mpeg;base64,AQ==` and `data:audio/mpeg;base64,Ag==`. Replacement fixtures use tab 7 and also tab 8 as specified. Default unfinished audio has `currentTime=3`, `ended=false`. Every test explicitly admits a real session/attempt through production paths before releasing or superseding its gates. A test that seeds a page record for a focused injection check must use a real install/selection/intent operation first.

### 11.2 Worker, audio, and ordering cases

The following named cases are mandatory; parameterization may group schedules while retaining individual failure labels.

| ID | Exact controlled schedule | Required observations |
| --- | --- | --- |
| W01 `pause_loading_boundaries` | Parameterize held settings storage, first 200 ms cache tick, fallback fetch after 10,000 ms miss, fetch body, and highlight injection execution/completion. Accept pause while held; release each gate; drain eligible work. | Pause changes worker desired immediately. Its MAIN application is dispatched without releasing the held loader/highlight. No Audio constructor/play while paused. Same loader/A becomes ready. Resume creates/plays once. |
| W02 `activation_executes_after_pause` | Load A; hold its old running activation at dispatch. Accept/execute/complete newer pause. Only then execute/complete old activation. | Zero constructor/play calls from old activation; page/worker/bar paused. This is the decisive no-compensation test. |
| W03 `activation_executes_before_pause` | Execute old activation and hold its script completion and play outcome. Execute/complete newer pause, then complete the old script and settle its old play promise. | Exactly the allowed pre-pause play call; audio paused; no later play or playing Media Session assignment; no worker rollback. Run old play resolution and rejection variants. |
| W04 `stale_resume_both_execution_orders` | Existing unfinished audio; pause, then accept resume R and newer pause P. Parameterize R executes before P but completes last, and P executes before R. Independently delay old render. | Final paused intent/audio/UI. First order permits R's pre-P play only; second order permits no R play. Old UI cannot overwrite P's three attributes. |
| W05 `rapid_toggle_parity` | Starting running, submit two and then three bound toggles without any render completion. Execute newer intent/render injections first, older last; reverse promise completion too. | Worker parity is running for two, paused for three. Glyph/title/aria-label and page intent agree after draining. Messages are toggle actions regardless of stale glyph. |
| W06 `unfinished_resume_identity` | Pause default audio at time 3; resume and repeat explicit resume after its acknowledgment. | Same A/audioId/object/time, no seek/fetch/constructor, one resume play, one completion observer; redundant resume does not play again. |
| W07 `resume_during_pending_load` | Hold fallback fetch; pause, resume, pause, resume before releasing fetch. | One load/fallback, one Audio creation/initial play on latest running stamp, one observer/advance. Repeat ending paused: zero starts until next resume. |
| W08 `ready_paused_resume` | Complete load/highlight under pause; leave A ready; resume twice. | Retained URL, single creation/play, no obsolete preceding Audio played. |
| W09 `end_observed_paused_replay` | Pause A, set its audio ended, execute and complete its poll while paused, then resume. | End marker belongs to A; new attempt at idx 0 and new Audio, one replay. Old duplicate end samples cannot advance/replay it. |
| W10 `end_observed_after_resume` | Pause A, set ended, resume before delivering an owned ended poll, then complete poll. | Resume makes no play on ended A. With modes off next attempt is idx 1; parameterize repeat-current => new idx 0 and loop at last => idx 0. No forced paused-end replay. |
| W11 `pause_at_completion_decision` | Execute an ended poll but hold completion. Accept pause and mode change before completing it. Also test completion commits first, then pause. | First order leaves current idx with paused-end marker and no replay/advance. Second order may select next/replay attempt, which becomes paused with no stale start. No async DOM mode authority. |
| W12 `stop_initializer` | Parameterize start held at tabs.query, keyboard selection extraction, settings storage, lease/setup and highlight initialization; stop; apply available captured retirement fences before executing old dispatched setup; release old gates/completions. | No pipeline resurrection, Audio/start/bar/media setup after the relevant retirement fence. Old worker continuations issue no new setup. No unhandled rejection; current public state idle. |
| W13 `replace_initializer_latest_wins` | Two separate variants: (a) admit S1 and hold its target lookup before publication; admit/complete S2, then release S1; (b) publish S1 as initializing, hold its settings continuation, then admit/complete S2 and release S1. Run both same tab/index and tabs 7/8. Apply captured retirement/admission fences before executing already-dispatched S1 effects after replacement. | (a) Superseded S1 never publishes. (b) S1's earlier initializing publication and any legitimately completed setup are allowed; it cannot republish or issue further continuations after losing ownership. In both, only S2 remains live and may start/render after the relevant fences; old work cannot mutate its normalized settings/cache/owner. |
| W14 `replace_pending_attempt_boundaries` | Parameterize old cache/fetch/body/highlight/activation dispatch; replace S1 with S2 at idx 0 in tab 7, apply S1's captured retirement fences and S2's relevant MAIN/isolated admission fences, then release/execute S1; repeat with S2 tab 8 while still retiring S1 in tab 7. | Old work cannot create/play/modify S2, set its paused-end marker, or write its cache. No worker continuation dispatches after losing ownership, and already-dispatched old effects do nothing after their tab's retirement/admission fence applies. |
| W15 `same_index_and_return_navigation` | Hold A at idx 0; jump to idx 0 (A2), or jump to 1 then 0 (A3); release old load/start/poll. | Distinct attempt IDs. Only latest attempt owns Audio/observer/advance; index equality never revives A. |
| W16 `poll_identity_and_no_overlap` | Hold one 500 ms injected poll in flight, advance clock by 2,000 ms, navigate/replay same index, execute old poll and complete it last. | Only one old poll in flight; it cannot inspect replacement as its own, mutate end flags, or advance new attempt. Missing audio is not ended. |
| W17 `late_failure` | Parameterize old settings/fallback/script/play failure after same-tab same-index S2 has started. | S2 audio/state/bar/metadata survive; no unconditional clear or cleanup. Invalidated play rejection after pause is nonfatal. |
| W18 `terminal_cleanup_overlap` | Parameterize stop, natural nonloop completion, fatal error; hold each cleanup injection at dispatch and separately at completion; publish S2 in same tab/index, then run/complete old cleanup. | S2 audio/bar/highlights/media survive. Old terminal finalization cannot clear S2. On uncontended cleanup, media handlers/metadata/keepalive clear before audio disposal, then highlights, bar, final clear. All cleanup steps attempted after one fails. |
| W19 `media_direct_without_worker` | Execute actual MAIN handlers with relay delivery withheld, for pause/play with unfinished audio and while no audio/loading. | Pause is immediate; unfinished play is immediate and same identity/time; no `chrome` access, no Audio creation for loading/ended; page revisions advance before effects. |
| W20 `media_pause_fences_queued_start` | Dispatch old activation/resume, execute direct media pause, then execute old operation with notification withheld. | No stale constructor/play after media pause. Page media revision gates it even before worker notification. |
| W21 `media_notification_reordering` | Apply media pause M1 then media resume M2; deliver M2 then M1 and duplicate M2. | Worker/bar end running with highest media revision; no duplicate Audio play from sync. Repeat final pause order. |
| W22 `mixed_media_worker_intents` | (a) Adopt media pause; accept popup resume and apply it; deliver old media pause again. (b) Dispatch worker resume on old media base; execute media pause first; apply worker resume; delay its receipt. (c) While that receipt is delayed, accept a later bar toggle, then deliver receipts/notifications out of order. | (a) No rollback after acknowledged popup intent. (b) Worker receives superseded + actual paused snapshot; no play. (c) Every page operation uses its bound revisions; newest applicable snapshot wins; no blind retry of superseded running intent. A new command after reconciliation can resume. |
| W23 `originating_session_messages` | Queue bar jump/toggle/mode/stop and media pause/resume/next/stop for S1; replace with S2 in same tab/index; deliver all S1 messages; also deliver from old tab 8. Then issue unbound popup pause. Separately, deliver a valid bound S1 stop while S2 is still an unpublished target-lookup ticket. | Published S2 survives stale S1 commands; they return canonical S2 snapshots without acceptance. Popup intentionally pauses S2. A bound stop of still-current S1 cannot cancel S2's unrelated pending ticket. |
| W24 `current_errors` | Reject current settings/fetch/play/activation/poll/bar update individually; include no-receipt/missing-Audio variants and play promise pending at command response. Also reject an obsolete activation/render after a newer pause in the same attempt. | No false play success; terminal idle/error and owned cleanup for current errors; truthful failed or already-accepted-pending response per §9. Superseded operation errors leave newer pause intact. Future preload failure remains nonfatal. Unsupported media leaves bar/audio usable. |
| W25 `modes_and_splitting` | Execute existing split fixtures against real splitter as well as retained Python checks; change loop/repeat flags through bound messages; run end/next/previous/jump boundaries. | Existing text arrays unchanged, mode exclusivity/defaults and boundary indices unchanged, one replay/advance at each end. |
| W26 `relay_and_world_invariants` | Install relay twice; setup media repeatedly; emit one full-token message; cleanup twice. Capture every script target/world and timer. | One relay listener/forward, complete unchanged tokens, all audio/media effects MAIN, no MAIN chrome, no port, one 25 s keepalive reset/cleared, all five action handlers installed/cleared. |
| W27 `snapshot_and_popup_responses` | Run real popup.js with mocked DOM/server/storage and real worker messaging. Hold pause response, accept resume/stop or replacement, deliver newer then older snapshots; include ignored/superseded/failed/transport-error responses. Repeat with a fresh worker ID and lower view revision, then a delayed response from the former worker. | Popup uses canonical worker ID/viewRevision ordering, not requested action; the former worker cannot overwrite the fresh worker's accepted snapshot. One bounded state refresh on transport error. `getPlaybackState` is responsive during every held long boundary and reflects accepted worker state. |
| W28 `lease_and_lifecycle_tombstones` | Dispatch old lease claim/install/show/highlight/retire, apply a newer claim/session or stop fence, then execute old operations. Test a claim whose observed token is unchanged but lifecycle floor advanced, CAS losing claim, and old equal-lifecycle install after retirement. | No old ownership reset/recreated UI/media/audio after the relevant fence. Bounded fences survive idle and reject obsolete work; fresh live worker can claim resources without restoring lost pipeline. |
| W29 `early_audio_outcome` | (a) Execute activation but hold its receipt; fulfill/reject play, deliver outcome first, then complete receipt. (b) From paused unfinished Audio at R4/M2/S3, direct media play creates M3/S4; hold N/R4/O13 (`starting`), settle play at O14, deliver F before N, then N/read. (c) Before settling (b), accept popup resume R5 based on M2; execute its superseded-by-media receipt C/R5/M3/S4/O13 but hold completion; settle play and deliver F/R5/M3/S4/O14, complete C, then deliver N/R4 (rejected below local floor R5). (d) Execute a read capturing `starting` before settlement, hold completion, deliver the future outcome first while its revision/serial is still unknown, then complete the read and coalesced resample. Parameterize fulfillment/rejection and newer pause/navigation/replacement before reconciliation. Admit action- and receipt-triggered samples while another read is in flight. | Early observations can establish identity/serial; old receipts cannot regress them. In every crossing, current rejection reaches idle/error; fulfillment reaches playing on the same Audio/time with one direct play and no constructor/retry. (c) Recovery requires no admission of N and N does not advance `lastMediaSequence`. (d) A post-outcome sample is guaranteed rather than forgetting F behind an earlier executed read. Newer owner/intent survives old F/read/continuation. At most one request and one coalesced resample; no provisional activation, blind play retry, or media-action consumption by any read. |
| W30 `navigation_resume_attempt_binding` | Hold new attempt selection after jump from idx 0 to 1; accept pause then resume and execute resume intent before selection. Release selection and new load; run stale old start last. | Resume cannot play idx 0's old Audio. Latest selected attempt activates once after its selection/intent are eligible. |
| W31 `delayed_media_navigation` | Queue media next/previous/stop at worker base R, accept/apply a later bound bar/popup pause at R+1, then deliver old media commands. Separately send two ordered media-next commands before their first worker injection completes, using at least three sentences; include a first-command receipt overtaking the second notification and a direct media pause interleaved before a delayed navigation is applied. | Delayed media commands cannot unpause/navigate/stop newer local intent. The ordered next burst advances twice, with distinct attempts and one surviving playback path; duplicate media sequences have no effect. Receipts do not consume actions, and old navigation cannot borrow the revision of a newer direct media pause to unpause it. |

### 11.3 Gesture and actual DOM cases

Use the **actual** injected bar/highlight/render functions. Run G01–G08 in real Chromium with controllable fixture timers. A lightweight DOM/event stub is optional for Node worker/setup/render scheduling; a second DOM implementation is not required. If a stub also runs gesture cases, it must implement their used bubbling/capture/containment and attribute behavior, and real Chromium must still corroborate them. It is not sufficient to call only an extracted selection predicate.

Fixture DOM: a paragraph `First sentence. Second sentence.`, its text node, and real created bar controls. A first-sentence range is offsets 0 through 15; a second-sentence range is 16 through 32; a double-click word range can select `Second` (16 through 22). Set the sentence list through real owned highlight initialization. Record complete runtime messages.

| ID | Events/timers | Required result |
| --- | --- | --- |
| G01 `retained_selection_controls` | Retain first-sentence range; mousedown/up/click toggle, then advance 150 ms. Parameterize previous/next/close, nested button child, labels/text/input, bar background, and a control dblclick. | Only intended bound control command(s), no jump from selection. Toggle sends `togglePlayback`. Modes still change/exclude each other and interactive labels/inputs do not drag. |
| G02 `pending_selection_invalidated` | Genuine new page selection schedules t=100 callback; at t=50 interact with each bar target in G01, or focus control and press Space/Enter; run beyond t=150. | No pending jump; actual control activation still works. Include synthetic `.click()` without mousedown. |
| G03 `bar_drag_release_outside` | Start background drag while text retained, mousemove over page, release on paragraph; run 150 ms. | Bar position changes, drag ends, no jump. Repeat pointer cancellation/lost gesture. |
| G04 `teardown_replacement_timers` | Schedule page-selection timeout, cleanup at t=50, create successor bar, then run old timeout and invoke old control reference. | No old message, no old listeners, successor bar unchanged; repeated cleanup safe. |
| G05 `genuine_page_selection` | Page mousedown with collapsed selection, change to second-sentence range, page mouseup, advance exactly 100 ms. | Exactly one bound `jumpToSentence` with idx 1. No jump at 99 ms; retained unchanged selection on later ordinary mouseup produces none. Re-selecting the same text after an intermediate selection change is still a genuine selection. |
| G06 `genuine_double_click_sequence` | First mousedown/up/click with collapsed selection; second mousedown, select `Second`, mouseup/click(detail 2), dblclick(detail 2), advance 150 ms. | Exactly one idx 1 jump, pending mouseup cancelled. Also use real Chromium-generated mouse events via CDP, not only synthetic dispatch. |
| G07 `selection_ticket_freshness` | Schedule first selection callback; before t=100 start a different page gesture/change selection or replace sentence owner; release old callback. | Old ticket cannot read newer selection/session and jump. Genuine second selection still works on its own timer. |
| G08 `render_attributes_order` | Before initial creation executes, apply a newer paused update; create the bar using the older supplied running snapshot. Then apply older running after paused; recreate under newer session, run old update/hide/show last. | Deferred initial update is materialized as paused when creation runs; no false missing-bar failure. Exact three-attribute mapping, old updates/cleanup ignored, compact controls retained, explicit accessible names present. |

### 11.4 Chromium smoke and test sensitivity

Use installed `/usr/bin/chromium`. `tests/extension_dom_smoke.cjs` must build a temporary HTML fixture by embedding real injected functions/source and a mock extension messaging boundary; do not modify or depend on the external PM reproduction files. The fixture must exercise G01–G08 with real `Range`, `Selection`, DOM containment/listener cleanup, and DOM attributes. Programmatic `dispatchEvent`/`.click()` checks cover the retained-selection reproduction, delayed callbacks, and keyboard-handler invalidation.

For G06, launch headless Chromium with a temporary user data directory and `--remote-debugging-pipe`; use Node `child_process` fd 3/4 and null-delimited Chrome DevTools Protocol JSON to create/attach a page and send `Input.dispatchMouseEvent` for the two-click sequence (`clickCount` 1 then 2), plus keyboard input where needed. This avoids a Playwright/Puppeteer/WebSocket dependency. Use actual text geometry from `Range.getBoundingClientRect()` to choose the word coordinate. Query the resulting messages and DOM attributes. A synthetic KeyboardEvent alone is not evidence of browser default button activation; use CDP Enter/Space for at least one button/checkbox case. For Enter, use `keyDown` with carriage-return text; the review feasibility probe found that `rawKeyDown` alone did not activate the default click. The smoke driver must exit nonzero on assertion/timeout/browser failure, terminate Chromium, and remove its temporary profile/HTML in finally. Use bounded per-step deadlines and a process-level timeout; no external server or audible TTS service is needed.

If the host requires disabling Chromium's sandbox, use a test-only command-line option in this driver and report that fact; it is not an extension change. A DOM fixture exercises real DOM behavior and injected code, but does not prove installed-extension permissions, OS hardware keys, real audio output, or service-worker eviction behavior. Record a manual installed-extension smoke result when available: retained-selection pause, pause during loading, resume position, keyboard control activation, and hardware play/pause/next/stop. Controlled MAIN handler/VM tests remain mandatory even if hardware smoke is unavailable. DOM smoke cannot be skipped silently on this host, where Chromium is available.

During implementation validation, demonstrate sensitivity of W02, W15/W16, and G02/G06 using a disposable copy: individually remove the page intent/media activation check, attempt ownership check, or gesture cancellation. Each relevant case must fail on its target observable (play/creation, successor mutation, or extra jump). Do not mutate the delivery worktree or add a permanent mutation-test subsystem. Running the old baseline may fail before these observables because interfaces changed, so baseline interface failure alone is insufficient sensitivity evidence.

## 12. Feasible validation commands and acceptance evidence

Run these **after implementation**, from the integration worktree. They are commands to be provided/run by the implementer; no result is asserted during drafting. New test files in §10 must support these exact entrypoints.

Targeted JavaScript and browser checks require Node with `node:test`/`vm` (the installed Node 24 is sufficient) and Chromium; they do not require Python dependencies or npm installation:

```bash
node --check extension/background.js
node --check extension/popup.js
node --test tests/extension_playback.test.cjs
node tests/extension_dom_smoke.cjs --chromium /usr/bin/chromium
```

Create an isolated Python environment outside the repository so requirements/full-suite collection does not depend on the host Python installation:

```bash
FLOW_BAR_TEST_ENV="$(mktemp -d /tmp/free-tts-flow-bar-venv.XXXXXX)"
python -m venv "$FLOW_BAR_TEST_ENV"
"$FLOW_BAR_TEST_ENV/bin/python" -m pip install -r requirements.txt pytest
PYTHONDONTWRITEBYTECODE=1 "$FLOW_BAR_TEST_ENV/bin/python" -m pytest -p no:cacheprovider -q tests/test_media_session.py tests/test_extension_split_sentences.py tests/test_extension_playback.py
PYTHONDONTWRITEBYTECODE=1 "$FLOW_BAR_TEST_ENV/bin/python" -m pytest -p no:cacheprovider -q
```

The pytest wrapper invokes both new Node entrypoints, checks their exit status, and makes browser absence/failure visible. Use a 120-second timeout for each Node behavioral/browser subprocess and report its stdout/stderr on failure. The full Python suite has existing optional ffmpeg-dependent integration cases; record actual skip reasons/availability rather than describing skipped cases as passed. No TypeScript typecheck command exists in this project; `node --check` is the appropriate JS syntax check. No real Edge TTS endpoint, npm install, or server modification is needed for deterministic extension tests.

Implementation acceptance evidence must include targeted/full results, syntax results, actual DOM smoke output, mutation sensitivity results, and any manual installed-extension smoke limits. It must account for every W/G fixture and all R1–R6. An independent frontier implementation audit remains required by the parent PM workflow. This specification neither runs that workflow nor mutates its ledger.

## 13. Design-review traceability and remaining decisions

| Review requirement | Resolved specification sections | Primary executable evidence |
| --- | --- | --- |
| R1: session/attempt/callback/initializer/terminal ownership and originating commands | §3 records/predicates, §4 admission/fences/cleanup, §6 attempt lifecycle, §8 source binding, §9 failures | W12–W18, W23, W28, G04/G07/G08 |
| R2: intent commit, audio/render ordering, realistic page acknowledgment, media/mixed operations | §5 complete ordering protocol and boundary table, §6.1 audio observation ordering, §7.2 render fence, §8 snapshots | W02–W05, W19–W22, W27–W31, G08 |
| R3: one pending path, resume identity/loading/end qualification, navigation/repeat | §2 retained rules, §3.2 attempts, §6 loading/completion/transition table | W01/W06–W11/W15/W16/W25 |
| R4: gesture origin/lifetime/descendants/drag/keyboard/double-click | §7.1 complete lifecycle and coalescing rule, §11.3/§11.4 real DOM | G01–G07, W15/W23 |
| R5: accepted versus audible state, failure responses, compact accessibility, popup consistency | §5.4 acknowledgments, §6 play outcomes, §7.2 three attributes, §8 popup boundary, §9 errors | W03/W04/W19/W24/W27, G02/G08 |
| R6: real functions, controlled scheduling, observable starts, preserved invariants, feasible checks | §10 file boundaries, §11 harness/fixtures/smoke/sensitivity, §12 commands/evidence | W01–W31 and G01–G08; retained splitter/media/full Python checks |

Routine choices are settled: session/attempt identities rather than index guards; bounded per-world lease/lifecycle/intent/media fences; direct media wins a conflict against a worker operation carrying an older media base; responses distinguish application from audible completion; page-selection callbacks are ticketed and double-click cancels queued mouseup work; terminal owned failures retire to idle/error; popup live synchronization is out of scope. There is no unresolved technical decision requiring user input. Independent specification review passed and the user granted final specification sign-off. Autonomous planning and implementation are the next phase. This document does not authorize delivery to the target branch by itself.
