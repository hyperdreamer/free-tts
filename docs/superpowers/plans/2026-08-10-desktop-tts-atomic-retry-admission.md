# Desktop TTS Atomic Retry Admission Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Close residual `F-17` by atomically ordering a request's retry admission
against STOP and the honest PAUSE boundary, so no retry POST can begin from a
stale abort decision.

**Architecture:** `SynthClient` retains HTTP retry mechanics but accepts a
request-scoped `reserve_retry` callback for attempt 2. `SpeechEngine` implements
that callback under the same lock used by STOP and `_reach_pause_boundary`, and
admits only a request ID the current generation still owns. The successful
reservation is the linearization point: if PAUSE/STOP wins, no POST starts; if
reservation wins, the owned request remains visible to existing DELETE handoff.

**Tech Stack:** Python 3.11+ standard library only, `threading.Event` for
deterministic concurrency tests, `pytest`.

## Global Constraints

- `desktop/` must import only the Python standard library. It must never import
  `server.py` or Flask.
- Standard output carries only Speech Dispatcher protocol traffic. All logging
  goes to standard error.
- Preserve exact SSIP response codes/events, one-chunk lookahead, real-index-mark
  PAUSE semantics, no-future-mark fallback, and one `704 PAUSE` with no `702 END`
  for a paused utterance.
- Preserve generation-local request ownership, duplicate-ID rejection, fresh IDs
  for ambiguous backend recovery, bounded DELETE-404 registration handoff, backend
  lifecycle recovery, decoder process/thread ownership, and every installer fix.
- Never modify `tests/test_extension_split_sentences.py` or
  `tests/test_media_session.py`.
- Never weaken, skip, delete, or make timing-dependent an existing test. Updating
  fake method signatures to match the extended client interface is allowed; changing
  assertions to make failures disappear is not.
- Concurrency regressions use deterministic barriers such as `threading.Event`.
  Never use `time.sleep` to order events.
- Run tests with `.venv/bin/python -m pytest` from the repository root. The baseline
  is 448 passed; a different total is acceptable only with no failures, errors, or
  new skips.
- This fresh run follows terminal run
  `.superpowers/sdd/2026-08-10-desktop-tts-engine-reload-provenance`. Never edit or
  reopen that run's terminal state.
- Final Frontier review covers the original merge base
  `b0127255c700ce99608709c4fe123a1afe6e4768` through the new HEAD and reconciles
  every carried finding `F-1` through `F-19`, `T10-F-1`, `T10-F-2`, `T11-F-1`, and
  `T2-F-1`.

## Task 1: Linearize retry admission with PAUSE and STOP (F-17)

**Implementer tier:** Frontier

**Problem:** Commit `2861207` made Retry-After waits interruptible and added
`pause_boundary_reached`, but attempt 2 still starts after an ordinary
`should_abort()` boolean check. That check releases `SpeechEngine._lock` before
`SynthClient` enters transport. PAUSE can acquire the lock, record its boundary,
and begin cleanup in that gap; the stale `False` then launches the retry POST. A
controller probe and fresh Frontier re-review both produced
`pause_boundary=True`, `transport_calls=2`, and successful audio from a POST that
started after PAUSE won.

**Files:**
- Modify: `desktop/synth.py:108-173`
- Modify: `desktop/module.py:64-99, 465-545`
- Test: `tests/test_desktop_synth.py:83-199`
- Test: `tests/test_desktop_module.py:60-100, 450-780`

**Interfaces:**
- Extends `SynthClient.synthesize(text: str, voice_name: str, rate: str,
  pitch: str, request_id: str, should_abort: Callable[[], bool] | None = None,
  reserve_retry: Callable[[], bool] | None = None) -> bytes`.
- Produces `_GenerationToken.owns_request(request_id: str) -> bool`, a
  lock-protected ownership query.
- Produces `SpeechEngine._reserve_retry(generation: _GenerationToken,
  request_id: str) -> bool`, whose successful return is the retry-start
  linearization point.
- `_fetch` passes callbacks bound to the exact request ID for both the original
  request and the fresh backend-recovery request.
- Preserves `SynthClient.cancel(request_id, *, still_wanted=None) -> bool` and all
  existing bounded handoff behavior unchanged.

### Steps

- [ ] **Step 1: Add a direct failing admission test.** In
  `tests/test_desktop_synth.py`, add this test to `TestSynthesize` after
  `test_stop_interrupts_retry_after_wait`:

```python
    def test_denied_retry_reservation_prevents_second_post(self):
        reservations = []
        client, transport = self._client(
            [(503, {"Retry-After": "0"}, b"{}")]
        )

        with pytest.raises(synth.Cancelled, match="retry"):
            client.synthesize(
                "hi",
                "v",
                "+0%",
                "+0Hz",
                "req1",
                should_abort=lambda: False,
                reserve_retry=lambda: reservations.append("attempt-2") or False,
            )

        assert reservations == ["attempt-2"]
        assert len(transport.calls) == 1
```

- [ ] **Step 2: Add the PAUSE-wins engine regression.** In
  `tests/test_desktop_module.py`, add a test beside
  `test_pause_interrupts_lookahead_retry_after_at_boundary`. Use the real
  `synth.SynthClient` and this deterministic structure:

```python
    def test_pause_boundary_wins_before_retry_reservation(self):
        reservation_entered = threading.Event()
        allow_reservation = threading.Event()
        pause_boundary_reached = threading.Event()
        retry_post_started = threading.Event()
        decode_entered = threading.Event()
        release_decode = threading.Event()
        pause_emitted = threading.Event()

        class TimelineIO(_FakeIO):
            def event_pause(self):
                super().event_pause()
                pause_emitted.set()

        class RetryTransport:
            def __init__(self):
                self._lock = threading.Lock()
                self.post_calls = 0

            def __call__(self, method, url, body, timeout):
                if method == "GET":
                    return 200, {}, json.dumps(PAYLOAD).encode("utf-8")
                if method == "DELETE":
                    return 200, {}, b'{"cancelled": true}'
                assert method == "POST"
                with self._lock:
                    self.post_calls += 1
                    call = self.post_calls
                if call == 1:
                    return 200, {}, b"current-mp3"
                if call == 2:
                    return 503, {"Retry-After": "0"}, b"{}"
                retry_post_started.set()
                return 200, {}, b"unexpected-retry"

        def gated_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            decode_entered.set()
            assert release_decode.wait(3)
            return b"\x01\x00" * 8

        class ReservationGatedEngine(module.SpeechEngine):
            def _reserve_retry(self, generation, request_id):
                reservation_entered.set()
                assert allow_reservation.wait(3)
                return super()._reserve_retry(generation, request_id)

            def _reach_pause_boundary(self, generation):
                reached = super()._reach_pause_boundary(generation)
                pause_boundary_reached.set()
                return reached

        transport = RetryTransport()
        client = synth.SynthClient(
            settings.DEFAULTS, transport=transport, sleep=lambda _seconds: None
        )
        fake_io = TimelineIO()
        engine = ReservationGatedEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            client,
            decoder=gated_decoder,
        )
        engine.handle_speak(TWO_CHUNK_SSML)
        assert reservation_entered.wait(2)
        assert decode_entered.wait(2)

        try:
            engine.handle_pause()
            release_decode.set()
            assert pause_boundary_reached.wait(2)
            allow_reservation.set()
            assert pause_emitted.wait(1)
            assert engine.wait_idle(1)
            assert retry_post_started.is_set() is False
            assert transport.post_calls == 2
            assert len(fake_io.audio) == 1
            assert fake_io.lines.count("704 PAUSE") == 1
            assert "702 END" not in fake_io.lines
        finally:
            release_decode.set()
            pause_boundary_reached.set()
            allow_reservation.set()
            engine.wait_idle(3)
```

This test must block **before** `_reserve_retry` takes the engine lock, then let
`_reach_pause_boundary` win that same lock. It must never manually release a retry
POST; no retry POST is allowed to begin.

- [ ] **Step 3: Add the retry-wins composition regression.** Add a second
  engine-level test using real `SynthClient`. It must establish the opposite legal
  ordering:

  1. current POST returns 200 and current decode waits on an Event;
  2. lookahead POST returns 503 with Retry-After 0;
  3. an overridden `_reserve_retry` calls `super()` first, asserts True, sets
     `retry_reserved`, then waits on `allow_transport` before returning;
  4. after `retry_reserved`, request PAUSE, release current decode, and wait until
     `_reach_pause_boundary` records the boundary;
  5. release `allow_transport`, letting attempt 2 enter a transport that coordinates
     registration with Events;
  6. DELETE returns 404 before registration, opens registration, retries to 200,
     and releases POST as 499/cancelled;
  7. assert statuses `[404, 200]`, exactly one retry POST, one `704 PAUSE`, no
     `702 END`, and prompt worker exit.

Use `threading.Event` for `retry_reserved`, `allow_transport`, `registered`,
`allow_registration`, and `post_cancelled`. Adapt the existing
`test_pause_retries_lookahead_cancel_after_registration_handoff` transport timeline
rather than using sleeps. This proves the reservation is not merely a denial switch:
when retry wins, cleanup observes and cancels the already-owned request.

- [ ] **Step 4: Run the new tests and confirm RED for the intended interface and
  race.**

```bash
.venv/bin/python -m pytest \
  tests/test_desktop_synth.py::TestSynthesize::test_denied_retry_reservation_prevents_second_post \
  tests/test_desktop_module.py::TestCancellationOwnership::test_pause_boundary_wins_before_retry_reservation \
  tests/test_desktop_module.py::TestCancellationOwnership::test_retry_reservation_wins_then_pause_cancels_it \
  -q
```

Expected: failures because `SynthClient.synthesize` has no `reserve_retry`
parameter, `SpeechEngine` has no `_reserve_retry`, and the current stale boolean
path can start attempt 2.

- [ ] **Step 5: Extend the HTTP client interface and linearize attempt 2.** In
  `desktop/synth.py`, extend the method signature:

```python
    def synthesize(
        self,
        text: str,
        voice_name: str,
        rate: str,
        pitch: str,
        request_id: str,
        should_abort: Callable[[], bool] | None = None,
        reserve_retry: Callable[[], bool] | None = None,
    ) -> bytes:
```

At the top of the retry loop, replace the ordinary first check with attempt-aware
admission:

```python
        for attempt in (1, 2):
            if attempt == 2 and reserve_retry is not None:
                if not reserve_retry():
                    raise Cancelled("retry no longer admitted")
            elif should_abort is not None and should_abort():
                raise Cancelled("aborted before request")
```

Keep `_wait_before_retry` unchanged: its sliced checks still make STOP and a PAUSE
boundary interrupt Retry-After promptly. The callback above is authoritative for
the final attempt-2 admission. Do not add another unlocked check between a
successful reservation and `_transport`; a successful reservation intentionally
means retry won the ordering and cleanup must cancel it as owned work.

- [ ] **Step 6: Add request ownership and engine retry reservation.** In
  `_GenerationToken`, add:

```python
    def owns_request(self, request_id: str) -> bool:
        with self._lock:
            return request_id in self.requests
```

In `SpeechEngine`, immediately after `_reach_pause_boundary`, add:

```python
    def _reserve_retry(
        self,
        generation: _GenerationToken,
        request_id: str,
    ) -> bool:
        """Atomically admit attempt 2 against STOP and the PAUSE boundary."""
        with self._lock:
            return (
                generation is self._generation
                and not generation.cancelled.is_set()
                and not generation.pause_boundary_reached.is_set()
                and generation.owns_request(request_id)
            )
```

This engine-lock acquisition is the linearization point. The nested generation
lock is safe because existing request methods release their lock before calling
anything that acquires `SpeechEngine._lock`; do not introduce the inverse order.

- [ ] **Step 7: Bind each synthesis call to its exact request ID.** In both
  `_client.synthesize` calls inside `_fetch`, add the callback:

```python
                reserve_retry=lambda: self._reserve_retry(
                    generation, request_id
                ),
```

For the backend-recovery call, bind `retry_id`, not `request_id`:

```python
                    reserve_retry=lambda: self._reserve_retry(
                        generation, retry_id
                    ),
```

The default-argument closure is unnecessary because each lambda is consumed
synchronously within its own `_client.synthesize` call and its ID is not reassigned.

- [ ] **Step 8: Update test adapters to the extended interface.** Every fake or
  wrapper `synthesize` in `tests/test_desktop_module.py` must accept
  `reserve_retry=None` after `should_abort=None`. For wrappers that forward into a
  real client, forward it by keyword:

```python
        return original(
            text,
            voice_name,
            rate,
            pitch,
            request_id,
            should_abort=should_abort,
            reserve_retry=reserve_retry,
        )
```

Use this search to prove no adapter was missed:

```bash
rg -n "def synthesize\(" tests/test_desktop_module.py
```

Do not change any existing assertion while updating signatures.

- [ ] **Step 9: Run focused tests and the complete suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_synth.py tests/test_desktop_module.py -q
.venv/bin/python -m pytest tests/test_desktop_audio.py tests/test_desktop_synth.py tests/test_desktop_module.py -q
.venv/bin/python -m pytest -q
git diff --check
```

Expected: all pass, with no failures, errors, or new skips.

- [ ] **Step 10: Prove the PAUSE-wins regression is falsifiable.** In a scratch
  archive outside the active worktree, remove only the two `reserve_retry=`
  arguments from `_fetch` (or otherwise restore the current stale-check path) and
  run
  `test_pause_boundary_wins_before_retry_reservation`. It must fail because the
  retry POST starts or the reservation barrier is never reached. Do not mutate the
  active worktree for this probe.

- [ ] **Step 11: Inspect and commit.**

```bash
git diff --check
git status --short
git diff --stat
git add desktop/synth.py desktop/module.py \
  tests/test_desktop_synth.py tests/test_desktop_module.py
git commit -m "fix(desktop): atomically admit synthesis retries"
```
