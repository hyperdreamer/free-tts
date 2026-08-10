# Desktop TTS Cancellation Deadlines Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Make STOP and PAUSE reclaim the speech worker within a bounded time by
removing output I/O from the cancellation critical path, bounding the waits the
worker performs, and giving cancellation cleanup one shared deadline.

**Architecture:** Four independent corrections in `desktop/`. Task 1 stops holding
the engine lock across protocol output so cancellation can always record intent.
Task 2 stops the terminal event from waiting on executor drain and replaces the
unbounded `future.result()` with abort-aware polling. Task 3 gives mid-utterance
backend recovery one absolute deadline that is re-checked between steps. Task 4
records cleanup intent for internal error exits and shares one handoff deadline
across every request ID.

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
  boundaries, single-process Waitress, and all installer guarantees.
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
- `tests/test_desktop_module.py::TestBackendRecovery::test_health_to_post_race_restarts_refreshes_and_retries_once`
  must keep passing unchanged: mid-utterance recovery stays functional when no
  STOP or PAUSE arrives.
- Concurrency regressions order events with `threading.Event` or an injected
  clock. Never use `time.sleep` to order a race, and never assert on wall-clock
  durations that depend on machine speed.
- Run tests with `.venv/bin/python -m pytest` from the repository root. The
  baseline is 451 passed; a different total is acceptable only with no failures,
  no errors, and no new skips.
- Do not add a runtime dependency. Do not touch the browser frontend, the Chrome
  extension, or `server.py`.
- Out of scope for this plan, and not to be implemented even if noticed:
  nonblocking stdout via `poll`/`select`, HTTP response body size caps, logging
  budgets, PCM slab processing, chunk resizing, and making `fcntl.flock`
  interruptible.

## Task 1: Let cancellation record intent without waiting on output

**Implementer tier:** Frontier

**Problem:** `SpeechEngine._emit` holds `self._lock` while calling into
`ProtocolIO`, and `ProtocolIO` holds its own lock across `write()` and `flush()`.
`handle_stop` and `handle_pause` need that same `self._lock`. When the Speech
Dispatcher peer stops reading stdout, a controller probe observed
`handle_stop()` failing to return within 1.5s and `generation.cancelled` never
being set at all. Cancellation cannot even be recorded, so this is worse than a
latency defect: the worker never learns it should stop.

The fix separates two things the lock currently conflates. Deciding whether a
generation may still write, and marking a generation cancelled, must not queue
behind an in-progress write. Suppression of post-STOP output must not regress:
that is the entire purpose of the current design.

**Files:**
- Modify: `desktop/module.py:64-99, 243-268, 313-330`
- Test: `tests/test_desktop_module.py`

**Interfaces:**
- Produces: `_GenerationToken.cancel() -> None`, setting `cancelled` without
  acquiring the engine lock.
- Produces: `SpeechEngine._emit_lock: threading.Lock`, an output-serialisation
  lock distinct from `self._lock`.
- Preserves: `SpeechEngine._emit(generation, action, *, allow_cancelled=False) -> bool`
  with its existing signature, return contract, and suppression semantics.
- Preserves: `handle_stop() -> None` and `handle_pause() -> None` signatures.
- Consumes: `_GenerationToken.cancelled` and `pause_requested`, unchanged.

### Steps

- [ ] **Step 1: Write the failing convoy regressions.** Add this class to
  `tests/test_desktop_module.py`, immediately before `class TestPauseFallback:`.

```python
class TestCancellationUnderBlockedOutput:
    """A peer that stops reading stdout must not block cancellation intent."""

    def _blocked_engine(self, write_entered, release_write):
        class BlockingIO(_FakeIO):
            def send_audio(self, pcm, sample_rate=24000):
                write_entered.set()
                assert release_write.wait(5)
                super().send_audio(pcm, sample_rate)

        fake_io = BlockingIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=lambda mp3, ffmpeg_path, sample_rate, cancel: b"\x01\x00" * 8,
        )
        return engine, fake_io

    def test_stop_records_intent_while_a_write_is_blocked(self):
        write_entered = threading.Event()
        release_write = threading.Event()
        returned = threading.Event()
        engine, _fake_io = self._blocked_engine(write_entered, release_write)

        engine.handle_speak(SSML)
        assert write_entered.wait(3)
        generation = engine._generation

        threading.Thread(
            target=lambda: (engine.handle_stop(), returned.set()),
            daemon=True,
        ).start()
        try:
            assert returned.wait(2), "handle_stop blocked behind a stdout write"
            assert generation.cancelled.is_set()
        finally:
            release_write.set()
            engine.wait_idle(3)

    def test_pause_records_intent_while_a_write_is_blocked(self):
        write_entered = threading.Event()
        release_write = threading.Event()
        returned = threading.Event()
        engine, _fake_io = self._blocked_engine(write_entered, release_write)

        engine.handle_speak(TWO_CHUNK_SSML)
        assert write_entered.wait(3)
        generation = engine._generation

        threading.Thread(
            target=lambda: (engine.handle_pause(), returned.set()),
            daemon=True,
        ).start()
        try:
            assert returned.wait(2), "handle_pause blocked behind a stdout write"
            assert generation.pause_requested.is_set()
        finally:
            release_write.set()
            engine.wait_idle(3)

    def test_audio_after_stop_is_still_suppressed(self):
        """The convoy fix must not reopen the post-STOP output race."""
        entered_decode = threading.Event()
        release_decode = threading.Event()

        class TimelineIO(_FakeIO):
            def send_audio(self, pcm, sample_rate=24000):
                self.lines.append("AUDIO")
                super().send_audio(pcm, sample_rate)

        def blocked_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            entered_decode.set()
            assert release_decode.wait(3)
            return b"\x01\x00" * 8

        fake_io = TimelineIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=blocked_decoder,
        )
        engine.handle_speak(SSML)
        assert entered_decode.wait(2)

        engine.handle_stop()
        fake_io.lines.append("STOP_RETURNED")
        release_decode.set()
        assert engine.wait_idle(3)

        boundary = fake_io.lines.index("STOP_RETURNED")
        assert not any(
            line in {"701 BEGIN", "AUDIO"} or line.startswith("700:")
            for line in fake_io.lines[boundary + 1 :]
        )
        assert fake_io.lines.count("703 STOP") == 1
```

- [ ] **Step 2: Run them and confirm the first two fail for the right reason.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k CancellationUnderBlockedOutput -q
```

Expected: `test_stop_records_intent_while_a_write_is_blocked` and
`test_pause_records_intent_while_a_write_is_blocked` fail on the `returned.wait(2)`
assertion, because both handlers are queued behind the engine lock held across the
blocked write. `test_audio_after_stop_is_still_suppressed` passes already; it is
the guard that the fix does not regress suppression.

- [ ] **Step 3: Give the token a lock-free cancel.** In `_GenerationToken`, add
  this method after `__init__`:

```python
    def cancel(self) -> None:
        """Mark this generation cancelled without taking the engine lock."""
        self.cancelled.set()
```

- [ ] **Step 4: Split output serialisation from state.** In `SpeechEngine.__init__`,
  beside its `self._lock = threading.Lock()` at `desktop/module.py:139` — not the
  identical line at `desktop/module.py:72`, which belongs to `_GenerationToken` —
  add:

```python
        # Serialises protocol output. Held across writes, so it must never be
        # required by STOP or PAUSE to record cancellation intent.
        self._emit_lock = threading.Lock()
```

Replace `_emit` with a version that decides under the state lock, releases it, and
then writes under the output lock:

```python
    def _emit(
        self,
        generation: _GenerationToken,
        action: Callable[[], None],
        *,
        allow_cancelled: bool = False,
    ) -> bool:
        """Check generation state, then emit without holding the state lock.

        Output is serialised by ``_emit_lock``. The state lock is released before
        writing so a peer that has stopped reading stdout cannot prevent STOP or
        PAUSE from recording intent. Post-STOP suppression is preserved by
        re-checking under ``_emit_lock``: a writer that lost the race to STOP
        observes the cancellation and writes nothing.
        """
        with self._lock:
            if generation is not self._generation:
                return False
            if generation.cancelled.is_set() and not allow_cancelled:
                return False
        with self._emit_lock:
            with self._lock:
                if generation is not self._generation:
                    return False
                if generation.cancelled.is_set() and not allow_cancelled:
                    return False
            action()
            return True
```

The re-check inside `_emit_lock` is what preserves suppression. Ordering is
`_emit_lock` then `self._lock`, and it is acquired in that order nowhere else, so
no cycle is introduced. Never call `action()` while holding `self._lock`.

- [ ] **Step 5: Take cancellation off the output path.** In `handle_stop`, replace
  `generation.cancelled.set()` with `generation.cancel()`. The surrounding
  `with self._lock` block that reads `self._worker` and `self._generation` stays as
  it is: it performs no output and is already short.

Leave `handle_pause` structurally unchanged. Once `_emit` no longer writes under
`self._lock`, its existing short critical section can no longer queue behind a
write.

- [ ] **Step 6: Run the new class, the engine file, then the whole suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k CancellationUnderBlockedOutput -q
.venv/bin/python -m pytest tests/test_desktop_module.py -q
.venv/bin/python -m pytest -q
```

Expected: all pass, with no failures, no errors, and no new skips. The existing
`test_stop_suppresses_pending_output` and
`test_pause_retries_lookahead_cancel_after_registration_handoff` must still pass
untouched.

- [ ] **Step 7: Prove the suppression re-check is load-bearing.** In a scratch
  archive under `/tmp`, delete only the inner re-check block inside `_emit_lock`
  (the second `with self._lock:` and its two `return False` branches), then run
  `test_audio_after_stop_is_still_suppressed`. It must fail, showing the re-check
  is what prevents post-STOP audio rather than incidental timing. Do not mutate
  the active worktree; remove the scratch copy afterwards.

- [ ] **Step 8: Commit.**

```bash
git diff --check
git status --short
git add desktop/module.py tests/test_desktop_module.py
git commit -m "fix(desktop): record cancellation without waiting on output"
```

## Task 2: Bound the worker's own waits after cancellation

**Implementer tier:** Frontier

**Problem:** Two waits in `_speak_worker` have no bound. `mp3 = future.result()`
at `desktop/module.py:376` waits forever, so a STOP arriving while a POST is in
flight cannot reclaim the worker until the HTTP call ends. Separately, the worker
runs inside `with ThreadPoolExecutor(...)`, whose `__exit__` calls
`shutdown(wait=True)`, and the terminal event is emitted only after that block
exits. A running lookahead future cannot be cancelled by `Future.cancel()`, so
`703 STOP` and `704 PAUSE` are withheld until the lookahead finishes on its own.
Controller probes observed both: STOP returned while the worker stayed alive with
no `703 STOP`, and executor shutdown was entered with no `704 PAUSE`.

These are why the audit's worst-case chains reach hundreds of seconds. The
correction is to make the worker stop waiting once cancellation is decided,
while leaving normal synthesis timing untouched.

**Files:**
- Modify: `desktop/module.py:60-62, 335-462`
- Test: `tests/test_desktop_module.py`

**Interfaces:**
- Produces: `_CANCEL_DRAIN_SECONDS: float = 1.25`, the worker's post-cancellation
  wait budget.
- Produces: `_FUTURE_POLL_INTERVAL: float = 0.05`, the completion poll cadence.
- Produces: `SpeechEngine._await_chunk(generation, future) -> bytes`, which returns
  the chunk's MP3 bytes or raises `Cancelled` when the generation is abandoned.
- Consumes: `_should_abort(generation)` and `_reach_pause_boundary(generation)`
  from Task 1's module state, unchanged.
- Preserves: one-chunk lookahead, `_fetch`'s signature, request ownership, and the
  existing outcome values `"end"`, `"stop"`, and `"pause"`.

### Steps

- [ ] **Step 1: Write the failing worker-reclaim regressions.** Add this class to
  `tests/test_desktop_module.py` immediately after
  `class TestCancellationUnderBlockedOutput:`.

```python
class TestWorkerReclaimIsBounded:
    """STOP and PAUSE must not wait on synthesis that nobody wants."""

    def test_stop_reclaims_worker_while_a_post_is_in_flight(self):
        post_entered = threading.Event()
        release_post = threading.Event()

        class WedgedClient(_FakeClient):
            def synthesize(
                self,
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=None,
                reserve_retry=None,
            ):
                self.requests.append((text, voice_name, rate, pitch))
                post_entered.set()
                assert release_post.wait(10)
                return self._audio

        client = WedgedClient()
        engine, fake_io = _engine(client=client)
        engine.handle_speak(SSML)
        assert post_entered.wait(3)

        try:
            engine.handle_stop()
            assert engine.wait_idle(3), "worker waited for the abandoned POST"
            assert fake_io.lines.count("703 STOP") == 1
            assert "702 END" not in fake_io.lines
        finally:
            release_post.set()
            engine.wait_idle(3)

    def test_pause_emits_at_boundary_while_lookahead_is_wedged(self):
        lookahead_entered = threading.Event()
        release_lookahead = threading.Event()

        class WedgedLookaheadClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def synthesize(
                self,
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=None,
                reserve_retry=None,
            ):
                self.calls += 1
                self.requests.append((text, voice_name, rate, pitch))
                if self.calls == 1:
                    return self._audio
                lookahead_entered.set()
                assert release_lookahead.wait(10)
                return self._audio

        client = WedgedLookaheadClient()
        engine, fake_io = _engine(client=client)
        engine.handle_speak(TWO_CHUNK_SSML)
        assert lookahead_entered.wait(3)

        try:
            engine.handle_pause()
            assert engine.wait_idle(3), "worker waited for the discarded lookahead"
            assert fake_io.lines.count("704 PAUSE") == 1
            assert "702 END" not in fake_io.lines
        finally:
            release_lookahead.set()
            engine.wait_idle(3)

    def test_uncancelled_speech_still_waits_for_synthesis(self):
        """The bound applies to abandoned work only, never to wanted speech."""
        engine, fake_io = _engine()
        engine.handle_speak(TWO_CHUNK_SSML)
        assert engine.wait_idle(5)
        assert fake_io.lines[-1] == "702 END"
        assert len(fake_io.audio) == 2
```

- [ ] **Step 2: Run them and confirm the first two fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k WorkerReclaimIsBounded -q
```

Expected: the STOP and PAUSE tests fail on `engine.wait_idle(3)`, because
`future.result()` and executor shutdown both wait for the wedged call.
`test_uncancelled_speech_still_waits_for_synthesis` passes already and guards
against over-correcting.

- [ ] **Step 3: Add the two bounds.** In `desktop/module.py`, next to
  `_WORKER_RECLAIM_SECONDS`, add:

```python
# How long the speech worker may keep waiting for work it has abandoned. It
# applies only after STOP, or after PAUSE reaches its honest boundary; wanted
# synthesis keeps the configured request timeout.
_CANCEL_DRAIN_SECONDS = 1.25
_FUTURE_POLL_INTERVAL = 0.05
```

- [ ] **Step 4: Replace the unbounded future wait.** Add this method to
  `SpeechEngine`, directly after `_should_abort_fetch`:

```python
    def _await_chunk(
        self,
        generation: _GenerationToken,
        future: "Future[bytes]",
    ) -> bytes:
        """Wait for one chunk, giving up when the generation is abandoned.

        A wanted chunk waits as long as synthesis needs. Once the generation is
        abandoned the wait is capped, because the worker cannot cancel a running
        HTTP call and must not withhold the terminal event until it returns.
        """
        deadline: float | None = None
        while True:
            try:
                return future.result(timeout=_FUTURE_POLL_INTERVAL)
            except FuturesTimeout:
                pass
            if self._should_abort(generation):
                if deadline is None:
                    deadline = time.monotonic() + _CANCEL_DRAIN_SECONDS
                elif time.monotonic() >= deadline:
                    raise Cancelled("abandoned chunk exceeded its drain deadline")
```

Add the imports this needs at the top of the module: `import time`, and
`from concurrent.futures import Future, ThreadPoolExecutor` plus
`from concurrent.futures import TimeoutError as FuturesTimeout`. Keep the existing
`ThreadPoolExecutor` import working rather than duplicating it.

In `_speak_worker`, replace `mp3 = future.result()` with:

```python
                    mp3 = self._await_chunk(generation, future)
```

`Cancelled` is already caught by the worker's `except (Cancelled, DecodeCancelled)`
handler, which sets `outcome = "stop"`, so an abandoned chunk needs no new branch.

- [ ] **Step 5: Stop letting executor drain gate the terminal event.** In
  `_speak_worker`, replace the `with ThreadPoolExecutor(...) as pool:` context
  manager with an explicitly owned executor whose shutdown does not block the
  terminal event:

```python
        pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="free-tts-pre")
        try:
            ...  # the existing body, unchanged
        finally:
            if pending is not None:
                pending.cancel()
            self._cancel_outstanding(generation)
            # Queued work is dropped and the terminal event is not held behind a
            # running fetch: the fetch itself is bounded by _await_chunk and the
            # decoder's cancellation, so no thread is abandoned indefinitely.
            pool.shutdown(wait=False, cancel_futures=True)
```

The existing `finally` block that cancels `pending` and calls
`_cancel_outstanding` moves into this `finally`; do not run either twice. Keep the
terminal-event emission after this block, exactly where it is now, so ordering of
`703`/`704`/`702` relative to cleanup is unchanged.

- [ ] **Step 6: Run the new class, the engine file, then the whole suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k WorkerReclaimIsBounded -q
.venv/bin/python -m pytest tests/test_desktop_module.py -q
.venv/bin/python -m pytest -q
```

Expected: all pass with no failures, errors, or new skips. Confirm specifically
that `test_pause_boundary_wins_before_retry_reservation`,
`test_retry_reservation_wins_then_pause_cancels_it`, and
`test_stale_worker_cannot_cancel_a_newer_generation` still pass unchanged.

- [ ] **Step 7: Check for leaked executor threads.** Add this assertion to the end
  of `test_stop_reclaims_worker_while_a_post_is_in_flight`, after
  `release_post.set()` and the final `wait_idle`:

```python
        assert engine.wait_idle(3)
        release_post.set()
        for _ in range(40):
            if not [
                thread
                for thread in threading.enumerate()
                if thread.name.startswith("free-tts-pre")
            ]:
                break
            time.sleep(0.05)
        assert not [
            thread
            for thread in threading.enumerate()
            if thread.name.startswith("free-tts-pre")
        ]
```

Import `time` in the test module if it is not already imported. This polls for the
released prefetch thread to finish rather than sleeping a fixed duration, so it
does not order a race by sleeping; it only bounds how long it will wait for a
thread that has already been released to exit.

- [ ] **Step 8: Commit.**

```bash
git diff --check
git status --short
git add desktop/module.py tests/test_desktop_module.py
git commit -m "fix(desktop): bound abandoned synthesis waits"
```

## Task 3: Give mid-utterance recovery an absolute deadline (F-21)

**Implementer tier:** Frontier

**Problem:** When a POST fails mid-utterance, `SpeechEngine._fetch` calls
`self._controller.ensure_ready()` and `self._refresh_catalog()` synchronously. It
checks cancellation before and after that pair, but neither call receives the
generation, so a STOP or PAUSE arriving inside them is invisible. A controller
probe held recovery and observed the worker still alive with no `704 PAUSE` until
recovery was released manually. With shipped defaults the recovery suffix is
nominally about 59s: two 3s probes, a readiness loop that measured 32.5s against a
declared 30s budget, and a 20s voice fetch.

Recovery itself must keep working, so the correction is a deadline rather than
removal. The deadline is absolute and re-checked between steps, because a
`urllib` `timeout=` is per-read: a trickling body measured 0.970s against a 0.1s
timeout, so lowering numbers alone guarantees nothing.

**Files:**
- Modify: `desktop/module.py:280-295, 498-560`
- Test: `tests/test_desktop_module.py`
- Test: `tests/test_desktop_backend.py`

**Interfaces:**
- Consumes: `_FUTURE_POLL_INTERVAL` and the module-level `import time` added by
  Task 2. Both already exist when this task runs; if either is missing, add it
  rather than duplicating an existing import.
- Produces: `_RECOVERY_DEADLINE_SECONDS: float = 1.0`, the whole-recovery budget
  once cancellation is pending.
- Produces: `SpeechEngine._recover_backend(generation) -> None`, which performs
  readiness plus catalog refresh and raises `Cancelled` when the generation is
  abandoned or the budget is spent.
- Consumes: `BackendController.ensure_ready()` and `VoiceCatalog` exactly as they
  are. This task does not change `desktop/backend.py` or `desktop/synth.py`, and
  must not make `flock` interruptible or slice any HTTP timeout.
- Preserves: `_refresh_catalog() -> VoiceCatalog`, fresh recovery request IDs, and
  the single recovery attempt per failed chunk.

### Steps

- [ ] **Step 1: Write the failing recovery regressions.** Add this class to
  `tests/test_desktop_module.py` immediately after `class TestBackendRecovery:`.

```python
class TestRecoveryIsCancellable:
    """STOP and PAUSE must reclaim the worker during backend recovery."""

    def _recovering_engine(self, recovery_entered, release_recovery):
        from desktop.synth import SynthError

        class RecoveringClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = 0

            def synthesize(
                self,
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=None,
                reserve_retry=None,
            ):
                self.calls += 1
                self.requests.append((text, voice_name, rate, pitch))
                if self.calls == 1:
                    return self._audio
                raise SynthError("backend vanished mid-utterance")

        class WedgedController(_FakeController):
            def ensure_ready(self):
                self.calls += 1
                if self.calls > 1:
                    recovery_entered.set()
                    assert release_recovery.wait(10)

        engine, fake_io = _engine(
            client=RecoveringClient(), controller=WedgedController()
        )
        return engine, fake_io

    def test_stop_during_recovery_reclaims_the_worker(self):
        recovery_entered = threading.Event()
        release_recovery = threading.Event()
        engine, fake_io = self._recovering_engine(
            recovery_entered, release_recovery
        )

        engine.handle_speak(TWO_CHUNK_SSML)
        assert recovery_entered.wait(3)

        try:
            engine.handle_stop()
            assert engine.wait_idle(3), "worker waited for wedged recovery"
            assert fake_io.lines.count("703 STOP") == 1
            assert "702 END" not in fake_io.lines
        finally:
            release_recovery.set()
            engine.wait_idle(3)

    def test_pause_during_recovery_reclaims_the_worker(self):
        recovery_entered = threading.Event()
        release_recovery = threading.Event()
        engine, fake_io = self._recovering_engine(
            recovery_entered, release_recovery
        )

        engine.handle_speak(TWO_CHUNK_SSML)
        assert recovery_entered.wait(3)

        try:
            engine.handle_pause()
            assert engine.wait_idle(3), "worker waited for wedged recovery"
            assert len(fake_io.lines) > 0
            assert fake_io.lines.count("702 END") == 0
            terminal = [
                line for line in fake_io.lines if line in {"703 STOP", "704 PAUSE"}
            ]
            assert len(terminal) == 1
        finally:
            release_recovery.set()
            engine.wait_idle(3)

    def test_recovery_still_succeeds_without_cancellation(self):
        """The deadline must not fire when nobody cancelled."""
        engine, fake_io, client, state = _lifecycle_engine(fail_first_post=True)
        engine.handle_speak(SSML)
        assert engine.wait_idle(5)
        assert state["spawns"] == 1
        assert client.attempts == 2
        assert fake_io.lines[-1] == "702 END"
```

- [ ] **Step 2: Run them and confirm the first two fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k RecoveryIsCancellable -q
```

Expected: the STOP and PAUSE tests fail on `engine.wait_idle(3)` because
`ensure_ready()` is wedged and the worker has no way to abandon it.
`test_recovery_still_succeeds_without_cancellation` passes already.

- [ ] **Step 3: Add the recovery budget constant.** Next to
  `_CANCEL_DRAIN_SECONDS` in `desktop/module.py`, add:

```python
# Whole-recovery budget once STOP or a PAUSE boundary is pending. Recovery that
# nobody has cancelled keeps the configured startup and voice timeouts.
_RECOVERY_DEADLINE_SECONDS = 1.0
```

- [ ] **Step 4: Run recovery on an owned thread with a deadline.** Add this method
  to `SpeechEngine`, immediately after `_refresh_catalog`:

```python
    def _recover_backend(self, generation: _GenerationToken) -> None:
        """Restart the backend and refresh voices, abandonable on cancellation.

        ``ensure_ready`` blocks on a startup file lock, health probes, and process
        launch, and ``voices`` performs one HTTP request; none of them accepts a
        cancellation signal, and a ``urllib`` timeout is per-read rather than a
        whole-call deadline. Recovery therefore runs on a thread this method owns.
        A cancelled generation stops waiting at ``_RECOVERY_DEADLINE_SECONDS``
        while that thread finishes on its own: it holds no protocol lock and no
        generation state, so it cannot emit output or affect a later utterance.
        """
        outcome: dict[str, BaseException] = {}
        done = threading.Event()

        def recover() -> None:
            try:
                self.catalog = None
                self._controller.ensure_ready()  # type: ignore[attr-defined]
                self._refresh_catalog()
            except BaseException as exc:  # noqa: BLE001 - re-raised on the worker
                outcome["error"] = exc
            finally:
                done.set()

        helper = threading.Thread(
            target=recover, name="free-tts-recover", daemon=True
        )
        helper.start()
        deadline: float | None = None
        while not done.wait(_FUTURE_POLL_INTERVAL):
            if self._should_abort_fetch(generation):
                if deadline is None:
                    deadline = time.monotonic() + _RECOVERY_DEADLINE_SECONDS
                elif time.monotonic() >= deadline:
                    raise Cancelled("recovery exceeded its cancellation deadline")
        error = outcome.get("error")
        if error is not None:
            raise error
```

- [ ] **Step 5: Route `_fetch` through it.** In `_fetch`'s `except SynthError:`
  branch, replace these three lines:

```python
            self.catalog = None
            self._controller.ensure_ready()  # type: ignore[attr-defined]
            self._refresh_catalog()
```

with:

```python
            self._recover_backend(generation)
```

Leave both surrounding `_should_abort_fetch` checks, the fresh `retry_id`, and the
`finally` blocks exactly as they are.

- [ ] **Step 6: Record why the helper thread is acceptable here.** The decoder's
  I/O thread is joined unconditionally because it owns a child process and pipes.
  This helper owns neither, so add exactly this comment above the `helper.start()`
  call, and no other commentary:

```python
        # Not joined on the cancelled path by design: unlike the decoder's I/O
        # thread, this one owns no child process, no pipe, and no protocol lock.
```

- [ ] **Step 7: Run the new class, both test files, then the whole suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k RecoveryIsCancellable -q
.venv/bin/python -m pytest tests/test_desktop_module.py tests/test_desktop_backend.py -q
.venv/bin/python -m pytest -q
```

Expected: all pass. `test_health_to_post_race_restarts_refreshes_and_retries_once`
and `test_later_speak_restarts_backend_after_idle_exit` must pass unchanged, and
`desktop/backend.py` must be untouched by this task.

- [ ] **Step 8: Commit.**

```bash
git diff --check
git status --short
git diff --stat
git add desktop/module.py tests/test_desktop_module.py
git commit -m "fix(desktop): bound backend recovery on cancellation"
```

## Task 4: Record cleanup intent and share one handoff deadline (F-20)

**Implementer tier:** Advanced

**Problem:** Two defects in the same cleanup path.

First, `_cancellation_still_wanted` reports user intent: it is true only when
`cancelled` or `pause_requested` is set. On an internal error exit — decode
failure, protocol failure, or an unexpected exception — neither flag is set, so
the bounded DELETE-404 handoff retry gives up on its first miss. A controller
probe failed the current chunk's decode while the lookahead POST was
pre-registration and observed exactly one DELETE 404, after which cleanup stopped
retrying and the lookahead outlived it.

Second, each `SynthClient.cancel` call computes its own 1s handoff deadline and
checks it only after a DELETE returns. One DELETE admitted just under the deadline
can still consume its full 5s timeout, so a single ID approaches 6s, and a
generation owning several IDs can approach 24s of serial cleanup.

**Files:**
- Modify: `desktop/module.py:64-99, 424-442, 565-585`
- Modify: `desktop/synth.py:175-210`
- Test: `tests/test_desktop_module.py`
- Test: `tests/test_desktop_synth.py`

**Interfaces:**
- Produces: `_GenerationToken.cleanup_started: threading.Event`, set before
  cleanup delivers any DELETE.
- Produces: `_GenerationToken.begin_cleanup() -> None`, setting that event without
  taking the engine lock.
- Extends: `SynthClient.cancel(request_id, *, still_wanted=None, deadline=None) -> bool`,
  where `deadline` is an absolute `time.monotonic()` value shared by every ID in
  one cleanup pass. When omitted, behaviour is exactly as today.
- Produces: `_CLEANUP_DEADLINE_SECONDS: float = 1.0` in `desktop/module.py`, the
  whole-pass cleanup budget.
- Consumes: the module-level `import time` in `desktop/module.py` added by Task 2,
  and the existing `import time` in `desktop/synth.py:8`. Do not duplicate either.
- Preserves: `_cancel_outstanding`, `_cancel_requests`, generation-local ownership,
  `take_requests` single-delivery semantics, and the 404-then-200 handoff.

### Steps

- [ ] **Step 1: Write the failing cleanup regressions.** Add this class to
  `tests/test_desktop_module.py` immediately after `class TestRecoveryIsCancellable:`.

```python
class TestErrorExitCleanup:
    """An internal failure must still cancel prefetched backend work."""

    def test_decode_failure_retries_cleanup_across_registration(self):
        from desktop.audio import DecodeError

        lookahead_entered = threading.Event()
        registered = threading.Event()
        allow_registration = threading.Event()
        release_lookahead = threading.Event()

        class HandoffClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.statuses = []
                self.lookahead_id = None

            def synthesize(
                self,
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=None,
                reserve_retry=None,
            ):
                self.calls += 1
                self.requests.append((text, voice_name, rate, pitch))
                if self.calls == 1:
                    return self._audio
                self.lookahead_id = request_id
                lookahead_entered.set()
                assert allow_registration.wait(3)
                registered.set()
                assert release_lookahead.wait(3)
                return self._audio

            def cancel(self, request_id, *, still_wanted=None, deadline=None):
                if not registered.is_set():
                    self.statuses.append(404)
                    allow_registration.set()
                    if still_wanted is None or not still_wanted():
                        return False
                    assert registered.wait(3)
                self.statuses.append(200)
                self.cancelled.append(request_id)
                release_lookahead.set()
                return True

        client = HandoffClient()

        def failing_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            assert lookahead_entered.wait(3)
            raise DecodeError("forced current-chunk decode failure")

        fake_io = _FakeIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            client,
            decoder=failing_decoder,
        )
        engine.handle_speak(TWO_CHUNK_SSML)

        try:
            assert engine.wait_idle(3)
            assert client.statuses == [404, 200]
            assert client.cancelled == [client.lookahead_id]
            assert fake_io.lines.count("703 STOP") == 1
        finally:
            allow_registration.set()
            registered.set()
            release_lookahead.set()
            engine.wait_idle(3)

    def test_cleanup_intent_is_recorded_before_delete(self):
        generation = module._GenerationToken()
        assert generation.cleanup_started.is_set() is False
        generation.begin_cleanup()
        assert generation.cleanup_started.is_set() is True
        assert generation.cancelled.is_set() is False
```

- [ ] **Step 2: Write the failing shared-deadline test.** Add this to
  `tests/test_desktop_synth.py` in `class TestCancel:`.

```python
    def test_shared_deadline_stops_admitting_deletes(self):
        clock = [0.0]
        transport = _Transport(
            [(404, {}, b"{}"), (404, {}, b"{}"), (404, {}, b"{}")]
        )

        def slow_delete(method, url, body, timeout):
            clock[0] += 5.0
            return transport(method, url, body, timeout)

        client = synth.SynthClient(
            _config(), transport=slow_delete, sleep=lambda _s: None
        )
        client._monotonic = lambda: clock[0]

        deadline = clock[0] + 1.0
        assert (
            client.cancel("req1", still_wanted=lambda: True, deadline=deadline)
            is False
        )
        assert len(transport.calls) == 1

        assert (
            client.cancel("req2", still_wanted=lambda: True, deadline=deadline)
            is False
        )
        assert len(transport.calls) == 1
```

- [ ] **Step 3: Run both and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k ErrorExitCleanup -q
.venv/bin/python -m pytest tests/test_desktop_synth.py -k shared_deadline -q
```

Expected: `cleanup_started`/`begin_cleanup` and the `deadline` parameter do not
exist, and the decode-failure test stops after one 404 because cleanup intent is
never recorded.

- [ ] **Step 4: Add cleanup intent to the token.** In `_GenerationToken.__init__`,
  after `self.pause_boundary_reached = threading.Event()`, add:

```python
        self.cleanup_started = threading.Event()
```

and add this method beside `cancel()`:

```python
    def begin_cleanup(self) -> None:
        """Record that cleanup owns this generation's remaining requests.

        Distinct from ``cancelled``: an internal failure abandons the utterance
        without the client having asked for STOP, and cleanup must still complete
        the bounded registration handoff.
        """
        self.cleanup_started.set()
```

- [ ] **Step 5: Make cleanup intent count as wanted.** In
  `_cancellation_still_wanted`, add `cleanup_started` to the disjunction:

```python
    def _cancellation_still_wanted(self, generation: _GenerationToken) -> bool:
        with self._lock:
            return generation is self._generation and (
                generation.cancelled.is_set()
                or generation.pause_requested.is_set()
                or generation.cleanup_started.is_set()
            )
```

- [ ] **Step 6: Record intent before cleanup runs.** In `_cancel_outstanding`, set
  the flag before taking the requests, so any DELETE it issues is already covered:

```python
    def _cancel_outstanding(self, generation: _GenerationToken) -> None:
        generation.begin_cleanup()
        self._cancel_requests(generation, generation.take_requests())
```

- [ ] **Step 7: Share one deadline across the pass.** Add the constant beside
  `_RECOVERY_DEADLINE_SECONDS`:

```python
# Whole-pass budget for cancelling one generation's outstanding request ids.
_CLEANUP_DEADLINE_SECONDS = 1.0
```

and pass it from `_cancel_requests`:

```python
    def _cancel_requests(
        self, generation: _GenerationToken, request_ids: list[str]
    ) -> None:
        deadline = time.monotonic() + _CLEANUP_DEADLINE_SECONDS
        for request_id in request_ids:
            self._client.cancel(  # type: ignore[attr-defined]
                request_id,
                still_wanted=lambda: self._cancellation_still_wanted(
                    generation
                ),
                deadline=deadline,
            )
```

- [ ] **Step 8: Honour the deadline before each DELETE.** In `desktop/synth.py`,
  add an injectable clock in `SynthClient.__init__` beside `self._sleep`:

```python
        self._monotonic = time.monotonic
```

Then change `cancel` to accept and respect the shared deadline. Replace its
signature and the deadline computation:

```python
    def cancel(
        self,
        request_id: str,
        *,
        still_wanted: Callable[[], bool] | None = None,
        deadline: float | None = None,
    ) -> bool:
```

Inside, replace `deadline = time.monotonic() + _CANCEL_HANDOFF_SECONDS` with:

```python
        own_deadline = self._monotonic() + _CANCEL_HANDOFF_SECONDS
        limit = own_deadline if deadline is None else min(own_deadline, deadline)
```

Check the limit **before** each DELETE rather than only after one, and use `limit`
in place of the old `deadline`:

```python
        while True:
            if self._monotonic() >= limit:
                logger.debug(
                    "Cancel for %s ran out of handoff time", request_id
                )
                return False
            try:
                status, _headers, _body = self._transport(
                    "DELETE", url, None, _CANCEL_TIMEOUT
                )
            except OSError as exc:
                logger.debug(
                    "Cancel for %s could not be delivered: %s", request_id, exc
                )
                return False
            if status != 404:
                return True
            if still_wanted is None or not still_wanted():
                return False
            if self._monotonic() >= limit:
                logger.debug(
                    "Cancel for %s was never registered by the backend", request_id
                )
                return False
            self._sleep(_CANCEL_RETRY_INTERVAL)
```

- [ ] **Step 9: Update fake cancel signatures.** Every fake `cancel` in the test
  suite must accept `deadline=None`. Find them all and update only their
  signatures:

```bash
rg -n "def cancel\(" tests/
```

- [ ] **Step 10: Run the focused files, then the whole suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_synth.py tests/test_desktop_module.py -q
.venv/bin/python -m pytest -q
```

Expected: all pass, no new skips. `test_delete_before_registration_is_retried_until_it_lands`
and `test_pause_retries_lookahead_cancel_after_registration_handoff` must still
pass unchanged.

- [ ] **Step 11: Commit.**

```bash
git diff --check
git status --short
git add desktop/module.py desktop/synth.py \
  tests/test_desktop_module.py tests/test_desktop_synth.py
git commit -m "fix(desktop): cancel prefetched work after internal failures"
```
