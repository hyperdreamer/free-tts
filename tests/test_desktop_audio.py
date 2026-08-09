"""MP3 decoding through ffmpeg and PCM gain."""

import array
import subprocess
import sys
import threading

import pytest

from desktop import audio


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
