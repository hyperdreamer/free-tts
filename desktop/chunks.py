"""Split Speech Dispatcher SSML into synthesis-sized chunks at index marks.

No XML parser is used. Upstream's Python module helper strips SSML with a plain
character scanner, and following that keeps entity expansion impossible while
staying byte-compatible with what the server sends.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MARK_PATTERN = re.compile(r'<mark\s+name="(__spd_[^"]*)"\s*/>')
"""Server-inserted index marks; only these are chunk boundaries."""

_ENTITIES = (
    ("&lt;", "<"),
    ("&gt;", ">"),
    ("&amp;", "&"),
    ("&quot;", '"'),
    ("&apos;", "'"),
)


@dataclass(frozen=True)
class Chunk:
    """One synthesis unit and the index mark that ends it, if any."""

    text: str
    mark: str | None


def strip_ssml(message: str) -> str:
    """Remove markup and decode the five XML entities, like upstream does."""
    out: list[str] = []
    omit = False
    index = 0
    length = len(message)
    while index < length:
        char = message[index]
        if char == "<":
            omit = True
            index += 1
            continue
        if char == ">":
            omit = False
            index += 1
            continue
        if omit:
            index += 1
            continue
        if char == "&":
            for entity, replacement in _ENTITIES:
                if message.startswith(entity, index):
                    out.append(replacement)
                    index += len(entity)
                    break
            else:
                out.append(char)
                index += 1
            continue
        out.append(char)
        index += 1
    return "".join(out)


def _hard_split(text: str, max_chars: int) -> list[str]:
    """Break over-long text at whitespace, falling back to a hard cut."""
    pieces: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        cut = window.rfind(" ")
        if cut <= 0:
            pieces.append(remaining[:max_chars])
            remaining = remaining[max_chars:]
            continue
        pieces.append(remaining[:cut])
        remaining = remaining[cut + 1 :]
    if remaining:
        pieces.append(remaining)
    return pieces


def split_marked(ssml: str, max_chars: int = 400) -> list[Chunk]:
    """Split ``ssml`` into chunks, each ending at its index mark when present.

    A mark belongs to the chunk it terminates, because the server inserts marks
    after sentence-ending punctuation. Reporting a chunk's mark therefore means
    "everything up to here has been spoken".
    """
    chunks: list[Chunk] = []
    cursor = 0
    for match in MARK_PATTERN.finditer(ssml):
        segment = strip_ssml(ssml[cursor : match.start()]).strip()
        cursor = match.end()
        if not segment:
            continue
        pieces = _hard_split(segment, max_chars)
        for piece in pieces[:-1]:
            chunks.append(Chunk(piece, None))
        chunks.append(Chunk(pieces[-1], match.group(1)))
    tail = strip_ssml(ssml[cursor:]).strip()
    if tail:
        for piece in _hard_split(tail, max_chars):
            chunks.append(Chunk(piece, None))
    return chunks
