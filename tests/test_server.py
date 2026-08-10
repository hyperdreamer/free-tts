"""Deterministic tests for server.py helpers, config, SSML parsing, Flask endpoints,
semaphore timeout, and known-voice behavior.

No network access. No real server. Mock edge_tts and related I/O.
"""

import asyncio
import os
import pathlib
import subprocess
import sys
import threading
from io import BytesIO
from unittest import mock

import pytest

# Prevent edge_tts from being imported by the module-level code.
# The factory uses asyncio.run(_refresh_voice_cache()) — mock it before import.
with (
    mock.patch("edge_tts.list_voices", new_callable=mock.AsyncMock),
    mock.patch("edge_tts.Communicate"),
    mock.patch("server._voice_cache_ready", True),
):
    import server

# ---------------------------------------------------------------------------
# Config helpers: precedence, validation, clamping
# ---------------------------------------------------------------------------


class TestConfigHelpers:
    """Tests for _cfg, _cfg_int, _cfg_list with env-var > dict > default logic."""

    def test_cfg_env_wins_over_cache(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TTS_TEMP_VAR", "env-value")
        with mock.patch.object(server, "_CONFIG_CACHE", {"temp_key": "cache-value"}):
            result = server._cfg("temp_key", "TTS_TEMP_VAR", "default-value")
        assert result == "env-value"

    def test_cfg_cache_wins_when_no_env(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TTS_TEMP_VAR", raising=False)
        with mock.patch.object(server, "_CONFIG_CACHE", {"temp_key": "cache-value"}):
            result = server._cfg("temp_key", "TTS_TEMP_VAR", "default-value")
        assert result == "cache-value"

    def test_cfg_default_when_neither_env_nor_cache(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_TEMP_VAR", raising=False)
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg("temp_key", "TTS_TEMP_VAR", "default-value")
        assert result == "default-value"

    def test_cfg_int_clamps_below_minimum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_MAX_CONCURRENT", "-5")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_int(
                "max_concurrent", "TTS_MAX_CONCURRENT", 2, minimum=0
            )
        assert result == 2  # clamped to default

    def test_cfg_int_clamps_above_maximum(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_PORT", "99999")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_int("port", "TTS_PORT", 5000, minimum=1, maximum=65535)
        assert result == 5000

    def test_cfg_int_valid_value(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TTS_PORT", "8080")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_int("port", "TTS_PORT", 5000, minimum=1, maximum=65535)
        assert result == 8080

    def test_cfg_int_invalid_string_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_MAX_CONCURRENT", "not-a-number")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_int(
                "max_concurrent", "TTS_MAX_CONCURRENT", 2, minimum=0
            )
        assert result == 2

    def test_cfg_list_env_comma_separated(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_CORS_ORIGINS", "https://a.com, https://b.com")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_list("cors_origins", "TTS_CORS_ORIGINS", ["null"])
        assert result == ["https://a.com", "https://b.com"]

    def test_cfg_list_cache_array(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TTS_CORS_ORIGINS", raising=False)
        with mock.patch.object(server, "_CONFIG_CACHE", {"cors_origins": ["a", "b"]}):
            result = server._cfg_list("cors_origins", "TTS_CORS_ORIGINS", ["null"])
        assert result == ["a", "b"]

    def test_cfg_list_default(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("TTS_CORS_ORIGINS", raising=False)
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_list("cors_origins", "TTS_CORS_ORIGINS", ["null"])
        assert result == ["null"]

    def test_cfg_list_empty_env_uses_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("TTS_CORS_ORIGINS", "  ")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg_list("cors_origins", "TTS_CORS_ORIGINS", ["null"])
        assert result == ["null"]

    def test_cfg_list_invalid_type_falls_back(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("TTS_CORS_ORIGINS", raising=False)
        with mock.patch.object(server, "_CONFIG_CACHE", {"cors_origins": 12345}):
            result = server._cfg_list("cors_origins", "TTS_CORS_ORIGINS", ["null"])
        assert result == ["null"]

    def test_cfg_coerce_to_int(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("TTS_TEMP", "42")
        with mock.patch.object(server, "_CONFIG_CACHE", {}):
            result = server._cfg("temp_key", "TTS_TEMP", 0, coerce=int)
        assert result == 42
        assert isinstance(result, int)


def test_config_only_mode_ignores_tts_setting_env(monkeypatch):
    monkeypatch.setenv("TTS_PORT", "7000")
    with (
        mock.patch.object(server, "_CONFIG_ONLY", True),
        mock.patch.object(server, "_CONFIG_CACHE", {"port": 6123}),
    ):
        assert server._cfg_int(
            "port", "TTS_PORT", 5000, minimum=1, maximum=65535
        ) == 6123


def test_config_only_path_ignores_external_tts_config(tmp_path, monkeypatch):
    monkeypatch.setenv("TTS_CONFIG", str(tmp_path / "foreign.json"))
    expected = pathlib.Path(server.__file__).resolve().parent / "config.json"
    assert server._config_path(config_only=True) == expected
    assert server._config_path(config_only=False) == tmp_path / "foreign.json"


class TestProcessContract:
    def test_waitress_satisfies_single_process_contract(self):
        server._validate_process_model("waitress")

    def test_embedded_gunicorn_mode_is_rejected(self):
        with pytest.raises(RuntimeError, match="single-process"):
            server._validate_process_model("gunicorn")

    def test_embedded_gunicorn_entrypoint_exits_before_app_startup(self):
        env = dict(os.environ)
        env["TTS_SERVER"] = "gunicorn"
        env["TTS_CONFIG"] = "/nonexistent-free-tts-config.json"
        env.pop("FLASK_DEBUG", None)

        result = subprocess.run(
            [sys.executable, str(pathlib.Path(server.__file__))],
            env=env,
            capture_output=True,
            check=False,
            timeout=10,
        )

        assert result.returncode == 1
        assert b"single-process" in result.stderr

    def test_external_gunicorn_request_is_rejected(self, monkeypatch):
        monkeypatch.setattr(server, "_voice_cache_ready", True)
        app = server.create_app()
        app.config["TESTING"] = True
        with app.test_client() as client:
            response = client.get(
                "/health",
                environ_overrides={"SERVER_SOFTWARE": "gunicorn/23.0.0"},
            )
        assert response.status_code == 503
        assert "single-process" in response.get_json()["error"]


# ---------------------------------------------------------------------------
# Rate / pitch parsing
# ---------------------------------------------------------------------------


class TestParseRate:
    """Tests for _parse_rate covering all branches of the docstring."""

    @pytest.mark.parametrize(
        "raw,expected",
        [
            (None, server.DEFAULT_RATE),
            ("", server.DEFAULT_RATE),
            ("+20%", "+20%"),
            ("-10%", "-10%"),
            ("0%", "+0%"),
            ("50%", "-50%"),
            ("100%", "+0%"),
            ("150%", "+50%"),
            ("200%", "+100%"),
            ("x-slow", "x-slow"),
            ("fast", "fast"),
            ("+20ms", "+20ms"),
            ("  +30%  ", "+30%"),
            ("0.5%", "+0%"),
        ],
    )
    def test_parse_rate(self, raw, expected):
        assert server._parse_rate(raw) == expected

    def test_parse_rate_non_numeric_percent(self):
        assert server._parse_rate("abc%") == "abc%"


class TestParsePitch:
    """Tests for _parse_pitch covering all branches."""

    def test_none_returns_default(self):
        assert server._parse_pitch(None) == server.DEFAULT_PITCH

    def test_empty_returns_default(self):
        assert server._parse_pitch("") == server.DEFAULT_PITCH

    def test_zero_percent_converts(self):
        assert server._parse_pitch("0%") == "+0Hz"

    def test_signed_hz_passthrough(self):
        assert server._parse_pitch("+20Hz") == "+20Hz"

    def test_named_preset_passthrough(self):
        assert server._parse_pitch("x-low") == "x-low"

    def test_whitespace_handled(self):
        assert server._parse_pitch("  0%  ") == "+0Hz"


# ---------------------------------------------------------------------------
# SSML extraction: valid and invalid cases
# ---------------------------------------------------------------------------


VALID_SSML_TEMPLATE = (
    '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">'
    '<voice name="{voice}">'
    '<prosody rate="{rate}" pitch="{pitch}">'
    "{text}"
    "</prosody>"
    "</voice>"
    "</speak>"
)


class TestSSMLExtraction:
    """Tests for extract_tts_params with valid and invalid SSML."""

    def test_parses_voice_rate_pitch_text(self):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural",
            rate="+10%",
            pitch="+5Hz",
            text="Hello world.",
        )
        req = server.extract_tts_params(ssml)
        assert req.voice == "en-US-AriaNeural"
        assert req.rate == "+10%"
        assert req.pitch == "+5Hz"
        assert req.text == "Hello world."

    def test_missing_voice_uses_default(self):
        ssml = (
            '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0">'
            '<prosody rate="+0%" pitch="+0Hz">Text</prosody>'
            "</speak>"
        )
        req = server.extract_tts_params(ssml)
        assert req.voice == server.DEFAULT_VOICE
        assert req.text == "Text"

    def test_missing_prosody_defaults(self):
        ssml = (
            '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">'
            '<voice name="en-US-AriaNeural">Text without prosody</voice>'
            "</speak>"
        )
        req = server.extract_tts_params(ssml)
        assert req.rate == server.DEFAULT_RATE
        assert req.pitch == server.DEFAULT_PITCH
        assert req.text == "Text without prosody"

    def test_no_voice_tag_uses_defaults(self):
        ssml = (
            '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0">'
            "Just some text"
            "</speak>"
        )
        req = server.extract_tts_params(ssml)
        assert req.voice == server.DEFAULT_VOICE
        assert req.rate == server.DEFAULT_RATE
        assert req.pitch == server.DEFAULT_PITCH
        assert req.text == "Just some text"

    def test_malformed_ssml_raises(self):
        with pytest.raises(ValueError, match="Malformed SSML"):
            server.extract_tts_params("<speak><unclosed>")

    def test_wrong_root_element_raises(self):
        with pytest.raises(ValueError, match="root element must be <speak>"):
            server.extract_tts_params("<foo>text</foo>")

    def test_no_text_raises(self):
        ssml = (
            '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">'
            '<voice name="en-US-AriaNeural">'
            '<prosody rate="+0%" pitch="+0Hz">'
            "   "
            "</prosody>"
            "</voice>"
            "</speak>"
        )
        with pytest.raises(ValueError, match="No speakable text"):
            server.extract_tts_params(ssml)

    def test_text_outside_prosody_preserved(self):
        ssml = (
            '<speak xmlns="http://www.w3.org/2001/10/synthesis" version="1.0" xml:lang="en-US">'
            '<voice name="en-US-AriaNeural">'
            "Sibling text outside prosody."
            '<prosody rate="+0%" pitch="+0Hz">Prosody text.</prosody>'
            "</voice>"
            "</speak>"
        )
        req = server.extract_tts_params(ssml)
        assert "Sibling text" in req.text
        assert "Prosody text." in req.text

    def test_rate_parsing_flow_through(self):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural",
            rate="80%",
            pitch="+0Hz",
            text="Slow.",
        )
        req = server.extract_tts_params(ssml)
        assert req.rate == "-20%"


# ---------------------------------------------------------------------------
# Known-voice behavior
# ---------------------------------------------------------------------------


class TestKnownVoice:
    """Tests for _is_known_voice under various cache states."""

    def test_cache_not_ready_accepts_all(self):
        with mock.patch.object(server, "_voice_cache_ready", False):
            assert server._is_known_voice("anything-at-all") is True

    def test_cache_ready_rejects_unknown(self):
        with (
            mock.patch.object(server, "_voice_cache_ready", True),
            mock.patch.object(
                server, "_voice_cache", [{"ShortName": "en-US-AriaNeural"}]
            ),
        ):
            assert server._is_known_voice("en-US-AriaNeural") is True
            assert server._is_known_voice("nonexistent-voice") is False

    def test_empty_cache_rejects_all(self):
        with (
            mock.patch.object(server, "_voice_cache_ready", True),
            mock.patch.object(server, "_voice_cache", []),
        ):
            assert server._is_known_voice("en-US-AriaNeural") is False


# ---------------------------------------------------------------------------
# Flask endpoint error responses
# ---------------------------------------------------------------------------


class TestFlaskErrorResponses:
    """Tests for Flask endpoint error responses using the test client."""

    @pytest.fixture
    def client(self):
        """Create a Flask test client with mocked edge_tts."""
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

    def test_missing_ssml_field(self, client):
        resp = client.post("/generate-and-download-tts", json={})
        assert resp.status_code == 400
        assert "ssml" in resp.get_json()["error"].lower()

    def test_ssml_not_string(self, client):
        resp = client.post("/generate-and-download-tts", json={"ssml": 123})
        assert resp.status_code == 400

    def test_empty_ssml_string(self, client):
        resp = client.post("/generate-and-download-tts", json={"ssml": "   "})
        assert resp.status_code == 400
        assert "empty" in resp.get_json()["error"].lower()

    def test_malformed_ssml_400(self, client):
        resp = client.post("/generate-and-download-tts", json={"ssml": "<not-valid>"})
        assert resp.status_code == 400

    def test_unknown_voice_400(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="nonexistent-voice",
            rate="+0%",
            pitch="+0Hz",
            text="Hello.",
        )
        resp = client.post("/generate-and-download-tts", json={"ssml": ssml})
        assert resp.status_code == 400
        assert "unknown voice" in resp.get_json()["error"].lower()

    def test_health_endpoint(self, client):
        resp = client.get("/health")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["status"] == "ok"
        assert data["voice_cache_ready"] is True

    def test_voices_endpoint(self, client):
        with mock.patch.object(
            server,
            "_voice_cache",
            [{"ShortName": "en-US-AriaNeural", "Gender": "Female", "Locale": "en-US"}],
        ):
            with mock.patch.object(
                server,
                "_LANGUAGE_LIST",
                [{"locale": "en-US", "name": "English (United States)"}],
            ):
                resp = client.get("/voices")
        assert resp.status_code == 200
        data = resp.get_json()
        assert "voices" in data
        assert "languages" in data
        assert data["default_voice"] == server.DEFAULT_VOICE

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

    def test_health_reports_systemd_process_identity(self, client, monkeypatch):
        invocation_id = "a" * 32
        monkeypatch.setenv("INVOCATION_ID", invocation_id)
        monkeypatch.setattr(server.os, "getpid", lambda: 4242)

        payload = client.get("/health").get_json()

        assert payload["pid"] == 4242
        assert payload["invocation_id"] == invocation_id

    def test_health_reports_null_invocation_outside_systemd(self, client, monkeypatch):
        monkeypatch.delenv("INVOCATION_ID", raising=False)
        monkeypatch.setattr(server.os, "getpid", lambda: 4242)

        payload = client.get("/health").get_json()

        assert payload["pid"] == 4242
        assert payload["invocation_id"] is None

    def test_successful_tts_request(self, client):
        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural",
            rate="+0%",
            pitch="+0Hz",
            text="Hello world.",
        )
        mock_audio = BytesIO(b"\xff\xfb\x90\x00fake mp3 data")

        async def _mock_generate(_req, cancel_event=None):
            return mock_audio.getvalue()

        with mock.patch.object(server, "generate_audio", side_effect=_mock_generate):
            resp = client.post("/generate-and-download-tts", json={"ssml": ssml})
        assert resp.status_code == 200
        assert resp.mimetype == "audio/mpeg"
        assert b"fake mp3" in resp.data

    def test_no_json_body(self, client):
        resp = client.post(
            "/generate-and-download-tts", data="not-json", content_type="text/plain"
        )
        assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Semaphore concurrency guard – route-level tests
# ---------------------------------------------------------------------------


class TestSemaphoreGuard:
    """Flask route tests verifying the _TTS_SEMAPHORE concurrency guard."""

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

    def test_exhausted_semaphore_returns_503_with_retry_after(
        self, client, monkeypatch
    ):
        """When all slots are taken the route must return 503 + Retry-After
        and must NOT call generate_audio."""
        exhausted = threading.BoundedSemaphore(1)
        assert exhausted.acquire(blocking=False)  # take the only slot
        monkeypatch.setattr(server, "_TTS_SEMAPHORE", exhausted)
        monkeypatch.setattr(server, "TTS_QUEUE_TIMEOUT", 1)

        generate_called = False

        async def _fake_generate(_req, cancel_event=None):
            nonlocal generate_called
            generate_called = True
            return b"audio"

        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural",
            rate="+0%",
            pitch="+0Hz",
            text="Hello.",
        )
        with mock.patch.object(server, "generate_audio", side_effect=_fake_generate):
            resp = client.post("/generate-and-download-tts", json={"ssml": ssml})

        assert resp.status_code == 503
        assert resp.headers.get("Retry-After") == "1"
        assert not generate_called, (
            "generate_audio must not be called when semaphore is exhausted"
        )

    def test_slot_released_when_generate_audio_raises(self, client, monkeypatch):
        """An acquired semaphore slot must be released even when
        generate_audio raises, so subsequent callers are not starved."""
        guard = threading.BoundedSemaphore(1)
        monkeypatch.setattr(server, "_TTS_SEMAPHORE", guard)
        monkeypatch.setattr(server, "TTS_QUEUE_TIMEOUT", 5)

        raised = False

        async def _failing_generate(_req, cancel_event=None):
            nonlocal raised
            raised = True
            raise RuntimeError("synthesis failure")

        ssml = VALID_SSML_TEMPLATE.format(
            voice="en-US-AriaNeural",
            rate="+0%",
            pitch="+0Hz",
            text="Hello.",
        )
        with mock.patch.object(server, "generate_audio", side_effect=_failing_generate):
            client.post("/generate-and-download-tts", json={"ssml": ssml})

        assert raised
        # Slot must have been released (acquired, then released in finally).
        assert guard.acquire(blocking=False), (
            "slot was not released after generate_audio raised"
        )


# ---------------------------------------------------------------------------
# Error message sanitisation
# ---------------------------------------------------------------------------


class TestErrorMessage:
    """Tests for _error_message with production/dev modes."""

    def test_client_error_always_real_message(self, monkeypatch):
        monkeypatch.setattr(server, "PRODUCTION", True)
        msg = server._error_message(ValueError("bad input"), is_client_error=True)
        assert msg == "bad input"

    def test_server_error_sanitised_in_production(self, monkeypatch):
        monkeypatch.setattr(server, "PRODUCTION", True)
        msg = server._error_message(RuntimeError("secret"), is_client_error=False)
        assert "secret" not in msg
        assert "check server logs" in msg.lower()

    def test_server_error_raw_in_dev(self, monkeypatch):
        monkeypatch.setattr(server, "PRODUCTION", False)
        msg = server._error_message(RuntimeError("trace me"), is_client_error=False)
        assert msg == "trace me"


# ---------------------------------------------------------------------------
# Cancellation registry and the DELETE endpoint
# ---------------------------------------------------------------------------


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
            server._release_cancel_token("abc123", token)

    def test_cancel_is_idempotent_until_released(self, client):
        token = server._register_cancel_token("dup")
        try:
            assert client.delete("/tts-request/dup").status_code == 200
            assert client.delete("/tts-request/dup").status_code == 200
        finally:
            server._release_cancel_token("dup", token)
        assert client.delete("/tts-request/dup").status_code == 404

    def test_released_token_is_gone(self, client):
        token = server._register_cancel_token("gone")
        server._release_cancel_token("gone", token)
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

    def test_mid_await_cancellation_closes_stream_promptly(self):
        req = server.TTSRequest(
            voice="en-US-AriaNeural", rate="+0%", pitch="+0Hz", text="Hello."
        )
        token = server._register_cancel_token("blocked-read")

        async def scenario():
            entered = asyncio.Event()
            closed = asyncio.Event()

            class _Stream:
                async def __anext__(self):
                    entered.set()
                    await asyncio.Future()

                async def aclose(self):
                    closed.set()

            class _Comm:
                def stream(self):
                    return _Stream()

            with mock.patch.object(
                server.edge_tts, "Communicate", return_value=_Comm()
            ):
                task = asyncio.create_task(
                    server.generate_audio(req, cancel_event=token)
                )
                await entered.wait()
                token.set()
                with pytest.raises(server.CancelledError):
                    await asyncio.wait_for(task, 0.5)
                assert closed.is_set()

        try:
            asyncio.run(scenario())
        finally:
            server._release_cancel_token("blocked-read", token)

    def test_mid_await_delete_returns_499_and_releases_slot(
        self, monkeypatch
    ):
        entered = threading.Event()
        closed = threading.Event()

        class _Stream:
            async def __anext__(self):
                entered.set()
                await asyncio.Future()

            async def aclose(self):
                closed.set()

        class _Comm:
            def stream(self):
                return _Stream()

        guard = threading.BoundedSemaphore(1)
        monkeypatch.setattr(server, "_TTS_SEMAPHORE", guard)
        monkeypatch.setattr(server, "TTS_STALL_TIMEOUT", 0)
        monkeypatch.setattr(server, "_voice_cache_ready", True)
        monkeypatch.setattr(
            server, "_voice_cache", [{"ShortName": "en-US-AriaNeural"}]
        )
        monkeypatch.setattr(server.edge_tts, "Communicate", lambda *a, **k: _Comm())
        app = server.create_app()
        app.config["TESTING"] = True
        responses = []

        def post_request():
            with app.test_client() as post_client:
                responses.append(
                    post_client.post(
                        "/generate-and-download-tts",
                        json={
                            "ssml": VALID_SSML_TEMPLATE.format(
                                voice="en-US-AriaNeural",
                                rate="+0%",
                                pitch="+0Hz",
                                text="Hello.",
                            ),
                            "request_id": "midawait",
                        },
                    )
                )

        thread = threading.Thread(target=post_request, daemon=True)
        thread.start()
        assert entered.wait(1)
        with app.test_client() as cancel_client:
            assert cancel_client.delete("/tts-request/midawait").status_code == 200
        thread.join(1)

        assert not thread.is_alive()
        assert responses[0].status_code == 499
        assert closed.is_set()
        assert guard.acquire(blocking=False)

    def test_delete_cancels_wait_for_concurrency_slot(self, monkeypatch):
        guard = threading.BoundedSemaphore(1)
        assert guard.acquire(blocking=False)
        registered = threading.Event()
        real_register = server._register_cancel_token

        def register(request_id):
            token = real_register(request_id)
            registered.set()
            return token

        monkeypatch.setattr(server, "_register_cancel_token", register)
        monkeypatch.setattr(server, "_TTS_SEMAPHORE", guard)
        monkeypatch.setattr(server, "TTS_QUEUE_TIMEOUT", 60)
        monkeypatch.setattr(server, "_voice_cache_ready", True)
        monkeypatch.setattr(
            server, "_voice_cache", [{"ShortName": "en-US-AriaNeural"}]
        )
        app = server.create_app()
        app.config["TESTING"] = True
        responses = []

        def post_request():
            with app.test_client() as post_client:
                responses.append(
                    post_client.post(
                        "/generate-and-download-tts",
                        json={
                            "ssml": VALID_SSML_TEMPLATE.format(
                                voice="en-US-AriaNeural",
                                rate="+0%",
                                pitch="+0Hz",
                                text="Queued.",
                            ),
                            "request_id": "queued",
                        },
                    )
                )

        thread = threading.Thread(target=post_request, daemon=True)
        thread.start()
        try:
            assert registered.wait(1)
            with app.test_client() as cancel_client:
                assert cancel_client.delete("/tts-request/queued").status_code == 200
            thread.join(1)
            assert not thread.is_alive()
            assert responses[0].status_code == 499
        finally:
            guard.release()
            thread.join(1)


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
