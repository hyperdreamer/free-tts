# Desktop TTS Cleanup Ownership Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Close `F-23` by moving the cancellation-cleanup bound off the delivery
call and onto the speech worker's own path, so a wedged DELETE cannot hold the
terminal event and STOP cleanup stops being schedule-sensitive.

**Architecture:** One correction in `desktop/module.py`. The previous run's final
fix wrapped delivery in a daemon thread **inside** `_cancel_requests`, but
`handle_stop` already dispatches `_cancel_requests` on its own `free-tts-cancel`
daemon thread. That second hop means `wait_idle()` can observe the worker idle
before delivery has entered `client.cancel()`, so an outstanding backend request can
remain uncancelled. This plan reverts the nested thread, keeps `_cancel_requests`
synchronous over one generation-memoized deadline, and bounds only the worker's
cleanup wait via a new `_bounded_cancel_outstanding` called from `_speak_worker`'s
`finally`.

**Tech Stack:** Python 3.11+ standard library only, `threading.Event` barriers,
`pytest`.

## Global Constraints

- `desktop/` must import only the Python standard library. It must never import
  `server.py` or Flask.
- Standard output carries only Speech Dispatcher protocol traffic. All logging
  goes to standard error.
- Preserve exact SSIP codes and events: `200 OK SPEAKING`, `701 BEGIN`,
  `702 END`, `703 STOP`, `704 PAUSE`, `700:<mark>`, and the error codes already
  in use. Exactly one terminal event per accepted utterance; a paused utterance
  emits one `704 PAUSE` and no `702 END`.
- Preserve honest PAUSE boundaries: a real future index mark when one remains,
  otherwise the next chunk boundary. Never fabricate a mark, and never emit a
  boundary the audio has not reached.
- Preserve every earlier fix: generation-local request ownership, duplicate-ID
  409 rejection, fresh IDs for ambiguous backend recovery, bounded DELETE-404
  registration handoff, atomic retry admission via `reserve_retry`, decoder
  process and I/O-thread ownership, backend revalidation at LIST/SPEAK
  boundaries, single-process Waitress, all installer guarantees, and this
  branch's cancellation deadlines: `_emit_lock` separation, `_await_chunk`
  polling with `_CANCEL_DRAIN_SECONDS`, `_recovery_abandoned` with
  `_RECOVERY_DEADLINE_SECONDS`, and `begin_cleanup()`.
- No audio frame, index mark, `701 BEGIN`, or `702 END` for a generation may be
  written after that generation's STOP has been observed. Cancellation
  correctness is not permitted to regress in exchange for latency.
- Timeouts passed to `urllib` are per-socket-read inactivity limits, not
  whole-call deadlines. Any bound this plan calls absolute must be enforced by a
  monotonic deadline that is re-checked between steps, never by lowering a
  `timeout=` value alone.
- Never modify `tests/test_extension_split_sentences.py` or
  `tests/test_media_session.py`.
- Never weaken, skip, delete, or make timing-dependent an existing test. Adapting
  a call site to a changed signature is allowed; changing an assertion to make a
  failure disappear is not.
- `tests/test_desktop_module.py::TestStop::test_stop_cancels_outstanding_requests`
  must pass **repeatably under parallel stress**, not merely once. It is the test
  `F-23` made flaky and it must not be modified.
- `tests/test_desktop_module.py::TestBackendRecovery::test_health_to_post_race_restarts_refreshes_and_retries_once`
  must keep passing unchanged: mid-utterance recovery stays functional when no
  STOP or PAUSE arrives.
- Concurrency regressions order events with `threading.Event` or an injected
  clock. Never use `time.sleep` to order a race, and never assert on wall-clock
  durations that depend on machine speed.
- Run tests with `.venv/bin/python -m pytest` from the repository root. The
  baseline is 466 passed with no skips; a different total is acceptable only with
  no failures, no errors, and no new skips.
- Do not add a runtime dependency. Do not touch the browser frontend, the Chrome
  extension, or `server.py`.
- Out of scope for this plan, and not to be implemented even if noticed:
  nonblocking stdout via `poll`/`select`, HTTP response body size caps, logging
  budgets, PCM slab processing, chunk resizing, making `fcntl.flock`
  interruptible, and any change to `desktop/synth.py` or `desktop/backend.py`.

## Task 1: Bound cleanup on the worker's path, not the delivery call

**Implementer tier:** Frontier

**Problem:** `F-23`, filed `Important` and load-bearing by the previous run's final
re-reviewer. `handle_stop` dispatches `_cancel_requests` on a `free-tts-cancel`
daemon thread, and commit `7d26977` then added a **second** daemon hop inside
`_cancel_requests` itself. The speech worker can therefore be observed idle before
the nested helper enters `client.cancel()`, leaving an outstanding backend request
uncancelled.

Controller measurements of
`tests/test_desktop_module.py::TestStop::test_stop_cancels_outstanding_requests`
in isolated parallel processes, failing with `client.cancelled == []`:

```text
HEAD 7d26977: 7 / 20 failures
base 7605c6d: 0 / 20 failures
```

The single-process suite passes 466, so this defect is invisible without parallel
stress.

The correct bound belongs on the **worker's** path. `_speak_worker`'s `finally` is
what withholds the terminal event, so that is the only place that must stop waiting
at a deadline. `handle_stop`'s own dispatch already runs off the protocol thread and
needs no additional bound.

**Files:**
- Modify: `desktop/module.py` — `_speak_worker` `finally`, `_cancel_requests`
- Test: `tests/test_desktop_module.py`

**Interfaces:**
- Produces: `SpeechEngine._bounded_cancel_outstanding(generation) -> None`, which
  runs `_cancel_outstanding` on an owned `free-tts-cleanup` daemon thread and joins
  it only for the remaining generation cleanup budget.
- Preserves: `_GenerationToken.cleanup_deadline() -> float` exactly as committed in
  `7d26977`, including its memoization under the token's own lock.
- Preserves: `SpeechEngine._cancel_outstanding(generation) -> None` and
  `_cancel_requests(generation, request_ids) -> None` signatures.
- Preserves: `SynthClient.cancel(request_id, *, still_wanted=None, deadline=None)`
  entirely unchanged; `desktop/synth.py` is not edited by this task.
- Preserves: `handle_stop() -> None`, including its existing single
  `free-tts-cancel` dispatch.

### Steps

- [ ] **Step 1: Write the failing parallel-safety regression.** Add this class to
  `tests/test_desktop_module.py`, immediately before `class TestErrorExitCleanup:`.
  It pins the bound directly on the worker's cleanup entry point, which is what
  makes it deterministic:

````python
class TestWorkerCleanupIsBounded:
    """A wedged DELETE must not hold the worker past the cleanup deadline."""

    def test_bounded_cleanup_returns_at_the_deadline(self):
        delete_entered = threading.Event()
        release_delete = threading.Event()

        class BlockingClient(_FakeClient):
            def cancel(self, request_id, *, still_wanted=None, deadline=None):
                delete_entered.set()
                assert release_delete.wait(5)
                return super().cancel(
                    request_id, still_wanted=still_wanted, deadline=deadline
                )

        engine, _ = _engine(client=BlockingClient())
        generation = module._GenerationToken()
        generation.cancelled.set()
        generation.add_request("req-1")
        generation.add_request("req-2")

        try:
            started = time.monotonic()
            engine._bounded_cancel_outstanding(generation)
            elapsed = time.monotonic() - started

            assert delete_entered.wait(1), "cleanup never entered the DELETE"
            assert elapsed < module._CLEANUP_DEADLINE_SECONDS + 1.0
        finally:
            release_delete.set()
````

  Run it and watch it fail with `AttributeError` on
  `_bounded_cancel_outstanding`. Record the exact failure.

- [ ] **Step 2: Revert the nested delivery thread.** In `_cancel_requests`, remove
  the inner `deliver()` closure, the `free-tts-cancel` thread it starts, the
  `helper.join(...)`, and the `if not request_ids: return` guard added in
  `7d26977`. Restore the plain synchronous loop, keeping **only** the shared
  deadline source:

```python
    def _cancel_requests(
        self, generation: _GenerationToken, request_ids: list[str]
    ) -> None:
        deadline = generation.cleanup_deadline()
        for request_id in request_ids:
            self._client.cancel(  # type: ignore[attr-defined]
                request_id,
                still_wanted=lambda: self._cancellation_still_wanted(
                    generation
                ),
                deadline=deadline,
            )
```

  Leave `cleanup_deadline()`, `begin_cleanup()`, and `_cancel_outstanding`
  untouched. Do not edit `desktop/synth.py`.

- [ ] **Step 3: Bound the worker's cleanup wait.** In `_speak_worker`'s `finally`,
  replace `self._cancel_outstanding(generation)` with
  `self._bounded_cancel_outstanding(generation)`, and add the method immediately
  above `_cancel_outstanding`:

```python
    def _bounded_cancel_outstanding(
        self, generation: _GenerationToken
    ) -> None:
        """Clean up without holding the terminal event past the deadline.

        An admitted DELETE cannot be interrupted: ``urllib``'s timeout is a
        per-read inactivity limit, so a trickling response outlives any absolute
        budget. Delivery therefore runs on a thread this method owns and the
        worker stops waiting at the generation's cleanup deadline. The helper
        holds no protocol lock and no generation state, so it cannot emit output
        or affect a later utterance.
        """
        deadline = generation.cleanup_deadline()
        helper = threading.Thread(
            target=self._cancel_outstanding,
            args=(generation,),
            name="free-tts-cleanup",
            daemon=True,
        )
        helper.start()
        helper.join(max(0.0, deadline - time.monotonic()))
```

  `handle_stop` keeps calling `_cancel_requests` directly on its existing
  `free-tts-cancel` thread, so cancellation delivery stays observable to
  `wait_idle()`.

- [ ] **Step 4: Confirm the regression passes and the flake is gone.** Run the new
  test, then the protected test **20 times in isolated parallel processes** and
  record the tally. Expected, from controller verification of this exact shape:

```text
delete_entered=True worker_cleanup_returned_in=1.000s budget=1.0s bounded=True
protected-test failures: 0 / 20
```

  A single green run is not sufficient evidence for this task.

- [ ] **Step 5: Prove the bound is falsifiable.** In a throwaway `/tmp` archive of
  your commit, never in the worktree, replace the `helper.join(...)` line with
  `helper.join()` and run the new regression. It must fail. Confirm the mutation
  actually applied and that the failure is the asserted bound rather than an
  error such as `AttributeError` or a name error; a mutant that errors proves
  nothing. Report the observed output.

- [ ] **Step 6: Verify the whole suite and the protected invariants.** Run:
  - `.venv/bin/python -m pytest tests/test_desktop_module.py tests/test_desktop_synth.py -q`
  - `.venv/bin/python -m pytest -q` — expect 466 plus your new test, no failures,
    no new skips.
  - These must pass unchanged: `test_stop_cancels_outstanding_requests`,
    `test_health_to_post_race_restarts_refreshes_and_retries_once`,
    `test_pause_boundary_wins_before_retry_reservation`,
    `test_pause_emits_at_boundary_while_lookahead_is_wedged`,
    `test_delete_before_registration_is_retried_until_it_lands`,
    `test_pause_retries_lookahead_cancel_after_registration_handoff`,
    and the whole `TestRecoveryIsCancellable` and `TestErrorExitCleanup` classes.

- [ ] **Step 7: Commit.** Run `git diff --check`, inspect `git status --porcelain`
  and `git diff --stat`, confirm only `desktop/module.py` and
  `tests/test_desktop_module.py` changed, then commit as
  `fix(desktop): bound cleanup on the worker path` and report the SHA.
