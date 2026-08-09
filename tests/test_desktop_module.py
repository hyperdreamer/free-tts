"""Engine state machine: acceptance, events, stop, pause, and settings."""

import contextlib
import dataclasses
import io
import threading

import pytest

from desktop import backend, module, settings, voices

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
        self.voice_calls = 0
        self.requests = []
        self.cancelled = []

    def voices(self):
        self.voice_calls += 1
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload

    def synthesize(self, text, voice_name, rate, pitch, request_id, should_abort=None):
        self.requests.append((text, voice_name, rate, pitch))
        if self._error is not None:
            raise self._error
        return self._audio

    def cancel(self, request_id, *, still_wanted=None):
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

    def test_revalidates_backend_and_catalog_on_every_listing(self):
        controller = _FakeController()
        client = _FakeClient()
        engine, _ = _engine(client=client, controller=controller)
        engine.list_voices()
        engine.list_voices()
        assert controller.calls == 2
        assert client.voice_calls == 2

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

    def test_stop_during_decode_suppresses_every_later_emission(self):
        entered_decode = threading.Event()
        release_decode = threading.Event()

        class TimelineIO(_FakeIO):
            def send_audio(self, pcm, sample_rate=24000):
                self.lines.append("AUDIO")
                super().send_audio(pcm, sample_rate)

        def blocked_decoder(mp3, ffmpeg_path, sample_rate):
            entered_decode.set()
            release_decode.wait()
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
        assert entered_decode.wait(1)

        engine.handle_stop()
        fake_io.lines.append("STOP_RETURNED")
        release_decode.set()

        assert engine.wait_idle()
        boundary = fake_io.lines.index("STOP_RETURNED")
        assert not any(
            line in {"701 BEGIN", "AUDIO"} or line.startswith("700:")
            for line in fake_io.lines[boundary + 1 :]
        )
        assert fake_io.lines.count("703 STOP") == 1

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


class _LifecycleClient(_FakeClient):
    def __init__(self, state, *, fail_first_post=False):
        super().__init__()
        self._state = state
        self._fail_first_post = fail_first_post
        self.attempts = 0

    def voices(self):
        if not self._state["alive"]:
            from desktop.synth import SynthError

            raise SynthError("backend disappeared")
        return super().voices()

    def synthesize(self, text, voice_name, rate, pitch, request_id, should_abort=None):
        from desktop.synth import SynthError

        self.attempts += 1
        self.requests.append((text, voice_name, rate, pitch))
        if self._fail_first_post and self.attempts == 1:
            self._state["alive"] = False
            raise SynthError("connection refused")
        if not self._state["alive"]:
            raise SynthError("connection refused")
        return self._audio


def _lifecycle_engine(*, fail_first_post=False):
    state = {"alive": True, "spawns": 0}

    def fetch(_url, _timeout):
        if not state["alive"]:
            raise OSError("connection refused")
        return {
            "status": "ok",
            "service": "free-tts",
            "api_version": 1,
            "voice_cache_ready": True,
        }

    def spawn(_command, _env, _log_path):
        state["alive"] = True
        state["spawns"] += 1
        return type("Proc", (), {"poll": lambda self: None})()

    @contextlib.contextmanager
    def lock():
        yield

    ticks = [0.0]

    def clock():
        ticks[0] += 0.1
        return ticks[0]

    controller = backend.BackendController(
        dataclasses.replace(settings.DEFAULTS, startup_timeout=3),
        fetch=fetch,
        spawn=spawn,
        sleep=lambda _seconds: None,
        clock=clock,
        lock_factory=lock,
    )
    client = _LifecycleClient(state, fail_first_post=fail_first_post)
    engine, fake_io = _engine(client=client, controller=controller)
    return engine, fake_io, client, state


class TestBackendRecovery:
    def test_later_speak_restarts_backend_after_idle_exit(self):
        engine, fake_io, client, state = _lifecycle_engine()
        engine.handle_speak(SSML)
        assert engine.wait_idle()

        state["alive"] = False
        fake_io.lines.clear()
        engine.handle_speak(SSML)

        assert engine.wait_idle()
        assert state["spawns"] == 1
        assert client.voice_calls == 2
        assert fake_io.lines[-1] == "702 END"

    def test_health_to_post_race_restarts_refreshes_and_retries_once(self):
        engine, fake_io, client, state = _lifecycle_engine(fail_first_post=True)
        engine.handle_speak(SSML)

        assert engine.wait_idle()
        assert state["spawns"] == 1
        assert client.voice_calls == 2
        assert client.attempts == 2
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


class TestCancellationOwnership:
    """Cancellation reaches the backend and never crosses generations."""

    def test_delete_before_registration_is_retried_until_it_lands(self):
        registered = threading.Event()
        release_post = threading.Event()
        allow_registration = threading.Event()

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
                assert allow_registration.wait(2)
                registered.set()
                release_post.wait(2)
                return self._audio

            def cancel(self, request_id, *, still_wanted=None):
                if not registered.is_set():
                    self.statuses.append(404)
                    allow_registration.set()
                    if still_wanted is not None and still_wanted():
                        assert registered.wait(2)
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

    def test_stale_worker_cannot_cancel_a_newer_generation(self, monkeypatch):
        monkeypatch.setattr(module, "_WORKER_RECLAIM_SECONDS", 0.0)

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
                    self.release_first.wait()
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
