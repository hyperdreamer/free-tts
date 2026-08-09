"""The Speech Dispatcher output-module wire protocol.

Every response string and byte sequence here mirrors the reference C
implementation in speechd's ``module_process.c``. Nothing but protocol traffic
may be written to the stream, and writes are serialised because events are
emitted from a worker thread while the main loop is answering commands.
"""

from __future__ import annotations

import logging
import threading
from typing import BinaryIO

from desktop.audio import BITS, CHANNELS, native_big_endian

logger = logging.getLogger("free-tts.protocol")

OK_LOADED = "299 OK LOADED SUCCESSFULLY"
ERR_CANT_INIT = "399 ERR CANT INIT MODULE"
OK_RECEIVING_MESSAGE = "202 OK RECEIVING MESSAGE"
OK_SPEAKING = "200 OK SPEAKING"
ERR_CANT_SPEAK = "301 ERROR CANT SPEAK"
OK_RECEIVING_SETTINGS = "203 OK RECEIVING SETTINGS"
OK_SETTINGS_RECEIVED = "203 OK SETTINGS RECEIVED"
OK_RECEIVING_AUDIO_SETTINGS = "207 OK RECEIVING AUDIO SETTINGS"
OK_AUDIO_INITIALIZED = "203 OK AUDIO INITIALIZED"
OK_RECEIVING_LOGLEVEL_SETTINGS = "207 OK RECEIVING LOGLEVEL SETTINGS"
OK_LOGLEVEL_SET = "203 OK LOGLEVEL SET"
OK_QUIT = "210 OK QUIT"
ERR_UNKNOWN_COMMAND = "300 ERR UNKNOWN COMMAND"
ERR_BAD_SYNTAX = "302 ERROR BAD SYNTAX"
ERR_BAD_PARAM = "303 ERROR INVALID PARAMETER OR VALUE"
ERR_CANT_LIST_VOICES = "304 CANT LIST VOICES"
OK_VOICE_LIST_SENT = "200 OK VOICE LIST SENT"

MAX_AUDIO_CHUNK_BYTES = 10000
_ESCAPE = 0x7D
_INVERT = 0x20
_FRAME_BYTES = CHANNELS * BITS // 8


def escape_audio(data: bytes) -> bytes:
    """HDLC-escape newline and the escape byte so audio stays line-safe."""
    if not data:
        return b""
    out = bytearray()
    for byte in data:
        if byte in (_ESCAPE, 0x0A):
            out.append(_ESCAPE)
            out.append(byte ^ _INVERT)
        else:
            out.append(byte)
    return bytes(out)


def parse_settings(lines: list[str]) -> dict[str, str]:
    """Parse ``name=value`` settings lines, ignoring malformed ones."""
    settings: dict[str, str] = {}
    for line in lines:
        if "=" not in line:
            logger.debug("Ignoring malformed settings line: %r", line)
            continue
        name, _, value = line.partition("=")
        settings[name.strip()] = value
    return settings


class ProtocolIO:
    """Reads commands and writes replies, events, and audio."""

    def __init__(self, stdin: BinaryIO, stdout: BinaryIO) -> None:
        self._stdin = stdin
        self._stdout = stdout
        self._lock = threading.Lock()

    def read_line(self) -> str | None:
        """Return the next line without its newline, or None at end of input."""
        raw = self._stdin.readline()
        if not raw:
            return None
        return raw.decode("utf-8", "replace").rstrip("\n")

    def read_data_block(self) -> list[str]:
        """Read lines up to the terminating dot, un-stuffing leading dots."""
        lines: list[str] = []
        while True:
            line = self.read_line()
            if line is None or line == ".":
                return lines
            lines.append(line[1:] if line.startswith(".") else line)

    def read_message(self) -> str:
        """Read a dot-terminated message body as a single string."""
        return "\n".join(self.read_data_block())

    def send(self, line: str) -> None:
        """Write one protocol line."""
        with self._lock:
            self._write(line.encode("utf-8") + b"\n")

    def send_multiline(self, detail_lines: list[str], final: str) -> None:
        """Write detail lines followed by their terminating status line."""
        payload = b"".join(line.encode("utf-8") + b"\n" for line in detail_lines)
        with self._lock:
            self._write(payload + final.encode("utf-8") + b"\n")

    def send_voices(self, rows: list[tuple[str, str, str]]) -> None:
        """Write a LIST VOICES reply, or report that listing is impossible."""
        if not rows:
            self.send(ERR_CANT_LIST_VOICES)
            return
        detail = [f"200-{name}\t{language}\t{variant}" for name, language, variant in rows]
        self.send_multiline(detail, OK_VOICE_LIST_SENT)

    def send_audio(self, pcm: bytes, sample_rate: int = 24000) -> None:
        """Send PCM to the server in bounded, escaped frames."""
        if not pcm:
            return
        big_endian = 1 if native_big_endian() else 0
        step = MAX_AUDIO_CHUNK_BYTES - (MAX_AUDIO_CHUNK_BYTES % _FRAME_BYTES)
        for offset in range(0, len(pcm), step):
            frame = pcm[offset : offset + step]
            header = (
                f"705-bits={BITS}\n"
                f"705-num_channels={CHANNELS}\n"
                f"705-sample_rate={sample_rate}\n"
                f"705-num_samples={len(frame) // _FRAME_BYTES}\n"
                f"705-big_endian={big_endian}\n"
                "705-AUDIO"
            ).encode("utf-8")
            with self._lock:
                self._write(
                    header + b"\x00" + escape_audio(frame) + b"\n705 AUDIO\n"
                )

    def event_begin(self) -> None:
        """Announce that audio has started."""
        self.send("701 BEGIN")

    def event_end(self) -> None:
        """Announce normal completion."""
        self.send("702 END")

    def event_stop(self) -> None:
        """Announce that speech was stopped."""
        self.send("703 STOP")

    def event_pause(self) -> None:
        """Announce that speech was paused."""
        self.send("704 PAUSE")

    def index_mark(self, mark: str) -> None:
        """Report that an index mark has been reached."""
        self.send_multiline([f"700-{mark}"], "700 INDEX MARK")

    def _write(self, payload: bytes) -> None:
        self._stdout.write(payload)
        flush = getattr(self._stdout, "flush", None)
        if flush is not None:
            flush()
