# Using free-tts as a desktop TTS engine

This makes Okular's built-in **Tools > Speak** actions, and any other Speech
Dispatcher client, speak through free-tts voices.

## Requirements

- `speech-dispatcher` and `libspeechd` (Qt's bridge to it)
- `ffmpeg` (decodes backend MP3 to PCM)
- Python 3.11+

On Arch:

```bash
sudo pacman -S --needed speech-dispatcher ffmpeg
```

## Install

```bash
python -m desktop.install install
```

This is a per-user install. It writes to:

| Path | Contents |
|---|---|
| `~/.local/share/free-tts/` | runtime copy of the server and adapter, plus a private virtualenv |
| `~/.local/libexec/speech-dispatcher-modules/sd_free-tts` | module launcher |
| `~/.config/speech-dispatcher/modules/free-tts.conf` | module config |
| `~/.config/speech-dispatcher/speechd.conf` | one marked block registering the module |

Nothing outside your home directory is touched. Your existing `speechd.conf` is
backed up once to `speechd.conf.free-tts.bak`, and only the marked block is ever
rewritten.

Restart open Qt applications afterwards: Qt reads the voice list once when its
TTS engine starts.

## Verify

```bash
spd-say -o free-tts -L
spd-say -w -o free-tts -y en-US-AvaMultilingualNeural "Desktop speech is working."
```

Then open a document in Okular and use **Tools > Speak**.

### Optional: check what Qt applications see

If you have PyQt6 or PySide6 installed, this reports the voices and state
transitions Qt exposes, which is what Okular consumes:

```python
# qt-probe.py — run with: python qt-probe.py
from PyQt6.QtCore import QCoreApplication, QTimer
from PyQt6.QtTextToSpeech import QTextToSpeech

app = QCoreApplication([])
tts = QTextToSpeech("speechd")
print("voices:", [(v.name(), v.locale().name()) for v in tts.availableVoices()][:5])
tts.stateChanged.connect(lambda s: print("state:", s))
tts.say("Checking Qt integration.")
QTimer.singleShot(8000, app.quit)
app.exec()
```

Expect `Ready`, `Speaking`, then `Ready`. Gender reads as unknown; see the
limitation below.

## How the backend starts

On first use the adapter checks `GET /health`.

- A healthy free-tts backend already running is reused and never stopped or
  reconfigured, including on uninstall.
- Otherwise the adapter starts one and gives it a 5 minute idle timeout, so it
  exits on its own after you stop reading.
- If something else answers on that port, the adapter refuses to start a second
  backend and reports an error rather than speaking to an unknown service.

A backend you started by hand keeps running with no idle timeout.

## Configuration

`~/.config/free-tts/config.json` configures the adapter. Every key is optional,
and each has a `FREE_TTS_*` environment override.

| Key | Default | Meaning |
|---|---|---|
| `backend_url` | `http://127.0.0.1:5000` | backend base URL |
| `autostart` | `true` | allow on-demand startup |
| `idle_timeout` | `300` | idle seconds before an adapter-started backend exits (`0` disables) |
| `startup_timeout` | `30` | readiness wait after starting a backend |
| `request_timeout` | `120` | per-sentence synthesis timeout |
| `max_chunk_chars` | `400` | cap for long unpunctuated segments |
| `ffmpeg_path` | `ffmpeg` | decoder executable |

Voice, rate, pitch, and volume come from the calling application, not from this
file. Rate maps to -50%..+200%, pitch to -50Hz..+50Hz, and volume to a PCM gain.

## Known limitation

Qt reports every Speech Dispatcher voice with **unknown gender**, because Speech
Dispatcher's voice record has no gender field. Voice names and locales are exact
and selecting one works; only Qt's gender filter is unavailable. `spd-say -t
female1` still works, since the adapter keeps gender internally.

## Logs

- module: `~/.cache/speech-dispatcher/log/speech-dispatcher.log`
- adapter-started backend: `~/.cache/free-tts/backend.log`

Set `FREE_TTS_DEBUG=1` for verbose module logging. Spoken text is never logged.

## Uninstall

```bash
python -m desktop.install uninstall
```

Removes only what was installed, restores any `DefaultModule` it displaced, and
leaves Speech Dispatcher, unrelated config, and running backends alone.
