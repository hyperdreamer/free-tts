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
