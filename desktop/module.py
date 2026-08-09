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
        self._registered = threading.Event()
        self._go = threading.Event()
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

        self._registered.clear()
        self._go.clear()
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
        # The worker registers its first request id, then parks on ``_go``.
        # Waiting here guarantees that by the time this returns, at least one
        # request is registered and cancellable, no matter how STOP races in.
        self._registered.wait(5.0)
        self._go.set()

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
                    if pending is None:
                        request_id = self._register_request()
                        self._registered.set()
                        self._go.wait(5.0)
                        future = pool.submit(
                            self._fetch,
                            token,
                            chunk.text,
                            voice_name,
                            rate,
                            pitch,
                            request_id,
                        )
                    else:
                        future = pending
                        pending = None
                    if index + 1 < len(chunks) and not self._should_abort(token):
                        pending = pool.submit(
                            self._fetch,
                            token,
                            chunks[index + 1].text,
                            voice_name,
                            rate,
                            pitch,
                            self._register_request(),
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

    def _register_request(self) -> str:
        """Create and register a request id before it is submitted.

        Registration happens in the worker thread ahead of the first blocking
        call, so a STOP arriving right after handle_speak always finds the
        request already cancellable.
        """
        request_id = new_request_id()
        with self._lock:
            self._active_requests.add(request_id)
        return request_id

    def _fetch(
        self,
        token: int,
        text: str,
        voice_name: str,
        rate: str,
        pitch: str,
        request_id: str,
    ) -> bytes:
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
            self._active_requests.clear()
        for request_id in outstanding:
            self._client.cancel(request_id)  # type: ignore[attr-defined]

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
            # speechd sends STOP before QUIT when it wants to abort, so a
            # bare QUIT must let the current message finish, not kill it.
            while not engine.wait_idle():
                pass
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
