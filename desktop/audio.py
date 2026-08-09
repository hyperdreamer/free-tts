"""Decode backend MP3 into PCM for Speech Dispatcher's audio channel."""

from __future__ import annotations

import array
import contextlib
import logging
import subprocess
import sys
import threading
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


class DecodeCancelled(DecodeError):
    """Decoding stopped because its generation was cancelled."""


def native_big_endian() -> bool:
    """True when this interpreter's native sample order is big-endian."""
    return sys.byteorder == "big"


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
