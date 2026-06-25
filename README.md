# free-tts

Browser-based SSML text-to-speech generator powered by Microsoft Edge TTS.

paste SSML or use the visual **Text Input** builder to pick a voice, set speed/pitch, and generate speech. Uses [edge-tts](https://github.com/rany2/edge-tts) which communicates with Microsoft's online TTS service — your text is sent over the network. No API key required.

## Features

### Web frontend
- **Text Input tab** — visual voice picker with hundreds of voices across many languages
  - Language dropdown + gender filter + voice search
  - Two-column voice list (Preview | Selected)
  - Speed slider (−50% to +200%) and pitch slider
  - **Sentence-by-sentence preview** with pre-caching — plays full text, no 30s limit
  - Live SSML preview panel
- **SSML tab** — raw SSML editing with live template pre-fill
- **Server** — Flask + Waitress/Gunicorn, `/voices` endpoint auto-populated from edge-tts on startup
- **Production-ready** — configurable CORS origins, SSML size limits, concurrency control, request logging, stall detection, graceful shutdown

### Chrome Extension
Right-click any text on any page and hear it spoken — Edge-style sentence-by-sentence reading with in-page highlighting.

- **Right-click "Speak this"** — splits text into sentences, plays sequentially with pre-caching
- **Yellow highlight bar** tracks current sentence on the page with smooth auto-scroll
- **Keyboard shortcut** `Ctrl+Shift+S` — speaks selected text (or full page if nothing selected)
- **Popup** — voice selector, speed slider, text input, Speak/Stop buttons
- **Options** — configurable server URL, highlight color
- **"Stop speaking"** context menu item appears while audio is playing

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

### Chrome Extension

1. Start the server first: `python server.py`
2. Go to `chrome://extensions`, enable **Developer mode**
3. Click **Load unpacked** → select the `extension/` directory
4. Right-click any text on any page → **Speak this**

Right-click the extension icon → **Options** to configure the server URL and highlight color.

## Configuration

Copy `config.example.json` to `config.json` and edit:
```bash
cp config.example.json config.json
```

All settings can be set via `config.json` or environment variables. Env vars take precedence.

### Network

| config.json | Env var | Default | Description |
|---|---|---|---|
| `host` | `TTS_HOST` | `127.0.0.1` | Listen address. `0.0.0.0` for all interfaces, `127.0.0.1` for local only. |
| `port` | `TTS_PORT` | `5000` | Listen port. |

### TTS

| config.json | Env var | Default | Description |
|---|---|---|---|
| `default_voice` | `TTS_DEFAULT_VOICE` | `en-US-AvaMultilingualNeural` | Fallback voice when SSML omits `<voice name>`. See `GET /voices` for available names. |
| `max_ssml_length` | `TTS_MAX_SSML_LENGTH` | `200000` | Max SSML payload in bytes. Set `0` to disable the limit. |
| `tts_stall_timeout` | `TTS_STALL_TIMEOUT` | `60` | Seconds of silence from Microsoft before aborting. `0` = disable stall detection. |
| `max_concurrent` | `TTS_MAX_CONCURRENT` | `2` | Max concurrent TTS generation requests. `0` = unlimited. |

### CORS

| config.json | Env var | Default | Description |
|---|---|---|---|
| `cors_origins` | `TTS_CORS_ORIGINS` | local + LAN | Allowed browser origins. Array of strings — plain matches (`"https://example.com"`) or regex patterns (`"^https?://192\\.168\\..*$"`). `"null"` allows `file://` pages. Env var uses comma-separated values. |

### WSGI server

| config.json | Env var | Default | Description |
|---|---|---|---|
| `wsgi_server` | `TTS_SERVER` | `waitress` | WSGI server: `waitress` or `gunicorn`. |
| `waitress_threads` | `TTS_WAITRESS_THREADS` | `4` | Waitress worker threads. |
| `gunicorn_workers` | `TTS_GUNICORN_WORKERS` | `2` | Gunicorn worker processes. |
| `gunicorn_threads` | `TTS_GUNICORN_THREADS` | `4` | Threads per Gunicorn worker. |

### Development

| Env var | Description |
|---|---|
| `FLASK_DEBUG=1` | Dev mode: auto-reload, verbose error pages, Flask built-in server. |
| `TTS_CONFIG` | Path to a custom config.json file. |

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
  ],
  "default_voice": "en-US-AvaMultilingualNeural"
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
