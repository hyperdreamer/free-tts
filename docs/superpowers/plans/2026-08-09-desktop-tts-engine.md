# Desktop TTS Engine Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make Okular's built-in Speak actions use `free-tts` transparently by shipping a per-user Speech Dispatcher output module that bridges Speech Dispatcher to the existing Flask synthesis API.

**Architecture:** A new `desktop/` Python package implements a Speech Dispatcher output module: it speaks the module protocol on stdin/stdout, enumerates Edge voices from `GET /voices`, starts the Flask backend on demand (never touching a backend it did not start), synthesises segment-by-segment through `POST /generate-and-download-tts`, decodes MP3 to PCM with `ffmpeg`, and returns audio to Speech Dispatcher over the server-audio channel. `server.py` gains health identity, request cancellation, and an opt-in idle shutdown.

**Tech Stack:** Python 3.11+ standard library only for `desktop/` (no new runtime dependencies), existing Flask/edge-tts server, `ffmpeg` CLI for decoding, Speech Dispatcher 0.11+ module protocol, pytest for tests.

## Global Constraints

- Python 3.11 is the version floor. `desktop/` must import only Python standard library modules; no new entries in `requirements.txt`. Do not use an XML parser for Speech Dispatcher SSML: follow upstream's `speechd_python_modules/module_utils.py`, which strips SSML with a plain character scanner, so no entity expansion is ever possible.
- `desktop/` must not import `server.py` or `flask`. It talks to the backend only over HTTP using `urllib.request`.
- The design spec is `docs/superpowers/specs/2026-08-09-desktop-tts-engine-design.md`. Read it before starting; it is authoritative for behavior.
- Never write anything except protocol traffic to stdout in `desktop/`. Diagnostics go to stderr via the `logging` module. Stdout is opened in binary mode and must stay binary.
- A backend the adapter did not start is never stopped, signalled, or reconfigured.
- Speech Dispatcher protocol response codes must match the reference implementation byte for byte: `299-<msg>` then `299 OK LOADED SUCCESSFULLY` on init success; `399-<msg>` then `399 ERR CANT INIT MODULE` on init failure; `202 OK RECEIVING MESSAGE` then `200 OK SPEAKING` or `301 ERROR CANT SPEAK` for SPEAK; `203 OK RECEIVING SETTINGS` then `203 OK SETTINGS RECEIVED` for SET; `207 OK RECEIVING AUDIO SETTINGS` then `203 OK AUDIO INITIALIZED` for AUDIO; `207 OK RECEIVING LOGLEVEL SETTINGS` then `203 OK LOGLEVEL SET` for LOGLEVEL; `200 OK DEBUGGING ON`/`200 OK DEBUGGING OFF` for DEBUG; `210 OK QUIT` for QUIT; `300 ERR UNKNOWN COMMAND` otherwise; `302 ERROR BAD SYNTAX`, `303 ERROR INVALID PARAMETER OR VALUE`, `304 CANT LIST VOICES` for the corresponding failures.
- Events are `701 BEGIN`, `702 END`, `703 STOP`, `704 PAUSE`, and `700-<mark>` followed by `700 INDEX MARK`.
- Tests run with a local virtualenv. If `.venv` is absent, create it first: `python -m venv .venv && .venv/bin/pip install -r requirements.txt pytest`. Always run tests as `.venv/bin/python -m pytest`.
- `.venv/` is gitignored. Never commit it, and never commit `config.json`.
- All 97 pre-existing tests must keep passing. Do not modify `tests/test_extension_split_sentences.py` or `tests/test_media_session.py`.
- Every task's requirements implicitly include this section.

## Task 1: Server health identity

**Implementer tier:** Fast

**Files:**

- Modify: `server.py:211-215`
- Modify: `server.py:780-787`
- Test: `tests/test_server.py`

**Interfaces:**

- Consumes: nothing; this is the first task.
- Produces: module constants `SERVICE_NAME: str = "free-tts"` and `API_VERSION: int = 1` in `server.py`, and a `GET /health` JSON body of exactly `{"status": "ok", "service": "free-tts", "api_version": 1, "voice_cache_ready": <bool>}`.

- [ ] **Step 1: Write the failing test**

Append to the `TestFlaskErrorResponses` class in `tests/test_server.py`:

```python
    def test_health_reports_service_identity(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["service"] == "free-tts"
        assert data["api_version"] == 1
        assert data["status"] == "ok"
        assert data["voice_cache_ready"] is True

    def test_health_identity_constants_exported(self):
        assert server.SERVICE_NAME == "free-tts"
        assert server.API_VERSION == 1
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_server.py -k "health" -q`
Expected: FAIL, `KeyError: 'service'`.

- [ ] **Step 3: Write the minimal implementation**

In `server.py`, directly after the `SSML_NAMESPACE` definition, add:

```python
SERVICE_NAME: str = "free-tts"
"""Stable service identity so clients can detect a port conflict."""

API_VERSION: int = 1
"""Incremented only on a breaking change to the adapter-facing HTTP contract."""
```

Replace the body of the `health()` view with:

```python
        return jsonify(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "api_version": API_VERSION,
                "voice_cache_ready": _voice_cache_ready,
            }
        )
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (99 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 5: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "feat(server): expose service identity and API version on /health"
```

## Task 2: Server request cancellation

**Implementer tier:** Standard

**Files:**

- Modify: `server.py:211-215`
- Modify: `server.py:600-660`
- Modify: `server.py:819-880`
- Test: `tests/test_server.py`

**Interfaces:**

- Consumes: `SERVICE_NAME` and `API_VERSION` from Task 1 (no direct call, same file).
- Produces, all in `server.py`:
  - `class CancelledError(Exception)` — raised inside generation when a cancel token is set.
  - `_REQUEST_ID_RE: re.Pattern[str]` matching `^[A-Za-z0-9_-]{1,64}$`.
  - `_register_cancel_token(request_id: str) -> threading.Event`
  - `_release_cancel_token(request_id: str) -> None`
  - `_cancel_request(request_id: str) -> bool` — `True` if a live token was found and set.
  - `generate_audio(req: TTSRequest, cancel_event: threading.Event | None = None) -> bytes`
  - `POST /generate-and-download-tts` accepts an optional `"request_id"` string field; a malformed one is `400`. A cancelled generation returns `499` with body `{"error": "Request cancelled."}`.
  - `DELETE /tts-request/<request_id>` returns `200` with `{"cancelled": true}` for a live request, `404` with `{"error": "Unknown request id."}` otherwise.

- [ ] **Step 1: Write the failing test**

Add a new class at the end of `tests/test_server.py`:

```python
class TestCancellation:
    """Cancellation registry and the DELETE endpoint."""

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

    def test_cancel_unknown_id_returns_404(self, client):
        resp = client.delete("/tts-request/does-not-exist")
        assert resp.status_code == 404

    def test_cancel_live_request_returns_200(self, client):
        token = server._register_cancel_token("abc123")
        try:
            resp = client.delete("/tts-request/abc123")
            assert resp.status_code == 200
            assert resp.get_json()["cancelled"] is True
            assert token.is_set()
        finally:
            server._release_cancel_token("abc123")

    def test_cancel_is_idempotent_until_released(self, client):
        server._register_cancel_token("dup")
        try:
            assert client.delete("/tts-request/dup").status_code == 200
            assert client.delete("/tts-request/dup").status_code == 200
        finally:
            server._release_cancel_token("dup")
        assert client.delete("/tts-request/dup").status_code == 404

    def test_released_token_is_gone(self, client):
        server._register_cancel_token("gone")
        server._release_cancel_token("gone")
        assert client.delete("/tts-request/gone").status_code == 404

    def test_malformed_request_id_rejected(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hi."
        )
        resp = client.post(
            "/generate-and-download-tts",
            json={"ssml": ssml, "request_id": "bad id!"},
        )
        assert resp.status_code == 400

    def test_cancelled_generation_returns_499(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hi."
        )

        async def _cancelled(_req, cancel_event=None):
            raise server.CancelledError("cancelled")

        with mock.patch.object(server, "generate_audio", side_effect=_cancelled):
            resp = client.post(
                "/generate-and-download-tts",
                json={"ssml": ssml, "request_id": "tok1"},
            )
        assert resp.status_code == 499

    def test_registry_cleared_after_request(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hi."
        )

        async def _ok(_req, cancel_event=None):
            assert cancel_event is not None
            return b"\xff\xfbaudio"

        with mock.patch.object(server, "generate_audio", side_effect=_ok):
            resp = client.post(
                "/generate-and-download-tts",
                json={"ssml": ssml, "request_id": "tok2"},
            )
        assert resp.status_code == 200
        assert client.delete("/tts-request/tok2").status_code == 404

    def test_generate_audio_raises_when_token_set(self):
        req = server.TTSRequest(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hello."
        )
        token = threading.Event()
        token.set()

        class _Stream:
            async def __anext__(self):
                return {"type": "audio", "data": b"\x00\x01"}

            async def aclose(self):
                return None

        class _Comm:
            def stream(self):
                return _Stream()

        with mock.patch.object(server.edge_tts, "Communicate", return_value=_Comm()):
            with pytest.raises(server.CancelledError):
                asyncio.run(server.generate_audio(req, cancel_event=token))
```

Add `import asyncio` to the imports at the top of `tests/test_server.py`.

- [ ] **Step 2: Update the three existing `generate_audio` mocks**

The new keyword argument breaks three existing mocks. Change their signatures in `tests/test_server.py`, leaving their bodies alone:

- in `test_successful_tts_request`: `async def _mock_generate(_req, cancel_event=None):`
- in `test_exhausted_semaphore_returns_503_with_retry_after`: `async def _fake_generate(_req, cancel_event=None):`
- in `test_slot_released_when_generate_audio_raises`: `async def _failing_generate(_req, cancel_event=None):`

- [ ] **Step 3: Run the tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_server.py -q`
Expected: FAIL, `AttributeError: <module 'server'> does not have the attribute 'CancelledError'`.

- [ ] **Step 4: Write the minimal implementation**

In `server.py`, after the `API_VERSION` constant, add the registry:

```python
class CancelledError(Exception):
    """Raised inside generation when its cancellation token is set."""


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""Adapter-supplied request ids: opaque, bounded, log- and route-safe."""

_CANCEL_REGISTRY: dict[str, threading.Event] = {}
_CANCEL_LOCK = threading.Lock()


def _register_cancel_token(request_id: str) -> threading.Event:
    """Create and store a cancellation token for an in-flight request."""
    event = threading.Event()
    with _CANCEL_LOCK:
        _CANCEL_REGISTRY[request_id] = event
    return event


def _release_cancel_token(request_id: str) -> None:
    """Drop a token once its request finished, however it finished."""
    with _CANCEL_LOCK:
        _CANCEL_REGISTRY.pop(request_id, None)


def _cancel_request(request_id: str) -> bool:
    """Set the token for ``request_id``. Return False if it is not live."""
    with _CANCEL_LOCK:
        event = _CANCEL_REGISTRY.get(request_id)
    if event is None:
        return False
    event.set()
    return True
```

- [ ] **Step 5: Make generation cancellable**

Change the `generate_audio` signature and add a token check inside its loop. Replace the signature line and add the check immediately after the `if chunk.get("type") == "audio":` block's enclosing `while True:` header region, so the check runs once per iteration before writing:

```python
async def generate_audio(
    req: TTSRequest, cancel_event: threading.Event | None = None
) -> bytes:
```

Inside the `while True:` loop, as its first statement:

```python
        if cancel_event is not None and cancel_event.is_set():
            try:
                await stream.aclose()
            except Exception:
                pass
            raise CancelledError("Generation cancelled by client.")
```

Add `Raises: CancelledError: If ``cancel_event`` is set mid-stream.` to the docstring's Raises section.

- [ ] **Step 6: Wire the route and add the DELETE endpoint**

In `generate_and_download_tts`, after the existing `ssml` validation and before parsing, extract and validate the id:

```python
        request_id = body.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
                return jsonify({"error": "Invalid 'request_id'."}), 400  # type: ignore[return-value]
```

Replace the synthesis block so the token is registered around the whole attempt and always released:

```python
        cancel_event = (
            _register_cancel_token(request_id) if request_id is not None else None
        )
        try:
            if _TTS_SEMAPHORE is not None:
                acquired = _TTS_SEMAPHORE.acquire(timeout=TTS_QUEUE_TIMEOUT)
                if not acquired:
                    resp = jsonify({"error": "Server busy, try again later."})
                    resp.headers["Retry-After"] = str(TTS_QUEUE_TIMEOUT)
                    return resp, 503  # type: ignore[return-value]
                try:
                    audio = await generate_audio(tts_req, cancel_event=cancel_event)
                finally:
                    _TTS_SEMAPHORE.release()
            else:
                audio = await generate_audio(tts_req, cancel_event=cancel_event)
        except CancelledError:
            logger.info("TTS request cancelled by client.")
            return jsonify({"error": "Request cancelled."}), 499  # type: ignore[return-value]
        except TimeoutError as exc:
            logger.error("TTS stall detected after %ds", TTS_STALL_TIMEOUT)
            return jsonify({"error": _error_message(exc)}), 504  # type: ignore[return-value]
        except RuntimeError as exc:
            logger.error("TTS generation failed: %s", exc)
            return jsonify({"error": _error_message(exc)}), 500  # type: ignore[return-value]
        except Exception as exc:
            logger.exception("Unexpected TTS error")
            return jsonify({"error": _error_message(exc)}), 500  # type: ignore[return-value]
        finally:
            if request_id is not None:
                _release_cancel_token(request_id)
```

Add the cancellation route next to the health route:

```python
    @app.route("/tts-request/<request_id>", methods=["DELETE"])
    def cancel_tts_request(request_id: str) -> Response:
        """Cancel an in-flight generation so its concurrency slot is released."""
        if not _REQUEST_ID_RE.match(request_id):
            return jsonify({"error": "Unknown request id."}), 404  # type: ignore[return-value]
        if _cancel_request(request_id):
            return jsonify({"cancelled": True})
        return jsonify({"error": "Unknown request id."}), 404  # type: ignore[return-value]
```

Add `"DELETE"` to the CORS `methods` list in `create_app`.

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (107 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 8: Commit**

```bash
git add server.py tests/test_server.py
git commit -m "feat(server): cancellable synthesis requests via request_id"
```

## Task 3: Server idle shutdown

**Implementer tier:** Standard

**Files:**

- Modify: `server.py:174-180`
- Modify: `server.py:722-760`
- Modify: `config.example.json`
- Test: `tests/test_server.py`

**Interfaces:**

- Consumes: `_register_cancel_token`, `_release_cancel_token` from Task 2 (same file, no signature dependency).
- Produces, all in `server.py`:
  - `TTS_IDLE_TIMEOUT: int` — resolved from config key `idle_timeout` / env `TTS_IDLE_TIMEOUT`, default `0` (disabled), minimum `0`.
  - `class IdleShutdownWatchdog` with `__init__(self, timeout: int, on_shutdown: Callable[[], None], clock: Callable[[], float] = time.monotonic)`, methods `begin_request() -> None`, `end_request() -> None`, `poll() -> bool` (returns `True` once it has fired), and attribute `fired: bool`.
  - `_IDLE_WATCHDOG: IdleShutdownWatchdog | None` module global, set by `create_app()` when `TTS_IDLE_TIMEOUT > 0`.

- [ ] **Step 1: Write the failing test**

Add a new class at the end of `tests/test_server.py`:

```python
class TestIdleShutdownWatchdog:
    """Idle shutdown is opt-in and must never fire during synthesis."""

    def _watchdog(self, timeout=300):
        now = [1000.0]
        calls = []
        dog = server.IdleShutdownWatchdog(
            timeout=timeout,
            on_shutdown=lambda: calls.append("shutdown"),
            clock=lambda: now[0],
        )
        return dog, now, calls

    def test_does_not_fire_before_timeout(self):
        dog, now, calls = self._watchdog()
        now[0] += 299
        assert dog.poll() is False
        assert calls == []

    def test_fires_after_timeout(self):
        dog, now, calls = self._watchdog()
        now[0] += 301
        assert dog.poll() is True
        assert calls == ["shutdown"]

    def test_never_fires_while_request_active(self):
        dog, now, calls = self._watchdog()
        dog.begin_request()
        now[0] += 100_000
        assert dog.poll() is False
        assert calls == []

    def test_timer_restarts_after_request_ends(self):
        dog, now, calls = self._watchdog()
        dog.begin_request()
        now[0] += 100_000
        dog.end_request()
        now[0] += 299
        assert dog.poll() is False
        now[0] += 2
        assert dog.poll() is True

    def test_fires_only_once(self):
        dog, now, calls = self._watchdog()
        now[0] += 301
        assert dog.poll() is True
        assert dog.poll() is False
        assert calls == ["shutdown"]

    def test_overlapping_requests_use_refcount(self):
        dog, now, calls = self._watchdog()
        dog.begin_request()
        dog.begin_request()
        dog.end_request()
        now[0] += 100_000
        assert dog.poll() is False
        dog.end_request()
        now[0] += 301
        assert dog.poll() is True

    def test_disabled_when_timeout_zero(self):
        dog, now, calls = self._watchdog(timeout=0)
        now[0] += 100_000
        assert dog.poll() is False
        assert calls == []

    def test_health_check_does_not_extend_idle_window(self, monkeypatch):
        """Only synthesis touches the watchdog; /health must not."""
        dog, now, calls = self._watchdog()
        monkeypatch.setattr(server, "_IDLE_WATCHDOG", dog)
        with (
            mock.patch.object(server, "_voice_cache_ready", True),
            mock.patch.object(server, "_voice_cache", []),
        ):
            app = server.create_app()
            app.config["TESTING"] = True
            with app.test_client() as client:
                now[0] += 200
                client.get("/health")
                now[0] += 101
                assert dog.poll() is True

    def test_default_idle_timeout_is_disabled(self, monkeypatch):
        monkeypatch.delenv("TTS_IDLE_TIMEOUT", raising=False)
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            assert server._cfg_int("idle_timeout", "TTS_IDLE_TIMEOUT", 0, minimum=0) == 0
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_server.py -k Idle -q`
Expected: FAIL, `AttributeError: <module 'server'> does not have the attribute 'IdleShutdownWatchdog'`.

- [ ] **Step 3: Write the watchdog**

In `server.py`, after the `_TTS_SEMAPHORE` definition, add the config value:

```python
TTS_IDLE_TIMEOUT: int = _cfg_int("idle_timeout", "TTS_IDLE_TIMEOUT", 0, minimum=0)
"""Seconds of inactivity before self-shutdown. 0 = never (the default).

Only the desktop adapter sets this, for a backend it started itself. A backend
started by hand keeps running.
"""
```

Add the class after `_error_message`:

```python
class IdleShutdownWatchdog:
    """Fires ``on_shutdown`` after ``timeout`` seconds with no synthesis.

    Pure logic: it never reads the wall clock directly and never starts a
    thread, so its behavior is fully testable with an injected clock. Call
    :meth:`poll` from a supervisor loop.
    """

    def __init__(
        self,
        timeout: int,
        on_shutdown: Any,
        clock: Any = time.monotonic,
    ) -> None:
        self._timeout = timeout
        self._on_shutdown = on_shutdown
        self._clock = clock
        self._lock = threading.Lock()
        self._active = 0
        self._last_activity = clock()
        self.fired = False

    def begin_request(self) -> None:
        """Mark a synthesis request as started."""
        with self._lock:
            self._active += 1

    def end_request(self) -> None:
        """Mark a synthesis request as finished and restart the idle window."""
        with self._lock:
            if self._active > 0:
                self._active -= 1
            self._last_activity = self._clock()

    def poll(self) -> bool:
        """Return True exactly once, when the idle window has elapsed."""
        with self._lock:
            if self.fired or self._timeout <= 0 or self._active > 0:
                return False
            if self._clock() - self._last_activity <= self._timeout:
                return False
            self.fired = True
        logger.info("Idle for %ds with no synthesis; shutting down.", self._timeout)
        self._on_shutdown()
        return True


_IDLE_WATCHDOG: IdleShutdownWatchdog | None = None
"""Set by create_app() when idle shutdown is enabled."""
```

- [ ] **Step 4: Start it and hook synthesis only**

In `create_app()`, after the voice-cache initialisation block, add:

```python
    global _IDLE_WATCHDOG
    if _IDLE_WATCHDOG is None and TTS_IDLE_TIMEOUT > 0:
        _IDLE_WATCHDOG = IdleShutdownWatchdog(
            timeout=TTS_IDLE_TIMEOUT,
            on_shutdown=lambda: os.kill(os.getpid(), signal.SIGTERM),
        )
        watchdog = _IDLE_WATCHDOG

        def _idle_supervisor() -> None:
            while not watchdog.poll():
                time.sleep(1.0)

        threading.Thread(
            target=_idle_supervisor, name="idle-shutdown", daemon=True
        ).start()
        logger.info("Idle shutdown armed: %ds.", TTS_IDLE_TIMEOUT)
```

Shutdown goes through `SIGTERM` so the existing `_handle_shutdown` handler unwinds the WSGI server from the main thread.

In `generate_and_download_tts`, wrap only the synthesis attempt. Immediately after the `cancel_event = ...` assignment add:

```python
        if _IDLE_WATCHDOG is not None:
            _IDLE_WATCHDOG.begin_request()
```

and in that statement's existing `finally:` block, before the token release:

```python
            if _IDLE_WATCHDOG is not None:
                _IDLE_WATCHDOG.end_request()
```

Do not touch `/health`, `/voices`, or the `before_request`/`after_request` hooks: polling health must not extend the window.

- [ ] **Step 5: Document the setting**

Add `"idle_timeout": 0,` to `config.example.json` directly after the `"queue_timeout": 30,` line.

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (116 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 7: Commit**

```bash
git add server.py config.example.json tests/test_server.py
git commit -m "feat(server): opt-in idle shutdown for adapter-started backends"
```

## Task 4: Adapter configuration and parameter mapping

**Implementer tier:** Standard

**Files:**

- Create: `desktop/__init__.py`
- Create: `desktop/settings.py`
- Test: `tests/test_desktop_settings.py`

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces, in `desktop/settings.py`:
  - `@dataclass(frozen=True) class AdapterConfig` with fields `backend_url: str`, `autostart: bool`, `idle_timeout: int`, `startup_timeout: int`, `request_timeout: int`, `max_chunk_chars: int`, `ffmpeg_path: str`.
  - `DEFAULTS: AdapterConfig` — `http://127.0.0.1:5000`, `True`, `300`, `30`, `120`, `400`, `"ffmpeg"`.
  - `config_path() -> pathlib.Path` — `$XDG_CONFIG_HOME/free-tts/config.json`, falling back to `~/.config`.
  - `load_config(path: pathlib.Path | None = None, env: Mapping[str, str] | None = None) -> AdapterConfig`.
  - `map_rate(rate: int) -> str`, `map_pitch(pitch: int) -> str`, `map_volume(volume: int) -> float`.
- `desktop/__init__.py` is empty apart from a one-line docstring.

- [ ] **Step 1: Write the failing test**

Create `tests/test_desktop_settings.py`:

```python
"""Adapter config resolution and Speech Dispatcher parameter mapping."""

import json

import pytest

from desktop import settings


class TestLoadConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = settings.load_config(tmp_path / "nope.json", env={})
        assert cfg == settings.DEFAULTS
        assert cfg.backend_url == "http://127.0.0.1:5000"
        assert cfg.idle_timeout == 300

    def test_file_overrides_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"backend_url": "http://127.0.0.1:6001"}))
        cfg = settings.load_config(path, env={})
        assert cfg.backend_url == "http://127.0.0.1:6001"
        assert cfg.autostart is True

    def test_env_overrides_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"backend_url": "http://127.0.0.1:6001"}))
        cfg = settings.load_config(
            path, env={"FREE_TTS_BACKEND_URL": "http://127.0.0.1:7002"}
        )
        assert cfg.backend_url == "http://127.0.0.1:7002"

    def test_trailing_slash_stripped(self, tmp_path):
        cfg = settings.load_config(
            tmp_path / "x.json", env={"FREE_TTS_BACKEND_URL": "http://h:5000/"}
        )
        assert cfg.backend_url == "http://h:5000"

    def test_autostart_false_from_env(self, tmp_path):
        cfg = settings.load_config(
            tmp_path / "x.json", env={"FREE_TTS_AUTOSTART": "false"}
        )
        assert cfg.autostart is False

    def test_malformed_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not json")
        assert settings.load_config(path, env={}) == settings.DEFAULTS

    def test_invalid_int_falls_back_to_default(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"idle_timeout": "soon"}))
        assert settings.load_config(path, env={}).idle_timeout == 300

    def test_negative_int_clamped_to_zero(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"idle_timeout": -5}))
        assert settings.load_config(path, env={}).idle_timeout == 0

    def test_config_path_respects_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert settings.config_path() == tmp_path / "free-tts" / "config.json"

    def test_config_path_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert settings.config_path() == tmp_path / ".config" / "free-tts" / "config.json"


class TestMapRate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0, "+0%"),
            (100, "+200%"),
            (50, "+100%"),
            (-100, "-50%"),
            (-50, "-25%"),
            (200, "+200%"),
            (-200, "-50%"),
        ],
    )
    def test_map_rate(self, raw, expected):
        assert settings.map_rate(raw) == expected

    def test_monotonic(self):
        values = [
            int(settings.map_rate(r).rstrip("%")) for r in range(-100, 101, 10)
        ]
        assert values == sorted(values)


class TestMapPitch:
    @pytest.mark.parametrize(
        "raw,expected",
        [(0, "+0Hz"), (100, "+50Hz"), (-100, "-50Hz"), (50, "+25Hz"), (300, "+50Hz")],
    )
    def test_map_pitch(self, raw, expected):
        assert settings.map_pitch(raw) == expected


class TestMapVolume:
    @pytest.mark.parametrize(
        "raw,expected", [(100, 1.0), (0, 0.5), (-100, 0.0), (-200, 0.0), (200, 1.0)]
    )
    def test_map_volume(self, raw, expected):
        assert settings.map_volume(raw) == pytest.approx(expected)
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_settings.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'desktop'`.

- [ ] **Step 3: Write the implementation**

Create `desktop/__init__.py`:

```python
"""Desktop integration for free-tts (Speech Dispatcher output module)."""
```

Create `desktop/settings.py`:

```python
"""Adapter configuration and Speech Dispatcher parameter mapping.

Configuration precedence is env var > config file > built-in default, matching
server.py. This module is pure: no I/O beyond reading the config file, and no
dependency on the rest of the package.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, replace

logger = logging.getLogger("free-tts.settings")


@dataclass(frozen=True)
class AdapterConfig:
    """Resolved adapter settings."""

    backend_url: str
    autostart: bool
    idle_timeout: int
    startup_timeout: int
    request_timeout: int
    max_chunk_chars: int
    ffmpeg_path: str


DEFAULTS = AdapterConfig(
    backend_url="http://127.0.0.1:5000",
    autostart=True,
    idle_timeout=300,
    startup_timeout=30,
    request_timeout=120,
    max_chunk_chars=400,
    ffmpeg_path="ffmpeg",
)

_INT_FIELDS = ("idle_timeout", "startup_timeout", "request_timeout", "max_chunk_chars")
_STR_FIELDS = ("backend_url", "ffmpeg_path")


def config_path() -> pathlib.Path:
    """Return the adapter's own config path, honouring XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return base / "free-tts" / "config.json"


def _as_bool(raw: object, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    logger.warning("Invalid boolean %r; using %r.", raw, default)
    return default


def _as_int(raw: object, default: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Invalid integer %r; using %d.", raw, default)
        return default
    return max(0, value)


def load_config(
    path: pathlib.Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AdapterConfig:
    """Resolve settings from env vars, then the config file, then defaults."""
    env = os.environ if env is None else env
    path = config_path() if path is None else path

    raw: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
            else:
                logger.warning("%s is not a JSON object; ignoring.", path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)

    updates: dict[str, object] = {}
    for field in _STR_FIELDS:
        value = env.get(f"FREE_TTS_{field.upper()}", raw.get(field))
        if value is not None:
            updates[field] = str(value).strip().rstrip("/")
    for field in _INT_FIELDS:
        value = env.get(f"FREE_TTS_{field.upper()}", raw.get(field))
        if value is not None:
            updates[field] = _as_int(value, getattr(DEFAULTS, field))
    autostart = env.get("FREE_TTS_AUTOSTART", raw.get("autostart"))
    if autostart is not None:
        updates["autostart"] = _as_bool(autostart, DEFAULTS.autostart)

    return replace(DEFAULTS, **updates)  # type: ignore[arg-type]


def _clamp(value: int) -> int:
    return max(-100, min(100, value))


def map_rate(rate: int) -> str:
    """Map Speech Dispatcher rate (-100..100) to an edge-tts rate string.

    Negative maps to -50%..0% and positive to 0%..+200%, matching the range the
    web frontend already exposes. Monotonic across the whole domain.
    """
    value = _clamp(rate)
    percent = round(value * 0.5) if value < 0 else round(value * 2)
    return f"+{percent}%" if percent >= 0 else f"{percent}%"


def map_pitch(pitch: int) -> str:
    """Map Speech Dispatcher pitch (-100..100) to an edge-tts Hz offset."""
    hz = round(_clamp(pitch) * 0.5)
    return f"+{hz}Hz" if hz >= 0 else f"{hz}Hz"


def map_volume(volume: int) -> float:
    """Map Speech Dispatcher volume (-100..100) to a 0.0..1.0 PCM gain."""
    return (_clamp(volume) + 100) / 200.0
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (143 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 5: Commit**

```bash
git add desktop/__init__.py desktop/settings.py tests/test_desktop_settings.py
git commit -m "feat(desktop): adapter config and Speech Dispatcher parameter mapping"
```

## Task 5: Backend controller with on-demand startup

**Implementer tier:** Advanced

**Files:**

- Create: `desktop/backend.py`
- Test: `tests/test_desktop_backend.py`

**Interfaces:**

- Consumes: `AdapterConfig`, `DEFAULTS`, `load_config` from `desktop/settings.py` (Task 4). `AdapterConfig` is a frozen dataclass with fields `backend_url: str`, `autostart: bool`, `idle_timeout: int`, `startup_timeout: int`, `request_timeout: int`, `max_chunk_chars: int`, `ffmpeg_path: str`. Also consumes the `/health` contract from Task 1: `{"status": "ok", "service": "free-tts", "api_version": 1, "voice_cache_ready": bool}`.
- Produces, in `desktop/backend.py`:
  - `EXPECTED_API_VERSION: int = 1`
  - `class BackendUnavailable(Exception)`
  - `@dataclass(frozen=True) class Health` with `reachable: bool`, `service_ok: bool`, `voice_cache_ready: bool`, `detail: str = ""`
  - `install_root() -> pathlib.Path`, `runtime_log_path() -> pathlib.Path`, `lock_path() -> pathlib.Path`
  - `class BackendController` with `__init__(self, config, *, fetch=None, spawn=None, sleep=time.sleep, clock=time.monotonic, lock_factory=None)`, methods `probe() -> Health` and `ensure_ready() -> None`, and property `started_by_adapter -> bool`.
  - `fetch` is `Callable[[str, float], object]` returning parsed JSON and raising `OSError` when unreachable.
  - `spawn` is `Callable[[list[str], dict[str, str], pathlib.Path], subprocess.Popen]`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_desktop_backend.py`:

```python
"""Backend health probing, ownership rules, and on-demand startup."""

import contextlib
import dataclasses

import pytest

from desktop import backend, settings


@contextlib.contextmanager
def _noop_lock():
    yield


def _config(**overrides):
    return dataclasses.replace(settings.DEFAULTS, **overrides)


HEALTHY = {
    "status": "ok",
    "service": "free-tts",
    "api_version": 1,
    "voice_cache_ready": True,
}


class _FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.pid = 4242

    def poll(self):
        return self._exit_code


def _controller(responses, *, config=None, spawn=None, clock=None):
    """Build a controller whose probe answers come from a scripted list."""
    calls = {"fetch": 0, "spawn": []}
    queue = list(responses)

    def fetch(url, timeout):
        calls["fetch"] += 1
        item = queue.pop(0) if queue else queue_last[0]
        queue_last[0] = item
        if isinstance(item, Exception):
            raise item
        return item

    queue_last = [responses[-1] if responses else OSError("refused")]

    def default_spawn(command, env, log_path):
        calls["spawn"].append((command, env, log_path))
        return _FakeProc()

    ticks = [0.0]

    def default_clock():
        ticks[0] += 1.0
        return ticks[0]

    ctrl = backend.BackendController(
        config or _config(),
        fetch=fetch,
        spawn=spawn or default_spawn,
        sleep=lambda _seconds: None,
        clock=clock or default_clock,
        lock_factory=_noop_lock,
    )
    return ctrl, calls


class TestProbe:
    def test_healthy_backend(self):
        ctrl, _ = _controller([HEALTHY])
        health = ctrl.probe()
        assert health.reachable is True
        assert health.service_ok is True
        assert health.voice_cache_ready is True

    def test_unreachable_backend(self):
        ctrl, _ = _controller([OSError("connection refused")])
        health = ctrl.probe()
        assert health.reachable is False
        assert health.service_ok is False

    def test_wrong_service_is_not_ok(self):
        ctrl, _ = _controller([{"status": "ok", "service": "grafana"}])
        health = ctrl.probe()
        assert health.reachable is True
        assert health.service_ok is False
        assert "service" in health.detail

    def test_unsupported_api_version_is_not_ok(self):
        ctrl, _ = _controller(
            [{"status": "ok", "service": "free-tts", "api_version": 99}]
        )
        health = ctrl.probe()
        assert health.service_ok is False
        assert "api_version" in health.detail

    def test_non_dict_payload_is_not_ok(self):
        ctrl, _ = _controller([["not", "a", "dict"]])
        assert ctrl.probe().service_ok is False


class TestEnsureReady:
    def test_reuses_healthy_backend_without_spawning(self):
        ctrl, calls = _controller([HEALTHY])
        ctrl.ensure_ready()
        assert calls["spawn"] == []
        assert ctrl.started_by_adapter is False

    def test_starts_backend_when_unreachable(self):
        ctrl, calls = _controller(
            [OSError("refused"), OSError("refused"), HEALTHY]
        )
        ctrl.ensure_ready()
        assert len(calls["spawn"]) == 1
        assert ctrl.started_by_adapter is True

    def test_spawn_passes_idle_timeout(self):
        ctrl, calls = _controller([OSError("refused"), OSError("refused"), HEALTHY])
        ctrl.ensure_ready()
        _command, env, _log = calls["spawn"][0]
        assert env["TTS_IDLE_TIMEOUT"] == "300"

    def test_spawn_omitted_when_autostart_disabled(self):
        ctrl, calls = _controller(
            [OSError("refused")], config=_config(autostart=False)
        )
        with pytest.raises(backend.BackendUnavailable, match="autostart"):
            ctrl.ensure_ready()
        assert calls["spawn"] == []

    def test_port_conflict_never_spawns_or_speaks(self):
        ctrl, calls = _controller([{"status": "ok", "service": "other"}])
        with pytest.raises(backend.BackendUnavailable, match="another service"):
            ctrl.ensure_ready()
        assert calls["spawn"] == []
        assert ctrl.started_by_adapter is False

    def test_second_call_skips_probe_once_ready(self):
        ctrl, calls = _controller([HEALTHY])
        ctrl.ensure_ready()
        first = calls["fetch"]
        ctrl.ensure_ready()
        assert calls["fetch"] == first

    def test_recheck_under_lock_adopts_backend_started_by_racer(self):
        """Unreachable before the lock, healthy inside it: do not spawn."""
        ctrl, calls = _controller([OSError("refused"), HEALTHY])
        ctrl.ensure_ready()
        assert calls["spawn"] == []
        assert ctrl.started_by_adapter is False

    def test_startup_timeout_reports_log_path(self):
        ctrl, _calls = _controller(
            [OSError("refused")], config=_config(startup_timeout=3)
        )
        with pytest.raises(backend.BackendUnavailable) as excinfo:
            ctrl.ensure_ready()
        assert "log" in str(excinfo.value).lower()

    def test_early_exit_reports_exit_code(self):
        def dying_spawn(command, env, log_path):
            return _FakeProc(exit_code=1)

        ctrl, _calls = _controller([OSError("refused")], spawn=dying_spawn)
        with pytest.raises(backend.BackendUnavailable, match="exited"):
            ctrl.ensure_ready()

    def test_never_stops_backend_it_did_not_start(self):
        ctrl, _ = _controller([HEALTHY])
        ctrl.ensure_ready()
        assert not hasattr(ctrl, "stop")
        assert ctrl.started_by_adapter is False


class TestPaths:
    def test_install_root_prefers_explicit_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FREE_TTS_HOME", str(tmp_path / "custom"))
        assert backend.install_root() == tmp_path / "custom"

    def test_install_root_uses_xdg_data_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FREE_TTS_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert backend.install_root() == tmp_path / "free-tts"

    def test_lock_path_uses_runtime_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert backend.lock_path() == tmp_path / "free-tts" / "startup.lock"

    def test_real_file_lock_round_trips(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        with backend.file_lock():
            assert backend.lock_path().exists()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_backend.py -q`
Expected: FAIL, `ImportError: cannot import name 'backend' from 'desktop'`.

- [ ] **Step 3: Write the implementation**

Create `desktop/backend.py`:

```python
"""Backend discovery, ownership, and on-demand startup.

Ownership rule, enforced structurally: this module offers no way to stop a
backend. A backend found already running is used and left alone; one this
adapter starts is given a self-managed idle timeout instead of being supervised.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from desktop.settings import AdapterConfig

logger = logging.getLogger("free-tts.backend")

EXPECTED_API_VERSION = 1
_SERVICE_NAME = "free-tts"
_PROBE_TIMEOUT = 3.0
_MAX_LOG_BYTES = 1 << 20


class BackendUnavailable(Exception):
    """The backend cannot be used, with a reason safe to log."""


@dataclass(frozen=True)
class Health:
    """Outcome of one /health probe."""

    reachable: bool
    service_ok: bool
    voice_cache_ready: bool
    detail: str = ""


def install_root() -> pathlib.Path:
    """Directory holding the installed runtime copy."""
    explicit = os.environ.get("FREE_TTS_HOME")
    if explicit:
        return pathlib.Path(explicit)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    return base / "free-tts"


def runtime_log_path() -> pathlib.Path:
    """Log file for a backend this adapter starts."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".cache"
    return base / "free-tts" / "backend.log"


def lock_path() -> pathlib.Path:
    """Startup lock, so concurrent first requests start at most one backend."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = (
        pathlib.Path(runtime)
        if runtime
        else pathlib.Path(os.environ.get("TMPDIR", "/tmp"))
    )
    return base / "free-tts" / "startup.lock"


@contextlib.contextmanager
def file_lock() -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of the block."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _fetch_json(url: str, timeout: float) -> object:
    """GET and parse JSON. Raises OSError when the endpoint is unreachable."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"status": "error", "http_status": exc.code}
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise OSError(str(exc)) from exc


def _spawn_backend(
    command: list[str], env: dict[str, str], log_path: pathlib.Path
) -> subprocess.Popen:
    """Start the backend detached, with output appended to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > _MAX_LOG_BYTES:
        log_path.write_bytes(b"")
    handle = log_path.open("ab", buffering=0)
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
        env=env,
        start_new_session=True,
        cwd=str(install_root()),
    )


class BackendController:
    """Finds, validates, and if needed starts the synthesis backend."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        fetch: Callable[[str, float], object] | None = None,
        spawn: Callable[[list[str], dict[str, str], pathlib.Path], object]
        | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        lock_factory: Callable[[], object] | None = None,
    ) -> None:
        self._config = config
        self._fetch = fetch or _fetch_json
        self._spawn = spawn or _spawn_backend
        self._sleep = sleep
        self._clock = clock
        self._lock_factory = lock_factory or file_lock
        self._ready = False
        self._started_by_adapter = False

    @property
    def started_by_adapter(self) -> bool:
        """True only when this adapter launched the running backend."""
        return self._started_by_adapter

    def probe(self) -> Health:
        """Check the backend's identity and readiness."""
        url = f"{self._config.backend_url}/health"
        try:
            payload = self._fetch(url, _PROBE_TIMEOUT)
        except OSError as exc:
            return Health(False, False, False, str(exc))
        if not isinstance(payload, dict):
            return Health(True, False, False, "health response was not an object")
        if payload.get("service") != _SERVICE_NAME:
            return Health(
                True,
                False,
                False,
                f"unexpected service {payload.get('service')!r} on this port",
            )
        try:
            version = int(payload.get("api_version", 0))
        except (TypeError, ValueError):
            version = 0
        if version != EXPECTED_API_VERSION:
            return Health(
                True, False, False, f"unsupported api_version {version!r}"
            )
        return Health(True, True, bool(payload.get("voice_cache_ready")), "")

    def ensure_ready(self) -> None:
        """Guarantee a usable backend, starting one only when necessary."""
        if self._ready:
            return
        health = self.probe()
        if health.service_ok:
            logger.info("Reusing the backend already running; leaving it alone.")
            self._ready = True
            return
        self._reject_if_occupied(health)
        if not self._config.autostart:
            raise BackendUnavailable(
                "backend is not running and autostart is disabled"
            )
        with self._lock_factory():  # type: ignore[union-attr]
            health = self.probe()
            if health.service_ok:
                logger.info("Backend appeared while waiting for the lock.")
                self._ready = True
                return
            self._reject_if_occupied(health)
            self._start_and_wait()

    def _reject_if_occupied(self, health: Health) -> None:
        if health.reachable and not health.service_ok:
            raise BackendUnavailable(
                f"{self._config.backend_url} is served by another service; "
                f"refusing to start a second backend or speak to it "
                f"({health.detail})"
            )

    def _server_command(self) -> list[str]:
        root = install_root()
        venv_python = root / ".venv" / "bin" / "python"
        interpreter = str(venv_python) if venv_python.exists() else sys.executable
        return [interpreter, str(root / "server.py")]

    def _spawn_env(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self._config.backend_url)
        env = dict(os.environ)
        env["TTS_IDLE_TIMEOUT"] = str(self._config.idle_timeout)
        if parsed.hostname:
            env["TTS_HOST"] = parsed.hostname
        env["TTS_PORT"] = str(parsed.port or 5000)
        env.pop("FLASK_DEBUG", None)
        return env

    def _start_and_wait(self) -> None:
        log_path = runtime_log_path()
        command = self._server_command()
        logger.info("Starting backend on demand: %s", " ".join(command))
        process = self._spawn(command, self._spawn_env(), log_path)
        deadline = self._clock() + self._config.startup_timeout
        while self._clock() < deadline:
            exit_code = process.poll()  # type: ignore[attr-defined]
            if exit_code is not None:
                raise BackendUnavailable(
                    f"backend exited with code {exit_code} during startup; "
                    f"see log {log_path}"
                )
            if self.probe().service_ok:
                self._ready = True
                self._started_by_adapter = True
                logger.info("Backend ready; idle shutdown handled by the backend.")
                return
            self._sleep(0.25)
        raise BackendUnavailable(
            f"backend did not become ready within "
            f"{self._config.startup_timeout}s; see log {log_path}"
        )
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (164 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 5: Commit**

```bash
git add desktop/backend.py tests/test_desktop_backend.py
git commit -m "feat(desktop): backend health probing and on-demand startup"
```

## Task 6: SSML chunking and voice resolution

**Implementer tier:** Standard

**Files:**

- Create: `desktop/chunks.py`
- Create: `desktop/voices.py`
- Test: `tests/test_desktop_chunks.py`
- Test: `tests/test_desktop_voices.py`

**Interfaces:**

- Consumes: nothing from earlier tasks.
- Produces, in `desktop/chunks.py`:
  - `@dataclass(frozen=True) class Chunk` with `text: str` and `mark: str | None`.
  - `strip_ssml(message: str) -> str`
  - `split_marked(ssml: str, max_chars: int = 400) -> list[Chunk]`
- Produces, in `desktop/voices.py`:
  - `@dataclass(frozen=True) class Voice` with `name: str`, `locale: str`, `gender: str`.
  - `class VoiceCatalog` with classmethod `from_payload(payload: object) -> VoiceCatalog`, and methods `__len__`, `protocol_rows() -> list[tuple[str, str, str]]`, `resolve(synthesis_voice: str | None = None, language: str | None = None, voice_type: str | None = None) -> Voice | None`, plus attribute `default_voice: str | None`.

- [ ] **Step 1: Write the failing chunk test**

Create `tests/test_desktop_chunks.py`:

```python
"""Speech Dispatcher SSML is split at __spd_ index marks, without an XML parser."""

from desktop.chunks import Chunk, split_marked, strip_ssml


class TestStripSsml:
    def test_plain_text_untouched(self):
        assert strip_ssml("Plain text.") == "Plain text."

    def test_tags_removed(self):
        assert strip_ssml("Hello <emphasis>world</emphasis>.") == "Hello world."

    def test_self_closing_tag_removed(self):
        assert strip_ssml('<speak>Hi <break time="500ms"/>there</speak>') == "Hi there"

    def test_named_entities_decoded(self):
        assert strip_ssml("&lt;a&gt; &amp; &quot;b&quot; &apos;c&apos;") == '<a> & "b" \'c\''

    def test_unknown_entity_kept(self):
        assert strip_ssml("Unknown &copy; stays.") == "Unknown &copy; stays."

    def test_unterminated_tag_dropped(self):
        assert strip_ssml("Keep <unfinished") == "Keep "


class TestSplitMarked:
    def test_splits_at_marks(self):
        ssml = (
            '<speak>Hello there. <mark name="__spd_0"/> '
            'How are you? <mark name="__spd_1"/></speak>'
        )
        assert split_marked(ssml) == [
            Chunk("Hello there.", "__spd_0"),
            Chunk("How are you?", "__spd_1"),
        ]

    def test_trailing_text_without_mark(self):
        ssml = '<speak>First. <mark name="__spd_0"/> No final mark</speak>'
        assert split_marked(ssml) == [
            Chunk("First.", "__spd_0"),
            Chunk("No final mark", None),
        ]

    def test_unmarked_message_is_one_chunk(self):
        assert split_marked("<speak>Just one sentence</speak>") == [
            Chunk("Just one sentence", None)
        ]

    def test_plain_text_message(self):
        assert split_marked("no tags at all") == [Chunk("no tags at all", None)]

    def test_empty_message_yields_nothing(self):
        assert split_marked("<speak>   </speak>") == []

    def test_blank_segments_dropped_but_marks_preserved(self):
        ssml = (
            '<speak>One. <mark name="__spd_0"/> <mark name="__spd_1"/> Two.</speak>'
        )
        assert split_marked(ssml) == [Chunk("One.", "__spd_0"), Chunk("Two.", None)]

    def test_entities_decoded_inside_chunks(self):
        ssml = '<speak>Tom &amp; Jerry. <mark name="__spd_0"/></speak>'
        assert split_marked(ssml) == [Chunk("Tom & Jerry.", "__spd_0")]

    def test_long_chunk_split_at_whitespace(self):
        words = " ".join(["word"] * 50)
        chunks = split_marked(f"<speak>{words}</speak>", max_chars=40)
        assert len(chunks) > 1
        assert all(len(chunk.text) <= 40 for chunk in chunks)
        assert " ".join(chunk.text for chunk in chunks) == words

    def test_long_chunk_keeps_mark_on_last_piece_only(self):
        words = " ".join(["word"] * 30)
        ssml = f'<speak>{words} <mark name="__spd_7"/></speak>'
        chunks = split_marked(ssml, max_chars=40)
        assert chunks[-1].mark == "__spd_7"
        assert all(chunk.mark is None for chunk in chunks[:-1])

    def test_unbreakable_run_is_hard_split(self):
        run = "x" * 100
        chunks = split_marked(f"<speak>{run}</speak>", max_chars=40)
        assert all(len(chunk.text) <= 40 for chunk in chunks)
        assert "".join(chunk.text for chunk in chunks) == run

    def test_non_spd_marks_are_not_split_points(self):
        ssml = '<speak>A <mark name="custom"/> B. <mark name="__spd_0"/></speak>'
        assert split_marked(ssml) == [Chunk("A  B.", "__spd_0")]
```

- [ ] **Step 2: Write the failing voice test**

Create `tests/test_desktop_voices.py`:

```python
"""Voice catalog построение and resolution rules."""

from desktop.voices import Voice, VoiceCatalog

PAYLOAD = {
    "default_voice": "en-US-AvaMultilingualNeural",
    "voices": [
        {
            "ShortName": "en-US-AvaMultilingualNeural",
            "Gender": "Female",
            "Locale": "en-US",
        },
        {"ShortName": "en-US-AndrewNeural", "Gender": "Male", "Locale": "en-US"},
        {"ShortName": "en-GB-SoniaNeural", "Gender": "Female", "Locale": "en-GB"},
        {"ShortName": "fr-FR-DeniseNeural", "Gender": "Female", "Locale": "fr-FR"},
        {"ShortName": "fr-FR-HenriNeural", "Gender": "Male", "Locale": "fr-FR"},
    ],
}


def _catalog():
    return VoiceCatalog.from_payload(PAYLOAD)


class TestFromPayload:
    def test_all_voices_loaded(self):
        assert len(_catalog()) == 5

    def test_default_voice_recorded(self):
        assert _catalog().default_voice == "en-US-AvaMultilingualNeural"

    def test_malformed_payload_is_empty(self):
        assert len(VoiceCatalog.from_payload(["nope"])) == 0

    def test_entries_missing_name_skipped(self):
        catalog = VoiceCatalog.from_payload({"voices": [{"Locale": "en-US"}]})
        assert len(catalog) == 0


class TestProtocolRows:
    def test_rows_use_exact_names_and_variant_none(self):
        rows = _catalog().protocol_rows()
        assert ("en-US-AvaMultilingualNeural", "en-US", "none") in rows
        assert all(variant == "none" for _name, _lang, variant in rows)

    def test_every_voice_is_listed(self):
        assert len(_catalog().protocol_rows()) == 5


class TestResolve:
    def test_exact_synthesis_voice_wins(self):
        voice = _catalog().resolve(
            synthesis_voice="fr-FR-HenriNeural", language="en-US"
        )
        assert voice == Voice("fr-FR-HenriNeural", "fr-FR", "Male")

    def test_null_synthesis_voice_ignored(self):
        voice = _catalog().resolve(synthesis_voice="NULL", language="fr-FR")
        assert voice is not None and voice.locale == "fr-FR"

    def test_unknown_synthesis_voice_falls_back_to_language(self):
        voice = _catalog().resolve(synthesis_voice="nope", language="en-GB")
        assert voice == Voice("en-GB-SoniaNeural", "en-GB", "Female")

    def test_language_underscore_form_matches(self):
        voice = _catalog().resolve(language="fr_FR")
        assert voice is not None and voice.locale == "fr-FR"

    def test_language_prefix_matches_region_variant(self):
        voice = _catalog().resolve(language="fr")
        assert voice is not None and voice.locale == "fr-FR"

    def test_female_voice_type_prefers_female(self):
        voice = _catalog().resolve(language="fr-FR", voice_type="female1")
        assert voice == Voice("fr-FR-DeniseNeural", "fr-FR", "Female")

    def test_male_voice_type_prefers_male(self):
        voice = _catalog().resolve(language="fr-FR", voice_type="male1")
        assert voice == Voice("fr-FR-HenriNeural", "fr-FR", "Male")

    def test_male2_falls_back_when_only_one_male(self):
        voice = _catalog().resolve(language="fr-FR", voice_type="male2")
        assert voice == Voice("fr-FR-HenriNeural", "fr-FR", "Male")

    def test_child_voice_type_still_resolves(self):
        voice = _catalog().resolve(language="en-US", voice_type="child_female")
        assert voice is not None and voice.gender == "Female"

    def test_unknown_language_uses_default_voice(self):
        voice = _catalog().resolve(language="ja-JP")
        assert voice is not None
        assert voice.name == "en-US-AvaMultilingualNeural"

    def test_first_voice_used_when_no_default(self):
        catalog = VoiceCatalog.from_payload({"voices": PAYLOAD["voices"]})
        voice = catalog.resolve(language="ja-JP")
        assert voice is not None
        assert voice.name == "en-US-AvaMultilingualNeural"

    def test_empty_catalog_resolves_to_none(self):
        assert VoiceCatalog.from_payload({}).resolve(language="en-US") is None
```

Replace the docstring's stray non-English word: the first line must read `"""Voice catalog construction and resolution rules."""`.

- [ ] **Step 3: Run both tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_desktop_chunks.py tests/test_desktop_voices.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'desktop.chunks'`.

- [ ] **Step 4: Write `desktop/chunks.py`**

```python
"""Split Speech Dispatcher SSML into synthesis-sized chunks at index marks.

No XML parser is used. Upstream's Python module helper strips SSML with a plain
character scanner, and following that keeps entity expansion impossible while
staying byte-compatible with what the server sends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MARK_PATTERN = re.compile(r'<mark\s+name="(__spd_[^"]*)"\s*/>')
"""Server-inserted index marks; only these are chunk boundaries."""

_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&amp;", "&"),
    ("&quot;", '"'),
    ("&apos;", "'"),
)


@dataclass(frozen=True)
class Chunk:
    """One synthesis unit and the index mark that ends it, if any."""

    text: str
    mark: str | None


def strip_ssml(message: str) -> str:
    """Remove markup and decode the five XML entities, like upstream does."""
    out: list[str] = []
    omit = False
    index = 0
    length = len(message)
    while index < length:
        char = message[index]
        if char == "<":
            omit = True
            index += 1
            continue
        if char == ">":
            omit = False
            index += 1
            continue
        if omit:
            index += 1
            continue
        if char == "&":
            for entity, replacement in _ENTITIES:
                if message.startswith(entity, index):
                    out.append(replacement)
                    index += len(entity)
                    break
            else:
                out.append(char)
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Break over-long text at whitespace, falling back to a hard cut."""
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind(" ")
        if cut <= 0:
            pieces.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
            continue
        pieces.append(remaining[:cut])
        remaining = remaining[cut + 1 :]
    if remaining:
        pieces.append(remaining)
    return pieces


def split_marked(ssml: str, max_chars: int = 400) -> list[Chunk]:
    """Split ``ssml`` into chunks, each ending at its index mark when present.

    A mark belongs to the chunk it terminates, because the server inserts marks
    after sentence-ending punctuation. Reporting a chunk's mark therefore means
    "everything up to here has been spoken".
    """
    chunks: list[Chunk] = []
    cursor = 0
    for match in MARK_PATTERN.finditer(ssml):
        segment = strip_ssml(ssml[cursor : match.start()]).strip()
        cursor = match.end()
        if not segment:
            continue
        pieces = _hard_split(segment, max_chars)
        for piece in pieces[:-1]:
            chunks.append(Chunk(piece, None))
        chunks.append(Chunk(pieces[-1], match.group(1)))
    tail = strip_ssml(ssml[cursor:]).strip()
    if tail:
        for piece in _hard_split(tail, max_chars):
            chunks.append(Chunk(piece, None))
    return chunks
```

- [ ] **Step 5: Write `desktop/voices.py`**

```python
"""Voice catalog built from GET /voices, plus Speech Dispatcher resolution.

The Speech Dispatcher variant field is always "none": Qt folds variant into the
locale, so anything else corrupts the locale it reports. Gender is kept here
only to serve symbolic voice types for non-Qt clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VOICE_TYPE = re.compile(r"^(child_)?(male|female)(\d*)$")


@dataclass(frozen=True)
class Voice:
    """One synthesis voice as exposed to Speech Dispatcher."""

    name: str
    locale: str
    gender: str


def _normalise(tag: str) -> str:
    return tag.replace("_", "-").strip().lower()


class VoiceCatalog:
    """Immutable snapshot of the backend's voice list."""

    def __init__(self, voices: list[Voice], default_voice: str | None) -> None:
        self._voices = voices
        self.default_voice = default_voice

    @classmethod
    def from_payload(cls, payload: object) -> VoiceCatalog:
        """Build a catalog from a parsed /voices response."""
        if not isinstance(payload, dict):
            return cls([], None)
        raw = payload.get("voices")
        voices: list[Voice] = []
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("ShortName", "")).strip()
                if not name:
                    continue
                voices.append(
                    Voice(
                        name=name,
                        locale=str(entry.get("Locale", "")).strip(),
                        gender=str(entry.get("Gender", "")).strip(),
                    )
                )
        default = payload.get("default_voice")
        return cls(voices, str(default) if isinstance(default, str) else None)

    def __len__(self) -> int:
        return len(self._voices)

    def protocol_rows(self) -> list[tuple[str, str, str]]:
        """Return (name, language, variant) rows for LIST VOICES."""
        return [(voice.name, voice.locale or "none", "none") for voice in self._voices]

    def _by_name(self, name: str) -> Voice | None:
        lowered = name.strip().lower()
        for voice in self._voices:
            if voice.name.lower() == lowered:
                return voice
        return None

    def _for_language(self, language: str) -> list[Voice]:
        wanted = _normalise(language)
        if not wanted:
            return []
        exact = [v for v in self._voices if _normalise(v.locale) == wanted]
        if exact:
            return exact
        prefix = wanted.split("-", 1)[0]
        return [v for v in self._voices if _normalise(v.locale).split("-", 1)[0] == prefix]

    @staticmethod
    def _pick_by_type(candidates: list[Voice], voice_type: str) -> Voice | None:
        match = _VOICE_TYPE.match(voice_type.strip().lower())
        if not match:
            return None
        wanted_gender = match.group(2)
        index = int(match.group(3)) - 1 if match.group(3) else 0
        gendered = [
            v for v in candidates if v.gender.strip().lower() == wanted_gender
        ]
        if not gendered:
            return None
        return gendered[index] if 0 <= index < len(gendered) else gendered[0]

    def resolve(
        self,
        synthesis_voice: str | None = None,
        language: str | None = None,
        voice_type: str | None = None,
    ) -> Voice | None:
        """Pick a voice: exact name, then locale, then default, then first."""
        if synthesis_voice and synthesis_voice != "NULL":
            exact = self._by_name(synthesis_voice)
            if exact is not None:
                return exact
        if language and language != "NULL":
            candidates = self._for_language(language)
            if candidates:
                if voice_type and voice_type != "NULL":
                    chosen = self._pick_by_type(candidates, voice_type)
                    if chosen is not None:
                        return chosen
                return candidates[0]
        if self.default_voice:
            fallback = self._by_name(self.default_voice)
            if fallback is not None:
                return fallback
        return self._voices[0] if self._voices else None
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (205 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 7: Commit**

```bash
git add desktop/chunks.py desktop/voices.py tests/test_desktop_chunks.py tests/test_desktop_voices.py
git commit -m "feat(desktop): SSML index-mark chunking and voice resolution"
```

## Task 7: Audio decoding and the synthesis client

**Implementer tier:** Advanced

**Files:**

- Create: `desktop/audio.py`
- Create: `desktop/synth.py`
- Test: `tests/test_desktop_audio.py`
- Test: `tests/test_desktop_synth.py`

**Interfaces:**

- Consumes: `AdapterConfig` from `desktop/settings.py` (Task 4), a frozen dataclass with fields `backend_url`, `autostart`, `idle_timeout`, `startup_timeout`, `request_timeout`, `max_chunk_chars`, `ffmpeg_path`. Consumes the server contract from Tasks 1-2: `POST /generate-and-download-tts` with body `{"ssml": str, "request_id": str}` returning MP3 bytes, `499` when cancelled, `503` with `Retry-After`; `DELETE /tts-request/<id>` returning `200` or `404`.
- Produces, in `desktop/audio.py`:
  - Constants `SAMPLE_RATE: int = 24000`, `BITS: int = 16`, `CHANNELS: int = 1`.
  - `class DecodeError(Exception)`
  - `native_big_endian() -> bool`
  - `decode_mp3(data: bytes, ffmpeg_path: str = "ffmpeg", sample_rate: int = SAMPLE_RATE, runner: Callable[..., object] = subprocess.run) -> bytes`
  - `apply_gain(pcm: bytes, gain: float) -> bytes`
- Produces, in `desktop/synth.py`:
  - `class SynthError(Exception)`, `class Cancelled(SynthError)`
  - `new_request_id() -> str`
  - `build_ssml(text: str, voice_name: str, rate: str, pitch: str) -> str`
  - `class SynthClient` with `__init__(self, config, *, transport=None, sleep=time.sleep)`, methods `voices() -> object`, `synthesize(text: str, voice_name: str, rate: str, pitch: str, request_id: str, should_abort: Callable[[], bool] | None = None) -> bytes`, `cancel(request_id: str) -> None`.
  - `transport` is `Callable[[str, str, bytes | None, float], tuple[int, Mapping[str, str], bytes]]` taking `(method, url, body, timeout)` and returning `(status, headers, body)`.

- [ ] **Step 1: Write the failing audio test**

Create `tests/test_desktop_audio.py`:

```python
"""MP3 decoding through ffmpeg and PCM gain."""

import array
import subprocess
import sys

import pytest

from desktop import audio


class _Result:
    def __init__(self, returncode=0, stdout=b"", stderr=b""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class TestDecodeMp3:
    def test_returns_pcm_from_stdout(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            captured["input"] = kwargs.get("input")
            return _Result(stdout=b"\x01\x00\x02\x00")

        pcm = audio.decode_mp3(b"mp3-bytes", runner=runner)
        assert pcm == b"\x01\x00\x02\x00"
        assert captured["input"] == b"mp3-bytes"

    def test_requests_mono_s16le_at_sample_rate(self):
        captured = {}

        def runner(command, **kwargs):
            captured["command"] = command
            return _Result(stdout=b"\x00\x00")

        audio.decode_mp3(b"x", ffmpeg_path="/usr/bin/ffmpeg", runner=runner)
        command = captured["command"]
        assert command[0] == "/usr/bin/ffmpeg"
        assert "s16le" in command
        assert "pcm_s16le" in command
        assert str(audio.SAMPLE_RATE) in command
        assert command[command.index("-ac") + 1] == "1"

    def test_nonzero_exit_raises_with_stderr(self):
        def runner(command, **kwargs):
            return _Result(returncode=1, stderr=b"bad data")

        with pytest.raises(audio.DecodeError, match="bad data"):
            audio.decode_mp3(b"x", runner=runner)

    def test_empty_output_raises(self):
        def runner(command, **kwargs):
            return _Result(stdout=b"")

        with pytest.raises(audio.DecodeError, match="no audio"):
            audio.decode_mp3(b"x", runner=runner)

    def test_missing_ffmpeg_raises_decode_error(self):
        def runner(command, **kwargs):
            raise FileNotFoundError("ffmpeg")

        with pytest.raises(audio.DecodeError, match="ffmpeg"):
            audio.decode_mp3(b"x", runner=runner)

    def test_odd_length_output_is_trimmed_to_whole_frames(self):
        def runner(command, **kwargs):
            return _Result(stdout=b"\x01\x00\x02")

        assert audio.decode_mp3(b"x", runner=runner) == b"\x01\x00"

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


class TestApplyGain:
    def _samples(self, pcm):
        values = array.array("h")
        values.frombytes(pcm)
        return list(values)

    def test_unity_gain_is_identity(self):
        pcm = array.array("h", [1000, -1000, 32767]).tobytes()
        assert audio.apply_gain(pcm, 1.0) == pcm

    def test_half_gain_halves_samples(self):
        pcm = array.array("h", [1000, -1000]).tobytes()
        assert self._samples(audio.apply_gain(pcm, 0.5)) == [500, -500]

    def test_zero_gain_silences(self):
        pcm = array.array("h", [1000, -1000]).tobytes()
        assert self._samples(audio.apply_gain(pcm, 0.0)) == [0, 0]

    def test_clamps_to_int16_range(self):
        pcm = array.array("h", [32767, -32768]).tobytes()
        assert self._samples(audio.apply_gain(pcm, 4.0)) == [32767, -32768]

    def test_empty_input(self):
        assert audio.apply_gain(b"", 0.5) == b""


class TestEndianness:
    def test_matches_interpreter(self):
        assert audio.native_big_endian() is (sys.byteorder == "big")
```

- [ ] **Step 2: Write the failing synth test**

Create `tests/test_desktop_synth.py`:

```python
"""HTTP synthesis client: SSML building, cancellation, and 503 retry."""

import dataclasses
import json

import pytest

from desktop import settings, synth


def _config(**overrides):
    return dataclasses.replace(settings.DEFAULTS, **overrides)


class _Transport:
    """Scripted transport recording every call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def __call__(self, method, url, body, timeout):
        self.calls.append((method, url, body, timeout))
        if not self._responses:
            raise AssertionError(f"unexpected extra call: {method} {url}")
        item = self._responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


class TestBuildSsml:
    def test_contains_voice_rate_pitch_and_text(self):
        ssml = synth.build_ssml("Hello", "en-US-AvaMultilingualNeural", "+10%", "-5Hz")
        assert 'name="en-US-AvaMultilingualNeural"' in ssml
        assert 'rate="+10%"' in ssml
        assert 'pitch="-5Hz"' in ssml
        assert "Hello" in ssml
        assert ssml.startswith("<speak")

    def test_escapes_text_markup(self):
        ssml = synth.build_ssml("a < b & c > d", "v", "+0%", "+0Hz")
        assert "&lt;" in ssml and "&amp;" in ssml and "&gt;" in ssml

    def test_escapes_quotes_in_voice_name(self):
        ssml = synth.build_ssml("hi", 'v"x', "+0%", "+0Hz")
        assert '"v&quot;x"' in ssml or "&quot;" in ssml


class TestRequestId:
    def test_is_url_safe_and_bounded(self):
        import re

        for _ in range(20):
            token = synth.new_request_id()
            assert re.match(r"^[A-Za-z0-9_-]{1,64}$", token)

    def test_ids_are_unique(self):
        assert len({synth.new_request_id() for _ in range(50)}) == 50


class TestVoices:
    def test_parses_payload(self):
        payload = json.dumps({"voices": [], "default_voice": "v"}).encode()
        transport = _Transport([(200, {}, payload)])
        client = synth.SynthClient(_config(), transport=transport)
        assert client.voices() == {"voices": [], "default_voice": "v"}
        assert transport.calls[0][0] == "GET"
        assert transport.calls[0][1].endswith("/voices")

    def test_non_200_raises(self):
        transport = _Transport([(500, {}, b"{}")])
        client = synth.SynthClient(_config(), transport=transport)
        with pytest.raises(synth.SynthError):
            client.voices()

    def test_invalid_json_raises(self):
        transport = _Transport([(200, {}, b"not json")])
        client = synth.SynthClient(_config(), transport=transport)
        with pytest.raises(synth.SynthError):
            client.voices()


class TestSynthesize:
    def _client(self, responses, sleeps=None):
        transport = _Transport(responses)
        client = synth.SynthClient(
            _config(),
            transport=transport,
            sleep=(sleeps.append if sleeps is not None else (lambda _s: None)),
        )
        return client, transport

    def test_returns_audio_bytes(self):
        client, transport = self._client([(200, {}, b"\xff\xfbmp3")])
        audio_bytes = client.synthesize("hi", "v", "+0%", "+0Hz", "req1")
        assert audio_bytes == b"\xff\xfbmp3"
        method, url, body, _timeout = transport.calls[0]
        assert method == "POST"
        assert url.endswith("/generate-and-download-tts")
        assert json.loads(body)["request_id"] == "req1"

    def test_499_raises_cancelled(self):
        client, _ = self._client([(499, {}, b'{"error":"Request cancelled."}')])
        with pytest.raises(synth.Cancelled):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")

    def test_503_retries_once_then_succeeds(self):
        sleeps = []
        client, transport = self._client(
            [(503, {"Retry-After": "2"}, b"{}"), (200, {}, b"audio")], sleeps=sleeps
        )
        assert client.synthesize("hi", "v", "+0%", "+0Hz", "req1") == b"audio"
        assert len(transport.calls) == 2
        assert sleeps == [2.0]

    def test_503_retry_delay_is_capped(self):
        sleeps = []
        client, _ = self._client(
            [(503, {"Retry-After": "9999"}, b"{}"), (200, {}, b"audio")], sleeps=sleeps
        )
        client.synthesize("hi", "v", "+0%", "+0Hz", "req1")
        assert sleeps == [5.0]

    def test_503_twice_raises(self):
        client, _ = self._client(
            [(503, {"Retry-After": "1"}, b"{}"), (503, {"Retry-After": "1"}, b"{}")]
        )
        with pytest.raises(synth.SynthError, match="busy"):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")

    def test_503_not_retried_when_aborting(self):
        client, transport = self._client([(503, {"Retry-After": "1"}, b"{}")])
        with pytest.raises(synth.Cancelled):
            client.synthesize(
                "hi", "v", "+0%", "+0Hz", "req1", should_abort=lambda: True
            )
        assert len(transport.calls) == 1

    def test_error_body_message_surfaces(self):
        client, _ = self._client([(400, {}, b'{"error":"Unknown voice: v"}')])
        with pytest.raises(synth.SynthError, match="Unknown voice"):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")

    def test_transport_oserror_becomes_syntherror(self):
        client, _ = self._client([OSError("connection reset")])
        with pytest.raises(synth.SynthError, match="connection reset"):
            client.synthesize("hi", "v", "+0%", "+0Hz", "req1")


class TestCancel:
    def test_sends_delete(self):
        transport = _Transport([(200, {}, b'{"cancelled":true}')])
        client = synth.SynthClient(_config(), transport=transport)
        client.cancel("req9")
        method, url, _body, _timeout = transport.calls[0]
        assert method == "DELETE"
        assert url.endswith("/tts-request/req9")

    def test_404_is_not_an_error(self):
        transport = _Transport([(404, {}, b'{"error":"Unknown request id."}')])
        client = synth.SynthClient(_config(), transport=transport)
        client.cancel("gone")

    def test_transport_failure_is_swallowed(self):
        transport = _Transport([OSError("down")])
        client = synth.SynthClient(_config(), transport=transport)
        client.cancel("req9")
```

- [ ] **Step 3: Run both tests and confirm they fail**

Run: `.venv/bin/python -m pytest tests/test_desktop_audio.py tests/test_desktop_synth.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'desktop.audio'`.

- [ ] **Step 4: Write `desktop/audio.py`**

```python
"""Decode backend MP3 into PCM for Speech Dispatcher's audio channel."""

from __future__ import annotations

import array
import logging
import subprocess
import sys
from collections.abc import Callable

logger = logging.getLogger("free-tts.audio")

SAMPLE_RATE = 24000
BITS = 16
CHANNELS = 1
_FRAME_BYTES = CHANNELS * BITS // 8
_INT16_MIN = -32768
_INT16_MAX = 32767


class DecodeError(Exception):
    """ffmpeg could not turn the response into usable PCM."""


def native_big_endian() -> bool:
    """True when this interpreter's native sample order is big-endian."""
    return sys.byteorder == "big"


def decode_mp3(
    data: bytes,
    ffmpeg_path: str = "ffmpeg",
    sample_rate: int = SAMPLE_RATE,
    runner: Callable[..., object] = subprocess.run,
) -> bytes:
    """Decode MP3 bytes to mono 16-bit PCM at ``sample_rate``.

    Output is emitted in the machine's native byte order so it can be handed to
    ``array`` directly; the protocol layer reports the matching endianness flag.
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
        result = runner(command, input=data, capture_output=True, check=False)
    except OSError as exc:
        raise DecodeError(f"could not run ffmpeg ({ffmpeg_path}): {exc}") from exc

    if getattr(result, "returncode", 1) != 0:
        detail = getattr(result, "stderr", b"") or b""
        raise DecodeError(
            f"ffmpeg failed: {detail.decode('utf-8', 'replace').strip()[:200]}"
        )
    pcm = getattr(result, "stdout", b"") or b""
    if not pcm:
        raise DecodeError("ffmpeg produced no audio")
    usable = len(pcm) - (len(pcm) % _FRAME_BYTES)
    return pcm[:usable]


def apply_gain(pcm: bytes, gain: float) -> bytes:
    """Scale 16-bit samples by ``gain``, clamping to the int16 range."""
    if not pcm or gain == 1.0:
        return pcm
    samples = array.array("h")
    samples.frombytes(pcm)
    scaled = array.array(
        "h",
        (
            _INT16_MIN
            if value * gain < _INT16_MIN
            else _INT16_MAX
            if value * gain > _INT16_MAX
            else int(value * gain)
            for value in samples
        ),
    )
    return scaled.tobytes()
```

- [ ] **Step 5: Write `desktop/synth.py`**

```python
"""HTTP client for the free-tts synthesis API."""

from __future__ import annotations

import json
import logging
import secrets
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping

from desktop.settings import AdapterConfig

logger = logging.getLogger("free-tts.synth")

_MAX_RETRY_DELAY = 5.0
_CANCEL_TIMEOUT = 5.0
_VOICES_TIMEOUT = 20.0

SSML_TEMPLATE = (
    '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" '
    'xml:lang="en-US"><voice name="{voice}">'
    '<prosody rate="{rate}" pitch="{pitch}">{text}</prosody></voice></speak>'
)


class SynthError(Exception):
    """Synthesis could not be completed."""


class Cancelled(SynthError):
    """Synthesis was cancelled, by us or by the backend."""


def new_request_id() -> str:
    """Opaque, URL-safe id so a request can be cancelled later."""
    return secrets.token_hex(8)


def _escape_text(value: str) -> str:
    return value.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _escape_attr(value: str) -> str:
    return _escape_text(value).replace('"', "&quot;").replace("'", "&apos;")


def build_ssml(text: str, voice_name: str, rate: str, pitch: str) -> str:
    """Build the SSML document the backend expects for one chunk."""
    return SSML_TEMPLATE.format(
        voice=_escape_attr(voice_name),
        rate=_escape_attr(rate),
        pitch=_escape_attr(pitch),
        text=_escape_text(text),
    )


def _http_transport(
    method: str, url: str, body: bytes | None, timeout: float
) -> tuple[int, Mapping[str, str], bytes]:
    """Perform one HTTP call, mapping HTTP errors onto status codes."""
    headers = {"Content-Type": "application/json"} if body is not None else {}
    request = urllib.request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return response.status, dict(response.headers), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers or {}), exc.read()
    except (urllib.error.URLError, TimeoutError) as exc:
        raise OSError(str(exc)) from exc


class SynthClient:
    """Talks to the local backend over HTTP. Holds no playback state."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        transport: Callable[
            [str, str, bytes | None, float], tuple[int, Mapping[str, str], bytes]
        ]
        | None = None,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._transport = transport or _http_transport
        self._sleep = sleep

    def voices(self) -> object:
        """Fetch and parse the backend's voice catalog payload."""
        url = f"{self._config.backend_url}/voices"
        try:
            status, _headers, body = self._transport("GET", url, None, _VOICES_TIMEOUT)
        except OSError as exc:
            raise SynthError(f"could not load voices: {exc}") from exc
        if status != 200:
            raise SynthError(f"voice listing failed with status {status}")
        try:
            return json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise SynthError(f"voice listing was not valid JSON: {exc}") from exc

    def synthesize(
        self,
        text: str,
        voice_name: str,
        rate: str,
        pitch: str,
        request_id: str,
        should_abort: Callable[[], bool] | None = None,
    ) -> bytes:
        """Return MP3 bytes for one chunk, retrying a single 503."""
        url = f"{self._config.backend_url}/generate-and-download-tts"
        payload = json.dumps(
            {
                "ssml": build_ssml(text, voice_name, rate, pitch),
                "request_id": request_id,
            }
        ).encode("utf-8")

        for attempt in (1, 2):
            if should_abort is not None and should_abort():
                raise Cancelled("aborted before request")
            try:
                status, headers, body = self._transport(
                    "POST", url, payload, float(self._config.request_timeout)
                )
            except OSError as exc:
                raise SynthError(str(exc)) from exc

            if status == 200:
                return body
            if status == 499:
                raise Cancelled("backend reported the request as cancelled")
            if status == 503 and attempt == 1:
                if should_abort is not None and should_abort():
                    raise Cancelled("aborted instead of retrying")
                self._sleep(self._retry_delay(headers))
                continue
            if status == 503:
                raise SynthError("backend busy after retry")
            raise SynthError(self._error_detail(status, body))
        raise SynthError("backend busy after retry")

    def cancel(self, request_id: str) -> None:
        """Best-effort cancellation so the backend slot is freed promptly."""
        url = f"{self._config.backend_url}/tts-request/{request_id}"
        try:
            self._transport("DELETE", url, None, _CANCEL_TIMEOUT)
        except OSError as exc:
            logger.debug("Cancel for %s could not be delivered: %s", request_id, exc)

    @staticmethod
    def _retry_delay(headers: Mapping[str, str]) -> float:
        raw = headers.get("Retry-After") or headers.get("retry-after") or "1"
        try:
            delay = float(raw)
        except ValueError:
            delay = 1.0
        return max(0.0, min(_MAX_RETRY_DELAY, delay))

    @staticmethod
    def _error_detail(status: int, body: bytes) -> str:
        try:
            parsed = json.loads(body.decode("utf-8"))
            if isinstance(parsed, dict) and isinstance(parsed.get("error"), str):
                return f"{parsed['error']} (status {status})"
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        return f"synthesis failed with status {status}"
```

- [ ] **Step 6: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (244 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 7: Commit**

```bash
git add desktop/audio.py desktop/synth.py tests/test_desktop_audio.py tests/test_desktop_synth.py
git commit -m "feat(desktop): ffmpeg PCM decoding and synthesis HTTP client"
```

## Task 8: Speech Dispatcher wire protocol

**Implementer tier:** Advanced

**Files:**

- Create: `desktop/protocol.py`
- Test: `tests/test_desktop_protocol.py`

**Interfaces:**

- Consumes: `SAMPLE_RATE`, `BITS`, `CHANNELS`, `native_big_endian()` from `desktop/audio.py` (Task 7).
- Produces, in `desktop/protocol.py`:
  - Response constants: `OK_LOADED = "299 OK LOADED SUCCESSFULLY"`, `ERR_CANT_INIT = "399 ERR CANT INIT MODULE"`, `OK_RECEIVING_MESSAGE = "202 OK RECEIVING MESSAGE"`, `OK_SPEAKING = "200 OK SPEAKING"`, `ERR_CANT_SPEAK = "301 ERROR CANT SPEAK"`, `OK_RECEIVING_SETTINGS = "203 OK RECEIVING SETTINGS"`, `OK_SETTINGS_RECEIVED = "203 OK SETTINGS RECEIVED"`, `OK_RECEIVING_AUDIO_SETTINGS = "207 OK RECEIVING AUDIO SETTINGS"`, `OK_AUDIO_INITIALIZED = "203 OK AUDIO INITIALIZED"`, `OK_RECEIVING_LOGLEVEL_SETTINGS = "207 OK RECEIVING LOGLEVEL SETTINGS"`, `OK_LOGLEVEL_SET = "203 OK LOGLEVEL SET"`, `OK_QUIT = "210 OK QUIT"`, `ERR_UNKNOWN_COMMAND = "300 ERR UNKNOWN COMMAND"`, `ERR_BAD_SYNTAX = "302 ERROR BAD SYNTAX"`, `ERR_BAD_PARAM = "303 ERROR INVALID PARAMETER OR VALUE"`, `ERR_CANT_LIST_VOICES = "304 CANT LIST VOICES"`, `OK_VOICE_LIST_SENT = "200 OK VOICE LIST SENT"`.
  - `MAX_AUDIO_CHUNK_BYTES: int = 10000`
  - `escape_audio(data: bytes) -> bytes`
  - `parse_settings(lines: list[str]) -> dict[str, str]`
  - `class ProtocolIO` with `__init__(self, stdin, stdout)`, methods `read_line() -> str | None`, `read_data_block() -> list[str]`, `read_message() -> str`, `send(line: str) -> None`, `send_multiline(detail_lines: list[str], final: str) -> None`, `send_voices(rows: list[tuple[str, str, str]]) -> None`, `send_audio(pcm: bytes, sample_rate: int = 24000) -> None`, `event_begin() -> None`, `event_end() -> None`, `event_stop() -> None`, `event_pause() -> None`, `index_mark(mark: str) -> None`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_desktop_protocol.py`:

```python
"""Byte-level conformance with the Speech Dispatcher module protocol."""

import array
import io

from desktop import protocol


def _io(input_bytes=b""):
    stdin = io.BytesIO(input_bytes)
    stdout = io.BytesIO()
    return protocol.ProtocolIO(stdin, stdout), stdout


class TestEscapeAudio:
    def test_plain_bytes_unchanged(self):
        assert protocol.escape_audio(b"\x01\x02\x03") == b"\x01\x02\x03"

    def test_newline_escaped(self):
        assert protocol.escape_audio(b"\n") == b"\x7d\x2a"

    def test_escape_byte_escaped(self):
        assert protocol.escape_audio(b"\x7d") == b"\x7d\x5d"

    def test_mixed_payload(self):
        assert protocol.escape_audio(b"a\nb\x7dc") == b"a\x7d\x2ab\x7d\x5dc"

    def test_empty(self):
        assert protocol.escape_audio(b"") == b""

    def test_escaped_output_contains_no_raw_newline(self):
        payload = bytes(range(256))
        assert b"\n" not in protocol.escape_audio(payload)


class TestReading:
    def test_read_line_strips_newline(self):
        io_, _ = _io(b"SPEAK\n")
        assert io_.read_line() == "SPEAK"

    def test_read_line_returns_none_at_eof(self):
        io_, _ = _io(b"")
        assert io_.read_line() is None

    def test_read_line_decodes_utf8(self):
        io_, _ = _io("caf\u00e9\n".encode("utf-8"))
        assert io_.read_line() == "caf\u00e9"

    def test_read_line_survives_invalid_utf8(self):
        io_, _ = _io(b"\xff\xfe\n")
        assert io_.read_line() is not None

    def test_read_data_block_stops_at_dot(self):
        io_, _ = _io(b"rate=10\npitch=-5\n.\ntrailing\n")
        assert io_.read_data_block() == ["rate=10", "pitch=-5"]
        assert io_.read_line() == "trailing"

    def test_read_data_block_unstuffs_dots(self):
        io_, _ = _io(b"..\n.hidden\n.\n")
        assert io_.read_data_block() == [".", "hidden"]

    def test_read_data_block_at_eof_returns_partial(self):
        io_, _ = _io(b"a=1\n")
        assert io_.read_data_block() == ["a=1"]

    def test_read_message_joins_lines(self):
        io_, _ = _io(b"<speak>one\ntwo</speak>\n.\n")
        assert io_.read_message() == "<speak>one\ntwo</speak>"

    def test_read_message_empty_block(self):
        io_, _ = _io(b".\n")
        assert io_.read_message() == ""


class TestParseSettings:
    def test_parses_key_values(self):
        assert protocol.parse_settings(["rate=10", "pitch=-5"]) == {
            "rate": "10",
            "pitch": "-5",
        }

    def test_keeps_equals_in_value(self):
        assert protocol.parse_settings(["voice=a=b"]) == {"voice": "a=b"}

    def test_ignores_lines_without_separator(self):
        assert protocol.parse_settings(["garbage", "rate=1"]) == {"rate": "1"}

    def test_empty_list(self):
        assert protocol.parse_settings([]) == {}


class TestWriting:
    def test_send_appends_newline_and_flushes(self):
        io_, out = _io()
        io_.send(protocol.OK_SPEAKING)
        assert out.getvalue() == b"200 OK SPEAKING\n"

    def test_send_multiline_prefixes_detail_lines(self):
        io_, out = _io()
        io_.send_multiline(["299-ready"], protocol.OK_LOADED)
        assert out.getvalue() == b"299-ready\n299 OK LOADED SUCCESSFULLY\n"

    def test_events_use_exact_codes(self):
        io_, out = _io()
        io_.event_begin()
        io_.event_end()
        io_.event_stop()
        io_.event_pause()
        assert out.getvalue() == b"701 BEGIN\n702 END\n703 STOP\n704 PAUSE\n"

    def test_index_mark_is_two_lines(self):
        io_, out = _io()
        io_.index_mark("__spd_3")
        assert out.getvalue() == b"700-__spd_3\n700 INDEX MARK\n"

    def test_send_voices_tab_separated_then_terminator(self):
        io_, out = _io()
        io_.send_voices([("en-US-AvaMultilingualNeural", "en-US", "none")])
        assert out.getvalue() == (
            b"200-en-US-AvaMultilingualNeural\ten-US\tnone\n200 OK VOICE LIST SENT\n"
        )

    def test_send_voices_empty_reports_cannot_list(self):
        io_, out = _io()
        io_.send_voices([])
        assert out.getvalue() == b"304 CANT LIST VOICES\n"


class TestSendAudio:
    def _pcm(self, samples):
        return array.array("h", samples).tobytes()

    def test_headers_describe_the_track(self):
        io_, out = _io()
        io_.send_audio(self._pcm([1, 2, 3, 4]), sample_rate=24000)
        text = out.getvalue()
        assert b"705-bits=16\n" in text
        assert b"705-num_channels=1\n" in text
        assert b"705-sample_rate=24000\n" in text
        assert b"705-num_samples=4\n" in text
        assert b"705-big_endian=" in text

    def test_frame_structure_and_terminator(self):
        io_, out = _io()
        io_.send_audio(self._pcm([1]), sample_rate=24000)
        payload = out.getvalue()
        assert b"705-AUDIO\x00" in payload
        assert payload.endswith(b"705 AUDIO\n")

    def test_payload_is_escaped(self):
        io_, out = _io()
        # 0x0a0a as one sample would otherwise emit a raw newline.
        io_.send_audio(b"\x0a\x0a", sample_rate=24000)
        body = out.getvalue().split(b"705-AUDIO\x00", 1)[1]
        assert b"\x7d\x2a" in body

    def test_large_buffer_is_split_into_frames(self):
        io_, out = _io()
        io_.send_audio(b"\x00\x01" * 12000, sample_rate=24000)
        assert out.getvalue().count(b"705 AUDIO\n") == 3

    def test_frame_never_exceeds_chunk_limit(self):
        io_, out = _io()
        io_.send_audio(b"\x00\x01" * 12000, sample_rate=24000)
        for header in out.getvalue().split(b"705-num_samples=")[1:]:
            count = int(header.split(b"\n", 1)[0])
            assert count * 2 <= protocol.MAX_AUDIO_CHUNK_BYTES

    def test_empty_pcm_writes_nothing(self):
        io_, out = _io()
        io_.send_audio(b"", sample_rate=24000)
        assert out.getvalue() == b""
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_protocol.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'desktop.protocol'`.

- [ ] **Step 3: Write the implementation**

Create `desktop/protocol.py`:

```python
"""The Speech Dispatcher output-module wire protocol.

Every response string and byte sequence here mirrors the reference C
implementation in speechd's ``module_process.c``. Nothing but protocol traffic
may be written to the stream, and writes are serialised because events are
emitted from a worker thread while the main loop is answering commands.
"""

from __future__ import annotations

import logging
import threading
from typing import BinaryIO

from desktop.audio import BITS, CHANNELS, native_big_endian

logger = logging.getLogger("free-tts.protocol")

OK_LOADED = "299 OK LOADED SUCCESSFULLY"
ERR_CANT_INIT = "399 ERR CANT INIT MODULE"
OK_RECEIVING_MESSAGE = "202 OK RECEIVING MESSAGE"
OK_SPEAKING = "200 OK SPEAKING"
ERR_CANT_SPEAK = "301 ERROR CANT SPEAK"
OK_RECEIVING_SETTINGS = "203 OK RECEIVING SETTINGS"
OK_SETTINGS_RECEIVED = "203 OK SETTINGS RECEIVED"
OK_RECEIVING_AUDIO_SETTINGS = "207 OK RECEIVING AUDIO SETTINGS"
OK_AUDIO_INITIALIZED = "203 OK AUDIO INITIALIZED"
OK_RECEIVING_LOGLEVEL_SETTINGS = "207 OK RECEIVING LOGLEVEL SETTINGS"
OK_LOGLEVEL_SET = "203 OK LOGLEVEL SET"
OK_QUIT = "210 OK QUIT"
ERR_UNKNOWN_COMMAND = "300 ERR UNKNOWN COMMAND"
ERR_BAD_SYNTAX = "302 ERROR BAD SYNTAX"
ERR_BAD_PARAM = "303 ERROR INVALID PARAMETER OR VALUE"
ERR_CANT_LIST_VOICES = "304 CANT LIST VOICES"
OK_VOICE_LIST_SENT = "200 OK VOICE LIST SENT"

MAX_AUDIO_CHUNK_BYTES = 10000
_ESCAPE = 0x7D
_INVERT = 0x20
_FRAME_BYTES = CHANNELS * BITS // 8


def escape_audio(data: bytes) -> bytes:
    """HDLC-escape newline and the escape byte so audio stays line-safe."""
    if not data:
        return b""
    out = bytearray()
    for byte in data:
        if byte in (_ESCAPE, 0x0A):
            out.append(_ESCAPE)
            out.append(byte ^ _INVERT)
        else:
            out.append(byte)
    return bytes(out)


def parse_settings(lines: list[str]) -> dict[str, str]:
    """Parse ``name=value`` settings lines, ignoring malformed ones."""
    settings: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            logger.debug("Ignoring malformed settings line: %r", line)
            continue
        name, _, value = line.partition("=")
        settings[name.strip()] = value
    return settings


class ProtocolIO:
    """Reads commands and writes replies, events, and audio."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._lock = threading.Lock()

    def read_line(self) -> str | None:
        """Return the next line without its newline, or None at end of input."""
        raw = self._stdin.readline()
        if not raw:
            return None
        return raw.decode("utf-8", "replace").rstrip("\n")

    def read_data_block(self) -> list[str]:
        """Read lines up to the terminating dot, un-stuffing leading dots."""
        lines: list[str] = []
        while True:
            line = self.read_line()
            if line is None or line == ".":
                return lines
            lines.append(line[1:] if line.startswith(".") else line)

    def read_message(self) -> str:
        """Read a dot-terminated message body as a single string."""
        return "\n".join(self.read_data_block())

    def send(self, line: str) -> None:
        """Write one protocol line."""
        with self._lock:
            self._write(line.encode("utf-8") + b"\n")

    def send_multiline(self, detail_lines: list[str], final: str) -> None:
        """Write detail lines followed by their terminating status line."""
        payload = b"".join(line.encode("utf-8") + b"\n" for line in detail_lines)
        with self._lock:
            self._write(payload + final.encode("utf-8") + b"\n")

    def send_voices(self, rows: list[tuple[str, str, str]]) -> None:
        """Write a LIST VOICES reply, or report that listing is impossible."""
        if not rows:
            self.send(ERR_CANT_LIST_VOICES)
            return
        detail = [f"200-{name}\t{language}\t{variant}" for name, language, variant in rows]
        self.send_multiline(detail, OK_VOICE_LIST_SENT)

    def send_audio(self, pcm: bytes, sample_rate: int = 24000) -> None:
        """Send PCM to the server in bounded, escaped frames."""
        if not pcm:
            return
        big_endian = 1 if native_big_endian() else 0
        step = MAX_AUDIO_CHUNK_BYTES - (MAX_AUDIO_CHUNK_BYTES % _FRAME_BYTES)
        for offset in range(0, len(pcm), step):
            frame = pcm[offset : offset + step]
            header = (
                f"705-bits={BITS}\n"
                f"705-num_channels={CHANNELS}\n"
                f"705-sample_rate={sample_rate}\n"
                f"705-num_samples={len(frame) // _FRAME_BYTES}\n"
                f"705-big_endian={big_endian}\n"
                "705-AUDIO"
            ).encode("utf-8")
            with self._lock:
                self._write(
                    header + b"\x00" + escape_audio(frame) + b"\n705 AUDIO\n"
                )

    def event_begin(self) -> None:
        """Announce that audio has started."""
        self.send("701 BEGIN")

    def event_end(self) -> None:
        """Announce normal completion."""
        self.send("702 END")

    def event_stop(self) -> None:
        """Announce that speech was stopped."""
        self.send("703 STOP")

    def event_pause(self) -> None:
        """Announce that speech was paused."""
        self.send("704 PAUSE")

    def index_mark(self, mark: str) -> None:
        """Report that an index mark has been reached."""
        self.send_multiline([f"700-{mark}"], "700 INDEX MARK")

    def _write(self, payload: bytes) -> None:
        self._stdout.write(payload)
        flush = getattr(self._stdout, "flush", None)
        if flush is not None:
            flush()
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (280 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 5: Commit**

```bash
git add desktop/protocol.py tests/test_desktop_protocol.py
git commit -m "feat(desktop): Speech Dispatcher wire protocol with server audio"
```

## Task 9: Speech engine and module entry point

**Implementer tier:** Advanced

**Files:**

- Create: `desktop/module.py`
- Test: `tests/test_desktop_module.py`

**Interfaces:**

- Consumes:
  - `desktop.settings`: `AdapterConfig`, `load_config(path=None, env=None) -> AdapterConfig`, `map_rate(int) -> str`, `map_pitch(int) -> str`, `map_volume(int) -> float` (Task 4).
  - `desktop.backend`: `BackendController(config, *, fetch=None, spawn=None, sleep=..., clock=..., lock_factory=None)` with `ensure_ready() -> None`, and `BackendUnavailable` (Task 5).
  - `desktop.chunks`: `Chunk(text: str, mark: str | None)`, `split_marked(ssml: str, max_chars: int = 400) -> list[Chunk]` (Task 6).
  - `desktop.voices`: `Voice(name, locale, gender)`, `VoiceCatalog.from_payload(payload) -> VoiceCatalog` with `protocol_rows()`, `resolve(synthesis_voice=None, language=None, voice_type=None) -> Voice | None` (Task 6).
  - `desktop.synth`: `SynthClient(config, *, transport=None, sleep=...)` with `voices()`, `synthesize(text, voice_name, rate, pitch, request_id, should_abort=None) -> bytes`, `cancel(request_id)`; `new_request_id() -> str`; `SynthError`; `Cancelled` (Task 7).
  - `desktop.audio`: `decode_mp3(data, ffmpeg_path="ffmpeg", sample_rate=SAMPLE_RATE, runner=subprocess.run) -> bytes`, `apply_gain(pcm, gain) -> bytes`, `SAMPLE_RATE`, `DecodeError` (Task 7).
  - `desktop.protocol`: `ProtocolIO(stdin, stdout)` with `read_line()`, `read_data_block()`, `read_message()`, `send(line)`, `send_multiline(detail, final)`, `send_voices(rows)`, `send_audio(pcm, sample_rate=24000)`, `event_begin()`, `event_end()`, `event_stop()`, `event_pause()`, `index_mark(mark)`; `parse_settings(lines) -> dict[str, str]`; and every response constant (Task 8).
- Produces, in `desktop/module.py`:
  - `class SpeechEngine` with `__init__(self, io, config, controller, client, *, decoder=None)`, methods `apply_settings(settings: dict[str, str]) -> bool`, `list_voices() -> None`, `handle_speak(message: str) -> None`, `handle_stop() -> None`, `handle_pause() -> None`, `wait_idle(timeout: float = 5.0) -> bool`, `close() -> None`, and attribute `catalog: VoiceCatalog | None`.
  - `check_ffmpeg(ffmpeg_path: str, runner=subprocess.run) -> None` raising `RuntimeError`.
  - `run(argv: list[str], stdin, stdout) -> int` — the full INIT handshake plus command loop.
  - `main() -> int` — console entry point.

- [ ] **Step 1: Write the failing test**

Create `tests/test_desktop_module.py`:

```python
"""Engine state machine: acceptance, events, stop, pause, and settings."""

import dataclasses
import io

import pytest

from desktop import module, settings, voices

PAYLOAD = {
    "default_voice": "en-US-AvaMultilingualNeural",
    "voices": [
        {
            "ShortName": "en-US-AvaMultilingualNeural",
            "Gender": "Female",
            "Locale": "en-US",
        },
        {"ShortName": "fr-FR-HenriNeural", "Gender": "Male", "Locale": "fr-FR"},
    ],
}


class _FakeIO:
    """Captures protocol traffic instead of writing to a pipe."""

    def __init__(self):
        self.lines = []
        self.audio = []

    def send(self, line):
        self.lines.append(line)

    def send_multiline(self, detail, final):
        self.lines.extend(detail)
        self.lines.append(final)

    def send_voices(self, rows):
        self.lines.append(f"VOICES:{len(rows)}")

    def send_audio(self, pcm, sample_rate=24000):
        self.audio.append(pcm)

    def event_begin(self):
        self.lines.append("701 BEGIN")

    def event_end(self):
        self.lines.append("702 END")

    def event_stop(self):
        self.lines.append("703 STOP")

    def event_pause(self):
        self.lines.append("704 PAUSE")

    def index_mark(self, mark):
        self.lines.append(f"700:{mark}")


class _FakeController:
    def __init__(self, error=None):
        self.error = error
        self.calls = 0

    def ensure_ready(self):
        self.calls += 1
        if self.error is not None:
            raise self.error


class _FakeClient:
    def __init__(self, payload=None, audio=b"mp3", error=None):
        self._payload = PAYLOAD if payload is None else payload
        self._audio = audio
        self._error = error
        self.requests = []
        self.cancelled = []

    def voices(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def synthesize(self, text, voice_name, rate, pitch, request_id, should_abort=None):
        self.requests.append((text, voice_name, rate, pitch))
        if self._error is not None:
            raise self._error
        return self._audio

    def cancel(self, request_id):
        self.cancelled.append(request_id)


def _engine(client=None, controller=None, config=None):
    fake_io = _FakeIO()
    engine = module.SpeechEngine(
        fake_io,
        config or settings.DEFAULTS,
        controller or _FakeController(),
        client or _FakeClient(),
        decoder=lambda mp3, ffmpeg_path, sample_rate: b"\x01\x00" * 8,
    )
    return engine, fake_io


SSML = '<speak>Hello there. <mark name="__spd_0"/></speak>'
TWO_CHUNK_SSML = (
    '<speak>One. <mark name="__spd_0"/> Two. <mark name="__spd_1"/></speak>'
)


class TestListVoices:
    def test_lists_every_voice(self):
        engine, fake_io = _engine()
        engine.list_voices()
        assert fake_io.lines == ["VOICES:2"]

    def test_starts_backend_on_first_listing(self):
        controller = _FakeController()
        engine, _ = _engine(controller=controller)
        engine.list_voices()
        assert controller.calls == 1

    def test_backend_failure_reports_cannot_list(self):
        from desktop.backend import BackendUnavailable

        engine, fake_io = _engine(controller=_FakeController(BackendUnavailable("no")))
        engine.list_voices()
        assert fake_io.lines == ["304 CANT LIST VOICES"]

    def test_voice_load_failure_reports_cannot_list(self):
        from desktop.synth import SynthError

        engine, fake_io = _engine(client=_FakeClient(payload=SynthError("boom")))
        engine.list_voices()
        assert fake_io.lines == ["304 CANT LIST VOICES"]


class TestApplySettings:
    def test_accepts_known_parameters(self):
        engine, _ = _engine()
        assert engine.apply_settings(
            {"rate": "20", "pitch": "-10", "volume": "0", "language": "fr-FR"}
        )

    def test_rejects_out_of_range_rate(self):
        engine, _ = _engine()
        assert engine.apply_settings({"rate": "500"}) is False

    def test_rejects_non_numeric_rate(self):
        engine, _ = _engine()
        assert engine.apply_settings({"rate": "quick"}) is False

    def test_ignores_pitch_range_without_failing(self):
        engine, _ = _engine()
        assert engine.apply_settings({"pitch_range": "50"})

    def test_unknown_parameter_is_rejected(self):
        engine, _ = _engine()
        assert engine.apply_settings({"nonsense": "1"}) is False

    def test_null_strings_are_accepted(self):
        engine, _ = _engine()
        assert engine.apply_settings(
            {"synthesis_voice": "NULL", "language": "NULL", "voice": "NULL"}
        )

    def test_settings_reach_synthesis(self):
        client = _FakeClient()
        engine, _ = _engine(client=client)
        engine.apply_settings({"rate": "50", "pitch": "100", "language": "fr-FR"})
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        _text, voice_name, rate, pitch = client.requests[0]
        assert voice_name == "fr-FR-HenriNeural"
        assert rate == "+100%"
        assert pitch == "+50Hz"


class TestSpeak:
    def test_accepts_then_emits_begin_and_end(self):
        engine, fake_io = _engine()
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        assert fake_io.lines[0] == "200 OK SPEAKING"
        assert "701 BEGIN" in fake_io.lines
        assert fake_io.lines[-1] == "702 END"

    def test_audio_is_sent(self):
        engine, fake_io = _engine()
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        assert fake_io.audio

    def test_index_mark_reported_between_chunks(self):
        engine, fake_io = _engine()
        engine.handle_speak(TWO_CHUNK_SSML)
        assert engine.wait_idle()
        assert "700:__spd_0" in fake_io.lines
        assert "700:__spd_1" in fake_io.lines
        assert fake_io.lines.index("700:__spd_0") < fake_io.lines.index("700:__spd_1")

    def test_empty_message_is_refused(self):
        engine, fake_io = _engine()
        engine.handle_speak("<speak>   </speak>")
        assert fake_io.lines == ["301 ERROR CANT SPEAK"]

    def test_backend_unavailable_refuses_before_accepting(self):
        from desktop.backend import BackendUnavailable

        engine, fake_io = _engine(controller=_FakeController(BackendUnavailable("no")))
        engine.handle_speak(SSML)
        assert fake_io.lines == ["301 ERROR CANT SPEAK"]

    def test_synthesis_failure_after_acceptance_stops_message(self):
        from desktop.synth import SynthError

        engine, fake_io = _engine(client=_FakeClient(error=SynthError("boom")))
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        assert fake_io.lines[0] == "200 OK SPEAKING"
        assert fake_io.lines[-1] == "703 STOP"

    def test_decode_failure_after_acceptance_stops_message(self):
        from desktop.audio import DecodeError

        fake_io = _FakeIO()

        def failing_decoder(mp3, ffmpeg_path, sample_rate):
            raise DecodeError("no ffmpeg")

        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=failing_decoder,
        )
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        assert fake_io.lines[-1] == "703 STOP"

    def test_volume_gain_applied_to_pcm(self):
        fake_io = _FakeIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=lambda mp3, ffmpeg_path, sample_rate: b"\xff\x7f",
        )
        engine.apply_settings({"volume": "-100"})
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        assert fake_io.audio == [b"\x00\x00"]


class TestStop:
    def test_stop_while_idle_is_not_an_error(self):
        engine, fake_io = _engine()
        engine.handle_stop()
        assert engine.wait_idle()
        assert fake_io.lines == []

    def test_stop_emits_stop_event_once(self):
        engine, fake_io = _engine()
        engine.handle_speak(TWO_CHUNK_SSML)
        engine.handle_stop()
        assert engine.wait_idle()
        assert fake_io.lines.count("703 STOP") == 1
        assert "702 END" not in fake_io.lines

    def test_stop_cancels_outstanding_requests(self):
        client = _FakeClient()
        engine, _ = _engine(client=client)
        engine.handle_speak(TWO_CHUNK_SSML)
        engine.handle_stop()
        assert engine.wait_idle()
        assert client.cancelled

    def test_new_speak_after_stop_works(self):
        engine, fake_io = _engine()
        engine.handle_speak(SSML)
        engine.handle_stop()
        assert engine.wait_idle()
        fake_io.lines.clear()
        engine.handle_speak(SSML)
        assert engine.wait_idle()
        assert fake_io.lines[-1] == "702 END"


class TestPause:
    def test_pause_reports_mark_then_pause_event(self):
        engine, fake_io = _engine()
        engine.handle_speak(TWO_CHUNK_SSML)
        engine.handle_pause()
        assert engine.wait_idle()
        assert fake_io.lines[-1] == "704 PAUSE"
        assert any(line.startswith("700:") for line in fake_io.lines)

    def test_pause_while_idle_is_not_an_error(self):
        engine, fake_io = _engine()
        engine.handle_pause()
        assert engine.wait_idle()
        assert fake_io.lines == []

    def test_pause_does_not_also_emit_end(self):
        engine, fake_io = _engine()
        engine.handle_speak(TWO_CHUNK_SSML)
        engine.handle_pause()
        assert engine.wait_idle()
        assert "702 END" not in fake_io.lines


class TestCheckFfmpeg:
    def test_passes_when_ffmpeg_runs(self):
        module.check_ffmpeg(
            "ffmpeg", runner=lambda *a, **k: type("R", (), {"returncode": 0})()
        )

    def test_raises_when_missing(self):
        def runner(*args, **kwargs):
            raise FileNotFoundError("ffmpeg")

        with pytest.raises(RuntimeError, match="ffmpeg"):
            module.check_ffmpeg("ffmpeg", runner=runner)

    def test_raises_on_nonzero_exit(self):
        with pytest.raises(RuntimeError, match="ffmpeg"):
            module.check_ffmpeg(
                "ffmpeg", runner=lambda *a, **k: type("R", (), {"returncode": 1})()
            )


class TestRunHandshake:
    def test_requires_init_first(self):
        stdout = io.BytesIO()
        code = module.run([], io.BytesIO(b"SPEAK\n"), stdout)
        assert code != 0
        assert b"399" in stdout.getvalue()

    def test_eof_before_init_exits_nonzero(self):
        assert module.run([], io.BytesIO(b""), io.BytesIO()) != 0
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_module.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'desktop.module'`.

- [ ] **Step 3: Write the engine**

Create `desktop/module.py` with the imports, `check_ffmpeg`, and the engine:

```python
"""Speech Dispatcher output module for free-tts.

Runs as ``sd_free-tts <configfile>``. Speech happens on a worker thread so the
command loop can answer STOP and PAUSE immediately, which the protocol requires.
A generation token guards every emission: a worker whose token is stale writes
nothing, so audio from a superseded message can never reach the server.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor

from desktop import protocol
from desktop.audio import SAMPLE_RATE, DecodeError, apply_gain, decode_mp3
from desktop.backend import BackendController, BackendUnavailable
from desktop.chunks import split_marked
from desktop.settings import (
    AdapterConfig,
    load_config,
    map_pitch,
    map_rate,
    map_volume,
)
from desktop.synth import Cancelled, SynthClient, SynthError, new_request_id
from desktop.voices import VoiceCatalog

logger = logging.getLogger("free-tts.module")

_NUMERIC_SETTINGS = ("rate", "pitch", "volume", "pitch_range")
_STRING_SETTINGS = (
    "voice",
    "synthesis_voice",
    "language",
    "punctuation_mode",
    "spelling_mode",
    "cap_let_recogn",
)


def check_ffmpeg(
    ffmpeg_path: str, runner: Callable[..., object] = subprocess.run
) -> None:
    """Fail fast at init when the decoder is missing or broken."""
    try:
        result = runner(
            [ffmpeg_path, "-version"], capture_output=True, check=False
        )
    except OSError as exc:
        raise RuntimeError(f"ffmpeg is required but could not be run: {exc}") from exc
    if getattr(result, "returncode", 1) != 0:
        raise RuntimeError(f"ffmpeg at {ffmpeg_path!r} exited non-zero for -version")


class SpeechEngine:
    """Owns speech state and drives synthesis for one Speech Dispatcher session."""

    def __init__(
        self,
        io: object,
        config: AdapterConfig,
        controller: object,
        client: object,
        *,
        decoder: Callable[..., bytes] | None = None,
    ) -> None:
        self._io = io
        self._config = config
        self._controller = controller
        self._client = client
        self._decode = decoder or (
            lambda mp3, ffmpeg_path, sample_rate: decode_mp3(
                mp3, ffmpeg_path=ffmpeg_path, sample_rate=sample_rate
            )
        )
        self.catalog: VoiceCatalog | None = None

        self._lock = threading.Lock()
        self._token = 0
        self._stop_requested = False
        self._pause_requested = False
        self._worker: threading.Thread | None = None
        self._rate = 0
        self._pitch = 0
        self._volume = 100
        self._language: str | None = None
        self._voice_type: str | None = None
        self._synthesis_voice: str | None = None
        self._active_requests: set[str] = set()

    def apply_settings(self, settings: dict[str, str]) -> bool:
        """Apply a SET block. Return False if any parameter was invalid."""
        ok = True
        for name, raw in settings.items():
            if name in _NUMERIC_SETTINGS:
                try:
                    value = int(raw)
                except (TypeError, ValueError):
                    ok = False
                    continue
                if not -100 <= value <= 100:
                    ok = False
                    continue
                if name == "rate":
                    self._rate = value
                elif name == "pitch":
                    self._pitch = value
                elif name == "volume":
                    self._volume = value
                continue
            if name in _STRING_SETTINGS:
                cleaned = None if raw == "NULL" else raw
                if name == "language":
                    self._language = cleaned
                elif name == "voice":
                    self._voice_type = cleaned
                elif name == "synthesis_voice":
                    self._synthesis_voice = cleaned
                continue
            logger.debug("Rejecting unknown parameter %r", name)
            ok = False
        return ok

    def list_voices(self) -> None:
        """Answer LIST VOICES, starting the backend if this is first use."""
        try:
            catalog = self._ensure_catalog()
        except (BackendUnavailable, SynthError) as exc:
            logger.error("Cannot list voices: %s", exc)
            self._io.send(protocol.ERR_CANT_LIST_VOICES)  # type: ignore[attr-defined]
            return
        self._io.send_voices(catalog.protocol_rows())  # type: ignore[attr-defined]

    def handle_speak(self, message: str) -> None:
        """Validate and accept a message, then synthesise it on a worker."""
        chunks = split_marked(message, max_chars=self._config.max_chunk_chars)
        if not chunks:
            self._io.send(protocol.ERR_CANT_SPEAK)  # type: ignore[attr-defined]
            return
        try:
            catalog = self._ensure_catalog()
        except (BackendUnavailable, SynthError) as exc:
            logger.error("Cannot speak: %s", exc)
            self._io.send(protocol.ERR_CANT_SPEAK)  # type: ignore[attr-defined]
            return
        voice = catalog.resolve(
            synthesis_voice=self._synthesis_voice,
            language=self._language,
            voice_type=self._voice_type,
        )
        if voice is None:
            logger.error("No usable voice for language=%r", self._language)
            self._io.send(protocol.ERR_CANT_SPEAK)  # type: ignore[attr-defined]
            return

        self._join_worker()
        with self._lock:
            self._token += 1
            token = self._token
            self._stop_requested = False
            self._pause_requested = False
        rate = map_rate(self._rate)
        pitch = map_pitch(self._pitch)
        gain = map_volume(self._volume)

        self._io.send(protocol.OK_SPEAKING)  # type: ignore[attr-defined]
        worker = threading.Thread(
            target=self._speak_worker,
            args=(token, chunks, voice.name, rate, pitch, gain),
            name="free-tts-speak",
            daemon=True,
        )
        with self._lock:
            self._worker = worker
        worker.start()

    def handle_stop(self) -> None:
        """Ask the worker to abandon the message. Returns immediately."""
        with self._lock:
            self._stop_requested = True
            outstanding = list(self._active_requests)
        for request_id in outstanding:
            self._client.cancel(request_id)  # type: ignore[attr-defined]

    def handle_pause(self) -> None:
        """Ask the worker to stop at the next index mark."""
        with self._lock:
            self._pause_requested = True

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Wait for the current message to finish. True if the worker is done."""
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        worker.join(timeout)
        return not worker.is_alive()

    def close(self) -> None:
        """Stop speech and wait briefly for the worker to unwind."""
        self.handle_stop()
        self.wait_idle()

    def _ensure_catalog(self) -> VoiceCatalog:
        if self.catalog is not None and len(self.catalog):
            return self.catalog
        self._controller.ensure_ready()  # type: ignore[attr-defined]
        catalog = VoiceCatalog.from_payload(self._client.voices())  # type: ignore[attr-defined]
        if not len(catalog):
            raise SynthError("backend returned no voices")
        self.catalog = catalog
        return catalog

    def _join_worker(self) -> None:
        with self._lock:
            worker = self._worker
            self._stop_requested = True
        if worker is not None and worker.is_alive():
            worker.join(10.0)
        with self._lock:
            self._worker = None

    def _is_current(self, token: int) -> bool:
        with self._lock:
            return token == self._token

    def _speak_worker(
        self,
        token: int,
        chunks: list[object],
        voice_name: str,
        rate: str,
        pitch: str,
        gain: float,
    ) -> None:
        """Synthesise every chunk with one-chunk lookahead, then report."""
        outcome = "end"
        began = False
        with ThreadPoolExecutor(max_workers=1, thread_name_prefix="free-tts-pre") as pool:
            pending = None
            try:
                for index, chunk in enumerate(chunks):
                    if self._should_abort(token):
                        outcome = "stop"
                        break
                    future = pending or pool.submit(
                        self._fetch, token, chunk.text, voice_name, rate, pitch
                    )
                    pending = None
                    if index + 1 < len(chunks) and not self._should_abort(token):
                        pending = pool.submit(
                            self._fetch,
                            token,
                            chunks[index + 1].text,
                            voice_name,
                            rate,
                            pitch,
                        )
                    mp3 = future.result()
                    if self._should_abort(token):
                        outcome = "stop"
                        break
                    pcm = apply_gain(
                        self._decode(mp3, self._config.ffmpeg_path, SAMPLE_RATE), gain
                    )
                    if not self._is_current(token):
                        return
                    if not began:
                        began = True
                        self._io.event_begin()  # type: ignore[attr-defined]
                    self._io.send_audio(pcm, SAMPLE_RATE)  # type: ignore[attr-defined]
                    if chunk.mark and self._is_current(token):
                        self._io.index_mark(chunk.mark)  # type: ignore[attr-defined]
                    with self._lock:
                        if self._stop_requested:
                            outcome = "stop"
                            break
                        if self._pause_requested:
                            outcome = "pause"
                            break
            except Cancelled:
                outcome = "stop"
            except (SynthError, DecodeError) as exc:
                logger.error("Synthesis aborted: %s", exc)
                outcome = "stop"
            except Exception:
                logger.exception("Unexpected synthesis failure")
                outcome = "stop"
            finally:
                if pending is not None:
                    pending.cancel()
                self._cancel_outstanding()

        if not self._is_current(token):
            return
        if outcome == "stop":
            self._io.event_stop()  # type: ignore[attr-defined]
        elif outcome == "pause":
            self._io.event_pause()  # type: ignore[attr-defined]
        else:
            self._io.event_end()  # type: ignore[attr-defined]

    def _should_abort(self, token: int) -> bool:
        with self._lock:
            return self._stop_requested or token != self._token

    def _fetch(
        self, token: int, text: str, voice_name: str, rate: str, pitch: str
    ) -> bytes:
        request_id = new_request_id()
        with self._lock:
            self._active_requests.add(request_id)
        try:
            return self._client.synthesize(  # type: ignore[attr-defined]
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=lambda: self._should_abort(token),
            )
        finally:
            with self._lock:
                self._active_requests.discard(request_id)

    def _cancel_outstanding(self) -> None:
        with self._lock:
            outstanding = list(self._active_requests)
        for request_id in outstanding:
            self._client.cancel(request_id)  # type: ignore[attr-defined]
```

- [ ] **Step 4: Write the entry point in the same file**

Append to `desktop/module.py`:

```python
def _configure_logging() -> None:
    """Log to stderr only: stdout belongs to the protocol."""
    level = logging.DEBUG if os.environ.get("FREE_TTS_DEBUG") else logging.INFO
    logging.basicConfig(
        stream=sys.stderr,
        level=level,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )


def run(argv: list[str], stdin: object, stdout: object) -> int:
    """Perform the INIT handshake and then serve commands until QUIT or EOF."""
    io = protocol.ProtocolIO(stdin, stdout)  # type: ignore[arg-type]
    config = load_config()

    first = io.read_line()
    if first != "INIT":
        io.send_multiline(
            ["399-server did not start with INIT"], protocol.ERR_CANT_INIT
        )
        return 3

    try:
        check_ffmpeg(config.ffmpeg_path)
    except RuntimeError as exc:
        io.send_multiline([f"399-{exc}"], protocol.ERR_CANT_INIT)
        return 1

    engine = SpeechEngine(
        io, config, BackendController(config), SynthClient(config)
    )
    io.send_multiline(["299-free-tts ready"], protocol.OK_LOADED)

    while True:
        line = io.read_line()
        if line is None:
            engine.close()
            return 0
        if line == "SPEAK":
            io.send(protocol.OK_RECEIVING_MESSAGE)
            engine.handle_speak(io.read_message())
        elif line in ("CHAR", "KEY"):
            io.send(protocol.OK_RECEIVING_MESSAGE)
            engine.handle_speak(f"<speak>{io.read_message()}</speak>")
        elif line == "SOUND_ICON":
            io.send(protocol.OK_RECEIVING_MESSAGE)
            io.read_message()
            io.send(protocol.ERR_CANT_SPEAK)
        elif line == "STOP":
            engine.handle_stop()
        elif line == "PAUSE":
            engine.handle_pause()
        elif line.startswith("LIST VOICES"):
            engine.list_voices()
        elif line == "SET":
            io.send(protocol.OK_RECEIVING_SETTINGS)
            if engine.apply_settings(protocol.parse_settings(io.read_data_block())):
                io.send(protocol.OK_SETTINGS_RECEIVED)
            else:
                io.send(protocol.ERR_BAD_PARAM)
        elif line == "AUDIO":
            io.send(protocol.OK_RECEIVING_AUDIO_SETTINGS)
            requested = protocol.parse_settings(io.read_data_block())
            method = requested.get("audio_output_method")
            if method == "server":
                io.send(protocol.OK_AUDIO_INITIALIZED)
            else:
                io.send(protocol.ERR_BAD_PARAM)
        elif line == "LOGLEVEL":
            io.send(protocol.OK_RECEIVING_LOGLEVEL_SETTINGS)
            protocol.parse_settings(io.read_data_block())
            io.send(protocol.OK_LOGLEVEL_SET)
        elif line.startswith("DEBUG"):
            parts = line.split()
            if len(parts) >= 2 and parts[1] in ("ON", "OFF"):
                io.send(f"200 OK DEBUGGING {parts[1]}")
            else:
                io.send(protocol.ERR_BAD_SYNTAX)
        elif line == "QUIT":
            engine.close()
            io.send(protocol.OK_QUIT)
            return 0
        else:
            io.send(protocol.ERR_UNKNOWN_COMMAND)


def main() -> int:
    """Console entry point used by the installed launcher."""
    _configure_logging()
    return run(sys.argv[1:], sys.stdin.buffer, sys.stdout.buffer)


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (318 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 6: Commit**

```bash
git add desktop/module.py tests/test_desktop_module.py
git commit -m "feat(desktop): speech engine, controls, and module entry point"
```

## Task 10: Per-user installer

**Implementer tier:** Advanced

**Files:**

- Create: `desktop/speechd_config.py`
- Create: `desktop/install.py`
- Create: `desktop/free-tts.conf`
- Test: `tests/test_desktop_speechd_config.py`
- Test: `tests/test_desktop_install.py`

**Interfaces:**

- Consumes: `install_root() -> pathlib.Path` from `desktop/backend.py` (Task 5), which honours `FREE_TTS_HOME`, then `XDG_DATA_HOME`, then `~/.local/share`, returning the directory ending in `free-tts`.
- Produces, in `desktop/speechd_config.py`:
  - `BEGIN_MARKER: str = "# BEGIN free-tts managed block (do not edit)"`
  - `END_MARKER: str = "# END free-tts managed block"`
  - `DISABLED_PREFIX: str = "#free-tts-disabled "`
  - `apply_managed_block(text: str, launcher_name: str, module_conf: str) -> str`
  - `remove_managed_block(text: str) -> str`
- Produces, in `desktop/install.py`:
  - `MODULE_NAME: str = "free-tts"`, `LAUNCHER_NAME: str = "sd_free-tts"`, `MODULE_CONF_NAME: str = "free-tts.conf"`
  - `RUNTIME_ENTRIES: tuple[str, ...] = ("server.py", "requirements.txt", "config.example.json", "desktop")`
  - `launcher_dir() -> pathlib.Path`, `speechd_config_dir() -> pathlib.Path`, `manifest_path() -> pathlib.Path`
  - `install(source_root, *, root=None, launcher=None, config_dir=None, venv_builder=None) -> dict[str, str]`
  - `uninstall(*, root=None, launcher=None, config_dir=None) -> list[str]`
  - `restart_speech_dispatcher(runner=subprocess.run) -> None`
  - `main(argv: list[str] | None = None) -> int`

- [ ] **Step 1: Write the failing config-editing test**

Create `tests/test_desktop_speechd_config.py`:

```python
"""Editing the user's speechd.conf inside a single managed block."""

from desktop import speechd_config as sc


def _apply(text):
    return sc.apply_managed_block(text, "sd_free-tts", "free-tts.conf")


class TestApply:
    def test_adds_block_to_empty_config(self):
        result = _apply("")
        assert sc.BEGIN_MARKER in result
        assert sc.END_MARKER in result
        assert 'AddModule "free-tts" "sd_free-tts" "free-tts.conf"' in result
        assert "DefaultModule free-tts" in result

    def test_preserves_existing_content(self):
        result = _apply("LogLevel 3\n")
        assert result.startswith("LogLevel 3\n")

    def test_is_idempotent(self):
        once = _apply("LogLevel 3\n")
        twice = sc.apply_managed_block(once, "sd_free-tts", "free-tts.conf")
        assert once == twice
        assert twice.count(sc.BEGIN_MARKER) == 1

    def test_replaces_a_stale_block(self):
        stale = f"{sc.BEGIN_MARKER}\nAddModule \"old\" \"sd_old\" \"old.conf\"\n{sc.END_MARKER}\n"
        result = _apply(stale)
        assert "sd_old" not in result
        assert result.count(sc.BEGIN_MARKER) == 1

    def test_comments_out_competing_default_module(self):
        result = _apply("DefaultModule espeak-ng\n")
        assert f"{sc.DISABLED_PREFIX}DefaultModule espeak-ng" in result
        assert "\nDefaultModule espeak-ng" not in result

    def test_leaves_already_commented_default_module_alone(self):
        result = _apply("# DefaultModule espeak-ng\n")
        assert "# DefaultModule espeak-ng" in result
        assert sc.DISABLED_PREFIX not in result

    def test_keeps_other_addmodule_lines(self):
        result = _apply('AddModule "espeak-ng" "sd_espeak-ng" "espeak-ng.conf"\n')
        assert 'AddModule "espeak-ng"' in result

    def test_no_horizontal_rules_or_trailing_blank_growth(self):
        result = _apply("LogLevel 3\n")
        assert result.endswith("\n")
        assert not result.endswith("\n\n\n")


class TestRemove:
    def test_removes_the_block(self):
        result = sc.remove_managed_block(_apply("LogLevel 3\n"))
        assert sc.BEGIN_MARKER not in result
        assert "DefaultModule free-tts" not in result
        assert "LogLevel 3" in result

    def test_restores_a_disabled_default_module(self):
        result = sc.remove_managed_block(_apply("DefaultModule espeak-ng\n"))
        assert "DefaultModule espeak-ng" in result
        assert sc.DISABLED_PREFIX not in result

    def test_is_idempotent(self):
        once = sc.remove_managed_block(_apply("LogLevel 3\n"))
        assert sc.remove_managed_block(once) == once

    def test_untouched_config_is_unchanged(self):
        original = "LogLevel 3\nDefaultVolume 100\n"
        assert sc.remove_managed_block(original) == original

    def test_round_trip_returns_original(self):
        original = "LogLevel 3\nDefaultModule espeak-ng\n"
        assert sc.remove_managed_block(_apply(original)) == original
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_speechd_config.py -q`
Expected: FAIL, `ModuleNotFoundError: No module named 'desktop.speechd_config'`.

- [ ] **Step 3: Write `desktop/speechd_config.py`**

```python
"""Edit the user's speechd.conf without disturbing anything we do not own.

All of our lines live between two markers. Everything outside them is treated as
the user's, with one exception: a competing uncommented ``DefaultModule`` is
prefixed so it can be restored verbatim on uninstall.
"""

from __future__ import annotations

BEGIN_MARKER = "# BEGIN free-tts managed block (do not edit)"
END_MARKER = "# END free-tts managed block"
DISABLED_PREFIX = "#free-tts-disabled "


def _strip_block(lines: list[str]) -> list[str]:
    out: list[str] = []
    inside = False
    for line in lines:
        if line.strip() == BEGIN_MARKER:
            inside = True
            continue
        if inside:
            if line.strip() == END_MARKER:
                inside = False
            continue
        out.append(line)
    return out


def apply_managed_block(text: str, launcher_name: str, module_conf: str) -> str:
    """Return ``text`` with exactly one current free-tts managed block."""
    lines = _strip_block(text.splitlines())
    rewritten: list[str] = []
    for line in lines:
        if line.strip().startswith("DefaultModule "):
            rewritten.append(f"{DISABLED_PREFIX}{line}")
        else:
            rewritten.append(line)
    while rewritten and not rewritten[-1].strip():
        rewritten.pop()

    block = [
        BEGIN_MARKER,
        f'AddModule "free-tts" "{launcher_name}" "{module_conf}"',
        "DefaultModule free-tts",
        END_MARKER,
    ]
    body = rewritten + ([""] if rewritten else []) + block
    return "\n".join(body) + "\n"


def remove_managed_block(text: str) -> str:
    """Return ``text`` with our block gone and any disabled line restored."""
    lines = _strip_block(text.splitlines())
    restored = [
        line[len(DISABLED_PREFIX) :] if line.startswith(DISABLED_PREFIX) else line
        for line in lines
    ]
    while restored and not restored[-1].strip():
        restored.pop()
    if not restored:
        return ""
    return "\n".join(restored) + "\n"
```

- [ ] **Step 4: Write the failing installer test**

Create `tests/test_desktop_install.py`:

```python
"""Per-user install, idempotent upgrade, and non-destructive uninstall."""

import json
import pathlib

import pytest

from desktop import install, speechd_config as sc


@pytest.fixture
def source_root(tmp_path):
    """A stand-in checkout with the files the installer copies."""
    root = tmp_path / "checkout"
    (root / "desktop").mkdir(parents=True)
    (root / "server.py").write_text("# server\n")
    (root / "requirements.txt").write_text("flask\n")
    (root / "config.example.json").write_text("{}\n")
    (root / "desktop" / "__init__.py").write_text("")
    (root / "desktop" / "module.py").write_text("# module\n")
    (root / "desktop" / "free-tts.conf").write_text("# module conf\n")
    return root


@pytest.fixture
def paths(tmp_path):
    return {
        "root": tmp_path / "share" / "free-tts",
        "launcher": tmp_path / "libexec" / "speech-dispatcher-modules",
        "config_dir": tmp_path / "config" / "speech-dispatcher",
    }


def _install(source_root, paths, venv_calls=None):
    def venv_builder(root):
        if venv_calls is not None:
            venv_calls.append(root)
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    return install.install(
        source_root,
        root=paths["root"],
        launcher=paths["launcher"],
        config_dir=paths["config_dir"],
        venv_builder=venv_builder,
    )


class TestInstall:
    def test_copies_runtime_files(self, source_root, paths):
        _install(source_root, paths)
        assert (paths["root"] / "server.py").is_file()
        assert (paths["root"] / "desktop" / "module.py").is_file()
        assert (paths["root"] / "requirements.txt").is_file()

    def test_creates_executable_launcher(self, source_root, paths):
        _install(source_root, paths)
        launcher = paths["launcher"] / install.LAUNCHER_NAME
        assert launcher.is_file()
        assert launcher.stat().st_mode & 0o111
        body = launcher.read_text()
        assert str(paths["root"]) in body
        assert "desktop.module" in body

    def test_installs_module_conf(self, source_root, paths):
        _install(source_root, paths)
        assert (paths["config_dir"] / "modules" / install.MODULE_CONF_NAME).is_file()

    def test_registers_module_in_speechd_conf(self, source_root, paths):
        _install(source_root, paths)
        text = (paths["config_dir"] / "speechd.conf").read_text()
        assert 'AddModule "free-tts"' in text
        assert "DefaultModule free-tts" in text

    def test_builds_the_private_venv(self, source_root, paths):
        calls = []
        _install(source_root, paths, venv_calls=calls)
        assert calls == [paths["root"]]
        assert (paths["root"] / ".venv" / "bin" / "python").is_file()

    def test_writes_manifest(self, source_root, paths):
        _install(source_root, paths)
        manifest = json.loads((paths["root"] / "install-manifest.json").read_text())
        assert manifest["root"] == str(paths["root"])
        assert manifest["launcher"].endswith(install.LAUNCHER_NAME)

    def test_backs_up_existing_speechd_conf_once(self, source_root, paths):
        paths["config_dir"].mkdir(parents=True)
        conf = paths["config_dir"] / "speechd.conf"
        conf.write_text("LogLevel 3\n")
        _install(source_root, paths)
        backup = paths["config_dir"] / "speechd.conf.free-tts.bak"
        assert backup.read_text() == "LogLevel 3\n"
        conf.write_text(conf.read_text() + "# later edit\n")
        _install(source_root, paths)
        assert backup.read_text() == "LogLevel 3\n"

    def test_upgrade_is_idempotent(self, source_root, paths):
        _install(source_root, paths)
        first = (paths["config_dir"] / "speechd.conf").read_text()
        _install(source_root, paths)
        assert (paths["config_dir"] / "speechd.conf").read_text() == first

    def test_upgrade_replaces_stale_runtime_file(self, source_root, paths):
        _install(source_root, paths)
        (paths["root"] / "desktop" / "stale.py").write_text("# gone next time\n")
        _install(source_root, paths)
        assert not (paths["root"] / "desktop" / "stale.py").exists()

    def test_upgrade_preserves_user_config(self, source_root, paths):
        _install(source_root, paths)
        user_config = paths["root"] / "config.json"
        user_config.write_text('{"port": 5001}\n')
        _install(source_root, paths)
        assert user_config.read_text() == '{"port": 5001}\n'

    def test_upgrade_preserves_existing_venv(self, source_root, paths):
        _install(source_root, paths)
        marker = paths["root"] / ".venv" / "marker"
        marker.write_text("keep me\n")
        calls = []
        _install(source_root, paths, venv_calls=calls)
        assert marker.read_text() == "keep me\n"
        assert calls == []

    def test_missing_source_file_is_reported(self, tmp_path, paths):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            _install(empty, paths)


class TestUninstall:
    def _uninstall(self, paths):
        return install.uninstall(
            root=paths["root"],
            launcher=paths["launcher"],
            config_dir=paths["config_dir"],
        )

    def test_removes_runtime_launcher_and_block(self, source_root, paths):
        _install(source_root, paths)
        self._uninstall(paths)
        assert not paths["root"].exists()
        assert not (paths["launcher"] / install.LAUNCHER_NAME).exists()
        text = (paths["config_dir"] / "speechd.conf").read_text()
        assert sc.BEGIN_MARKER not in text
        assert "DefaultModule free-tts" not in text

    def test_preserves_unrelated_user_config(self, source_root, paths):
        paths["config_dir"].mkdir(parents=True)
        (paths["config_dir"] / "speechd.conf").write_text("LogLevel 3\n")
        (paths["config_dir"] / "unrelated.conf").write_text("keep\n")
        _install(source_root, paths)
        self._uninstall(paths)
        assert (paths["config_dir"] / "unrelated.conf").read_text() == "keep\n"
        assert "LogLevel 3" in (paths["config_dir"] / "speechd.conf").read_text()

    def test_restores_previous_default_module(self, source_root, paths):
        paths["config_dir"].mkdir(parents=True)
        (paths["config_dir"] / "speechd.conf").write_text("DefaultModule espeak-ng\n")
        _install(source_root, paths)
        self._uninstall(paths)
        assert (
            (paths["config_dir"] / "speechd.conf").read_text()
            == "DefaultModule espeak-ng\n"
        )

    def test_is_idempotent(self, source_root, paths):
        _install(source_root, paths)
        self._uninstall(paths)
        assert self._uninstall(paths) == []

    def test_without_install_is_a_no_op(self, paths):
        assert self._uninstall(paths) == []


class TestPaths:
    def test_launcher_dir_is_user_libexec(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert install.launcher_dir() == (
            tmp_path / ".local" / "libexec" / "speech-dispatcher-modules"
        )

    def test_speechd_config_dir_respects_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert install.speechd_config_dir() == tmp_path / "speech-dispatcher"


class TestRestart:
    def test_failure_is_tolerated(self):
        def runner(*args, **kwargs):
            raise OSError("pkill missing")

        install.restart_speech_dispatcher(runner=runner)

    def test_runs_a_command(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("R", (), {"returncode": 0})()

        install.restart_speech_dispatcher(runner=runner)
        assert calls
```

- [ ] **Step 5: Write `desktop/free-tts.conf`**

```text
# Speech Dispatcher module configuration for free-tts.
#
# This file exists because Speech Dispatcher passes every module a config path.
# All adapter settings live in ~/.config/free-tts/config.json instead, so that
# one file configures the module regardless of how it was launched.
#
# Debug turns on verbose module logging in the Speech Dispatcher log.
Debug 0
```

- [ ] **Step 6: Write `desktop/install.py`**

```python
"""Per-user installer for the free-tts Speech Dispatcher module.

Installs into ~/.local and ~/.config only. Runtime files are staged and swapped,
the private virtualenv is created at its final path, and the user's speechd.conf
is edited only inside a marked block that uninstall can remove exactly.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable

from desktop.backend import install_root
from desktop.speechd_config import apply_managed_block, remove_managed_block

logger = logging.getLogger("free-tts.install")

MODULE_NAME = "free-tts"
LAUNCHER_NAME = "sd_free-tts"
MODULE_CONF_NAME = "free-tts.conf"
MANIFEST_NAME = "install-manifest.json"
RUNTIME_ENTRIES = ("server.py", "requirements.txt", "config.example.json", "desktop")
_PRESERVED = (".venv", "config.json", MANIFEST_NAME)

_LAUNCHER_TEMPLATE = """#!/bin/sh
# Managed by free-tts install. Launched by Speech Dispatcher as: {launcher} <conf>
FREE_TTS_HOME={root}
export FREE_TTS_HOME
PYTHONPATH={root}
export PYTHONPATH
exec {python} -m desktop.module "$@"
"""


def launcher_dir() -> pathlib.Path:
    """Where Speech Dispatcher looks for a user's module binaries."""
    return pathlib.Path.home() / ".local" / "libexec" / "speech-dispatcher-modules"


def speechd_config_dir() -> pathlib.Path:
    """The user's Speech Dispatcher configuration directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return base / "speech-dispatcher"


def manifest_path() -> pathlib.Path:
    """Where the install manifest lives."""
    return install_root() / MANIFEST_NAME


def _default_venv_builder(root: pathlib.Path) -> None:
    """Create the private virtualenv and install the server's requirements."""
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run(
        [
            str(venv / "bin" / "pip"),
            "install",
            "--quiet",
            "-r",
            str(root / "requirements.txt"),
        ],
        check=True,
    )


def _stage_runtime(source_root: pathlib.Path, staging: pathlib.Path) -> None:
    for name in RUNTIME_ENTRIES:
        source = source_root / name
        if not source.exists():
            raise FileNotFoundError(f"missing runtime entry in checkout: {source}")
        target = staging / name
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(source, target)


def install(
    source_root: pathlib.Path,
    *,
    root: pathlib.Path | None = None,
    launcher: pathlib.Path | None = None,
    config_dir: pathlib.Path | None = None,
    venv_builder: Callable[[pathlib.Path], None] | None = None,
) -> dict[str, str]:
    """Install or upgrade the per-user integration. Returns the manifest."""
    source_root = pathlib.Path(source_root)
    root = install_root() if root is None else pathlib.Path(root)
    launcher_directory = launcher_dir() if launcher is None else pathlib.Path(launcher)
    config_directory = (
        speechd_config_dir() if config_dir is None else pathlib.Path(config_dir)
    )
    build_venv = venv_builder or _default_venv_builder

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".free-tts-stage-", dir=root.parent))
    try:
        _stage_runtime(source_root, staging)
        for name in _PRESERVED:
            existing = root / name
            if existing.exists():
                shutil.move(str(existing), str(staging / name))
        if root.exists():
            shutil.rmtree(root)
        shutil.move(str(staging), str(root))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    if not (root / ".venv").exists():
        build_venv(root)

    venv_python = root / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    launcher_directory.mkdir(parents=True, exist_ok=True)
    launcher_file = launcher_directory / LAUNCHER_NAME
    launcher_file.write_text(
        _LAUNCHER_TEMPLATE.format(
            launcher=LAUNCHER_NAME, root=str(root), python=python
        ),
        encoding="utf-8",
    )
    launcher_file.chmod(0o755)

    modules_dir = config_directory / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "desktop" / MODULE_CONF_NAME, modules_dir / MODULE_CONF_NAME)

    speechd_conf = config_directory / "speechd.conf"
    original = speechd_conf.read_text(encoding="utf-8") if speechd_conf.is_file() else ""
    backup = config_directory / "speechd.conf.free-tts.bak"
    if original and not backup.exists():
        backup.write_text(original, encoding="utf-8")
    speechd_conf.write_text(
        apply_managed_block(original, LAUNCHER_NAME, MODULE_CONF_NAME),
        encoding="utf-8",
    )

    manifest = {
        "module": MODULE_NAME,
        "root": str(root),
        "launcher": str(launcher_file),
        "module_conf": str(modules_dir / MODULE_CONF_NAME),
        "speechd_conf": str(speechd_conf),
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Installed free-tts module into %s", root)
    return manifest


def uninstall(
    *,
    root: pathlib.Path | None = None,
    launcher: pathlib.Path | None = None,
    config_dir: pathlib.Path | None = None,
) -> list[str]:
    """Remove only what install created. Returns the paths actually removed."""
    root = install_root() if root is None else pathlib.Path(root)
    launcher_directory = launcher_dir() if launcher is None else pathlib.Path(launcher)
    config_directory = (
        speechd_config_dir() if config_dir is None else pathlib.Path(config_dir)
    )

    removed: list[str] = []
    speechd_conf = config_directory / "speechd.conf"
    if speechd_conf.is_file():
        current = speechd_conf.read_text(encoding="utf-8")
        cleaned = remove_managed_block(current)
        if cleaned != current:
            speechd_conf.write_text(cleaned, encoding="utf-8")
            removed.append(str(speechd_conf))

    module_conf = config_directory / "modules" / MODULE_CONF_NAME
    if module_conf.is_file():
        module_conf.unlink()
        removed.append(str(module_conf))

    launcher_file = launcher_directory / LAUNCHER_NAME
    if launcher_file.exists():
        launcher_file.unlink()
        removed.append(str(launcher_file))

    if root.exists():
        shutil.rmtree(root)
        removed.append(str(root))

    return removed


def restart_speech_dispatcher(runner: Callable[..., object] = subprocess.run) -> None:
    """Ask the user's Speech Dispatcher to exit so it reloads configuration."""
    try:
        runner(
            ["pkill", "-u", str(os.getuid()), "-x", "speech-dispatcher"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        logger.warning("Could not restart speech-dispatcher automatically: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """``python -m desktop.install [install|uninstall]``."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "install"
    if action == "install":
        manifest = install(pathlib.Path(__file__).resolve().parent.parent)
        restart_speech_dispatcher()
        print(f"Installed free-tts into {manifest['root']}")
        print("Restart any open Qt applications so they reload the voice list.")
        return 0
    if action == "uninstall":
        removed = uninstall()
        restart_speech_dispatcher()
        for path in removed:
            print(f"Removed {path}")
        return 0
    print(f"Unknown action {action!r}; use install or uninstall.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run the tests and confirm they pass**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures. (361 tests at time of writing; a different total is fine if you added cases, but nothing may fail or error.)

- [ ] **Step 8: Commit**

```bash
git add desktop/speechd_config.py desktop/install.py desktop/free-tts.conf tests/test_desktop_speechd_config.py tests/test_desktop_install.py
git commit -m "feat(desktop): per-user installer with managed speechd.conf block"
```

## Task 11: End-to-end protocol test and documentation

**Implementer tier:** Advanced

**Files:**

- Create: `tests/test_desktop_end_to_end.py`
- Create: `docs/desktop-tts.md`
- Modify: `README.md:1-60`
- Modify: `.gitignore:1-12`

**Interfaces:**

- Consumes:
  - `desktop.module`: `run(argv: list[str], stdin, stdout) -> int` and `main() -> int` (Task 9).
  - `desktop.protocol` response constants (Task 8).
  - `desktop.settings.load_config(path=None, env=None)`, which reads `FREE_TTS_BACKEND_URL`, `FREE_TTS_AUTOSTART`, `FREE_TTS_IDLE_TIMEOUT`, `FREE_TTS_STARTUP_TIMEOUT`, `FREE_TTS_REQUEST_TIMEOUT`, `FREE_TTS_MAX_CHUNK_CHARS`, `FREE_TTS_FFMPEG_PATH` (Task 4).
  - `server.py` endpoints from Tasks 1-3: `GET /health` with `service`/`api_version`, `GET /voices`, `POST /generate-and-download-tts` accepting `request_id`, `DELETE /tts-request/<id>`.
- Produces: `tests/test_desktop_end_to_end.py`, which runs the module as a real subprocess against a `http.server`-based fake backend and replays a full session; plus user documentation.

- [ ] **Step 1: Write the failing end-to-end test**

Create `tests/test_desktop_end_to_end.py`:

```python
"""Runs the module as a subprocess against a fake backend over real pipes.

This is the only test that exercises stdin/stdout framing, threading, and the
INIT handshake together, which unit tests deliberately stub out.
"""

import http.server
import json
import pathlib
import shutil
import subprocess
import sys
import threading

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]

VOICES = {
    "default_voice": "en-US-AvaMultilingualNeural",
    "languages": [{"locale": "en-US", "name": "English (United States)"}],
    "voices": [
        {
            "ShortName": "en-US-AvaMultilingualNeural",
            "Gender": "Female",
            "Locale": "en-US",
            "LanguageName": "English (United States)",
        },
        {
            "ShortName": "fr-FR-HenriNeural",
            "Gender": "Male",
            "Locale": "fr-FR",
            "LanguageName": "French (France)",
        },
    ],
}


def _mp3_tone() -> bytes:
    """A short real MP3, so the module's ffmpeg decode path is genuine."""
    result = subprocess.run(
        [
            "ffmpeg", "-hide_banner", "-loglevel", "error",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=0.15",
            "-f", "mp3", "pipe:1",
        ],
        capture_output=True,
        check=False,
    )
    if result.returncode != 0 or not result.stdout:
        pytest.skip("ffmpeg cannot generate a test tone here")
    return result.stdout


class _Handler(http.server.BaseHTTPRequestHandler):
    audio = b""
    seen: list[str] = []

    def log_message(self, *args):
        return

    def _json(self, status, payload):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/health":
            self._json(
                200,
                {
                    "status": "ok",
                    "service": "free-tts",
                    "api_version": 1,
                    "voice_cache_ready": True,
                },
            )
        elif self.path == "/voices":
            self._json(200, VOICES)
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        type(self).seen.append(payload.get("ssml", ""))
        self.send_response(200)
        self.send_header("Content-Type", "audio/mpeg")
        self.send_header("Content-Length", str(len(type(self).audio)))
        self.end_headers()
        self.wfile.write(type(self).audio)

    def do_DELETE(self):
        self._json(200, {"cancelled": True})


@pytest.fixture
def backend():
    if shutil.which("ffmpeg") is None:
        pytest.skip("ffmpeg not installed")
    _Handler.audio = _mp3_tone()
    _Handler.seen = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()


def _session(backend_url, script, timeout=60):
    """Feed ``script`` to the module and return its stdout bytes."""
    env = {
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(ROOT),
        "FREE_TTS_BACKEND_URL": backend_url,
        "FREE_TTS_AUTOSTART": "false",
        "HOME": "/nonexistent-free-tts-home",
        "XDG_CONFIG_HOME": "/nonexistent-free-tts-config",
    }
    result = subprocess.run(
        [sys.executable, "-m", "desktop.module", "/dev/null"],
        input=script,
        capture_output=True,
        env=env,
        cwd=str(ROOT),
        timeout=timeout,
    )
    return result.stdout


class TestFullSession:
    def test_init_audio_voices_speak_quit(self, backend):
        script = b"\n".join(
            [
                b"INIT",
                b"AUDIO",
                b"audio_output_method=server",
                b".",
                b"LIST VOICES",
                b"SET",
                b"rate=0",
                b"pitch=0",
                b"volume=100",
                b"language=en-US",
                b"synthesis_voice=en-US-AvaMultilingualNeural",
                b".",
                b"SPEAK",
                b'<speak>Hello there. <mark name="__spd_0"/></speak>',
                b".",
                b"QUIT",
                b"",
            ]
        )
        out = _session(backend, script)
        assert b"299 OK LOADED SUCCESSFULLY" in out
        assert b"203 OK AUDIO INITIALIZED" in out
        assert b"200 OK VOICE LIST SENT" in out
        assert b"200-en-US-AvaMultilingualNeural\ten-US\tnone" in out
        assert b"203 OK SETTINGS RECEIVED" in out
        assert b"202 OK RECEIVING MESSAGE" in out
        assert b"200 OK SPEAKING" in out
        assert b"701 BEGIN" in out
        assert b"705-AUDIO\x00" in out
        assert b"700-__spd_0" in out
        assert b"702 END" in out
        assert b"210 OK QUIT" in out

    def test_speaking_precedes_begin_and_end_is_last_event(self, backend):
        script = b"\n".join(
            [
                b"INIT",
                b"SPEAK",
                b'<speak>One. <mark name="__spd_0"/></speak>',
                b".",
                b"QUIT",
                b"",
            ]
        )
        out = _session(backend, script)
        assert out.index(b"200 OK SPEAKING") < out.index(b"701 BEGIN")
        assert out.index(b"701 BEGIN") < out.index(b"702 END")

    def test_every_voice_is_listed(self, backend):
        out = _session(backend, b"INIT\nLIST VOICES\nQUIT\n")
        assert out.count(b"200-") == 2
        assert b"fr-FR-HenriNeural\tfr-FR\tnone" in out

    def test_unknown_command_is_reported(self, backend):
        out = _session(backend, b"INIT\nFLY\nQUIT\n")
        assert b"300 ERR UNKNOWN COMMAND" in out

    def test_bad_setting_is_rejected(self, backend):
        out = _session(backend, b"INIT\nSET\nrate=9000\n.\nQUIT\n")
        assert b"303 ERROR INVALID PARAMETER OR VALUE" in out

    def test_non_server_audio_method_is_rejected(self, backend):
        out = _session(
            backend, b"INIT\nAUDIO\naudio_output_method=pulse\n.\nQUIT\n"
        )
        assert b"303 ERROR INVALID PARAMETER OR VALUE" in out

    def test_empty_message_is_refused(self, backend):
        out = _session(backend, b"INIT\nSPEAK\n<speak>  </speak>\n.\nQUIT\n")
        assert b"301 ERROR CANT SPEAK" in out
        assert b"701 BEGIN" not in out

    def test_multiline_message_is_reassembled(self, backend):
        script = b"\n".join(
            [
                b"INIT",
                b"SPEAK",
                b"<speak>First line.",
                b'Second line. <mark name="__spd_0"/></speak>',
                b".",
                b"QUIT",
                b"",
            ]
        )
        out = _session(backend, script)
        assert b"702 END" in out
        assert any("Second line" in ssml for ssml in _Handler.seen)

    def test_stop_before_speak_is_harmless(self, backend):
        out = _session(backend, b"INIT\nSTOP\nQUIT\n")
        assert b"210 OK QUIT" in out
        assert b"703 STOP" not in out

    def test_eof_without_quit_exits_cleanly(self, backend):
        out = _session(backend, b"INIT\n")
        assert b"299 OK LOADED SUCCESSFULLY" in out


class TestBackendUnavailable:
    def test_speak_refused_when_backend_is_down(self):
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not installed")
        out = _session(
            "http://127.0.0.1:9",
            b"INIT\nSPEAK\n<speak>Hi.</speak>\n.\nQUIT\n",
        )
        assert b"301 ERROR CANT SPEAK" in out
        assert b"701 BEGIN" not in out

    def test_voices_refused_when_backend_is_down(self):
        if shutil.which("ffmpeg") is None:
            pytest.skip("ffmpeg not installed")
        out = _session("http://127.0.0.1:9", b"INIT\nLIST VOICES\nQUIT\n")
        assert b"304 CANT LIST VOICES" in out


class TestStdoutDiscipline:
    def test_nothing_but_protocol_reaches_stdout(self, backend):
        out = _session(backend, b"INIT\nLIST VOICES\nQUIT\n")
        for line in out.split(b"\n"):
            if not line or line.startswith(b"705-AUDIO"):
                continue
            assert line[:1].isdigit(), f"non-protocol line on stdout: {line!r}"
```

- [ ] **Step 2: Run it and confirm it fails**

Run: `.venv/bin/python -m pytest tests/test_desktop_end_to_end.py -q`
Expected: FAIL, because `desktop/module.py` must handle a real pipe session end to end. If Tasks 1-10 are complete this may already pass; in that case confirm it passes and continue.

- [ ] **Step 3: Fix whatever the end-to-end run exposes**

Only touch `desktop/` files. Do not weaken an assertion to make it pass: the failures this test finds are framing, threading, and flushing bugs that unit tests cannot see. Re-run until it passes.

Run: `.venv/bin/python -m pytest tests/test_desktop_end_to_end.py -q`
Expected: PASS.

- [ ] **Step 4: Write `docs/desktop-tts.md`**

````markdown
# Using free-tts as a desktop TTS engine

This makes Okular's built-in **Tools > Speak** actions, and any other Speech
Dispatcher client, speak through free-tts voices.

## Requirements

- `speech-dispatcher` and `libspeechd` (Qt's bridge to it)
- `ffmpeg` (decodes backend MP3 to PCM)
- Python 3.11+

On Arch:

```bash
sudo pacman -S --needed speech-dispatcher ffmpeg
```

## Install

```bash
python -m desktop.install install
```

This is a per-user install. It writes to:

| Path | Contents |
|---|---|
| `~/.local/share/free-tts/` | runtime copy of the server and adapter, plus a private virtualenv |
| `~/.local/libexec/speech-dispatcher-modules/sd_free-tts` | module launcher |
| `~/.config/speech-dispatcher/modules/free-tts.conf` | module config |
| `~/.config/speech-dispatcher/speechd.conf` | one marked block registering the module |

Nothing outside your home directory is touched. Your existing `speechd.conf` is
backed up once to `speechd.conf.free-tts.bak`, and only the marked block is ever
rewritten.

Restart open Qt applications afterwards: Qt reads the voice list once when its
TTS engine starts.

## Verify

```bash
spd-say -o free-tts -L
spd-say -w -o free-tts -y en-US-AvaMultilingualNeural "Desktop speech is working."
```

Then open a document in Okular and use **Tools > Speak**.

### Optional: check what Qt applications see

If you have PyQt6 or PySide6 installed, this reports the voices and state
transitions Qt exposes, which is what Okular consumes:

```python
# qt-probe.py — run with: python qt-probe.py
from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtTextToSpeech import QTextToSpeech

app = QCoreApplication([])
tts = QTextToSpeech("speechd")
print("voices:", [(v.name(), v.locale().name()) for v in tts.availableVoices()][:5])
tts.stateChanged.connect(lambda s: print("state:", s))
tts.say("Checking Qt integration.")
QTimer.singleShot(8000, app.quit)
app.exec()
```

Expect `Ready`, `Speaking`, then `Ready`. Gender reads as unknown; see the
limitation below.

## How the backend starts

On first use the adapter checks `GET /health`.

- A healthy free-tts backend already running is reused and never stopped or
  reconfigured, including on uninstall.
- Otherwise the adapter starts one and gives it a 5 minute idle timeout, so it
  exits on its own after you stop reading.
- If something else answers on that port, the adapter refuses to start a second
  backend and reports an error rather than speaking to an unknown service.

A backend you started by hand keeps running with no idle timeout.

## Configuration

`~/.config/free-tts/config.json` configures the adapter. Every key is optional,
and each has a `FREE_TTS_*` environment override.

| Key | Default | Meaning |
|---|---|---|
| `backend_url` | `http://127.0.0.1:5000` | backend base URL |
| `autostart` | `true` | allow on-demand startup |
| `idle_timeout` | `300` | idle seconds before an adapter-started backend exits (`0` disables) |
| `startup_timeout` | `30` | readiness wait after starting a backend |
| `request_timeout` | `120` | per-sentence synthesis timeout |
| `max_chunk_chars` | `400` | cap for long unpunctuated segments |
| `ffmpeg_path` | `ffmpeg` | decoder executable |

Voice, rate, pitch, and volume come from the calling application, not from this
file. Rate maps to -50%..+200%, pitch to -50Hz..+50Hz, and volume to a PCM gain.

## Known limitation

Qt reports every Speech Dispatcher voice with **unknown gender**, because Speech
Dispatcher's voice record has no gender field. Voice names and locales are exact
and selecting one works; only Qt's gender filter is unavailable. `spd-say -t
female1` still works, since the adapter keeps gender internally.

## Logs

- module: `~/.cache/speech-dispatcher/log/speech-dispatcher.log`
- adapter-started backend: `~/.cache/free-tts/backend.log`

Set `FREE_TTS_DEBUG=1` for verbose module logging. Spoken text is never logged.

## Uninstall

```bash
python -m desktop.install uninstall
```

Removes only what was installed, restores any `DefaultModule` it displaced, and
leaves Speech Dispatcher, unrelated config, and running backends alone.
````

- [ ] **Step 5: Link it from `README.md`**

In `README.md`, after the `### Chrome Extension` feature bullet block that ends with the Options line, add a new section:

````markdown
### Desktop TTS engine (Okular, KDE, any Speech Dispatcher client)
Use free-tts voices from Okular's built-in **Tools > Speak** actions by installing
a per-user Speech Dispatcher output module:

```bash
sudo pacman -S --needed speech-dispatcher ffmpeg   # or your distro's equivalent
python -m desktop.install install
```

The backend starts on demand and exits when idle. See
[docs/desktop-tts.md](docs/desktop-tts.md) for configuration, verification, and
uninstall.
````

- [ ] **Step 6: Ignore the dev virtualenv**

Add `.venv-dev/` on its own line to `.gitignore`, after the existing `venv/` line, so a differently named local environment cannot be committed.

- [ ] **Step 7: Run the whole suite and confirm it passes**

Run: `.venv/bin/python -m pytest -q`
Expected: PASS, 379 tests, no failures and no errors.

- [ ] **Step 8: Commit**

```bash
git add tests/test_desktop_end_to_end.py docs/desktop-tts.md README.md .gitignore
git commit -m "test(desktop): end-to-end protocol session; docs for desktop engine"
```
