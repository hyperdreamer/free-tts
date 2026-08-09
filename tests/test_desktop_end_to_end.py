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
