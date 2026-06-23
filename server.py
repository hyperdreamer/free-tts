"""
edge-tts SSML Server
====================
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
    - Production WSGI: Waitress (default) or Gunicorn (via TTS_SERVER=gunicorn).
    - CORS enabled for cross-origin frontend requests.
    - Health-check endpoint with voice-cache readiness indicator.
    - Error messages sanitised in production mode.

Usage:
    python server.py                      # production (Waitress, port 5000)
    FLASK_DEBUG=1 python server.py        # development (Flask built-in)
    TTS_SERVER=gunicorn python server.py   # production (Gunicorn, port 5000)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
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
    if env_val is not None:
        return coerce(env_val)
    file_val = _CONFIG_CACHE.get(key)
    if file_val is not None:
        return coerce(file_val) if coerce is not str else str(file_val)
    return default


_CONFIG_CACHE = _load_config()

DEFAULT_VOICE: str = _cfg("default_voice", "TTS_DEFAULT_VOICE", "en-US-AvaMultilingualNeural")
"""Default voice name used when the SSML omits a <voice> element."""

DEFAULT_RATE: str = _cfg("default_rate", "TTS_DEFAULT_RATE", "+0%")
"""Default speaking rate (edge-tts format) when <prosody rate> is missing."""

DEFAULT_PITCH: str = _cfg("default_pitch", "TTS_DEFAULT_PITCH", "+0Hz")
"""Default pitch (edge-tts format) when <prosody pitch> is missing."""

SERVER_HOST: str = _cfg("host", "TTS_HOST", "127.0.0.1")
SERVER_PORT: int = _cfg("port", "TTS_PORT", 5000, coerce=int)

MAX_SSML_LENGTH: int = _cfg("max_ssml_length", "TTS_MAX_SSML_LENGTH", 256 * 1024, coerce=int)
"""Maximum SSML payload size in bytes. 256 KB default."""

TTS_TIMEOUT: int = _cfg("tts_timeout", "TTS_TIMEOUT", 0, coerce=int)
"""Gunicorn worker timeout. 0 = no limit (stall detection handles timeouts)."""

TTS_STALL_TIMEOUT: int = _cfg("tts_stall_timeout", "TTS_STALL_TIMEOUT", 60, coerce=int)
"""Seconds of silence (no data from edge-tts) before aborting. 0 = disable."""

TTS_MAX_CONCURRENT: int = _cfg("max_concurrent", "TTS_MAX_CONCURRENT", 2, coerce=int)
"""Maximum concurrent TTS generation requests. 0 = unlimited."""

WAITRESS_THREADS: int = _cfg("waitress_threads", "TTS_WAITRESS_THREADS", 4, coerce=int)
"""Number of Waitress worker threads."""

GUNICORN_WORKERS: int = _cfg("gunicorn_workers", "TTS_GUNICORN_WORKERS", 2, coerce=int)
"""Number of Gunicorn worker processes."""

GUNICORN_THREADS: int = _cfg("gunicorn_threads", "TTS_GUNICORN_THREADS", 4, coerce=int)
"""Threads per Gunicorn worker."""

WSGI_SERVER: str = _cfg("wsgi_server", "TTS_SERVER", "waitress")
"""Which WSGI server to use: 'waitress' or 'gunicorn'."""

SSML_NAMESPACE: str = "http://www.w3.org/2001/10/synthesis"
"""XML namespace URI for the SSML <speak> element."""

# ---------------------------------------------------------------------------
# Voice cache (populated at startup from edge-tts)
# ---------------------------------------------------------------------------
_voice_cache: list[dict[str, Any]] = []
"""List of voice dicts from edge-tts.list_voices(), cached at startup."""

_voice_cache_ready: bool = False
"""True once the voice cache has been successfully populated."""

_LANGUAGE_MAP: dict[str, str] = {}
"""Locale → human-readable language name, derived from voice data."""

_LANGUAGE_LIST: list[dict[str, str]] = []
"""Unique languages for the frontend dropdown, sorted by display name."""

# ---------------------------------------------------------------------------
# Locale → display name mapping (ISO 639-1 language + ISO 3166-1 region)
# ---------------------------------------------------------------------------
_LANG_NAMES: dict[str, str] = {
    "af": "Afrikaans", "sq": "Albanian", "am": "Amharic", "ar": "Arabic",
    "hy": "Armenian", "az": "Azerbaijani", "bn": "Bangla", "eu": "Basque",
    "bs": "Bosnian", "bg": "Bulgarian", "my": "Burmese", "ca": "Catalan",
    "yue": "Cantonese", "zh": "Chinese", "hr": "Croatian", "cs": "Czech",
    "da": "Danish", "nl": "Dutch", "en": "English", "et": "Estonian",
    "fil": "Filipino", "fi": "Finnish", "fr": "French", "gl": "Galician",
    "ka": "Georgian", "de": "German", "el": "Greek", "gu": "Gujarati",
    "he": "Hebrew", "hi": "Hindi", "hu": "Hungarian", "is": "Icelandic",
    "id": "Indonesian", "ga": "Irish", "it": "Italian", "ja": "Japanese",
    "jv": "Javanese", "kn": "Kannada", "kk": "Kazakh", "km": "Khmer",
    "ko": "Korean", "lo": "Lao", "lv": "Latvian", "lt": "Lithuanian",
    "mk": "Macedonian", "ms": "Malay", "ml": "Malayalam", "mt": "Maltese",
    "mr": "Marathi", "mn": "Mongolian", "ne": "Nepali", "nb": "Norwegian",
    "ps": "Pashto", "fa": "Persian", "pl": "Polish", "pt": "Portuguese",
    "pa": "Punjabi", "ro": "Romanian", "ru": "Russian", "sr": "Serbian",
    "si": "Sinhala", "sk": "Slovak", "sl": "Slovenian", "so": "Somali",
    "es": "Spanish", "su": "Sundanese", "sw": "Swahili", "sv": "Swedish",
    "ta": "Tamil", "te": "Telugu", "th": "Thai", "tr": "Turkish",
    "uk": "Ukrainian", "ur": "Urdu", "uz": "Uzbek", "vi": "Vietnamese",
    "cy": "Welsh", "zu": "Zulu",
}

_REGION_NAMES: dict[str, str] = {
    "AE": "United Arab Emirates", "AR": "Argentina", "AT": "Austria",
    "AU": "Australia", "BD": "Bangladesh", "BE": "Belgium", "BG": "Bulgaria",
    "BH": "Bahrain", "BO": "Bolivia", "BR": "Brazil", "CA": "Canada",
    "CH": "Switzerland", "CL": "Chile", "CN": "China", "CO": "Colombia",
    "CR": "Costa Rica", "CU": "Cuba", "CY": "Cyprus", "CZ": "Czechia",
    "DE": "Germany", "DK": "Denmark", "DO": "Dominican Republic",
    "DZ": "Algeria", "EC": "Ecuador", "EE": "Estonia", "EG": "Egypt",
    "ES": "Spain", "ET": "Ethiopia", "FI": "Finland", "FR": "France",
    "GB": "United Kingdom", "GH": "Ghana", "GR": "Greece", "GT": "Guatemala",
    "GQ": "Equatorial Guinea", "HK": "Hong Kong", "HN": "Honduras",
    "HR": "Croatia", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland",
    "IL": "Israel", "IN": "India", "IQ": "Iraq", "IS": "Iceland",
    "IT": "Italy", "JM": "Jamaica", "JO": "Jordan", "JP": "Japan",
    "KE": "Kenya", "KH": "Cambodia", "KR": "South Korea", "KW": "Kuwait",
    "LB": "Lebanon", "LK": "Sri Lanka", "LT": "Lithuania", "LV": "Latvia",
    "LY": "Libya", "MA": "Morocco", "MK": "North Macedonia", "MN": "Mongolia",
    "MT": "Malta", "MX": "Mexico", "MY": "Malaysia", "NG": "Nigeria",
    "NI": "Nicaragua", "NL": "Netherlands", "NO": "Norway", "NP": "Nepal",
    "NZ": "New Zealand", "OM": "Oman", "PA": "Panama", "PE": "Peru",
    "PH": "Philippines", "PK": "Pakistan", "PL": "Poland", "PR": "Puerto Rico",
    "PT": "Portugal", "PY": "Paraguay", "QA": "Qatar", "RO": "Romania",
    "RS": "Serbia", "RU": "Russia", "SA": "Saudi Arabia", "SE": "Sweden",
    "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "SN": "Senegal",
    "SV": "El Salvador", "SY": "Syria", "TH": "Thailand", "TN": "Tunisia",
    "TR": "Turkey", "TW": "Taiwan", "TZ": "Tanzania", "UA": "Ukraine",
    "UG": "Uganda", "US": "United States", "UY": "Uruguay", "VE": "Venezuela",
    "VN": "Vietnam", "YE": "Yemen", "ZA": "South Africa", "ZW": "Zimbabwe",
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


async def _refresh_voice_cache() -> None:
    """Fetch all available voices from edge-tts and rebuild the caches."""
    global _voice_cache, _voice_cache_ready, _LANGUAGE_MAP, _LANGUAGE_LIST

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

    # Mutate in-place so imported references stay valid
    _voice_cache.clear()
    _voice_cache.extend(new_voices)

    _LANGUAGE_MAP.clear()
    _LANGUAGE_MAP.update(seen_locales)

    _LANGUAGE_LIST.clear()
    _LANGUAGE_LIST.extend(
        sorted(
            [
                {"locale": loc, "name": name}
                for loc, name in seen_locales.items()
            ],
            key=lambda x: x["name"].lower(),
        )
    )
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

    # --- <voice> -----------------------------------------------------------
    voice_el = root.find(f"{{{SSML_NAMESPACE}}}voice")
    if voice_el is not None:
        voice_name = voice_el.get("name")
        if voice_name:
            voice = voice_name.strip()

        prosody_el = voice_el.find(f"{{{SSML_NAMESPACE}}}prosody")
        if prosody_el is not None:
            rate = _parse_rate(prosody_el.get("rate"))
            pitch = _parse_pitch(prosody_el.get("pitch"))
            text = " ".join(prosody_el.itertext()).strip()
        else:
            text = " ".join(voice_el.itertext()).strip()
            logger.warning(
                "No <prosody> inside <voice>; using default rate/pitch."
            )
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
async def generate_audio(req: TTSRequest) -> bytes:
    """Synthesise speech via edge-tts and return MP3 bytes.

    Args:
        req: Normalised TTS parameters.

    Returns:
        Raw MP3 audio bytes.

    Raises:
        RuntimeError: If edge-tts fails (e.g. invalid voice name).
        TimeoutError: If no data received from edge-tts for TTS_STALL_TIMEOUT seconds.
    """
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
    while True:
        try:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=TTS_STALL_TIMEOUT)
        except StopAsyncIteration:
            break
        except asyncio.TimeoutError:
            raise TimeoutError(
                f"No data from TTS service for {TTS_STALL_TIMEOUT}s (stall detected)"
            )
        if chunk.get("type") == "audio":
            buf.write(chunk.get("data", b""))

    if buf.tell() == 0:
        raise RuntimeError("edge-tts returned no audio data.")

    buf.seek(0)
    return buf.read()


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


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)

    # Don't serve static files or templates — API only
    app.config["PROPAGATE_EXCEPTIONS"] = True  # let WSGI server handle errors
    # Reject oversized request bodies before JSON parsing
    app.config["MAX_CONTENT_LENGTH"] = max(MAX_SSML_LENGTH * 2, 64 * 1024)

    CORS(app)

    # Concurrency limiter
    _tts_semaphore = asyncio.Semaphore(TTS_MAX_CONCURRENT) if TTS_MAX_CONCURRENT > 0 else None

    # Populate voice cache on startup (works with any WSGI entrypoint)
    if not _voice_cache_ready:
        try:
            asyncio.run(_refresh_voice_cache())
        except Exception as exc:
            logger.error("Failed to initialise voice cache: %s", exc)

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
                "voice_cache_ready": _voice_cache_ready,
            }
        )

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
        return jsonify(
            {
                "languages": _LANGUAGE_LIST,
                "voices": _voice_cache,
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

        # 1. Parse
        try:
            tts_req = extract_tts_params(ssml)
        except ValueError as exc:
            logger.warning("SSML parse error: %s", exc)
            return jsonify({"error": _error_message(exc, is_client_error=True)}), 400  # type: ignore[return-value]

        # 2. Synthesise (stall timeout handled inside generate_audio)
        try:
            if _tts_semaphore is not None:
                async with _tts_semaphore:
                    audio = await generate_audio(tts_req)
            else:
                audio = await generate_audio(tts_req)
        except asyncio.TimeoutError as exc:
            logger.error("TTS stall detected after %ds", TTS_STALL_TIMEOUT)
            return jsonify({"error": _error_message(exc)}), 504  # type: ignore[return-value]
        except RuntimeError as exc:
            logger.error("TTS generation failed: %s", exc)
            return jsonify({"error": _error_message(exc)}), 500  # type: ignore[return-value]
        except Exception as exc:
            logger.exception("Unexpected TTS error")
            return jsonify({"error": _error_message(exc)}), 500  # type: ignore[return-value]

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
    elif WSGI_SERVER == "gunicorn":
        try:
            from gunicorn.app.base import BaseApplication

            class StandaloneApplication(BaseApplication):
                def __init__(self, app: Flask, options: dict[str, Any]) -> None:
                    self.application = app
                    self.options = options
                    super().__init__()

                def load_config(self) -> None:
                    for key, value in self.options.items():
                        if key in self.cfg.settings and value is not None:
                            self.cfg.set(key.lower(), value)

                def load(self) -> Flask:
                    return self.application  # type: ignore[return-value]

            options = {
                "bind": f"{SERVER_HOST}:{SERVER_PORT}",
                "workers": GUNICORN_WORKERS,
                "threads": GUNICORN_THREADS,
                "worker_class": "gthread",
                "graceful_timeout": 30,
                "timeout": TTS_TIMEOUT + 10,
                "accesslog": "-",
                "errorlog": "-",
                "loglevel": "info",
                "preload_app": True,
            }
            logger.info(
                "Starting Gunicorn on %s:%d (workers=%d, threads=%d)",
                SERVER_HOST,
                SERVER_PORT,
                GUNICORN_WORKERS,
                GUNICORN_THREADS,
            )
            StandaloneApplication(application, options).run()
        except ImportError:
            logger.critical(
                "Gunicorn requested but not installed. Install with: pip install gunicorn"
            )
            sys.exit(1)
    else:
        if PRODUCTION:
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
            serve(application, host=SERVER_HOST, port=SERVER_PORT, threads=WAITRESS_THREADS, _quiet=True)
        else:
            # FLASK_DEBUG mode: use Flask dev server
            logger.info("Starting Flask development server on %s:%d", SERVER_HOST, SERVER_PORT)
            application.run(host=SERVER_HOST, port=SERVER_PORT, debug=True)
