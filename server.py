"""
edge-tts SSML Server
====================
A production-ready Flask server that accepts SSML input, extracts voice/rate/pitch
parameters, generates speech via Microsoft Edge TTS, and returns the audio as MP3.

Architecture:
    - Configuration via environment variables with sensible defaults.
    - Structured logging (no bare print statements).
    - Type-annotated helpers for SSML parsing and parameter transformation.
    - Flask application factory pattern for testability.
    - Production WSGI server (Waitress) when run directly.
    - CORS enabled for cross-origin frontend requests.
    - Health-check endpoint for monitoring.

Usage:
    python server.py                 # production mode (Waitress, port 5000)
    FLASK_DEBUG=1 python server.py   # development mode (Flask built-in, auto-reload)
"""

from __future__ import annotations

import logging
import os
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

import asyncio
from typing import Any

import edge_tts
from flask import Flask, Response, jsonify, request
from flask_cors import CORS

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.DEBUG if os.environ.get("FLASK_DEBUG") else logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("tts-server")

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
DEFAULT_VOICE: str = os.environ.get("TTS_DEFAULT_VOICE", "en-US-AvaMultilingualNeural")
"""Default voice name used when the SSML omits a <voice> element."""

DEFAULT_RATE: str = os.environ.get("TTS_DEFAULT_RATE", "+0%")
"""Default speaking rate (edge-tts format) when <prosody rate> is missing."""

DEFAULT_PITCH: str = os.environ.get("TTS_DEFAULT_PITCH", "+0Hz")
"""Default pitch (edge-tts format) when <prosody pitch> is missing."""

SERVER_HOST: str = os.environ.get("TTS_HOST", "0.0.0.0")
SERVER_PORT: int = int(os.environ.get("TTS_PORT", "5000"))

SSML_NAMESPACE: str = "http://www.w3.org/2001/10/synthesis"
"""XML namespace URI for the SSML <speak> element."""

# ---------------------------------------------------------------------------
# Voice cache (populated at startup from edge-tts)
# ---------------------------------------------------------------------------
_voice_cache: list[dict[str, Any]] = []
"""List of voice dicts from edge-tts.list_voices(), cached at startup."""

_LANGUAGE_MAP: dict[str, str] = {}
"""Locale → human-readable language name, derived from voice data."""

_LANGUAGE_LIST: list[dict[str, str]] = []
"""Unique languages for the frontend dropdown, sorted by display name."""


def _derive_language_name(friendly_name: str, locale: str) -> str:
    """Extract a clean language name from a FriendlyName or fall back to locale.

    edge-tts FriendlyNames look like:
        "Microsoft Ava Online (Natural) - English (United States)"
    We extract everything after the last ``" - "``.
    """
    if " - " in friendly_name:
        return friendly_name.rsplit(" - ", 1)[-1]
    return locale


async def _refresh_voice_cache() -> None:
    """Fetch all available voices from edge-tts and rebuild the caches."""
    global _voice_cache, _LANGUAGE_MAP, _LANGUAGE_LIST

    raw = await edge_tts.list_voices()
    _voice_cache = []
    seen_locales: dict[str, str] = {}

    for v in raw:
        locale = v.get("Locale", "")
        short = v.get("ShortName", "")
        gender = v.get("Gender", "")
        friendly = v.get("FriendlyName", "")

        lang_name = _derive_language_name(friendly, locale)
        if locale not in seen_locales:
            seen_locales[locale] = lang_name

        _voice_cache.append(
            {
                "ShortName": short,
                "Gender": gender,
                "Locale": locale,
                "FriendlyName": friendly,
                "LanguageName": lang_name,
            }
        )

    _LANGUAGE_MAP = seen_locales
    _LANGUAGE_LIST = sorted(
        [
            {"locale": loc, "name": name}
            for loc, name in seen_locales.items()
        ],
        key=lambda x: x["name"].lower(),
    )

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
        - ``"0%"``           →  ``"+0%"``   (safe no-change representation)
        - ``"N%"`` where N < 100  →  ``"-{100-N}%"``   (slow-down relative to 100 %)
        - ``"N%"`` where N ≥ 100  →  ``"+{N-100}%"``   (speed-up relative to 100 %)
        - Everything else (e.g. ``"x-slow"``, ``"+20ms"``) → passed through as-is.
    """
    if not raw:
        return DEFAULT_RATE

    raw = raw.strip()
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
            text = (prosody_el.text or "").strip()
        else:
            text = (voice_el.text or "").strip()
            logger.warning(
                "No <prosody> inside <voice>; using default rate/pitch."
            )
    else:
        text = (root.text or "").strip()
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
    async for chunk in communicate.stream():
        if chunk.get("type") == "audio":
            buf.write(chunk.get("data", b""))

    if buf.tell() == 0:
        raise RuntimeError("edge-tts returned no audio data.")

    buf.seek(0)
    return buf.read()


# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------
def create_app() -> Flask:
    """Create and configure the Flask application."""
    app = Flask(__name__)
    CORS(app)

    # -- Health check --------------------------------------------------------
    @app.route("/health", methods=["GET"])
    def health() -> Response:
        return jsonify({"status": "ok"})

    # -- Voices endpoint -----------------------------------------------------
    @app.route("/voices", methods=["GET"])
    def list_voices() -> Response:
        """Return all available edge-tts voices and languages.

        Response:
            {
                "languages": [{"locale": "en-US", "name": "English (United States)"}, ...],
                "voices": [{"ShortName": ..., "Gender": ..., "Locale": ..., ...}, ...]
            }
        """
        return jsonify(
            {
                "languages": _LANGUAGE_LIST,
                "voices": _voice_cache,
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
        if not body or "ssml" not in body:
            logger.warning("Request missing 'ssml' field.")
            return jsonify({"error": "Missing 'ssml' field in JSON body."}), 400  # type: ignore[return-value]

        ssml = body["ssml"].strip()
        if not ssml:
            return jsonify({"error": "Empty SSML string."}), 400  # type: ignore[return-value]

        # 1. Parse
        try:
            tts_req = extract_tts_params(ssml)
        except ValueError as exc:
            logger.warning("SSML parse error: %s", exc)
            return jsonify({"error": str(exc)}), 400  # type: ignore[return-value]

        # 2. Synthesise
        try:
            audio = await generate_audio(tts_req)
        except RuntimeError as exc:
            logger.error("TTS generation failed: %s", exc)
            return jsonify({"error": str(exc)}), 500  # type: ignore[return-value]

        # 3. Respond
        return Response(
            audio,
            mimetype="audio/mpeg",
            headers={"Content-Disposition": 'attachment; filename="tts-output.mp3"'},
        )

    return app


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    application = create_app()

    # Populate voice cache from edge-tts on every reboot
    asyncio.run(_refresh_voice_cache())

    if os.environ.get("FLASK_DEBUG"):
        logger.info(
            "Starting Flask development server on %s:%d", SERVER_HOST, SERVER_PORT
        )
        application.run(host=SERVER_HOST, port=SERVER_PORT, debug=True)
    else:
        try:
            from waitress import serve  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "Waitress not installed; falling back to Flask development server. "
                "Install with: pip install waitress"
            )
            application.run(host=SERVER_HOST, port=SERVER_PORT)
        else:
            logger.info(
                "Starting Waitress production server on %s:%d", SERVER_HOST, SERVER_PORT
            )
            serve(application, host=SERVER_HOST, port=SERVER_PORT)
