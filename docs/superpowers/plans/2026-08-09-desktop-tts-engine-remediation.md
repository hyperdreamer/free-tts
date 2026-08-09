# Desktop TTS Engine Residual Remediation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.

**Goal:** Close the six load-bearing residual findings (F-11 through F-16) that a
Frontier final re-review found in the desktop TTS engine, so STOP, PAUSE,
cancellation, decoder cleanup, and malformed configuration all behave correctly.

**Architecture:** Five independent remediations on the existing branch. The Flask
cancellation registry gains identity-safe ownership; the Speech Dispatcher adapter
gains per-generation request ownership plus a bounded cancellation handoff; PAUSE
gains a chunk-boundary fallback; the ffmpeg decoder becomes an owned, cancellable
subprocess; and `backend_url` is validated so malformed values surface as protocol
errors instead of crashing the command loop.

**Tech Stack:** Python 3.11+, Flask, Quart-style async views, Waitress, pytest,
`urllib`, `subprocess`, `threading`, `asyncio`, ffmpeg.

## Global Constraints

- `desktop/` must import only the Python standard library. It must never import
  `server.py` or `flask`.
- Standard output carries only Speech Dispatcher protocol traffic. All logging goes
  to standard error.
- Never modify `tests/test_extension_split_sentences.py` or
  `tests/test_media_session.py`.
- Never weaken, skip, delete, or make timing-dependent any existing test. Changing a
  test call site because a function signature changed is allowed; changing an
  assertion to make a failure disappear is not.
- Prove every concurrency behavior with deterministic barriers such as
  `threading.Event`, never with `time.sleep` as the ordering mechanism.
- Run tests with `.venv/bin/python -m pytest` from the repository root.
- The full suite must pass with no failures and no errors at the end of every task.
  It reports 378 passed at the time of writing; a different total is fine as long as
  nothing fails, errors, or is newly skipped.
- Keep the existing Speech Dispatcher response codes exactly as they are:
  `299/399/202/200/301/203/207/210/300/302/303/304`, and events `701 BEGIN`,
  `702 END`, `703 STOP`, `704 PAUSE`, `700-<mark>` followed by `700 INDEX MARK`.
- Do not add a runtime dependency, and do not touch the browser frontend or the
  Chrome extension.

## Task 1: Identity-safe cancellation registry (F-14)

**Implementer tier:** Advanced

**Problem:** `_register_cancel_token` silently overwrites a live token that already
uses the same `request_id`, and `_release_cancel_token` pops by string id alone. Two
live requests can therefore share one id, the first one's completion deletes the
second one's token, and the second becomes uncancellable while still holding a
concurrency slot.

**Files:**
- Modify: `server.py:285-310`
- Modify: `server.py:1108-1160`
- Test: `tests/test_server.py`

**Interfaces:**
- Produces: `DuplicateRequestId(Exception)` in `server.py`.
- Produces: `_register_cancel_token(request_id: str) -> _CancellationToken`, now
  raising `DuplicateRequestId` when that id is already live.
- Produces: `_release_cancel_token(request_id: str, token: _CancellationToken) -> None`,
  which removes the registry entry only when the stored token *is* `token`.
- Consumes: existing `_CANCEL_REGISTRY`, `_CANCEL_LOCK`, `_CancellationToken`, and
  `_cancel_request` from `server.py`, all unchanged in behavior.

### Steps

- [ ] **Step 1: Write the failing registry tests.** Append this class to
  `tests/test_server.py`, immediately before `class TestIdleShutdownWatchdog:`.

```python
class TestRequestIdOwnership:
    """A live request id belongs to exactly one request."""

    @pytest.fixture
    def client(self):
        with (
            mock.patch.object(server, "_voice_cache_ready", True),
            mock.patch.object(
                server, "_voice_cache", [{"ShortName": "en-US-AriaNeural"}]
            ),
        ):
            app = server.create_app()
            app.config["TESTING"] = True
            app.config["PROPAGATE_EXCEPTIONS"] = False
            with app.test_client() as c:
                yield c

    def test_duplicate_live_registration_is_refused(self):
        first = server._register_cancel_token("dup-live")
        try:
            with pytest.raises(server.DuplicateRequestId):
                server._register_cancel_token("dup-live")
            assert server._CANCEL_REGISTRY["dup-live"] is first
        finally:
            server._release_cancel_token("dup-live", first)

    def test_release_of_a_stale_token_keeps_the_live_entry(self):
        first = server._register_cancel_token("reused")
        server._release_cancel_token("reused", first)
        second = server._register_cancel_token("reused")
        try:
            server._release_cancel_token("reused", first)
            assert server._CANCEL_REGISTRY.get("reused") is second
            assert server._cancel_request("reused") is True
            assert second.is_set()
        finally:
            server._release_cancel_token("reused", second)

    def test_id_is_reusable_after_identity_safe_release(self):
        token = server._register_cancel_token("cycle")
        server._release_cancel_token("cycle", token)
        again = server._register_cancel_token("cycle")
        try:
            assert server._cancel_request("cycle") is True
            assert again.is_set()
            assert token.is_set() is False
        finally:
            server._release_cancel_token("cycle", again)

    def test_duplicate_live_request_id_returns_409(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hi."
        )
        held = server._register_cancel_token("held")
        try:
            resp = client.post(
                "/generate-and-download-tts",
                json={"ssml": ssml, "request_id": "held"},
            )
            assert resp.status_code == 409
            assert server._CANCEL_REGISTRY["held"] is held
        finally:
            server._release_cancel_token("held", held)

    def test_completed_request_releases_only_its_own_token(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hi."
        )

        async def _ok(_req, cancel_event=None):
            return b"\xff\xfbaudio"

        with mock.patch.object(server, "generate_audio", side_effect=_ok):
            assert client.post(
                "/generate-and-download-tts",
                json={"ssml": ssml, "request_id": "own1"},
            ).status_code == 200
        assert "own1" not in server._CANCEL_REGISTRY
```

- [ ] **Step 2: Run the new tests and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_server.py -k RequestIdOwnership -q
```

Expected: failures. `server.DuplicateRequestId` does not exist yet, and
`_release_cancel_token` takes one argument.

- [ ] **Step 3: Make the registry identity-safe.** In `server.py`, replace the
  registry helpers that currently sit between `_CANCEL_REGISTRY` and
  `_cancel_request` with this code.

```python
_CANCEL_REGISTRY: dict[str, _CancellationToken] = {}


class DuplicateRequestId(Exception):
    """A caller reused a request id that is still live."""


def _register_cancel_token(request_id: str) -> _CancellationToken:
    """Claim ``request_id`` for one live request.

    Ownership is exclusive: a second live registration would make the first
    request uncancellable and let either completion delete the other's token.
    """
    event = _CancellationToken()
    with _CANCEL_LOCK:
        if request_id in _CANCEL_REGISTRY:
            raise DuplicateRequestId(request_id)
        _CANCEL_REGISTRY[request_id] = event
    return event


def _release_cancel_token(request_id: str, token: _CancellationToken) -> None:
    """Drop ``request_id`` only if ``token`` still owns it.

    Releasing by id alone would let a slow request's cleanup delete the token of
    a newer request that reused the id.
    """
    with _CANCEL_LOCK:
        if _CANCEL_REGISTRY.get(request_id) is token:
            del _CANCEL_REGISTRY[request_id]
```

- [ ] **Step 4: Answer a duplicate id with 409 and release by identity.** In the
  `generate_and_download_tts` view in `server.py`, replace the token acquisition
  line with the block below, and change the `finally` clause's release call to pass
  the token.

```python
        cancel_event = None
        if request_id is not None:
            try:
                cancel_event = _register_cancel_token(request_id)
            except DuplicateRequestId:
                logger.warning("Refusing duplicate live request_id %r", request_id)
                return jsonify({"error": "request_id is already in flight."}), 409  # type: ignore[return-value]
```

In the same view's `finally` clause, replace the release with:

```python
            if request_id is not None and cancel_event is not None:
                _release_cancel_token(request_id, cancel_event)
```

- [ ] **Step 5: Run the new tests, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_server.py -k RequestIdOwnership -q
.venv/bin/python -m pytest -q
```

Expected: the five new tests pass, and the full suite passes with no failures and no
errors.

- [ ] **Step 6: Commit.**

```bash
git add server.py tests/test_server.py
git commit -m "fix(server): make cancellation request ids exclusively owned"
```

## Task 2: Cancellation handoff and per-generation request ownership (F-11, F-12)

**Implementer tier:** Frontier

**Problem:** Two defects share one code region.

F-11: the adapter's startup barrier only proves a request id is in its *own* set.
`SynthClient.synthesize` checks `should_abort` before the blocking POST, so STOP can
deliver `DELETE` before the server has registered the id. The server answers 404, the
adapter treats that as done, and the synthesis keeps its concurrency slot until the
request or stall timeout.

F-12: `_join_worker` waits ten seconds and then clears `self._worker` whether or not
the old thread exited, and every worker's `finally` clears the single shared
`_active_requests` set. A stale worker therefore cancels the *new* generation's
request, so an accepted message ends in STOP instead of END.

**Files:**
- Modify: `desktop/module.py:44-52`
- Modify: `desktop/module.py:140-210`
- Modify: `desktop/module.py:236-260`
- Modify: `desktop/module.py:420-500`
- Modify: `desktop/synth.py:15-20`
- Modify: `desktop/synth.py:139-147`
- Test: `tests/test_desktop_module.py`
- Test: `tests/test_desktop_synth.py`

**Interfaces:**
- Consumes: `_GenerationToken` with `cancelled: threading.Event` and
  `pause_requested: threading.Event` from `desktop/module.py`, extended here.
- Consumes: `SynthClient.cancel(request_id: str) -> None` and
  `SynthClient.synthesize(text, voice_name, rate, pitch, request_id, should_abort=None) -> bytes`
  from `desktop/synth.py`.
- Consumes: `protocol.ERR_CANT_SPEAK` from `desktop/protocol.py`.
- Produces: `SynthClient.cancel(request_id: str, *, still_wanted: Callable[[], bool] | None = None) -> bool`,
  returning True once the backend confirms cancellation. A 404 is retried while
  `still_wanted()` is true, bounded by `_CANCEL_HANDOFF_SECONDS = 1.0`.
- Produces: `_GenerationToken.requests: set[str]`, `add_request(request_id)`,
  `discard_request(request_id)`, and `take_requests() -> list[str]`, all guarded by
  the token's own lock, so a worker cancels only ids it owns.

### Steps

- [ ] **Step 1: Write the failing adapter tests.** Append this class to
  `tests/test_desktop_module.py`, immediately before `class TestCheckFfmpeg:`.

```python
class TestCancellationOwnership:
    """Cancellation reaches the backend and never crosses generations."""

    def test_delete_before_registration_is_retried_until_it_lands(self):
        registered = threading.Event()
        release_post = threading.Event()

        class RacingClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.statuses = []
                self.post_entered = threading.Event()

            def synthesize(
                self, text, voice_name, rate, pitch, request_id, should_abort=None
            ):
                self.requests.append((text, voice_name, rate, pitch))
                self.post_entered.set()
                registered.set()
                release_post.wait(2)
                return self._audio

            def cancel(self, request_id, *, still_wanted=None):
                # The first delivery attempt races ahead of registration.
                if not registered.is_set():
                    self.statuses.append(404)
                    if still_wanted is not None and still_wanted():
                        registered.wait(2)
                    else:
                        return False
                self.statuses.append(200)
                self.cancelled.append(request_id)
                release_post.set()
                return True

        client = RacingClient()
        engine, fake_io = _engine(client=client)
        engine.handle_speak(SSML)
        assert client.post_entered.wait(2)

        engine.handle_stop()
        assert engine.wait_idle(3)
        assert client.statuses[0] == 404
        assert client.statuses[-1] == 200
        assert client.cancelled
        assert fake_io.lines.count("703 STOP") == 1

    def test_stale_worker_cannot_cancel_a_newer_generation(self):
        class SlowClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.first_entered = threading.Event()
                self.release_first = threading.Event()
                self.calls = 0

            def synthesize(
                self, text, voice_name, rate, pitch, request_id, should_abort=None
            ):
                self.calls += 1
                self.requests.append((text, voice_name, rate, pitch))
                if self.calls == 1:
                    self.ids_first = request_id
                    self.first_entered.set()
                    self.release_first.wait(3)
                    return self._audio
                self.ids_second = request_id
                return self._audio

            def cancel(self, request_id, *, still_wanted=None):
                self.cancelled.append(request_id)
                return True

        client = SlowClient()
        engine, fake_io = _engine(client=client)
        engine.handle_speak(SSML)
        assert client.first_entered.wait(2)

        # The first worker is wedged, so the engine must refuse rather than
        # start a second generation over shared cancellation state.
        engine.handle_speak(SSML)
        assert fake_io.lines.count(protocol_err_cant_speak()) == 1

        client.release_first.set()
        assert engine.wait_idle(3)
        assert getattr(client, "ids_second", None) is None
        assert client.cancelled == [] or client.cancelled == [client.ids_first]

    def test_worker_cancels_only_its_own_request_ids(self):
        engine, _ = _engine()
        first = module._GenerationToken()
        second = module._GenerationToken()
        first.add_request("a1")
        second.add_request("b1")
        assert first.take_requests() == ["a1"]
        assert first.take_requests() == []
        assert second.take_requests() == ["b1"]

    def test_recovery_uses_a_fresh_request_id(self):
        engine, fake_io, client, state = _lifecycle_engine(fail_first_post=True)
        seen: list[str] = []

        original = client.synthesize

        def record(text, voice_name, rate, pitch, request_id, should_abort=None):
            seen.append(request_id)
            return original(text, voice_name, rate, pitch, request_id, should_abort)

        client.synthesize = record
        engine.handle_speak(SSML)
        assert engine.wait_idle(3)
        assert len(seen) == 2
        assert seen[0] != seen[1]
        assert fake_io.lines[-1] == "702 END"


def protocol_err_cant_speak():
    from desktop import protocol

    return protocol.ERR_CANT_SPEAK
```

- [ ] **Step 2: Write the failing cancel-transport test.** Append this class to
  `tests/test_desktop_synth.py`.

```python
class TestCancelHandoff:
    """DELETE tolerates the interval before the server registers the POST."""

    def test_404_is_retried_while_the_generation_still_wants_it(self):
        client, transport = self._client(
            [
                (404, {}, b'{"error": "Unknown request id."}'),
                (404, {}, b'{"error": "Unknown request id."}'),
                (200, {}, b'{"cancelled": true}'),
            ]
        )
        assert client.cancel("req1", still_wanted=lambda: True) is True
        assert len(transport.calls) == 3

    def test_404_stops_when_the_generation_no_longer_wants_it(self):
        client, transport = self._client(
            [(404, {}, b'{"error": "Unknown request id."}')]
        )
        assert client.cancel("req1", still_wanted=lambda: False) is False
        assert len(transport.calls) == 1

    def test_success_needs_no_retry(self):
        client, transport = self._client([(200, {}, b'{"cancelled": true}')])
        assert client.cancel("req1", still_wanted=lambda: True) is True
        assert len(transport.calls) == 1

    def test_transport_failure_is_swallowed(self):
        client, transport = self._client([OSError("refused")])
        assert client.cancel("req1", still_wanted=lambda: True) is False

    def _client(self, responses):
        transport = _Transport(responses)
        return (
            synth.SynthClient(
                settings.DEFAULTS, transport=transport, sleep=lambda _seconds: None
            ),
            transport,
        )
```

- [ ] **Step 3: Run both new test classes and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k CancellationOwnership -q
.venv/bin/python -m pytest tests/test_desktop_synth.py -k CancelHandoff -q
```

Expected: failures. `_GenerationToken` has no request methods, `cancel` takes no
`still_wanted`, and a second SPEAK currently starts a new generation.

- [ ] **Step 4: Make cancellation delivery survive the registration gap.** In
  `desktop/synth.py`, add the bound next to the other module constants:

```python
_CANCEL_HANDOFF_SECONDS = 1.0
_CANCEL_RETRY_INTERVAL = 0.05
```

Then replace `SynthClient.cancel` with:

```python
    def cancel(
        self,
        request_id: str,
        *,
        still_wanted: Callable[[], bool] | None = None,
    ) -> bool:
        """Deliver cancellation, tolerating the pre-registration interval.

        The adapter can send DELETE before the backend has registered the POST,
        which answers 404. That is "not yet", not "unknown", so retry briefly
        while this generation still wants the request cancelled.
        """
        url = f"{self._config.backend_url}/tts-request/{request_id}"
        deadline = time.monotonic() + _CANCEL_HANDOFF_SECONDS
        while True:
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
            if time.monotonic() >= deadline:
                logger.debug(
                    "Cancel for %s was never registered by the backend", request_id
                )
                return False
            self._sleep(_CANCEL_RETRY_INTERVAL)
```

- [ ] **Step 5: Give each generation its own request ownership.** In
  `desktop/module.py`, replace `class _GenerationToken` with:

```python
class _GenerationToken:
    """Invalidatable state and request ownership for one speech generation."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.pause_requested = threading.Event()
        self._lock = threading.Lock()
        self.requests: set[str] = set()

    def add_request(self, request_id: str) -> None:
        with self._lock:
            self.requests.add(request_id)

    def discard_request(self, request_id: str) -> None:
        with self._lock:
            self.requests.discard(request_id)

    def take_requests(self) -> list[str]:
        """Remove and return this generation's ids, so cleanup runs once."""
        with self._lock:
            taken = sorted(self.requests)
            self.requests.clear()
        return taken

    def snapshot_requests(self) -> list[str]:
        with self._lock:
            return sorted(self.requests)
```

- [ ] **Step 6: Refuse a new SPEAK while the old worker is wedged.** In
  `desktop/module.py`, replace `_join_worker` with `_reclaim_worker`, and delete the
  now-unused `_active_requests` initialisation from `__init__`.

```python
    def _reclaim_worker(self) -> bool:
        """Stop the previous generation. False if its worker is still alive."""
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        if worker.is_alive():
            self.handle_stop()
            worker.join(10.0)
        if worker.is_alive():
            logger.error("Previous speech worker did not exit; refusing new message")
            return False
        with self._lock:
            if self._worker is worker:
                self._worker = None
        return True
```

In `handle_speak`, replace the `self._join_worker()` call with the guarded form,
placed exactly where the old call was:

```python
        if not self._reclaim_worker():
            self._io.send(protocol.ERR_CANT_SPEAK)  # type: ignore[attr-defined]
            return
```

In `SpeechEngine.__init__`, delete this line:

```python
        self._active_requests: set[str] = set()
```

- [ ] **Step 7: Route every request through its own generation.** In
  `desktop/module.py`, replace `handle_stop`, `_register_request`, `_fetch`,
  `_cancel_outstanding`, and `_cancel_requests` with the versions below. Then change
  the worker's `finally` block to call `self._cancel_outstanding(generation)`, and
  change both `self._register_request()` calls in `_speak_worker` to
  `self._register_request(generation)`.

```python
    def handle_stop(self) -> None:
        """Invalidate active speech and cancel its backend requests."""
        with self._lock:
            worker = self._worker
            generation = self._generation
            if worker is None or not worker.is_alive() or generation is None:
                return
            generation.cancelled.set()
        outstanding = generation.snapshot_requests()
        if outstanding:
            threading.Thread(
                target=self._cancel_requests,
                args=(generation, outstanding),
                name="free-tts-cancel",
                daemon=True,
            ).start()
```

```python
    def _register_request(self, generation: _GenerationToken) -> str:
        """Create and register a request id owned by ``generation``.

        Registration happens in the worker thread ahead of the first blocking
        call, so a STOP arriving right after handle_speak always finds the
        request already cancellable.
        """
        request_id = new_request_id()
        generation.add_request(request_id)
        return request_id
```

```python
    def _fetch(
        self,
        generation: _GenerationToken,
        text: str,
        voice_name: str,
        rate: str,
        pitch: str,
        request_id: str,
    ) -> bytes:
        generation.add_request(request_id)
        try:
            return self._client.synthesize(  # type: ignore[attr-defined]
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=lambda: self._should_abort(generation),
            )
        except Cancelled:
            raise
        except SynthError:
            if self._should_abort(generation):
                raise Cancelled("aborted instead of recovering backend")
            self.catalog = None
            self._controller.ensure_ready()  # type: ignore[attr-defined]
            self._refresh_catalog()
            if self._should_abort(generation):
                raise Cancelled("aborted before retrying synthesis")
            # A fresh id: the first POST's delivery is ambiguous, so reusing the
            # id could put two live requests under one name.
            retry_id = self._register_request(generation)
            try:
                return self._client.synthesize(  # type: ignore[attr-defined]
                    text,
                    voice_name,
                    rate,
                    pitch,
                    retry_id,
                    should_abort=lambda: self._should_abort(generation),
                )
            finally:
                generation.discard_request(retry_id)
        finally:
            generation.discard_request(request_id)

    def _cancel_outstanding(self, generation: _GenerationToken) -> None:
        self._cancel_requests(generation, generation.take_requests())

    def _cancel_requests(
        self, generation: _GenerationToken, request_ids: list[str]
    ) -> None:
        for request_id in request_ids:
            self._client.cancel(  # type: ignore[attr-defined]
                request_id,
                still_wanted=lambda: generation.cancelled.is_set(),
            )
```

- [ ] **Step 8: Run the focused tests, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py tests/test_desktop_synth.py -q
.venv/bin/python -m pytest -q
```

Expected: both files pass, and the full suite passes with no failures and no errors.
If an existing test calls `cancel(request_id)` positionally, leave it: the new
keyword argument is optional.

- [ ] **Step 9: Commit.**

```bash
git add desktop/module.py desktop/synth.py tests/test_desktop_module.py tests/test_desktop_synth.py
git commit -m "fix(desktop): own cancellation per generation and survive the DELETE race"
```

## Task 3: PAUSE fallback without index marks (F-13)

**Implementer tier:** Advanced

**Problem:** the worker only honours PAUSE inside `if chunk.mark:`. A message with no
server-inserted marks, or one split only by `max_chunk_chars`, therefore ignores PAUSE
completely and reports `702 END`. Speech Dispatcher never learns the pause happened and
cannot resume from the intended boundary.

**Files:**
- Modify: `desktop/module.py:36-42`
- Modify: `desktop/module.py:430-450`
- Test: `tests/test_desktop_module.py`
- Test: `tests/test_desktop_end_to_end.py`

**Interfaces:**
- Consumes: `Chunk` with fields `text: str` and `mark: str | None` from
  `desktop/chunks.py`, and `split_marked(ssml, max_chars=400) -> list[Chunk]`.
- Consumes: `_GenerationToken.pause_requested: threading.Event` from Task 2.
- Produces: `_mark_ahead(chunks: list[object], index: int) -> bool`, a module-level
  helper in `desktop/module.py` that reports whether any chunk after ``index`` still
  carries a reportable index mark.

### Steps

- [ ] **Step 1: Write the failing unit tests.** Append this class to
  `tests/test_desktop_module.py`, immediately before `class TestCheckFfmpeg:`.

```python
class TestPauseFallback:
    """PAUSE is honoured even when no index mark is available."""

    class _GatedClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

        def synthesize(
            self, text, voice_name, rate, pitch, request_id, should_abort=None
        ):
            self.requests.append((text, voice_name, rate, pitch))
            self.entered.set()
            self.release.wait(3)
            return self._audio

    def test_unmarked_message_pauses_at_a_chunk_boundary(self):
        client = self._GatedClient()
        config = dataclasses.replace(settings.DEFAULTS, max_chunk_chars=12)
        engine, fake_io = _engine(client=client, config=config)
        engine.handle_speak("<speak>alpha bravo charlie delta echo</speak>")
        assert client.entered.wait(2)

        engine.handle_pause()
        client.release.set()
        assert engine.wait_idle(3)
        assert fake_io.lines.count("704 PAUSE") == 1
        assert "702 END" not in fake_io.lines
        assert not any(line.startswith("700:") for line in fake_io.lines)

    def test_single_unmarked_chunk_still_reports_pause(self):
        client = self._GatedClient()
        engine, fake_io = _engine(client=client)
        engine.handle_speak("<speak>Just one piece.</speak>")
        assert client.entered.wait(2)

        engine.handle_pause()
        client.release.set()
        assert engine.wait_idle(3)
        assert fake_io.lines.count("704 PAUSE") == 1
        assert "702 END" not in fake_io.lines

    def test_pause_waits_for_a_mark_that_is_still_coming(self):
        client = self._GatedClient()
        config = dataclasses.replace(settings.DEFAULTS, max_chunk_chars=12)
        engine, fake_io = _engine(client=client, config=config)
        engine.handle_speak(
            '<speak>alpha bravo charlie delta. <mark name="__spd_0"/></speak>'
        )
        assert client.entered.wait(2)

        engine.handle_pause()
        client.release.set()
        assert engine.wait_idle(3)
        assert "700:__spd_0" in fake_io.lines
        assert fake_io.lines.count("704 PAUSE") == 1
        assert fake_io.lines.index("700:__spd_0") < fake_io.lines.index("704 PAUSE")
        assert "702 END" not in fake_io.lines

    def test_mark_ahead_reports_remaining_marks(self):
        from desktop.chunks import Chunk

        chunks = [Chunk("a", None), Chunk("b", "__spd_0"), Chunk("c", None)]
        assert module._mark_ahead(chunks, 0) is True
        assert module._mark_ahead(chunks, 1) is False
        assert module._mark_ahead(chunks, 2) is False
```

- [ ] **Step 2: Write the failing subprocess transcript.** Append this test to
  `class TestFullSession` in `tests/test_desktop_end_to_end.py`, immediately after
  `test_active_pause_stops_after_mark_and_emits_one_pause`.

```python
    def test_active_pause_without_marks_emits_one_pause(self, backend):
        _Handler.control_mode = "pause"
        process = _start_session(backend)
        try:
            process.stdin.write(
                b"INIT\nSPEAK\n<speak>No marks here at all.</speak>\n.\n"
            )
            process.stdin.flush()
            assert _Handler.post_started.wait(5)
            out = _finish_session(process, b"PAUSE\nLIST VOICES\nQUIT\n")
        finally:
            if process.poll() is None:
                process.kill()
                process.wait()

        assert "POST_TIMEOUT" not in _Handler.control_events
        assert out.count(b"704 PAUSE") == 1
        assert b"700-__spd_" not in out
        assert b"702 END" not in out
```

- [ ] **Step 3: Run both and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py -k PauseFallback -q
.venv/bin/python -m pytest tests/test_desktop_end_to_end.py -k pause_without_marks -q
```

Expected: failures reporting `702 END` and no `704 PAUSE`, plus an `AttributeError`
for `module._mark_ahead`.

- [ ] **Step 4: Add the lookahead helper.** In `desktop/module.py`, add this
  module-level function immediately after the `_STRING_SETTINGS` tuple.

```python
def _mark_ahead(chunks: list[object], index: int) -> bool:
    """True when a later chunk still carries a reportable index mark.

    PAUSE prefers a real index mark so the client can resume exactly there. When
    none remains, the next chunk boundary is the only honest place to stop.
    """
    return any(chunk.mark for chunk in chunks[index + 1 :])  # type: ignore[attr-defined]
```

- [ ] **Step 5: Pause at a mark when one exists, otherwise at the chunk boundary.**
  In `_speak_worker` in `desktop/module.py`, replace the pause check that currently
  reads `if chunk.mark and generation.pause_requested.is_set():` with:

```python
                    if generation.pause_requested.is_set() and (
                        chunk.mark or not _mark_ahead(chunks, index)
                    ):
                        outcome = "pause"
                        break
```

- [ ] **Step 6: Run the focused tests, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_module.py tests/test_desktop_end_to_end.py -q
.venv/bin/python -m pytest -q
```

Expected: both files pass, including the pre-existing
`test_active_pause_stops_after_mark_and_emits_one_pause`, and the full suite passes
with no failures and no errors.

- [ ] **Step 7: Commit.**

```bash
git add desktop/module.py tests/test_desktop_module.py tests/test_desktop_end_to_end.py
git commit -m "fix(desktop): honour PAUSE for speech without index marks"
```

## Task 4: Owned, cancellable decoder processes (F-15)

**Implementer tier:** Frontier

**Problem:** `decode_mp3` calls `subprocess.run`, which cannot be interrupted, and
`_decode_interruptibly` merely stops *waiting* for a detached daemon thread. After
STOP, the decode thread and its ffmpeg child stay alive until ffmpeg exits on its
own. Repeated stops leak threads and processes.

**Files:**
- Modify: `desktop/audio.py:1-75`
- Modify: `desktop/module.py:80-95`
- Modify: `desktop/module.py:262-300`
- Test: `tests/test_desktop_audio.py`
- Test: `tests/test_desktop_module.py`

**Interfaces:**
- Consumes: `SAMPLE_RATE = 24000`, `BITS = 16`, `CHANNELS = 1`, `DecodeError`, and
  `native_big_endian()` from `desktop/audio.py`.
- Produces: `DecodeCancelled(DecodeError)` in `desktop/audio.py`.
- Produces: `decode_mp3(data: bytes, ffmpeg_path: str = "ffmpeg", sample_rate: int = SAMPLE_RATE, *, cancel: threading.Event | None = None, popen_factory: Callable[..., object] = subprocess.Popen) -> bytes`.
  The `runner=` parameter is gone; every caller and test must pass
  `popen_factory=` instead.
- Produces: `SpeechEngine._decode(mp3: bytes, ffmpeg_path: str, sample_rate: int, cancel: threading.Event) -> bytes`,
  a four-argument decoder contract. Injected test decoders must accept the fourth
  argument.

### Steps

- [ ] **Step 1: Rewrite the decoder tests for an owned process.** In
  `tests/test_desktop_audio.py`, replace `class _Result` and `class TestDecodeMp3`
  entirely with the code below. Leave `TestApplyGain` and `TestEndianness` untouched.

```python
class _FakeProcess:
    """Stands in for a Popen handle so cancellation is deterministic."""

    def __init__(
        self,
        *,
        stdout=b"",
        stderr=b"",
        returncode=0,
        blocks=False,
        ignores_terminate=False,
    ):
        self._stdout = stdout
        self._stderr = stderr
        self._final_returncode = returncode
        self._blocks = blocks
        self._ignores_terminate = ignores_terminate
        self.returncode = None
        self.terminated = False
        self.killed = False
        self.reaped = False
        self.stdin_closed = False
        self.communicate_calls = 0
        self._released = threading.Event()
        if not blocks:
            self._released.set()

    def communicate(self, input=None, timeout=None):
        """Called exactly once, blocking until the process is released."""
        self.stdin_closed = True
        self.communicate_calls += 1
        if not self._released.wait(10):
            raise AssertionError("decoder was never released or terminated")
        self.returncode = self._final_returncode
        self.reaped = True
        return self._stdout, self._stderr

    def terminate(self):
        self.terminated = True
        if not self._ignores_terminate:
            self._final_returncode = -15
            self._released.set()

    def kill(self):
        self.killed = True
        self._final_returncode = -9
        self._released.set()

    def wait(self, timeout=None):
        """Mirror Popen.wait: raise TimeoutExpired if still running."""
        if not self._released.wait(timeout if timeout is not None else 0):
            raise subprocess.TimeoutExpired(cmd="ffmpeg", timeout=timeout or 0)
        self.returncode = self._final_returncode
        self.reaped = True
        return self.returncode

    def release(self):
        self._released.set()


class TestDecodeMp3:
    def test_returns_pcm_from_stdout(self):
        captured = {}

        def factory(command, **kwargs):
            captured["command"] = command
            return _FakeProcess(stdout=b"\x01\x00\x02\x00")

        pcm = audio.decode_mp3(b"mp3-bytes", popen_factory=factory)
        assert pcm == b"\x01\x00\x02\x00"

    def test_requests_mono_s16le_at_sample_rate(self):
        captured = {}

        def factory(command, **kwargs):
            captured["command"] = command
            return _FakeProcess(stdout=b"\x00\x00")

        audio.decode_mp3(b"x", ffmpeg_path="/usr/bin/ffmpeg", popen_factory=factory)
        command = captured["command"]
        assert command[0] == "/usr/bin/ffmpeg"
        assert "s16le" in command
        assert "pcm_s16le" in command
        assert str(audio.SAMPLE_RATE) in command
        assert command[command.index("-ac") + 1] == "1"

    def test_nonzero_exit_raises_with_stderr(self):
        def factory(command, **kwargs):
            return _FakeProcess(returncode=1, stderr=b"bad data")

        with pytest.raises(audio.DecodeError, match="bad data"):
            audio.decode_mp3(b"x", popen_factory=factory)

    def test_empty_output_raises(self):
        def factory(command, **kwargs):
            return _FakeProcess(stdout=b"")

        with pytest.raises(audio.DecodeError, match="no audio"):
            audio.decode_mp3(b"x", popen_factory=factory)

    def test_missing_ffmpeg_raises_decode_error(self):
        def factory(command, **kwargs):
            raise FileNotFoundError("ffmpeg")

        with pytest.raises(audio.DecodeError, match="ffmpeg"):
            audio.decode_mp3(b"x", popen_factory=factory)

    def test_odd_length_output_is_trimmed_to_whole_frames(self):
        def factory(command, **kwargs):
            return _FakeProcess(stdout=b"\x01\x00\x02")

        assert audio.decode_mp3(b"x", popen_factory=factory) == b"\x01\x00"

    def test_cancellation_terminates_and_reaps_the_process(self):
        process = _FakeProcess(blocks=True)
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(audio.DecodeCancelled):
            audio.decode_mp3(b"x", cancel=cancel, popen_factory=lambda *a, **k: process)

        assert process.terminated is True
        assert process.reaped is True
        assert process.returncode is not None

    def test_cancellation_escalates_to_kill_when_terminate_is_ignored(self):
        process = _FakeProcess(blocks=True, ignores_terminate=True)
        cancel = threading.Event()
        cancel.set()

        with pytest.raises(audio.DecodeCancelled):
            audio.decode_mp3(b"x", cancel=cancel, popen_factory=lambda *a, **k: process)

        assert process.terminated is True
        assert process.killed is True
        assert process.reaped is True

    def test_cancellation_mid_decode_is_observed(self):
        process = _FakeProcess(blocks=True)
        cancel = threading.Event()
        started = threading.Event()

        def factory(command, **kwargs):
            started.set()
            return process

        errors = []

        def decode():
            try:
                audio.decode_mp3(b"x", cancel=cancel, popen_factory=factory)
            except BaseException as exc:  # noqa: BLE001 - recorded for assertion
                errors.append(exc)

        worker = threading.Thread(target=decode, daemon=True)
        worker.start()
        assert started.wait(2)
        cancel.set()
        worker.join(5)

        assert worker.is_alive() is False
        assert isinstance(errors[0], audio.DecodeCancelled)
        assert process.terminated is True
        assert process.reaped is True
        assert process.communicate_calls == 1
        assert not [
            thread
            for thread in threading.enumerate()
            if thread.name == "free-tts-decode-io"
        ]

    def test_real_ffmpeg_decodes_generated_tone(self):
        """Integration guard: only runs when ffmpeg is actually installed."""
        try:
            made = subprocess.run(
                [
                    "ffmpeg", "-hide_banner", "-loglevel", "error",
                    "-f", "lavfi", "-i", "sine=frequency=440:duration=0.2",
                    "-f", "mp3", "pipe:1",
                ],
                capture_output=True,
                check=False,
            )
        except FileNotFoundError:
            pytest.skip("ffmpeg not installed")
        if made.returncode != 0 or not made.stdout:
            pytest.skip("ffmpeg cannot generate a test tone here")
        pcm = audio.decode_mp3(made.stdout)
        assert len(pcm) > 1000
        assert len(pcm) % 2 == 0
```

Add `import threading` to that file's imports.

- [ ] **Step 2: Write the failing engine cleanup test.** Append this class to
  `tests/test_desktop_module.py`, immediately before `class TestCheckFfmpeg:`.

```python
class TestDecoderLifetime:
    """Cancelling speech leaves no decoder thread behind."""

    def test_stop_leaves_no_decode_thread_running(self):
        entered = threading.Event()
        cancel_seen = threading.Event()

        def cancellable_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            entered.set()
            if not cancel.wait(3):
                raise AssertionError("cancel event was never set")
            cancel_seen.set()
            from desktop.audio import DecodeCancelled

            raise DecodeCancelled("cancelled during decode")

        fake_io = _FakeIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=cancellable_decoder,
        )
        engine.handle_speak(SSML)
        assert entered.wait(2)

        engine.handle_stop()
        assert engine.wait_idle(3)
        assert cancel_seen.is_set()
        assert fake_io.lines.count("703 STOP") == 1
        assert not [
            thread
            for thread in threading.enumerate()
            if thread.name == "free-tts-decode"
        ]

    def test_decoder_receives_the_generation_cancel_event(self):
        seen = {}

        def recording_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            seen["cancel"] = cancel
            return b"\x01\x00" * 8

        fake_io = _FakeIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=recording_decoder,
        )
        engine.handle_speak(SSML)
        assert engine.wait_idle(3)
        assert isinstance(seen["cancel"], threading.Event)
        assert fake_io.lines[-1] == "702 END"
```

- [ ] **Step 3: Update the three existing decoder injections.** These call sites use
  the three-argument contract and must accept the fourth argument. In
  `tests/test_desktop_module.py`:

  - in `_engine`, change the decoder to
    `decoder=lambda mp3, ffmpeg_path, sample_rate, cancel: b"\x01\x00" * 8,`
  - in `test_decode_failure_after_acceptance_stops_message`, change
    `def failing_decoder(mp3, ffmpeg_path, sample_rate):` to
    `def failing_decoder(mp3, ffmpeg_path, sample_rate, cancel):`
  - in `test_volume_gain_applied_to_pcm`, change the decoder to
    `decoder=lambda mp3, ffmpeg_path, sample_rate, cancel: b"\xff\x7f",`
  - in `test_stop_during_decode_suppresses_every_later_emission`, change
    `def blocked_decoder(mp3, ffmpeg_path, sample_rate):` to
    `def blocked_decoder(mp3, ffmpeg_path, sample_rate, cancel):`

  In `TestPauseFallback` and any other class added in earlier tasks, apply the same
  fourth-argument change to any injected decoder.

- [ ] **Step 4: Run the new tests and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_audio.py -q
.venv/bin/python -m pytest tests/test_desktop_module.py -k DecoderLifetime -q
```

Expected: failures. `decode_mp3` still takes `runner=`, `audio.DecodeCancelled` does
not exist, and the engine still calls its decoder with three arguments.

- [ ] **Step 5: Own the ffmpeg process.** In `desktop/audio.py`, add `threading` to
  the imports, then add the cancellation error next to `DecodeError` and replace
  `decode_mp3` with the version below.

```python
class DecodeCancelled(DecodeError):
    """Decoding stopped because its generation was cancelled."""
```

```python
_POLL_INTERVAL = 0.05
_TERMINATE_GRACE = 0.5


def _stop_process(process: object) -> None:
    """Terminate, escalate, and always reap the decoder process."""
    with contextlib.suppress(Exception):
        process.terminate()  # type: ignore[attr-defined]
    try:
        process.wait(timeout=_TERMINATE_GRACE)  # type: ignore[attr-defined]
        return
    except Exception:
        pass
    with contextlib.suppress(Exception):
        process.kill()  # type: ignore[attr-defined]
    with contextlib.suppress(Exception):
        process.wait(timeout=_TERMINATE_GRACE)  # type: ignore[attr-defined]


def decode_mp3(
    data: bytes,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = SAMPLE_RATE,
    *,
    cancel: threading.Event | None = None,
    popen_factory: Callable[..., object] = subprocess.Popen,
) -> bytes:
    """Decode MP3 bytes to mono 16-bit PCM at ``sample_rate``.

    The process is owned rather than fire-and-forget: cancellation terminates and
    reaps ffmpeg, so a stopped generation cannot leave a child behind.
    """
    endian_format = "s16be" if native_big_endian() else "s16le"
    codec = "pcm_s16be" if native_big_endian() else "pcm_s16le"
    command = [
        ffmpeg_path,
        "-hide_banner",
        "-loglevel",
        "error",
        "-i",
        "pipe:0",
        "-f",
        endian_format,
        "-acodec",
        codec,
        "-ac",
        str(CHANNELS),
        "-ar",
        str(sample_rate),
        "pipe:1",
    ]
    try:
        process = popen_factory(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except OSError as exc:
        raise DecodeError(f"could not run ffmpeg ({ffmpeg_path}): {exc}") from exc

    if cancel is not None and cancel.is_set():
        _stop_process(process)
        raise DecodeCancelled("cancelled before decoding started")

    # communicate() is called exactly once, on a thread this function owns and
    # joins. Cancellation kills the process, which makes that call return, so no
    # thread and no ffmpeg child can outlive this decode.
    outcome: dict[str, object] = {}

    def pump() -> None:
        try:
            outcome["result"] = process.communicate(input=data)  # type: ignore[attr-defined]
        except BaseException as exc:  # noqa: BLE001 - re-raised on the caller
            outcome["error"] = exc

    pump_thread = threading.Thread(target=pump, name="free-tts-decode-io")
    pump_thread.start()
    while pump_thread.is_alive():
        if cancel is not None and cancel.is_set():
            _stop_process(process)
            pump_thread.join(_TERMINATE_GRACE * 2)
            raise DecodeCancelled("cancelled while decoding")
        pump_thread.join(_POLL_INTERVAL)

    error = outcome.get("error")
    if isinstance(error, OSError):
        _stop_process(process)
        raise DecodeError(f"ffmpeg failed: {error}") from error
    if isinstance(error, BaseException):
        _stop_process(process)
        raise error
    stdout, stderr = outcome["result"]  # type: ignore[misc]

    if cancel is not None and cancel.is_set():
        raise DecodeCancelled("cancelled after decoding")
    if getattr(process, "returncode", 1) != 0:
        detail = stderr or b""
        raise DecodeError(
            f"ffmpeg failed: {detail.decode('utf-8', 'replace').strip()[:200]}"
        )
    pcm = stdout or b""
    if not pcm:
        raise DecodeError("ffmpeg produced no audio")
    usable = len(pcm) - (len(pcm) % _FRAME_BYTES)
    return pcm[:usable]
```

Add `import contextlib` to that file's imports.

- [ ] **Step 6: Decode in the worker with the generation's event.** In
  `desktop/module.py`, replace the `self._decode` assignment in `__init__` with:

```python
        self._decode = decoder or (
            lambda mp3, ffmpeg_path, sample_rate, cancel: decode_mp3(
                mp3,
                ffmpeg_path=ffmpeg_path,
                sample_rate=sample_rate,
                cancel=cancel,
            )
        )
```

Delete the whole `_decode_interruptibly` method. In `_speak_worker`, replace the
`pcm = apply_gain(...)` statement with:

```python
                    pcm = apply_gain(
                        self._decode(
                            mp3,
                            self._config.ffmpeg_path,
                            SAMPLE_RATE,
                            generation.cancelled,
                        ),
                        gain,
                    )
```

Import the new error and treat it as a stop, changing the audio import line and the
worker's `except` clause:

```python
from desktop.audio import (
    SAMPLE_RATE,
    DecodeCancelled,
    DecodeError,
    apply_gain,
    decode_mp3,
)
```

```python
            except (Cancelled, DecodeCancelled):
                outcome = "stop"
```

Note: `DecodeCancelled` subclasses `DecodeError`, so it must be caught before the
existing `except (SynthError, DecodeError)` clause. Place it as shown above, where the
old bare `except Cancelled:` clause was.

- [ ] **Step 7: Run the focused tests, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_audio.py tests/test_desktop_module.py -q
.venv/bin/python -m pytest -q
```

Expected: both files pass, and the full suite passes with no failures and no errors.

- [ ] **Step 8: Commit.**

```bash
git add desktop/audio.py desktop/module.py tests/test_desktop_audio.py tests/test_desktop_module.py
git commit -m "fix(desktop): own and terminate the ffmpeg decoder on cancellation"
```

## Task 5: Validate backend_url and map URL failures to protocol errors (F-16)

**Implementer tier:** Advanced

**Problem:** `load_config` accepts any string for `backend_url`, and
`BackendController.probe` only catches `OSError`. With `backend_url=""`,
`urllib.request.Request("/health")` raises `ValueError: unknown url type`, which is
uncaught and kills the Speech Dispatcher command loop instead of producing `301` or
`304`.

**Files:**
- Modify: `desktop/settings.py:1-20`
- Modify: `desktop/settings.py:96-115`
- Modify: `desktop/backend.py:150-170`
- Modify: `desktop/module.py:500-530`
- Test: `tests/test_desktop_settings.py`
- Test: `tests/test_desktop_backend.py`
- Test: `tests/test_desktop_end_to_end.py`

**Interfaces:**
- Consumes: `AdapterConfig`, `DEFAULTS`, and `load_config(path=None, env=None)` from
  `desktop/settings.py`.
- Consumes: `Health(reachable, service_ok, voice_cache_ready, detail="", status_ok=False)`
  and `BackendController.probe() -> Health` from `desktop/backend.py`.
- Consumes: `protocol.ERR_CANT_INIT` from `desktop/protocol.py`.
- Produces: `ConfigError(Exception)` in `desktop/settings.py`, raised by
  `load_config` when `backend_url` is unusable.
- Produces: `validate_backend_url(value: str) -> str` in `desktop/settings.py`,
  returning the normalized URL with no trailing slash.

### Steps

- [ ] **Step 1: Write the failing settings tests.** Append this class to
  `tests/test_desktop_settings.py`, immediately before `class TestMapRate:`.

```python
class TestBackendUrlValidation:
    @pytest.mark.parametrize(
        "raw",
        [
            "",
            "   ",
            "/health",
            "127.0.0.1:5000",
            "ftp://127.0.0.1:5000",
            "http://",
            "http://127.0.0.1:notaport",
            "http://user:pass@127.0.0.1:5000",
            "http://127.0.0.1:5000/api",
            "http://127.0.0.1:5000?x=1",
            "http://127.0.0.1:5000#frag",
        ],
    )
    def test_unusable_urls_are_rejected(self, tmp_path, raw):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"backend_url": raw}))
        with pytest.raises(settings.ConfigError):
            settings.load_config(path, env={})

    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("http://127.0.0.1:5000", "http://127.0.0.1:5000"),
            ("http://127.0.0.1:5000/", "http://127.0.0.1:5000"),
            ("https://localhost:8443", "https://localhost:8443"),
            ("http://localhost", "http://localhost"),
            ("  http://127.0.0.1:5000  ", "http://127.0.0.1:5000"),
        ],
    )
    def test_valid_urls_normalize(self, tmp_path, raw, expected):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"backend_url": raw}))
        assert settings.load_config(path, env={}).backend_url == expected

    def test_env_value_is_validated_too(self, tmp_path):
        with pytest.raises(settings.ConfigError):
            settings.load_config(
                tmp_path / "missing.json", env={"FREE_TTS_BACKEND_URL": "nonsense"}
            )

    def test_validator_returns_normalized_url(self):
        assert (
            settings.validate_backend_url("http://127.0.0.1:5000/")
            == "http://127.0.0.1:5000"
        )
```

- [ ] **Step 2: Write the failing probe test.** Append this class to
  `tests/test_desktop_backend.py`.

```python
class TestMalformedBackendUrl:
    """A bad address must degrade to an unavailable backend, not an exception."""

    def test_probe_reports_unreachable_for_an_unusable_url(self):
        config = dataclasses.replace(settings.DEFAULTS, backend_url="")
        controller = backend.BackendController(config)
        health = controller.probe()
        assert health.reachable is False
        assert health.ready is False
        assert health.detail

    def test_ensure_ready_raises_backend_unavailable(self):
        config = dataclasses.replace(
            settings.DEFAULTS, backend_url="", autostart=False
        )
        controller = backend.BackendController(config)
        with pytest.raises(backend.BackendUnavailable):
            controller.ensure_ready()
```

If that file does not already import `dataclasses` and `settings`, add them.

- [ ] **Step 3: Write the failing subprocess test.** Append this class to
  `tests/test_desktop_end_to_end.py`.

```python
class TestMalformedConfiguration:
    def test_invalid_backend_url_fails_init_without_crashing(self):
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not installed")
        env = _module_env("http://127.0.0.1:9")
        env["FREE_TTS_BACKEND_URL"] = "not-a-url"
        result = subprocess.run(
            [sys.executable, "-m", "desktop.module", "/dev/null"],
            input=b"INIT\nQUIT\n",
            capture_output=True,
            env=env,
            cwd=str(ROOT),
            timeout=60,
        )
        assert b"399" in result.stdout
        assert b"Traceback" not in result.stderr
```

- [ ] **Step 4: Run all three and confirm they fail.**

```bash
.venv/bin/python -m pytest tests/test_desktop_settings.py -k BackendUrlValidation -q
.venv/bin/python -m pytest tests/test_desktop_backend.py -k MalformedBackendUrl -q
.venv/bin/python -m pytest tests/test_desktop_end_to_end.py -k MalformedConfiguration -q
```

Expected: failures. `settings.ConfigError` does not exist, and the probe raises
`ValueError` instead of returning a `Health`.

- [ ] **Step 5: Validate the URL at configuration time.** In `desktop/settings.py`,
  add `import urllib.parse` to the imports, then add this error and validator
  immediately after the `_STR_FIELDS` tuple.

```python
class ConfigError(Exception):
    """Configuration cannot be used, with a message safe to show at INIT."""


_ALLOWED_SCHEMES = ("http", "https")


def validate_backend_url(value: str) -> str:
    """Return a normalized backend URL or raise ``ConfigError``.

    The adapter builds request URLs by string concatenation, so an unusable base
    would otherwise surface as an uncaught ValueError deep inside urllib.
    """
    candidate = value.strip().rstrip("/")
    if not candidate:
        raise ConfigError("backend_url must not be empty")
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in _ALLOWED_SCHEMES:
        raise ConfigError(
            f"backend_url must use http or https, got {parsed.scheme!r}"
        )
    if not parsed.hostname:
        raise ConfigError("backend_url must include a hostname")
    if parsed.username or parsed.password:
        raise ConfigError("backend_url must not contain credentials")
    if parsed.query or parsed.fragment:
        raise ConfigError("backend_url must not contain a query or fragment")
    if parsed.path not in ("", "/"):
        raise ConfigError(
            f"backend_url must not include a path, got {parsed.path!r}"
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ConfigError(f"backend_url has an invalid port: {exc}") from exc
    if port is not None and not 1 <= port <= 65535:
        raise ConfigError(f"backend_url port is out of range: {port}")
    return candidate
```

In `load_config`, replace the string-field loop so `backend_url` is validated:

```python
    for field in _STR_FIELDS:
        value = env.get(f"FREE_TTS_{field.upper()}", raw.get(field))
        if value is not None:
            cleaned = str(value).strip().rstrip("/")
            if field == "backend_url":
                cleaned = validate_backend_url(str(value))
            updates[field] = cleaned
```

- [ ] **Step 6: Make the probe defensive.** In `desktop/backend.py`, replace the
  `try`/`except OSError` block at the start of `probe` with:

```python
        url = f"{self._config.backend_url}/health"
        try:
            payload = self._fetch(url, _PROBE_TIMEOUT)
        except OSError as exc:
            return Health(False, False, False, str(exc))
        except (ValueError, UnicodeError) as exc:
            # A malformed backend_url reaches urllib as ValueError; report it as
            # an unusable backend instead of killing the command loop.
            return Health(False, False, False, f"invalid backend_url: {exc}")
```

- [ ] **Step 7: Report a configuration error at INIT.** In `desktop/module.py`,
  change the settings import to include the error, then guard `load_config` in `run`.

```python
from desktop.settings import (
    AdapterConfig,
    ConfigError,
    load_config,
    map_pitch,
    map_rate,
    map_volume,
)
```

In `run`, replace `config = load_config()` with:

```python
    try:
        config = load_config()
    except ConfigError as exc:
        # INIT has not been answered yet, so read it first and then refuse.
        io.read_line()
        io.send_multiline([f"399-{exc}"], protocol.ERR_CANT_INIT)
        return 1
```

- [ ] **Step 8: Run the focused tests, then the full suite.**

```bash
.venv/bin/python -m pytest tests/test_desktop_settings.py tests/test_desktop_backend.py tests/test_desktop_end_to_end.py -q
.venv/bin/python -m pytest -q
```

Expected: all three files pass, and the full suite passes with no failures and no
errors.

- [ ] **Step 9: Commit.**

```bash
git add desktop/settings.py desktop/backend.py desktop/module.py tests/test_desktop_settings.py tests/test_desktop_backend.py tests/test_desktop_end_to_end.py
git commit -m "fix(desktop): validate backend_url and surface bad addresses as protocol errors"
```
