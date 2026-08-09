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
