"""Adapter configuration and Speech Dispatcher parameter mapping.

Configuration precedence is env var > config file > built-in default, matching
server.py. This module is pure: no I/O beyond reading the config file, and no
dependency on the rest of the package.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
from collections.abc import Mapping
from dataclasses import dataclass, replace

logger = logging.getLogger("free-tts.settings")


@dataclass(frozen=True)
class AdapterConfig:
    """Resolved adapter settings."""

    backend_url: str
    autostart: bool
    idle_timeout: int
    startup_timeout: int
    request_timeout: int
    max_chunk_chars: int
    ffmpeg_path: str


DEFAULTS = AdapterConfig(
    backend_url="http://127.0.0.1:5000",
    autostart=True,
    idle_timeout=300,
    startup_timeout=30,
    request_timeout=120,
    max_chunk_chars=400,
    ffmpeg_path="ffmpeg",
)

_INT_FIELDS = ("idle_timeout", "startup_timeout", "request_timeout", "max_chunk_chars")
_STR_FIELDS = ("backend_url", "ffmpeg_path")


def config_path() -> pathlib.Path:
    """Return the adapter's own config path, honouring XDG_CONFIG_HOME."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return base / "free-tts" / "config.json"


def _as_bool(raw: object, default: bool) -> bool:
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        lowered = raw.strip().lower()
        if lowered in {"1", "true", "yes", "on"}:
            return True
        if lowered in {"0", "false", "no", "off"}:
            return False
    logger.warning("Invalid boolean %r; using %r.", raw, default)
    return default


def _as_int(raw: object, default: int, *, minimum: int) -> int:
    try:
        value = int(raw)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        logger.warning("Invalid integer %r; using %d.", raw, default)
        return default
    if minimum == 0:
        return max(0, value)
    if value < minimum:
        logger.warning(
            "Integer %r is below the minimum %d; using %d.", raw, minimum, default
        )
        return default
    return value


def load_config(
    path: pathlib.Path | None = None,
    env: Mapping[str, str] | None = None,
) -> AdapterConfig:
    """Resolve settings from env vars, then the config file, then defaults."""
    env = os.environ if env is None else env
    path = config_path() if path is None else path

    raw: dict[str, object] = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                raw = loaded
            else:
                logger.warning("%s is not a JSON object; ignoring.", path)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to read %s: %s", path, exc)

    updates: dict[str, object] = {}
    for field in _STR_FIELDS:
        value = env.get(f"FREE_TTS_{field.upper()}", raw.get(field))
        if value is not None:
            updates[field] = str(value).strip().rstrip("/")
    for field in _INT_FIELDS:
        value = env.get(f"FREE_TTS_{field.upper()}", raw.get(field))
        if value is not None:
            minimum = 0 if field == "idle_timeout" else 1
            updates[field] = _as_int(
                value, getattr(DEFAULTS, field), minimum=minimum
            )
    autostart = env.get("FREE_TTS_AUTOSTART", raw.get("autostart"))
    if autostart is not None:
        updates["autostart"] = _as_bool(autostart, DEFAULTS.autostart)

    return replace(DEFAULTS, **updates)  # type: ignore[arg-type]


def _clamp(value: int) -> int:
    return max(-100, min(100, value))


def map_rate(rate: int) -> str:
    """Map Speech Dispatcher rate (-100..100) to an edge-tts rate string.

    Negative maps to -50%..0% and positive to 0%..+200%, matching the range the
    web frontend already exposes. Monotonic across the whole domain.
    """
    value = _clamp(rate)
    percent = round(value * 0.5) if value < 0 else round(value * 2)
    return f"+{percent}%" if percent >= 0 else f"{percent}%"


def map_pitch(pitch: int) -> str:
    """Map Speech Dispatcher pitch (-100..100) to an edge-tts Hz offset."""
    hz = round(_clamp(pitch) * 0.5)
    return f"+{hz}Hz" if hz >= 0 else f"{hz}Hz"


def map_volume(volume: int) -> float:
    """Map Speech Dispatcher volume (-100..100) to a 0.0..1.0 PCM gain."""
    return (_clamp(volume) + 100) / 200.0
