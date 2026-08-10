"""
edge-tts SSML Server
====================

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the LICENSE file at the repository root for the full license text.

A production-ready Flask server that accepts SSML input, extracts voice/rate/pitch
parameters, generates speech via Microsoft Edge TTS, and returns the audio as MP3.

Architecture:
    - Configuration via environment variables with sensible defaults.
    - Structured logging with request IDs and duration tracking.
    - Type-annotated helpers for SSML parsing and parameter transformation.
    - Flask application factory pattern for testability.
    - SSML size limit to prevent DoS via oversized payloads.
    - TTS generation timeout to prevent hung connections.
    - Graceful shutdown on SIGTERM/SIGINT.
    - Production WSGI: single-process Waitress.
    - CORS enabled for cross-origin frontend requests.
    - Health-check endpoint with voice-cache readiness indicator.
    - Error messages sanitised in production mode.

Usage:
    python server.py                      # production (Waitress, port 5000)
    FLASK_DEBUG=1 python server.py        # development (Flask built-in)
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import sys
import threading
import time
import defusedxml.ElementTree as ET  # type: ignore[import-untyped]
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any, Optional

import edge_tts
from flask import Flask, Response, g, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
PRODUCTION = not os.environ.get("FLASK_DEBUG")

logging.basicConfig(
    level=logging.INFO if PRODUCTION else logging.DEBUG,
    format=(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
        if PRODUCTION
        else "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    ),
)
logger = logging.getLogger("tts-server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
_CONFIG_PATH = Path(os.environ.get("TTS_CONFIG", Path(__file__).parent / "config.json"))


def _load_config() -> dict[str, Any]:
    """Load configuration from config.json, if it exists.

    Environment variables take precedence over the config file.
    Keys in config.json use snake_case and are mapped to TTS_* env vars.
    """
    cfg: dict[str, Any] = {}
    if _CONFIG_PATH.is_file():
        try:
            cfg = json.loads(_CONFIG_PATH.read_text())
            logger.info("Loaded config from %s", _CONFIG_PATH)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Failed to load %s: %s", _CONFIG_PATH, exc)
    return cfg


def _cfg(key: str, env_var: str, default: Any, coerce: type = str) -> Any:
    """Resolve a config value: env var > config.json > hardcoded default."""
    env_val = os.environ.get(env_var)
    raw_val = env_val if env_val is not None else _CONFIG_CACHE.get(key)
    if raw_val is not None:
        try:
            return coerce(raw_val) if coerce is not str else str(raw_val)
        except (TypeError, ValueError) as exc:
            logger.warning(
                "Invalid value for %s/%s (%r): %s. Using default %r.",
                env_var,
                key,
                raw_val,
                exc,
                default,
            )
            return default
    return default


def _cfg_int(
    key: str,
    env_var: str,
    default: int,
    *,
    minimum: int | None = None,
    maximum: int | None = None,
) -> int:
    """Resolve and clamp an integer config value."""
    value = int(_cfg(key, env_var, default, coerce=int))
    if minimum is not None and value < minimum:
        logger.warning(
            "%s/%s below minimum %d; using %d.", env_var, key, minimum, default
        )
        return default
    if maximum is not None and value > maximum:
        logger.warning(
            "%s/%s above maximum %d; using %d.", env_var, key, maximum, default
        )
        return default
    return value


def _cfg_list(key: str, env_var: str, default: list[str]) -> list[str]:
    """Resolve a string-list config value from JSON array or comma-separated env."""
    env_val = os.environ.get(env_var)
    raw_val = env_val if env_val is not None else _CONFIG_CACHE.get(key)
    if raw_val is None:
        return default
    if isinstance(raw_val, str):
        values = [item.strip() for item in raw_val.split(",") if item.strip()]
    elif isinstance(raw_val, list):
        values = [str(item).strip() for item in raw_val if str(item).strip()]
    else:
        logger.warning(
            "Invalid value for %s/%s (%r). Using default.", env_var, key, raw_val
        )
        return default
    return values or default


_CONFIG_CACHE = _load_config()

DEFAULT_VOICE: str = _cfg(
    "default_voice", "TTS_DEFAULT_VOICE", "en-US-AvaMultilingualNeural"
)
"""Default voice name used when the SSML omits a <voice> element."""

DEFAULT_RATE: str = _cfg("default_rate", "TTS_DEFAULT_RATE", "+0%")
"""Default speaking rate (edge-tts format) when <prosody rate> is missing."""

DEFAULT_PITCH: str = _cfg("default_pitch", "TTS_DEFAULT_PITCH", "+0Hz")
"""Default pitch (edge-tts format) when <prosody pitch> is missing."""

SERVER_HOST: str = _cfg("host", "TTS_HOST", "127.0.0.1")
SERVER_PORT: int = _cfg_int("port", "TTS_PORT", 5000, minimum=1, maximum=65535)

MAX_SSML_LENGTH: int = _cfg_int(
    "max_ssml_length", "TTS_MAX_SSML_LENGTH", 200_000, minimum=0
)
"""Maximum SSML payload size in bytes. 0 = unlimited."""

TTS_STALL_TIMEOUT: int = _cfg_int(
    "tts_stall_timeout", "TTS_STALL_TIMEOUT", 60, minimum=0
)
"""Seconds of silence (no data from edge-tts) before aborting. 0 = disable."""

TTS_MAX_CONCURRENT: int = _cfg_int("max_concurrent", "TTS_MAX_CONCURRENT", 2, minimum=0)
"""Maximum concurrent TTS generation requests. 0 = unlimited."""

TTS_QUEUE_TIMEOUT: int = _cfg_int("queue_timeout", "TTS_QUEUE_TIMEOUT", 30, minimum=5)
"""Seconds to wait for a TTS slot before returning 503. Minimum 5s."""

_TTS_SEMAPHORE: threading.BoundedSemaphore | None = (
    threading.BoundedSemaphore(TTS_MAX_CONCURRENT) if TTS_MAX_CONCURRENT > 0 else None
)
"""Process-wide thread-safe limiter for concurrent TTS generation."""

TTS_IDLE_TIMEOUT: int = _cfg_int("idle_timeout", "TTS_IDLE_TIMEOUT", 0, minimum=0)
"""Seconds of inactivity before self-shutdown. 0 = never (the default).

Only the desktop adapter sets this, for a backend it started itself. A backend
started by hand keeps running.
"""

WAITRESS_THREADS: int = _cfg_int(
    "waitress_threads", "TTS_WAITRESS_THREADS", 4, minimum=1
)
"""Number of Waitress worker threads."""

WSGI_SERVER: str = _cfg("wsgi_server", "TTS_SERVER", "waitress").lower()
"""Only the single-process Waitress server is supported."""


def _validate_process_model(server_name: str) -> None:
    """Reject process-local coordination behind a multi-process server."""
    if server_name != "waitress":
        raise RuntimeError(
            "The cancellation and idle-lifecycle contract requires a "
            "single-process server; use TTS_SERVER=waitress."
        )


CORS_ORIGINS: list[str] = _cfg_list(
    "cors_origins",
    "TTS_CORS_ORIGINS",
    [
        "null",
        r"^https?://localhost(?::\d+)?$",
        r"^https?://127\.0\.0\.1(?::\d+)?$",
    ],
)
"""Allowed browser origins. Defaults to local files and loopback web pages."""

SSML_NAMESPACE: str = "http://www.w3.org/2001/10/synthesis"
"""XML namespace URI for the SSML <speak> element."""

SERVICE_NAME: str = "free-tts"
"""Stable service identity so clients can detect a port conflict."""

API_VERSION: int = 1
"""Incremented only on a breaking change to the adapter-facing HTTP contract."""


class CancelledError(Exception):
    """Raised inside generation when its cancellation token is set."""


_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
"""Adapter-supplied request ids: opaque, bounded, log- and route-safe."""

_CANCEL_LOCK = threading.Lock()


class _CancellationToken:
    """Thread-safe cancellation state that can also be awaited by asyncio."""

    def __init__(self) -> None:
        self._event = threading.Event()
        self._lock = threading.Lock()
        self._waiters: set[
            tuple[asyncio.AbstractEventLoop, asyncio.Future[None]]
        ] = set()

    def is_set(self) -> bool:
        return self._event.is_set()

    def set(self) -> None:
        with self._lock:
            if self._event.is_set():
                return
            self._event.set()
            waiters = tuple(self._waiters)
        for loop, future in waiters:
            try:
                loop.call_soon_threadsafe(self._resolve, future)
            except RuntimeError:
                pass

    async def wait(self) -> None:
        if self._event.is_set():
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        waiter = (loop, future)
        with self._lock:
            if self._event.is_set():
                return
            self._waiters.add(waiter)
        try:
            await future
        finally:
            with self._lock:
                self._waiters.discard(waiter)

    @staticmethod
    def _resolve(future: asyncio.Future[None]) -> None:
        if not future.done():
            future.set_result(None)


_CANCEL_REGISTRY: dict[str, _CancellationToken] = {}


class DuplicateRequestId(Exception):
    """A caller reused a request id that is still live."""


def _register_cancel_token(request_id: str) -> _CancellationToken:
    """Claim ``request_id`` for one live request.

    Ownership is exclusive: a second live registration would make the first
    request uncancellable and let either completion delete the other's token.
    """
    event = _CancellationToken()
    with _CANCEL_LOCK:
        if request_id in _CANCEL_REGISTRY:
            raise DuplicateRequestId(request_id)
        _CANCEL_REGISTRY[request_id] = event
    return event


def _release_cancel_token(request_id: str, token: _CancellationToken) -> None:
    """Drop ``request_id`` only if ``token`` still owns it.

    Releasing by id alone would let a slow request's cleanup delete the token of
    a newer request that reused the id.
    """
    with _CANCEL_LOCK:
        if _CANCEL_REGISTRY.get(request_id) is token:
            del _CANCEL_REGISTRY[request_id]


def _cancel_request(request_id: str) -> bool:
    """Set the token for ``request_id``. Return False if it is not live."""
    with _CANCEL_LOCK:
        event = _CANCEL_REGISTRY.get(request_id)
    if event is None:
        return False
    event.set()
    return True


# ---------------------------------------------------------------------------
# Voice cache (populated at startup from edge-tts)
# ---------------------------------------------------------------------------
_voice_cache: list[dict[str, Any]] = []
"""List of voice dicts from edge-tts.list_voices(), cached at startup."""

_voice_cache_ready: bool = False
"""True once the voice cache has been successfully populated."""

_LANGUAGE_LIST: list[dict[str, str]] = []
"""Unique languages for the frontend dropdown, sorted by display name."""

_cache_lock = threading.Lock()
"""Serialises voice-cache mutations and reads."""

# ---------------------------------------------------------------------------
# Locale → display name mapping (ISO 639-1 language + ISO 3166-1 region)
# ---------------------------------------------------------------------------
_LANG_NAMES: dict[str, str] = {
    "af": "Afrikaans",
    "sq": "Albanian",
    "am": "Amharic",
    "ar": "Arabic",
    "hy": "Armenian",
    "az": "Azerbaijani",
    "bn": "Bangla",
    "eu": "Basque",
    "bs": "Bosnian",
    "bg": "Bulgarian",
    "my": "Burmese",
    "ca": "Catalan",
    "yue": "Cantonese",
    "zh": "Chinese",
    "hr": "Croatian",
    "cs": "Czech",
    "da": "Danish",
    "nl": "Dutch",
    "en": "English",
    "et": "Estonian",
    "fil": "Filipino",
    "fi": "Finnish",
    "fr": "French",
    "gl": "Galician",
    "ka": "Georgian",
    "de": "German",
    "el": "Greek",
    "gu": "Gujarati",
    "he": "Hebrew",
    "hi": "Hindi",
    "hu": "Hungarian",
    "is": "Icelandic",
    "id": "Indonesian",
    "ga": "Irish",
    "it": "Italian",
    "ja": "Japanese",
    "jv": "Javanese",
    "kn": "Kannada",
    "kk": "Kazakh",
    "km": "Khmer",
    "ko": "Korean",
    "lo": "Lao",
    "lv": "Latvian",
    "lt": "Lithuanian",
    "mk": "Macedonian",
    "ms": "Malay",
    "ml": "Malayalam",
    "mt": "Maltese",
    "mr": "Marathi",
    "mn": "Mongolian",
    "ne": "Nepali",
    "nb": "Norwegian",
    "ps": "Pashto",
    "fa": "Persian",
    "pl": "Polish",
    "pt": "Portuguese",
    "pa": "Punjabi",
    "ro": "Romanian",
    "ru": "Russian",
    "sr": "Serbian",
    "si": "Sinhala",
    "sk": "Slovak",
    "sl": "Slovenian",
    "so": "Somali",
    "es": "Spanish",
    "su": "Sundanese",
    "sw": "Swahili",
    "sv": "Swedish",
    "ta": "Tamil",
    "te": "Telugu",
    "th": "Thai",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "cy": "Welsh",
    "zu": "Zulu",
}

_REGION_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates",
    "AR": "Argentina",
    "AT": "Austria",
    "AU": "Australia",
    "BD": "Bangladesh",
    "BE": "Belgium",
    "BG": "Bulgaria",
    "BH": "Bahrain",
    "BO": "Bolivia",
    "BR": "Brazil",
    "CA": "Canada",
    "CH": "Switzerland",
    "CL": "Chile",
    "CN": "China",
    "CO": "Colombia",
    "CR": "Costa Rica",
    "CU": "Cuba",
    "CY": "Cyprus",
    "CZ": "Czechia",
    "DE": "Germany",
    "DK": "Denmark",
    "DO": "Dominican Republic",
    "DZ": "Algeria",
    "EC": "Ecuador",
    "EE": "Estonia",
    "EG": "Egypt",
    "ES": "Spain",
    "ET": "Ethiopia",
    "FI": "Finland",
    "FR": "France",
    "GB": "United Kingdom",
    "GH": "Ghana",
    "GR": "Greece",
    "GT": "Guatemala",
    "GQ": "Equatorial Guinea",
    "HK": "Hong Kong",
    "HN": "Honduras",
    "HR": "Croatia",
    "HU": "Hungary",
    "ID": "Indonesia",
    "IE": "Ireland",
    "IL": "Israel",
    "IN": "India",
    "IQ": "Iraq",
    "IS": "Iceland",
    "IT": "Italy",
    "JM": "Jamaica",
    "JO": "Jordan",
    "JP": "Japan",
    "KE": "Kenya",
    "KH": "Cambodia",
    "KR": "South Korea",
    "KW": "Kuwait",
    "LB": "Lebanon",
    "LK": "Sri Lanka",
    "LT": "Lithuania",
    "LV": "Latvia",
    "LY": "Libya",
    "MA": "Morocco",
    "MK": "North Macedonia",
    "MN": "Mongolia",
    "MT": "Malta",
    "MX": "Mexico",
    "MY": "Malaysia",
    "NG": "Nigeria",
    "NI": "Nicaragua",
    "NL": "Netherlands",
    "NO": "Norway",
    "NP": "Nepal",
    "NZ": "New Zealand",
    "OM": "Oman",
    "PA": "Panama",
    "PE": "Peru",
    "PH": "Philippines",
    "PK": "Pakistan",
    "PL": "Poland",
    "PR": "Puerto Rico",
    "PT": "Portugal",
    "PY": "Paraguay",
    "QA": "Qatar",
    "RO": "Romania",
    "RS": "Serbia",
    "RU": "Russia",
    "SA": "Saudi Arabia",
    "SE": "Sweden",
    "SG": "Singapore",
    "SI": "Slovenia",
    "SK": "Slovakia",
    "SN": "Senegal",
    "SV": "El Salvador",
    "SY": "Syria",
    "TH": "Thailand",
    "TN": "Tunisia",
    "TR": "Turkey",
    "TW": "Taiwan",
    "TZ": "Tanzania",
    "UA": "Ukraine",
    "UG": "Uganda",
    "US": "United States",
    "UY": "Uruguay",
    "VE": "Venezuela",
    "VN": "Vietnam",
    "YE": "Yemen",
    "ZA": "South Africa",
    "ZW": "Zimbabwe",
}


def _locale_display_name(locale: str) -> str:
    """Build a human-readable display name from a locale code like 'es-ES'.

    Uses ISO 639-1 language names and ISO 3166-1 region names.
    Falls back to the raw locale string if either part is unknown.
    """
    if "-" in locale:
        lang_code, region = locale.split("-", 1)
        lang = _LANG_NAMES.get(lang_code, lang_code)
        region_name = _REGION_NAMES.get(region, region)
        return f"{lang} ({region_name})"
    return _LANG_NAMES.get(locale, locale)


def _local_name(element: ET.Element) -> str:
    """Return the local tag name without an XML namespace."""
    return element.tag.rsplit("}", 1)[-1] if "}" in element.tag else element.tag


def _find_first(element: ET.Element, tag_name: str) -> ET.Element | None:
    """Find the first descendant matching a local tag name."""
    for child in element.iter():
        if _local_name(child) == tag_name:
            return child
    return None


def _is_known_voice(voice: str) -> bool:
    """Return whether a voice exists in the cache, if the cache is available."""
    if not _voice_cache_ready:
        return True
    # Hold the lock so the cache isn't mutated mid-iteration.
    with _cache_lock:
        return any(item.get("ShortName") == voice for item in _voice_cache)


async def _refresh_voice_cache() -> None:
    """Fetch all available voices from edge-tts and rebuild the caches."""
    global _voice_cache, _voice_cache_ready, _LANGUAGE_LIST

    try:
        raw = await edge_tts.list_voices()
    except Exception as exc:
        logger.error("Failed to refresh voice cache: %s", exc)
        return

    new_voices: list[dict[str, Any]] = []
    seen_locales: dict[str, str] = {}

    for v in raw:
        locale = v.get("Locale", "")
        short = v.get("ShortName", "")
        gender = v.get("Gender", "")

        lang_name = _locale_display_name(locale)
        if locale not in seen_locales:
            seen_locales[locale] = lang_name

        new_voices.append(
            {
                "ShortName": short,
                "Gender": gender,
                "Locale": locale,
                "LanguageName": lang_name,
            }
        )

    # Build the language list locally (no I/O).
    new_language_list = sorted(
        [{"locale": loc, "name": name} for loc, name in seen_locales.items()],
        key=lambda x: x["name"].lower(),
    )

    # Atomically swap all globals under the write-side lock.
    with _cache_lock:
        _voice_cache[:] = new_voices
        _LANGUAGE_LIST[:] = new_language_list
        _voice_cache_ready = True

    logger.info(
        "Voice cache refreshed: %d voices across %d languages.",
        len(_voice_cache),
        len(_LANGUAGE_LIST),
    )


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------
@dataclass
class TTSRequest:
    """Normalised TTS parameters extracted from the incoming SSML."""

    voice: str
    rate: str
    pitch: str
    text: str


# ---------------------------------------------------------------------------
# SSML Parsing helpers
# ---------------------------------------------------------------------------
def _parse_rate(raw: Optional[str]) -> str:
    """Transform an SSML ``rate`` attribute into an edge-tts-compatible value.

    Rules (applied in order):
        - ``None`` or empty  →  ``DEFAULT_RATE``
        - ``"+N%"`` or ``"-N%"`` (signed relative) → passed through as-is
        - ``"0%"``           →  ``"+0%"``   (safe no-change representation)
        - ``"N%"`` where N < 100  →  ``"-{100-N}%"``   (slow-down relative to 100 %)
        - ``"N%"`` where N ≥ 100  →  ``"+{N-100}%"``   (speed-up relative to 100 %)
        - Everything else (e.g. ``"x-slow"``, ``"+20ms"``) → passed through as-is.
    """
    if not raw:
        return DEFAULT_RATE

    raw = raw.strip()
    # Already a signed relative value (e.g. "+20%", "-10%") — pass through
    if raw.startswith("+") or raw.startswith("-"):
        return raw

    if raw.endswith("%"):
        try:
            val = float(raw[:-1])
        except ValueError:
            return raw  # non-numeric percentage – let edge-tts decide

        ival = round(val)
        if ival == 0:
            return "+0%"
        if ival < 100:
            return f"-{100 - ival}%"
        return f"+{ival - 100}%"

    # Non-percentage values: "x-slow", "fast", "+20ms", ...
    return raw


def _parse_pitch(raw: Optional[str]) -> str:
    """Transform an SSML ``pitch`` attribute into an edge-tts-compatible value.

    Rules:
        - ``None`` or empty  →  ``DEFAULT_PITCH``
        - ``"0%"``           →  ``"+0Hz"``  (edge-tts no-change pitch)
        - Everything else → passed through as-is (``"+20Hz"``, ``"x-low"``, …).
    """
    if not raw:
        return DEFAULT_PITCH

    raw = raw.strip()
    if raw == "0%":
        return "+0Hz"
    return raw


def extract_tts_params(ssml: str) -> TTSRequest:
    """Parse an SSML string and return normalised TTS parameters.

    Args:
        ssml: A well-formed SSML document containing at minimum a ``<speak>``
              root element.

    Returns:
        ``TTSRequest`` with voice, rate, pitch, and the plain-text content
        to synthesise.

    Raises:
        ValueError: If the SSML is malformed or contains no speakable text.
    """
    # Size guard — reject before parsing to prevent XML bomb attacks
    if MAX_SSML_LENGTH > 0:
        ssml_bytes = len(ssml.encode("utf-8"))
        if ssml_bytes > MAX_SSML_LENGTH:
            raise ValueError(
                f"SSML too large ({ssml_bytes} bytes). Maximum is {MAX_SSML_LENGTH} bytes."
            )

    try:
        root = ET.fromstring(ssml)
    except ET.ParseError as exc:
        raise ValueError(f"Malformed SSML: {exc}") from exc

    voice: str = DEFAULT_VOICE
    rate: str = DEFAULT_RATE
    pitch: str = DEFAULT_PITCH
    text: str = ""

    if _local_name(root) != "speak":
        raise ValueError("SSML root element must be <speak>.")

    # --- <voice> -----------------------------------------------------------
    voice_el = _find_first(root, "voice")
    if voice_el is not None:
        voice_name = voice_el.get("name")
        if voice_name:
            voice = voice_name.strip()

        prosody_el = _find_first(voice_el, "prosody")
        if prosody_el is not None:
            rate = _parse_rate(prosody_el.get("rate"))
            pitch = _parse_pitch(prosody_el.get("pitch"))
        # Collect text from ALL children of <voice>, not just <prosody>, so that
        # sibling text outside <prosody> is preserved.
        text = " ".join(t.strip() for t in voice_el.itertext() if t.strip()).strip()
        if prosody_el is None:
            logger.warning("No <prosody> inside <voice>; using default rate/pitch.")
    else:
        text = " ".join(root.itertext()).strip()
        logger.warning(
            "No <voice> element found; using default voice, rate, and pitch."
        )

    if not text:
        raise ValueError("No speakable text found in SSML.")

    logger.debug(
        "Parsed SSML → voice=%r rate=%r pitch=%r text=%r",
        voice,
        rate,
        pitch,
        text[:80],
    )
    return TTSRequest(voice=voice, rate=rate, pitch=pitch, text=text)


# ---------------------------------------------------------------------------
# TTS generation
# ---------------------------------------------------------------------------
async def _wait_for_cancel(
    cancel_event: _CancellationToken | threading.Event,
) -> None:
    if isinstance(cancel_event, _CancellationToken):
        await cancel_event.wait()
        return
    while not cancel_event.is_set():
        await asyncio.sleep(0.01)


async def _close_stream(stream: Any) -> None:
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await stream.aclose()


async def _cancel_stream_read(stream: Any, read_task: asyncio.Task[Any]) -> None:
    read_task.cancel()
    with contextlib.suppress(asyncio.CancelledError, Exception):
        await read_task
    await _close_stream(stream)


async def generate_audio(
    req: TTSRequest,
    cancel_event: _CancellationToken | threading.Event | None = None,
) -> bytes:
    """Synthesise speech, racing every blocked stream read with cancellation."""
    try:
        communicate = edge_tts.Communicate(
            req.text,
            voice=req.voice,
            rate=req.rate,
            pitch=req.pitch,
        )
    except Exception as exc:
        raise RuntimeError(f"Failed to initialise edge-tts: {exc}") from exc

    buf = BytesIO()
    stream = communicate.stream()
    cancel_task = (
        asyncio.create_task(_wait_for_cancel(cancel_event))
        if cancel_event is not None
        else None
    )
    try:
        while True:
            if cancel_event is not None and cancel_event.is_set():
                await _close_stream(stream)
                raise CancelledError("Generation cancelled by client.")

            read_task = asyncio.create_task(stream.__anext__())
            wait_for = {read_task}
            if cancel_task is not None:
                wait_for.add(cancel_task)
            timeout = TTS_STALL_TIMEOUT if TTS_STALL_TIMEOUT > 0 else None
            try:
                done, _pending = await asyncio.wait(
                    wait_for,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
            except BaseException:
                await _cancel_stream_read(stream, read_task)
                raise

            if cancel_task is not None and cancel_task in done:
                await _cancel_stream_read(stream, read_task)
                raise CancelledError("Generation cancelled by client.")
            if read_task not in done:
                await _cancel_stream_read(stream, read_task)
                raise TimeoutError(
                    f"No data from TTS service for {TTS_STALL_TIMEOUT}s "
                    "(stall detected)"
                )

            try:
                chunk = read_task.result()
            except StopAsyncIteration:
                break
            except BaseException:
                await _close_stream(stream)
                raise
            if chunk.get("type") == "audio":
                buf.write(chunk.get("data", b""))
    finally:
        if cancel_task is not None:
            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task

    if buf.tell() == 0:
        raise RuntimeError("edge-tts returned no audio data.")

    return buf.getvalue()


async def _acquire_tts_slot(
    semaphore: threading.BoundedSemaphore,
    cancel_event: _CancellationToken | None,
    timeout: float,
) -> bool:
    """Wait asynchronously for one process-local slot or cancellation."""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while True:
        if cancel_event is not None and cancel_event.is_set():
            raise CancelledError("Request cancelled while waiting for a TTS slot.")
        if semaphore.acquire(blocking=False):
            if cancel_event is not None and cancel_event.is_set():
                semaphore.release()
                raise CancelledError(
                    "Request cancelled while waiting for a TTS slot."
                )
            return True
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        delay = min(0.05, remaining)
        if cancel_event is None:
            await asyncio.sleep(delay)
            continue
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=delay)
        except TimeoutError:
            continue
        raise CancelledError("Request cancelled while waiting for a TTS slot.")


# ---------------------------------------------------------------------------
# Error message sanitisation
# ---------------------------------------------------------------------------
def _error_message(exc: Exception, is_client_error: bool = False) -> str:
    """Return a safe error message for the client.

    Client errors (400-level) always return the real message so users can
    correct their input.  Server errors (500-level) are sanitised in production.
    """
    if is_client_error:
        return str(exc)
    if PRODUCTION:
        return "TTS request failed. Check server logs for details."
    return str(exc)


class IdleShutdownWatchdog:
    """Fires ``on_shutdown`` after ``timeout`` seconds with no synthesis.

    Pure logic: it never reads the wall clock directly and never starts a
    thread, so its behavior is fully testable with an injected clock. Call
    :meth:`poll` from a supervisor loop.
    """

    def __init__(
        self,
        timeout: int,
        on_shutdown: Any,
        clock: Any = time.monotonic,
    ) -> None:
        self._timeout = timeout
        self._on_shutdown = on_shutdown
        self._clock = clock
        self._lock = threading.Lock()
        self._active = 0
        self._last_activity = clock()
        self.fired = False

    def begin_request(self) -> None:
        """Mark a synthesis request as started."""
        with self._lock:
            self._active += 1

    def end_request(self) -> None:
        """Mark a synthesis request as finished and restart the idle window."""
        with self._lock:
            if self._active > 0:
                self._active -= 1
            self._last_activity = self._clock()

    def poll(self) -> bool:
        """Return True exactly once, when the idle window has elapsed."""
        with self._lock:
            if self.fired or self._timeout <= 0 or self._active > 0:
                return False
            if self._clock() - self._last_activity <= self._timeout:
                return False
            self.fired = True
        logger.info("Idle for %ds with no synthesis; shutting down.", self._timeout)
        self._on_shutdown()
        return True


_IDLE_WATCHDOG: IdleShutdownWatchdog | None = None
"""Set by create_app() when idle shutdown is enabled."""


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # Don't serve static files or templates — API only
    app.config["PROPAGATE_EXCEPTIONS"] = True  # let WSGI server handle errors
    # Reject oversized request bodies before JSON parsing
    app.config["MAX_CONTENT_LENGTH"] = (
        max(MAX_SSML_LENGTH * 2, 64 * 1024) if MAX_SSML_LENGTH > 0 else None
    )

    cors_origins: list[str | re.Pattern[str]] = []
    for origin in CORS_ORIGINS:
        if origin.startswith("^"):
            cors_origins.append(re.compile(origin))
        else:
            cors_origins.append(origin)
    CORS(
        app,
        resources={r"/*": {"origins": cors_origins}},
        methods=["GET", "POST", "OPTIONS", "DELETE"],
        allow_headers=["Content-Type"],
        max_age=3600,
    )

    # Custom 500 handler — ensures JSON error contract even when Flask's
    # internal error handling is bypassed (PROPAGATE_EXCEPTIONS = True).
    @app.errorhandler(500)
    def _handle_500(error):
        return jsonify({"error": "Internal server error"}), 500

    # Populate voice cache on startup (works with any WSGI entrypoint)
    if not _voice_cache_ready:
        try:
            asyncio.run(_refresh_voice_cache())
        except Exception as exc:
            logger.error("Failed to initialise voice cache: %s", exc)

    # Arm idle shutdown only when explicitly enabled (adapter-started backends)
    global _IDLE_WATCHDOG
    if _IDLE_WATCHDOG is None and TTS_IDLE_TIMEOUT > 0:
        _IDLE_WATCHDOG = IdleShutdownWatchdog(
            timeout=TTS_IDLE_TIMEOUT,
            on_shutdown=lambda: os.kill(os.getpid(), signal.SIGTERM),
        )
        watchdog = _IDLE_WATCHDOG

        def _idle_supervisor() -> None:
            while not watchdog.poll():
                time.sleep(1.0)

        threading.Thread(
            target=_idle_supervisor, name="idle-shutdown", daemon=True
        ).start()
        logger.info("Idle shutdown armed: %ds.", TTS_IDLE_TIMEOUT)

    # -- Process contract ----------------------------------------------------
    @app.before_request
    def _enforce_process_contract():
        software = request.environ.get("SERVER_SOFTWARE", "").lower()
        if "gunicorn" in software:
            return (
                jsonify(
                    {
                        "error": (
                            "Gunicorn is unsupported: cancellation and idle "
                            "lifecycle require a single-process Waitress server."
                        )
                    }
                ),
                503,
            )
        return None

    # -- Request logging -----------------------------------------------------
    @app.before_request
    def _before_request() -> None:
        g.start_time = time.monotonic()
        g.request_id = os.urandom(4).hex()

    @app.after_request
    def _after_request(response: Response) -> Response:
        elapsed = time.monotonic() - g.get("start_time", time.monotonic())
        logger.info(
            "[%s] %s %s → %d (%.3fs)",
            g.get("request_id", "????"),
            request.method,
            request.path,
            response.status_code,
            elapsed,
        )
        return response

    # -- Health check --------------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health() -> Response:
        return jsonify(
            {
                "status": "ok",
                "service": SERVICE_NAME,
                "api_version": API_VERSION,
                "voice_cache_ready": _voice_cache_ready,
            }
        )

    @app.route("/tts-request/<request_id>", methods=["DELETE"])
    def cancel_tts_request(request_id: str) -> Response:
        """Cancel an in-flight generation so its concurrency slot is released."""
        if not _REQUEST_ID_RE.match(request_id):
            return jsonify({"error": "Unknown request id."}), 404  # type: ignore[return-value]
        if _cancel_request(request_id):
            return jsonify({"cancelled": True})
        return jsonify({"error": "Unknown request id."}), 404  # type: ignore[return-value]

    @app.errorhandler(413)
    def request_entity_too_large(exc: Exception) -> Response:
        return jsonify({"error": "Request body is too large."}), 413  # type: ignore[return-value]

    # -- Voices endpoint -----------------------------------------------------
    @app.route("/voices", methods=["GET"])
    def list_voices() -> Response:
        """Return all available edge-tts voices, languages, and server defaults.

        Response:
            {
                "languages": [{"locale": "en-US", "name": "English (United States)"}, ...],
                "voices": [{"ShortName": ..., "Gender": ..., "Locale": ..., ...}, ...],
                "default_voice": "en-US-EmmaMultilingualNeural"
            }
        """
        # Snapshot the cache under the lock so a concurrent refresh can't mutate
        # the lists mid-serialisation.
        with _cache_lock:
            languages_snapshot = list(_LANGUAGE_LIST)
            voices_snapshot = list(_voice_cache)
        return jsonify(
            {
                "languages": languages_snapshot,
                "voices": voices_snapshot,
                "default_voice": DEFAULT_VOICE,
            }
        )

    # -- TTS endpoint --------------------------------------------------------
    @app.route("/generate-and-download-tts", methods=["POST"])
    async def generate_and_download_tts() -> Response:
        """Accept SSML, produce MP3.

        Expects JSON: ``{"ssml": "<speak>...</speak>"}``.
        Returns the MP3 file as an attachment on success, or a JSON error body.
        """
        body = request.get_json(silent=True)
        if not body or not isinstance(body, dict) or "ssml" not in body:
            logger.warning("Request missing 'ssml' field.")
            return jsonify({"error": "Missing 'ssml' field in JSON body."}), 400  # type: ignore[return-value]

        ssml = body["ssml"]
        if not isinstance(ssml, str):
            return jsonify({"error": "'ssml' must be a string."}), 400  # type: ignore[return-value]
        if not ssml.strip():
            return jsonify({"error": "Empty SSML string."}), 400  # type: ignore[return-value]

        request_id = body.get("request_id")
        if request_id is not None:
            if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
                return jsonify({"error": "Invalid 'request_id'."}), 400  # type: ignore[return-value]

        # 1. Parse
        try:
            tts_req = extract_tts_params(ssml)
        except ValueError as exc:
            logger.warning("SSML parse error: %s", exc)
            return jsonify({"error": _error_message(exc, is_client_error=True)}), 400  # type: ignore[return-value]
        if not _is_known_voice(tts_req.voice):
            logger.warning("Unknown voice requested: %s", tts_req.voice)
            return jsonify({"error": f"Unknown voice: {tts_req.voice}"}), 400  # type: ignore[return-value]

        # 2. Synthesise (stall timeout handled inside generate_audio)
        cancel_event = None
        if request_id is not None:
            try:
                cancel_event = _register_cancel_token(request_id)
            except DuplicateRequestId:
                logger.warning("Refusing duplicate live request_id %r", request_id)
                return jsonify({"error": "request_id is already in flight."}), 409  # type: ignore[return-value]
        semaphore = _TTS_SEMAPHORE
        slot_acquired = False
        if _IDLE_WATCHDOG is not None:
            _IDLE_WATCHDOG.begin_request()
        try:
            if semaphore is not None:
                slot_acquired = await _acquire_tts_slot(
                    semaphore, cancel_event, TTS_QUEUE_TIMEOUT
                )
                if not slot_acquired:
                    resp = jsonify({"error": "Server busy, try again later."})
                    resp.headers["Retry-After"] = str(TTS_QUEUE_TIMEOUT)
                    return resp, 503  # type: ignore[return-value]
            audio = await generate_audio(tts_req, cancel_event=cancel_event)
        except CancelledError:
            logger.info("TTS request cancelled by client.")
            return jsonify({"error": "Request cancelled."}), 499  # type: ignore[return-value]
        except TimeoutError as exc:
            logger.error("TTS stall detected after %ds", TTS_STALL_TIMEOUT)
            return jsonify({"error": _error_message(exc)}), 504  # type: ignore[return-value]
        except RuntimeError as exc:
            logger.error("TTS generation failed: %s", exc)
            return jsonify({"error": _error_message(exc)}), 500  # type: ignore[return-value]
        except Exception as exc:
            logger.exception("Unexpected TTS error")
            return jsonify({"error": _error_message(exc)}), 500  # type: ignore[return-value]
        finally:
            if slot_acquired and semaphore is not None:
                semaphore.release()
            if _IDLE_WATCHDOG is not None:
                _IDLE_WATCHDOG.end_request()
            if request_id is not None and cancel_event is not None:
                _release_cancel_token(request_id, cancel_event)

        # 3. Respond
        return Response(
            audio,
            mimetype="audio/mpeg",
            headers={"Content-Disposition": 'attachment; filename="tts-output.mp3"'},
        )

    return app


# ---------------------------------------------------------------------------
# Signal handling
# ---------------------------------------------------------------------------
def _handle_shutdown(signum: int, frame: Any) -> None:
    """Log the signal and exit cleanly."""
    sig_name = signal.Signals(signum).name
    logger.info("Received %s — shutting down.", sig_name)
    sys.exit(0)


signal.signal(signal.SIGTERM, _handle_shutdown)
signal.signal(signal.SIGINT, _handle_shutdown)


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    if not os.environ.get("FLASK_DEBUG"):
        try:
            _validate_process_model(WSGI_SERVER)
        except RuntimeError as exc:
            logger.critical("%s", exc)
            sys.exit(1)

    application = create_app()

    if not _voice_cache_ready:
        logger.critical(
            "Voice cache failed to load. The /voices endpoint will return empty data. "
            "Check network connectivity and edge-tts availability."
        )

    if os.environ.get("FLASK_DEBUG"):
        logger.info(
            "Starting Flask development server on %s:%d", SERVER_HOST, SERVER_PORT
        )
        application.run(host=SERVER_HOST, port=SERVER_PORT, debug=True)
    else:
        try:
            from waitress import serve  # type: ignore[import-untyped]
        except ImportError:
            logger.critical(
                "Waitress not installed in production. Install with: pip install waitress"
            )
            sys.exit(1)
        logger.info(
            "Starting Waitress on %s:%d (threads=%d)",
            SERVER_HOST,
            SERVER_PORT,
            WAITRESS_THREADS,
        )
        serve(
            application,
            host=SERVER_HOST,
            port=SERVER_PORT,
            threads=WAITRESS_THREADS,
            _quiet=True,
        )
