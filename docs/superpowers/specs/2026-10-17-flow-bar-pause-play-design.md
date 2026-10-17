# Flow bar pause/play reliability design

Date: 2026-10-17
Status: Initial user design approval received; independent frontier review passed with zero design blockers
Delivery target: refs/heads/main at 30c025138d4cb2c51b6fd00f266ae79deac3a86e

## Background and reproduced faults

The browser plugin's floating control bar routes pause/resume to the extension service worker. Audio resides in the tab's MAIN execution world; the controls and sentence-selection listeners reside in its isolated world. The worker owns the sentence pipeline and desired paused state.

The existing `showControlBar` registers a document-wide mouseup handler. Its delayed callback treats any retained text selection as a request to jump to the matching sentence. A mouseup on the Pause button bubbles into that handler. A headless Chromium probe using the current injected control bar reproduced these messages for a selected sentence:

```json
[{"action":"pausePlayback"},{"action":"jumpToSentence","idx":0}]
```

The same click with the selection cleared sends only `pausePlayback`. `jumpToSentence` sets `isPaused = false` and starts playback, undoing the pause.

A separate executable Node VM probe of the real worker functions found that `playNextSentence` creates and plays an Audio element even if `sentencePipeline.isPaused` is already true. The cache wait, fetch, highlighting, and script injection introduce asynchronous windows in which a pause can occur. The observed result was `isPaused: true` with `audio.paused: false`. Normal mid-sentence pause and resume successfully reuse the existing audio position; preserve that behavior.

`updateControlBar` also updates the icon without updating its Pause/Resume tooltip. The click handler currently derives the command from the icon, so delayed rendering can produce duplicate pause requests on rapid clicks.

The headless Chromium reproduction validates the real DOM event handlers. The worker probe validates JavaScript behavior with mocked Chrome scripting and audio APIs. A real extension smoke test will complement these controlled reproductions where browser automation permits it.

## Goals and accepted behavior

- Clicking flow-bar controls must not initiate a sentence jump, including when text remains selected or a selection callback is pending.
- Genuine page text selection and double-click sentence jumping remain available.
- Pause takes effect during speech and during sentence loading. Completing a cache wait, fetch, highlight, or page injection must not start audible speech while the requested state is paused.
- Resume during an unfinished sentence continues the same Audio element at its current position.
- Resume during loading starts that pending sentence once when ready.
- Resume after the worker has observed a paused completed sentence follows the existing restart-current-sentence behavior. Do not introduce a new skip policy in this repair.
- Rapid clicks resolve against worker-owned state; the displayed icon is a rendering of that state, not the command authority.
- The button icon, tooltip, and accessible name consistently describe its next action: Pause while playing, Resume while paused.
- Existing stop, previous/next, loop, repeat-current, popup, and media-key behavior remain covered by regression checks.

## Approaches

1. **Targeted event and playback-state repair (recommended).** Separate flow-bar pointer events from sentence-selection gestures, invalidate pending selection callbacks on bar interaction and cleanup, and make the existing pipeline honor worker-owned pause intent across asynchronous work. Route the flow-bar toggle through worker-owned state. This addresses both demonstrated defects without replacing the playback architecture.
2. **Selection-handler repair only.** Smaller change that addresses the retained-selection reproduction, but leaves the demonstrated pause-during-loading failure unresolved.
3. **Replace the playback engine.** A broader state machine could unify buffering and control events, but is unnecessary for this repair and carries a wider regression surface.

## UI choices

Option A (recommended): retain the compact bar and synchronize the action tooltip and accessible name with the icon.

```text
Playing:  [⏮] [⏸] [⏭] [✕] [✓ ↻] [□ ↻1]   action name: Pause
Paused:   [⏮] [▶] [⏭] [✕] [✓ ↻] [□ ↻1]   action name: Resume
```

Option B: add visible text to the toggle, increasing bar width.

```text
Playing:  [⏮] [⏸ Pause ] [⏭] [✕] [✓ ↻] [□ ↻1]
Paused:   [⏮] [▶ Resume] [⏭] [✕] [✓ ↻] [□ ↻1]
```

Selected option: A, explicitly chosen by the user. Keep the compact icon controls and synchronize action tooltips and accessible names.

## Architecture and responsibilities

The existing service worker remains the source of truth for playback intent and sentence ownership. The MAIN-world audio element remains the source of playback position. The isolated-world control bar renders worker state and sends a toggle request; it does not infer the desired command from its glyph.

The page gesture handlers must distinguish control interaction from a new page selection. Any delayed selection callback must remain owned by its current control-bar instance and gesture, and must be invalidated by a bar interaction or teardown. Filtering applies to descendants of the bar, including labels and inputs, so controls cannot accidentally seek.

The playback path must retain the identity of the pipeline and pending sentence work across awaits. A pause changes desired state promptly. Pending work can finish loading, but its final decision to create/start audio must respect the latest intent; resume must continue or activate exactly one owned playback path. Stop or a new playback session invalidates old pending work. Navigation must not allow stale completion polling to advance a replacement playback.

The technical specification will define the precise boundary between worker state and MAIN-world audio actions, including the injection-order race between starting and pausing audio. A single pause check before an awaited injection is insufficient by itself.

## Error handling

Script or audio-play failures must be reported through the existing response/logging paths and must not leave the toggle showing successful playback when no playback can start. UI changes follow confirmed state. Cleanup invalidates delayed selection work and releases listeners so a removed/replaced bar cannot seek later.

## Verification strategy

Use executable regression tests against the real injected functions and worker code, with controllable scheduling rather than source-string assertions for the new behavior:

- Retain a sentence selection, mouseup/click Pause, and assert that playback remains paused and no jump is requested.
- Schedule selection handling, interact with the bar before its callback runs, and assert no delayed seek.
- Verify real page selection and double-click still request the expected sentence.
- Pause during cache wait, direct fetch, highlighting, and audio injection; release each boundary and assert no audible playback until resume.
- Resume an existing paused Audio element and verify position is preserved and no replacement is created.
- Resume buffered and completed-sentence states; verify a single owned playback path and no duplicate advancement.
- Exercise rapid toggles and media-key/popup state changes; assert icon, tooltip, and accessible name agree with worker state.
- Stop or replace a pipeline while work is pending and verify the old work cannot start or advance the new pipeline.
- Run existing extension tests, the complete project test suite, JavaScript syntax checks, and an independent frontier implementation audit before delivery.

## PM lifecycle

After initial design approval: independent frontier design review; frontier technical specification drafting and review; user specification sign-off; autonomous validated implementation plan, execution manifest, lane projection, deterministic SDD execution, candidate integration, and frontier implementation audit. The final delivery decision presents direct merge/push versus Pull Request after verification.

## Evidence locations

The external PM run report directory contains `selection-repro.html`, `selection-repro-browser-output.html`, and `playback-repro.cjs`. These diagnostic files and the external PM ledger are not deliverable source files.
