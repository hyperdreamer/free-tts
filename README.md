# free-tts

[![License: AGPL v3](https://img.shields.io/badge/License-AGPLv3-blue.svg)](https://www.gnu.org/licenses/agpl-3.0)

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
- **Server** — Flask + single-process Waitress, `/voices` endpoint auto-populated from edge-tts on startup
- **Production-ready** — configurable CORS origins, SSML size limits, concurrency control, request logging, stall detection, graceful shutdown

### Chrome Extension
Select text on any page and hear it spoken — Edge-style sentence-by-sentence reading with in-page highlighting. Activate via the popup or `Ctrl+Shift+U`.

- **Floating control bar** — prev/next sentence, pause/resume, stop, loop checkbox. Draggable anywhere on the page.
- **Yellow highlight overlays** track the current sentence with smooth auto-scroll
- **Sentence jumping** — double-click a word or drag-select text to jump directly to that sentence
- **Keyboard shortcut** `Ctrl+Shift+U` — toggles: speaks selection/full page when idle, stops playback when active
- **Popup** — voice selector, speed slider, text input, Speak/Pause/Stop buttons
- **Options** — configurable server URL (host + port) and highlight color

### Desktop TTS engine (Okular, KDE, any Speech Dispatcher client)
Use free-tts voices from Okular's built-in **Tools > Speak** actions by installing
a per-user Speech Dispatcher output module:

```bash
sudo pacman -S --needed speech-dispatcher ffmpeg   # or your distro's equivalent
python -m desktop.install install
```

The backend starts on demand and exits when idle. See
[docs/desktop-tts.md](docs/desktop-tts.md) for configuration, verification, and
uninstall.

## Installation

Install the server as a systemd **user** service, self-contained in
`~/.local/share/free-tts-server` with its own virtualenv. The checkout is only
needed at install time; you can move or delete it afterwards.

```bash
python install.py install server     # server + systemd user service
python install.py install desktop    # Speech Dispatcher module (Okular, KDE)
python install.py install all        # both
python install.py status             # what is installed, and unit state
python install.py uninstall server   # stop, disable, and remove
```

The installer is stdlib-only and never needs root. It refuses to write into a
directory it does not own, and checks Python 3.11+, the systemd user session,
and the host/port selected by the installed `config.json` before mutation.
`--force` bypasses endpoint occupancy only.

Server install and uninstall operations are serialized by a per-user runtime
lock. A concurrent installer invocation fails before preflight or mutation.
Direct edits to managed unit/link paths, or independent mutating `systemctl`
commands, are unsupported while an installer transaction is active; foreign
state observed when the transaction starts is retained and reported.

The systemd-managed service runs in config-only mode. Its installed
`config.json` is authoritative for host, port, voice, and runtime settings;
inherited per-setting `TTS_*` variables do not override it. `INVOCATION_ID`
remains available for service identity verification.

Manage the service afterwards with `systemctl --user`:

```bash
systemctl --user status free-tts
systemctl --user restart free-tts
journalctl --user -u free-tts -f
```

Keep `idle_timeout` at `0` in `~/.local/share/free-tts-server/config.json`. The
server arms its idle-shutdown watchdog only when `TTS_IDLE_TIMEOUT > 0`, and a
persistent service must not exit on its own. To run the service without a
graphical login, enable a lingering session with
`loginctl enable-linger $USER`.

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
4. Select text and press `Ctrl+Shift+U`, or open the popup and click **Speak**

Right-click the extension icon → **Options** to configure the server host, port, and highlight color.

## Configuration

Copy `config.example.json` to `config.json` and edit:
```bash
cp config.example.json config.json
```

When `server.py` runs manually, settings may come from `config.json` or the
listed environment variables, with environment variables taking precedence.
The installed systemd service uses the config-only behavior described above.

### Network

| config.json | Env var | Default | Description |
|---|---|---|---|
| `host` | `TTS_HOST` | `127.0.0.1` | Listen address. `0.0.0.0` for all interfaces, `127.0.0.1` for local only. |
| `port` | `TTS_PORT` | `5000` | Listen port. |

### TTS

| config.json | Env var | Default | Description |
|---|---|---|---|
| `default_voice` | `TTS_DEFAULT_VOICE` | `en-US-AvaMultilingualNeural` | Fallback voice when SSML omits `<voice name>`. See `GET /voices` for available names. |
| `default_rate` | `TTS_DEFAULT_RATE` | `+0%` | Default speaking rate when `<prosody rate>` is missing. Signed relative format (`+20%`, `-10%`, `+0%`). SSML percentages (`50%`, `150%`) and named presets (`x-slow`, `fast`) are also accepted and auto-converted. |
| `default_pitch` | `TTS_DEFAULT_PITCH` | `+0Hz` | Default pitch when `<prosody pitch>` is missing. Signed Hz format (`+0Hz`, `-5Hz`, `+10Hz`). Named presets (`x-low`, `high`) pass through unchanged. |
| `max_ssml_length` | `TTS_MAX_SSML_LENGTH` | `200000` | Max SSML payload in bytes. Set `0` to disable the limit. |
| `tts_stall_timeout` | `TTS_STALL_TIMEOUT` | `60` | Seconds of silence from Microsoft before aborting. `0` = disable stall detection. |
| `max_concurrent` | `TTS_MAX_CONCURRENT` | `2` | Max concurrent TTS generation requests in the server process. `0` = unlimited. |
| `queue_timeout` | `TTS_QUEUE_TIMEOUT` | `30` | Seconds to wait for a TTS slot before returning HTTP 503. Minimum 5s. |

### CORS

| config.json | Env var | Default | Description |
|---|---|---|---|
| `cors_origins` | `TTS_CORS_ORIGINS` | local + LAN | Allowed browser origins. Array of strings — plain matches (`"https://example.com"`) or regex patterns (`"^https?://192\\.168\\..*$"`). `"null"` allows `file://` pages. Env var uses comma-separated values. |

### WSGI server

The cancellation registry and idle-lifecycle accounting require one server
process. Waitress is the supported production server. `server.py` rejects
non-Waitress `TTS_SERVER` values, and requests arriving through Gunicorn are
rejected rather than receiving a partial cancellation contract.

| config.json | Env var | Default | Description |
|---|---|---|---|
| `wsgi_server` | `TTS_SERVER` | `waitress` | Must remain `waitress` for the single-process lifecycle contract. |
| `waitress_threads` | `TTS_WAITRESS_THREADS` | `4` | Waitress worker threads. |

### Development

| Env var | Description |
|---|---|
| `FLASK_DEBUG=1` | Dev mode: auto-reload, verbose error pages, Flask built-in server. |
| `TTS_CONFIG` | Path to a custom config.json file. |

## API

### `GET /health`

```json
{"status": "ok", "service": "free-tts", "api_version": 1, "voice_cache_ready": true}
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
# Production with single-process Waitress
python server.py

# Development with auto-reload
FLASK_DEBUG=1 python server.py
```

## Dependencies

- Python ≥ 3.11
- [edge-tts](https://github.com/rany2/edge-tts) — Microsoft Edge TTS client (LGPLv3)
- Flask + flask-cors — HTTP API
- Waitress — production WSGI server

## License

[GNU Affero General Public License v3.0](https://www.gnu.org/licenses/agpl-3.0) (AGPL-3.0).
See the [LICENSE](LICENSE) file for the full license text.
