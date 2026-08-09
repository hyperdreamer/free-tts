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
_WORKER_RECLAIM_SECONDS = 10.0


class _GenerationToken:
    """Invalidatable state and request ownership for one speech generation."""

    def __init__(self) -> None:
        self.cancelled = threading.Event()
        self.pause_requested = threading.Event()
        self._lock = threading.Lock()
        self.requests: set[str] = set()

    def add_request(self, request_id: str) -> None:
        with self._lock:
            self.requests.add(request_id)

    def discard_request(self, request_id: str) -> None:
        with self._lock:
            self.requests.discard(request_id)

    def take_requests(self) -> list[str]:
        """Remove and return this generation's ids, so cleanup runs once."""
        with self._lock:
            taken = sorted(self.requests)
            self.requests.clear()
        return taken

    def snapshot_requests(self) -> list[str]:
        with self._lock:
            return sorted(self.requests)


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
        self._generation: _GenerationToken | None = None
        self._worker: threading.Thread | None = None
        self._registered = threading.Event()
        self._go = threading.Event()
        self._rate = 0
        self._pitch = 0
        self._volume = 100
        self._language: str | None = None
        self._voice_type: str | None = None
        self._synthesis_voice: str | None = None

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

        if not self._reclaim_worker():
            self._io.send(protocol.ERR_CANT_SPEAK)  # type: ignore[attr-defined]
            return
        generation = _GenerationToken()
        with self._lock:
            self._generation = generation
        rate = map_rate(self._rate)
        pitch = map_pitch(self._pitch)
        gain = map_volume(self._volume)

        self._registered.clear()
        self._go.clear()
        self._io.send(protocol.OK_SPEAKING)  # type: ignore[attr-defined]
        worker = threading.Thread(
            target=self._speak_worker,
            args=(generation, chunks, voice.name, rate, pitch, gain),
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
        """Invalidate active speech and cancel its backend requests."""
        with self._lock:
            worker = self._worker
            generation = self._generation
            if worker is None or not worker.is_alive() or generation is None:
                return
            generation.cancelled.set()
        outstanding = generation.snapshot_requests()
        if outstanding:
            threading.Thread(
                target=self._cancel_requests,
                args=(generation, outstanding),
                name="free-tts-cancel",
                daemon=True,
            ).start()

    def handle_pause(self) -> None:
        """Ask the worker to stop at the next index mark."""
        with self._lock:
            worker = self._worker
            generation = self._generation
            if worker is not None and worker.is_alive() and generation is not None:
                generation.pause_requested.set()

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
        self.catalog = None
        self._controller.ensure_ready()  # type: ignore[attr-defined]
        return self._refresh_catalog()

    def _refresh_catalog(self) -> VoiceCatalog:
        payload = self._client.voices()  # type: ignore[attr-defined]
        catalog = VoiceCatalog.from_payload(payload)
        if not len(catalog):
            raise SynthError("backend returned no voices")
        self.catalog = catalog
        return catalog

    def _reclaim_worker(self) -> bool:
        """Stop the previous generation. False if its worker is still alive."""
        with self._lock:
            worker = self._worker
        if worker is None:
            return True
        if worker.is_alive():
            self.handle_stop()
            worker.join(_WORKER_RECLAIM_SECONDS)
        if worker.is_alive():
            logger.error("Previous speech worker did not exit; refusing new message")
            return False
        with self._lock:
            if self._worker is worker:
                self._worker = None
        return True

    def _emit(
        self,
        generation: _GenerationToken,
        action: Callable[[], None],
        *,
        allow_cancelled: bool = False,
    ) -> bool:
        """Check generation state and emit atomically with STOP invalidation."""
        with self._lock:
            if generation is not self._generation:
                return False
            if generation.cancelled.is_set() and not allow_cancelled:
                return False
            action()
            return True

    def _decode_interruptibly(
        self, generation: _GenerationToken, mp3: bytes
    ) -> bytes:
        """Stop waiting for a decoder as soon as this generation is cancelled."""
        done = threading.Event()
        result: list[bytes] = []
        errors: list[BaseException] = []

        def decode() -> None:
            try:
                result.append(
                    self._decode(mp3, self._config.ffmpeg_path, SAMPLE_RATE)
                )
            except BaseException as exc:
                errors.append(exc)
            finally:
                done.set()

        threading.Thread(
            target=decode,
            name="free-tts-decode",
            daemon=True,
        ).start()
        while not done.wait(0.02):
            if self._should_abort(generation):
                raise Cancelled("aborted while decoding")
        if self._should_abort(generation):
            raise Cancelled("aborted after decoding")
        if errors:
            raise errors[0]
        return result[0]

    def _speak_worker(
        self,
        generation: _GenerationToken,
        chunks: list[object],
        voice_name: str,
        rate: str,
        pitch: str,
        gain: float,
    ) -> None:
        """Synthesise every chunk with one-chunk lookahead, then report."""
        outcome = "end"
        began = False
        with ThreadPoolExecutor(
            max_workers=1, thread_name_prefix="free-tts-pre"
        ) as pool:
            pending = None
            try:
                for index, chunk in enumerate(chunks):
                    if self._should_abort(generation):
                        outcome = "stop"
                        break
                    if pending is None:
                        request_id = self._register_request(generation)
                        self._registered.set()
                        self._go.wait(5.0)
                        future = pool.submit(
                            self._fetch,
                            generation,
                            chunk.text,
                            voice_name,
                            rate,
                            pitch,
                            request_id,
                        )
                    else:
                        future = pending
                        pending = None
                    if index + 1 < len(chunks) and not self._should_abort(generation):
                        pending = pool.submit(
                            self._fetch,
                            generation,
                            chunks[index + 1].text,
                            voice_name,
                            rate,
                            pitch,
                            self._register_request(generation),
                        )
                    mp3 = future.result()
                    if self._should_abort(generation):
                        outcome = "stop"
                        break
                    pcm = apply_gain(
                        self._decode_interruptibly(generation, mp3), gain
                    )
                    if self._should_abort(generation):
                        outcome = "stop"
                        break
                    if not began:
                        if not self._emit(
                            generation,
                            lambda: self._io.event_begin(),  # type: ignore[attr-defined]
                        ):
                            outcome = "stop"
                            break
                        began = True
                    for offset in range(
                        0, len(pcm), protocol.MAX_AUDIO_CHUNK_BYTES
                    ):
                        frame = pcm[
                            offset : offset + protocol.MAX_AUDIO_CHUNK_BYTES
                        ]
                        if not self._emit(
                            generation,
                            lambda frame=frame: self._io.send_audio(  # type: ignore[attr-defined]
                                frame, SAMPLE_RATE
                            ),
                        ):
                            outcome = "stop"
                            break
                    if outcome == "stop":
                        break
                    if chunk.mark and not self._emit(
                        generation,
                        lambda: self._io.index_mark(chunk.mark),  # type: ignore[attr-defined]
                    ):
                        outcome = "stop"
                        break
                    if chunk.mark and generation.pause_requested.is_set():
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
                self._cancel_outstanding(generation)

        def emit_stop() -> bool:
            return self._emit(
                generation,
                lambda: self._io.event_stop(),  # type: ignore[attr-defined]
                allow_cancelled=True,
            )

        if outcome == "stop":
            emit_stop()
        elif outcome == "pause":
            if not self._emit(
                generation,
                lambda: self._io.event_pause(),  # type: ignore[attr-defined]
            ):
                emit_stop()
        elif not self._emit(
            generation,
            lambda: self._io.event_end(),  # type: ignore[attr-defined]
        ):
            emit_stop()

    def _should_abort(self, generation: _GenerationToken) -> bool:
        with self._lock:
            return (
                generation is not self._generation
                or generation.cancelled.is_set()
            )

    def _register_request(self, generation: _GenerationToken) -> str:
        """Create and register a request id owned by ``generation``.

        Registration happens in the worker thread ahead of the first blocking
        call, so a STOP arriving right after handle_speak always finds the
        request already cancellable.
        """
        request_id = new_request_id()
        generation.add_request(request_id)
        return request_id

    def _fetch(
        self,
        generation: _GenerationToken,
        text: str,
        voice_name: str,
        rate: str,
        pitch: str,
        request_id: str,
    ) -> bytes:
        generation.add_request(request_id)
        try:
            return self._client.synthesize(  # type: ignore[attr-defined]
                text,
                voice_name,
                rate,
                pitch,
                request_id,
                should_abort=lambda: self._should_abort(generation),
            )
        except Cancelled:
            raise
        except SynthError:
            if self._should_abort(generation):
                raise Cancelled("aborted instead of recovering backend")
            self.catalog = None
            self._controller.ensure_ready()  # type: ignore[attr-defined]
            self._refresh_catalog()
            if self._should_abort(generation):
                raise Cancelled("aborted before retrying synthesis")
            # A fresh id: the first POST's delivery is ambiguous, so reusing the
            # id could put two live requests under one name.
            retry_id = self._register_request(generation)
            try:
                return self._client.synthesize(  # type: ignore[attr-defined]
                    text,
                    voice_name,
                    rate,
                    pitch,
                    retry_id,
                    should_abort=lambda: self._should_abort(generation),
                )
            finally:
                generation.discard_request(retry_id)
        finally:
            generation.discard_request(request_id)

    def _cancel_outstanding(self, generation: _GenerationToken) -> None:
        self._cancel_requests(generation, generation.take_requests())

    def _cancel_requests(
        self, generation: _GenerationToken, request_ids: list[str]
    ) -> None:
        for request_id in request_ids:
            self._client.cancel(  # type: ignore[attr-defined]
                request_id,
                still_wanted=lambda: generation.cancelled.is_set(),
            )


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
