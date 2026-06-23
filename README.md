# free-tts

Browser-based SSML text-to-speech generator powered by Microsoft Edge TTS.

Paste SSML or use the visual **Text Input** builder to pick a voice, set speed/pitch, and generate speech. All synthesis happens locally via [edge-tts](https://github.com/rany2/edge-tts) — no cloud API key needed.

## Features

- **SSML tab** — raw SSML editing with live template pre-fill
- **Text Input tab** — visual voice picker with 322 voices across 142 languages
  - Language dropdown + gender filter + voice search
  - Two-column voice list (Preview | Selected)
  - Speed slider (−50% to +200%)
  - Pitch slider (−50% to +50%)
  - Live SSML preview
  - Results history with replay
- **Server** — Flask + Waitress/Gunicorn, `/voices` endpoint auto-populated from edge-tts on startup
- **Production-ready** — SSML size limits, TTS timeout, request logging, graceful shutdown

## Quick Start

```bash
# Install dependencies
pip install -r requirements.txt

# Start the server
python server.py

# Open the frontend
open index.html
```

The server runs on `http://localhost:5000`. Open `index.html` in a browser — it connects to the backend automatically.

## Configuration

All settings via environment variables:

| Variable | Default | Description |
|---|---|---|
| `TTS_HOST` | `0.0.0.0` | Listen address |
| `TTS_PORT` | `5000` | Listen port |
| `TTS_DEFAULT_VOICE` | `en-US-AvaMultilingualNeural` | Fallback voice |
| `TTS_MAX_SSML_LENGTH` | `51200` | Max SSML payload (bytes) |
| `TTS_TIMEOUT` | `60` | TTS generation timeout (seconds) |
| `TTS_SERVER` | `waitress` | WSGI server: `waitress` or `gunicorn` |
| `TTS_WAITRESS_THREADS` | `4` | Waitress worker threads |
| `TTS_GUNICORN_WORKERS` | `2` | Gunicorn worker processes |
| `TTS_GUNICORN_THREADS` | `4` | Gunicorn threads per worker |
| `FLASK_DEBUG` | (unset) | Set to `1` for dev mode (auto-reload, verbose errors) |

## API

### `GET /health`

```json
{"status": "ok", "voice_cache_ready": true}
```

### `GET /voices`

Returns all available voices and languages:

```json
{
  "languages": [
    {"locale": "en-US", "name": "English (United States)"},
    {"locale": "es-ES", "name": "Spanish (Spain)"}
  ],
  "voices": [
    {"ShortName": "en-US-AvaMultilingualNeural", "Gender": "Female", "Locale": "en-US", "LanguageName": "English (United States)"}
  ]
}
```

### `POST /generate-and-download-tts`

Accepts JSON with an `ssml` field, returns MP3 audio:

```bash
curl -X POST http://localhost:5000/generate-and-download-tts \
  -H "Content-Type: application/json" \
  -d '{"ssml":"<speak xmlns=\"http://www.w3.org/2001/10/synthesis\" version=\"1.0\" xml:lang=\"en-US\"><voice name=\"en-US-AvaMultilingualNeural\"><prosody rate=\"0%\" pitch=\"0%\">Hello world</prosody></voice></speak>"}' \
  -o output.mp3
```

## Deployment

```bash
# Production with Waitress (default)
python server.py

# Production with Gunicorn
TTS_SERVER=gunicorn TTS_GUNICORN_WORKERS=4 python server.py

# Development with auto-reload
FLASK_DEBUG=1 python server.py
```

## Dependencies

- Python ≥ 3.11
- [edge-tts](https://github.com/rany2/edge-tts) — Microsoft Edge TTS client
- Flask + flask-cors — HTTP API
- Waitress or Gunicorn — production WSGI server
