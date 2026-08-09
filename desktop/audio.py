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
