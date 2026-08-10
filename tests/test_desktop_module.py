"""Engine state machine: acceptance, events, stop, pause, and settings."""

import contextlib
import dataclasses
import io
import json
import threading

import pytest

from desktop import backend, module, settings, synth, voices

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
        decoder=lambda mp3, ffmpeg_path, sample_rate, cancel: b"\x01\x00" * 8,
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

        def failing_decoder(mp3, ffmpeg_path, sample_rate, cancel):
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
            decoder=lambda mp3, ffmpeg_path, sample_rate, cancel: b"\xff\x7f",
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

        def blocked_decoder(mp3, ffmpeg_path, sample_rate, cancel):
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

    def test_pause_retries_lookahead_cancel_after_registration_handoff(self):
        lookahead_started = threading.Event()
        decode_entered = threading.Event()
        release_decode = threading.Event()
        allow_registration = threading.Event()
        registered = threading.Event()
        post_cancelled = threading.Event()
        pause_emitted = threading.Event()

        class TimelineIO(_FakeIO):
            def event_pause(self):
                super().event_pause()
                pause_emitted.set()

        class RacingLookaheadClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.calls = 0
                self.lookahead_id = None
                self.statuses = []

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
                lookahead_started.set()
                assert allow_registration.wait(3)
                registered.set()
                assert post_cancelled.wait(3)
                raise module.Cancelled("lookahead request cancelled")

            def cancel(self, request_id, *, still_wanted=None):
                assert request_id == self.lookahead_id
                if not registered.is_set():
                    self.statuses.append(404)
                    allow_registration.set()
                    if still_wanted is None or not still_wanted():
                        return False
                    assert registered.wait(3)
                self.statuses.append(200)
                self.cancelled.append(request_id)
                post_cancelled.set()
                return True

        def gated_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            decode_entered.set()
            assert release_decode.wait(3)
            return b"\x01\x00" * 8

        client = RacingLookaheadClient()
        fake_io = TimelineIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            client,
            decoder=gated_decoder,
        )
        engine.handle_speak(TWO_CHUNK_SSML)
        assert lookahead_started.wait(3)
        assert decode_entered.wait(3)

        try:
            engine.handle_pause()
            release_decode.set()
            assert pause_emitted.wait(1)
            assert engine.wait_idle(1)
            assert client.statuses == [404, 200]
            assert client.cancelled == [client.lookahead_id]
            assert fake_io.lines.count("704 PAUSE") == 1
            assert "702 END" not in fake_io.lines
        finally:
            release_decode.set()
            post_cancelled.set()
            engine.wait_idle(3)

    def test_pause_interrupts_lookahead_retry_after_at_boundary(self):
        retry_wait_entered = threading.Event()
        pause_boundary_reached = threading.Event()
        release_uninterruptible_wait = threading.Event()
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
                    post_call = self.post_calls
                if post_call == 1:
                    return 200, {}, b"current-mp3"
                if post_call == 2:
                    return 503, {"Retry-After": "5"}, b"{}"
                retry_post_started.set()
                return 200, {}, b"unexpected-retry-mp3"

        def retry_sleep(seconds):
            retry_wait_entered.set()
            if seconds >= 1.0:
                release_uninterruptible_wait.wait(3)
            else:
                pause_boundary_reached.wait(3)

        def gated_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            decode_entered.set()
            assert release_decode.wait(3)
            return b"\x01\x00" * 8

        class BoundaryObservedEngine(module.SpeechEngine):
            def _reach_pause_boundary(self, generation):
                reached = super()._reach_pause_boundary(generation)
                pause_boundary_reached.set()
                return reached

        transport = RetryTransport()
        client = synth.SynthClient(
            settings.DEFAULTS, transport=transport, sleep=retry_sleep
        )
        fake_io = TimelineIO()
        engine = BoundaryObservedEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            client,
            decoder=gated_decoder,
        )
        engine.handle_speak(TWO_CHUNK_SSML)
        assert retry_wait_entered.wait(2)
        assert decode_entered.wait(2)

        try:
            engine.handle_pause()
            release_decode.set()
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
            release_uninterruptible_wait.set()
            engine.wait_idle(3)

    def test_pause_boundary_wins_before_retry_reservation(self):
        reservation_entered = threading.Event()
        allow_reservation = threading.Event()
        pause_boundary_reached = threading.Event()
        allow_boundary_return = threading.Event()
        reservation_finished = threading.Event()
        reservation_results = []
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
                result = super()._reserve_retry(generation, request_id)
                reservation_results.append(result)
                reservation_finished.set()
                return result

            def _reach_pause_boundary(self, generation):
                reached = super()._reach_pause_boundary(generation)
                pause_boundary_reached.set()
                assert allow_boundary_return.wait(3)
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
            assert reservation_finished.wait(2)
            assert reservation_results == [False]
            allow_boundary_return.set()
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
            allow_boundary_return.set()
            reservation_finished.set()
            engine.wait_idle(3)

    def test_retry_reservation_wins_then_pause_cancels_it(self):
        retry_reserved = threading.Event()
        allow_transport = threading.Event()
        registered = threading.Event()
        allow_registration = threading.Event()
        post_cancelled = threading.Event()
        decode_entered = threading.Event()
        release_decode = threading.Event()
        pause_boundary_reached = threading.Event()
        pause_emitted = threading.Event()

        class TimelineIO(_FakeIO):
            def event_pause(self):
                super().event_pause()
                pause_emitted.set()

        class RetryTransport:
            def __init__(self):
                self._lock = threading.Lock()
                self.post_calls = 0
                self.retry_post_calls = 0
                self.statuses = []

            def __call__(self, method, url, body, timeout):
                if method == "GET":
                    return 200, {}, json.dumps(PAYLOAD).encode("utf-8")
                if method == "DELETE":
                    with self._lock:
                        status = 200 if registered.is_set() else 404
                        self.statuses.append(status)
                    if status == 404:
                        allow_registration.set()
                        assert registered.wait(3)
                    else:
                        post_cancelled.set()
                    return status, {}, b'{"cancelled": true}'

                assert method == "POST"
                with self._lock:
                    self.post_calls += 1
                    call = self.post_calls
                if call == 1:
                    return 200, {}, b"current-mp3"
                if call == 2:
                    return 503, {"Retry-After": "0"}, b"{}"

                with self._lock:
                    self.retry_post_calls += 1
                assert allow_registration.wait(3)
                registered.set()
                assert post_cancelled.wait(3)
                return 499, {}, b'{"error": "Request cancelled."}'

        def gated_decoder(mp3, ffmpeg_path, sample_rate, cancel):
            decode_entered.set()
            assert release_decode.wait(3)
            return b"\x01\x00" * 8

        class ReservationGatedEngine(module.SpeechEngine):
            def _reserve_retry(self, generation, request_id):
                reserved = super()._reserve_retry(generation, request_id)
                assert reserved is True
                retry_reserved.set()
                assert allow_transport.wait(3)
                return reserved

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
        assert retry_reserved.wait(2)
        assert decode_entered.wait(2)

        try:
            engine.handle_pause()
            release_decode.set()
            assert pause_boundary_reached.wait(2)
            allow_transport.set()
            assert post_cancelled.wait(2)
            assert pause_emitted.wait(1)
            assert engine.wait_idle(1)
            assert transport.statuses == [404, 200]
            assert transport.retry_post_calls == 1
            assert fake_io.lines.count("704 PAUSE") == 1
            assert "702 END" not in fake_io.lines
        finally:
            release_decode.set()
            allow_transport.set()
            allow_registration.set()
            registered.set()
            post_cancelled.set()
            engine.wait_idle(3)

    def test_stale_worker_cannot_cancel_a_newer_generation(self, monkeypatch):
        monkeypatch.setattr(module, "_WORKER_RECLAIM_SECONDS", 0.0)

        class SlowClient(_FakeClient):
            def __init__(self):
                super().__init__()
                self.first_entered = threading.Event()
                self.release_first = threading.Event()
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

        def record(
            text,
            voice_name,
            rate,
            pitch,
            request_id,
            should_abort=None,
            reserve_retry=None,
        ):
            seen.append(request_id)
            return original(
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=should_abort,
                reserve_retry=reserve_retry,
            )

        client.synthesize = record
        engine.handle_speak(SSML)
        assert engine.wait_idle(3)
        assert len(seen) == 2
        assert seen[0] != seen[1]
        assert fake_io.lines[-1] == "702 END"


def protocol_err_cant_speak():
    from desktop import protocol

    return protocol.ERR_CANT_SPEAK


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
        audio_emit_entered = threading.Event()
        release_audio_emit = threading.Event()

        class GatedEmitLock:
            def __init__(self):
                self._lock = threading.Lock()
                self._entries = 0

            def __enter__(self):
                self._lock.acquire()
                self._entries += 1
                if self._entries == 2:
                    audio_emit_entered.set()
                    assert release_audio_emit.wait(3)
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                self._lock.release()

        class TimelineIO(_FakeIO):
            def send_audio(self, pcm, sample_rate=24000):
                self.lines.append("AUDIO")
                super().send_audio(pcm, sample_rate)

        fake_io = TimelineIO()
        engine = module.SpeechEngine(
            fake_io,
            settings.DEFAULTS,
            _FakeController(),
            _FakeClient(),
            decoder=lambda mp3, ffmpeg_path, sample_rate, cancel: b"\x01\x00" * 8,
        )
        engine._emit_lock = GatedEmitLock()
        engine.handle_speak(SSML)
        assert audio_emit_entered.wait(2)

        try:
            engine.handle_stop()
            fake_io.lines.append("STOP_RETURNED")
            release_audio_emit.set()
            assert engine.wait_idle(3)

            boundary = fake_io.lines.index("STOP_RETURNED")
            assert not any(
                line in {"701 BEGIN", "AUDIO"} or line.startswith("700:")
                for line in fake_io.lines[boundary + 1 :]
            )
            assert fake_io.lines.count("703 STOP") == 1
        finally:
            release_audio_emit.set()
            engine.wait_idle(3)


class TestPauseFallback:
    """PAUSE is honoured even when no index mark is available."""

    class _GatedClient(_FakeClient):
        def __init__(self):
            super().__init__()
            self.entered = threading.Event()
            self.release = threading.Event()

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


class TestMalformedBackendConfiguration:
    """Direct configs still produce protocol errors at command boundaries."""

    @staticmethod
    def _config():
        return dataclasses.replace(
            settings.DEFAULTS,
            backend_url="http://127.0.0.1:notaport",
            autostart=False,
        )

    def test_list_voices_reports_unavailable_for_invalid_http_url(self):
        config = self._config()
        engine, fake_io = _engine(
            config=config, controller=backend.BackendController(config)
        )

        engine.list_voices()

        assert fake_io.lines == ["304 CANT LIST VOICES"]

    def test_speak_reports_unavailable_for_invalid_http_url(self):
        config = self._config()
        engine, fake_io = _engine(
            config=config, controller=backend.BackendController(config)
        )

        engine.handle_speak(SSML)

        assert fake_io.lines == ["301 ERROR CANT SPEAK"]


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
