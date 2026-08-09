# free-tts as a desktop TTS engine (Okular and other Speech Dispatcher clients)

Date: 2026-08-09
Status: approved for implementation planning

## Problem

`free-tts` today exposes speech synthesis through an HTTP API (`POST
/generate-and-download-tts`) consumed by the bundled web frontend and the Chrome
extension. Desktop applications cannot use it. Okular's built-in **Speak**
actions go through Qt TextToSpeech, which on Linux talks to Speech Dispatcher.
There is no adapter between Speech Dispatcher and the `free-tts` HTTP API, so
`free-tts` voices are invisible to Okular and to every other Speech Dispatcher
client.

## Goal

Make Okular's existing Speak actions use `free-tts` transparently, by
implementing a per-user Speech Dispatcher output module named `free-tts`. No
changes to Okular. No separate reader UI.

## Scope decisions

Confirmed with the user during design:

| Decision | Choice |
|---|---|
| Integration target | Okular's own Speak actions, transparently |
| Desktop bridge | Speech Dispatcher (documented prerequisite), not a native Qt plugin |
| Voice exposure | Full dynamic Edge voice list from `GET /voices` |
| Backend lifecycle | On-demand: reuse a healthy backend, otherwise start one |
| Ownership | A backend found already running is never stopped or reconfigured |
| Idle shutdown | Only for adapter-started backends; default 300s, configurable |
| Install scope | Per-user (`~/.local`, `~/.config`), self-contained copy + private venv |
| Default module | Installer sets `free-tts` as the user's Speech Dispatcher default |

Non-goals: system-wide packaging, an always-on login service, a new desktop
reader window, changes to web frontend or extension behavior.

### Accepted upstream limitation

Qt's Speech Dispatcher plugin creates every `QVoice` with
`QVoice::Unknown` gender, because `SPDVoice` carries no gender field
(`qtspeech/src/plugins/tts/speechdispatcher/qtexttospeech_speechd.cpp`,
`updateVoices()`). Voice names and locales route correctly; gender metadata is
not available to Qt applications. Accepted.

Qt also caches the voice list when its TTS engine initializes, so Qt
applications must be restarted after install/uninstall.

## Architecture

```text
Okular
  -> Qt TextToSpeech speechd plugin
  -> per-user Speech Dispatcher
  -> free-tts output module (this design)
  -> local Flask API (existing server.py)
  -> edge-tts / Microsoft service

MP3 response
  -> ffmpeg PCM decoder
  -> Speech Dispatcher audio channel (705 AUDIO)
  -> PipeWire/PulseAudio
```

Four bounded components:

**Protocol adapter.** Owns Speech Dispatcher stdin/stdout: command parsing,
response codes, event notifications (`701 BEGIN`, `700 INDEX MARK`,
`702 END`, `703 STOP`, `704 PAUSE`), and current speech state. Standard output
is reserved exclusively for protocol traffic; diagnostics go to stderr.

**Backend controller.** Validates `GET /health`, serializes startup with a lock
under `XDG_RUNTIME_DIR`, rechecks health while holding the lock, launches the
installed server only when needed, and waits for readiness with a bounded
timeout. It never signals or reconfigures a backend it did not start.

**Speech pipeline.** Resolves the selected voice, maps rate/pitch/volume, parses
Speech Dispatcher's SSML index marks into sentence-sized chunks, requests MP3
audio with one-chunk lookahead, decodes to PCM, and reports events.

**Per-user installer.** Creates a private virtualenv and copies the runtime into
`~/.local/share/free-tts`; installs the module launcher into
`~/.local/libexec/speech-dispatcher-modules/`; idempotently registers
`free-tts` in `~/.config/speech-dispatcher/speechd.conf` inside a managed block;
preserves `~/.config/free-tts/config.json`; ships a matching uninstaller.

The Flask server stays the single synthesis boundary.

### Repository layout

New code lives under a dedicated `desktop/` package so the existing top-level
files keep their current roles. One file per component, each independently
testable:

```text
desktop/
  module.py       # entry point run by Speech Dispatcher
  protocol.py     # command parsing, response codes, event emission
  backend.py      # health check, locked on-demand startup, ownership rules
  pipeline.py     # voice resolution, chunking, prefetch, decode, gain
  settings.py     # rate/pitch/volume mapping, adapter config loading
  install.py      # per-user install / upgrade / uninstall + manifest
  free-tts.conf   # Speech Dispatcher module config template
tests/
  test_desktop_*.py
```

### Adapter configuration

`~/.config/free-tts/config.json` is the adapter's own file, separate from the
server's `config.json`. Every key has a working default, and each is overridable
by a `FREE_TTS_*` environment variable:

| Key | Default | Meaning |
|---|---|---|
| `backend_url` | `http://127.0.0.1:5000` | Backend base URL |
| `autostart` | `true` | Allow on-demand startup |
| `idle_timeout` | `300` | Idle seconds for an adapter-started backend (`0` disables) |
| `startup_timeout` | `30` | Readiness wait after spawning |
| `request_timeout` | `120` | Per-segment synthesis timeout |
| `max_chunk_chars` | `400` | Cap for unpunctuated segments |
| `ffmpeg_path` | `ffmpeg` | Decoder executable |

## Server changes (`server.py`)

1. **Health identity.** `GET /health` keeps its existing `status` and
   `voice_cache_ready` fields and adds `service: "free-tts"` plus an integer
   `api_version` (starting at `1`). The adapter requires both to match before it
   sends any synthesis request.
2. **Idle shutdown.** New setting (`idle_timeout` config key +
   `TTS_IDLE_TIMEOUT` env var), default `0` (disabled) for ordinary launches. The
   adapter passes `300` only to a backend it starts. The timer never expires
   while a synthesis request is active, and is not extended by health polling.
3. **Cancellation.** `POST /generate-and-download-tts` accepts an optional
   opaque `request_id`, and `DELETE /tts-request/<request_id>` cancels that
   in-flight generation so `STOP` releases backend concurrency slots instead of
   leaving current + lookahead requests occupying them. Unknown or completed IDs
   return 404. Cancellation is idempotent. Old IDs cannot affect later synthesis.

Existing endpoints, defaults, CORS behavior, and error contracts are unchanged.

## Runtime behavior

### Initialization and voices

- Speech Dispatcher starts the module only when its service is needed.
- `LIST VOICES` triggers the health/start sequence, then reads `GET /voices`.
- Every Edge voice is returned under its exact `ShortName` with its locale.
  Speech Dispatcher `variant` is `none`, because Qt folds that field into the
  locale rather than treating it as gender.
- The adapter retains endpoint gender metadata internally so non-Qt clients
  using symbolic voices (`female1`, `male2`, ...) still map sensibly.
- Voice resolution order: exact synthesis voice, then requested locale, then
  the server's `default_voice`, then the first available voice.

### Speech pipeline

1. Accept the SSML message from Speech Dispatcher.
2. Parse it safely, preserving `__spd_*` index marks and flattening unsupported
   markup to text.
3. Split at index marks, with a conservative length cap for long unpunctuated
   segments.
4. Generate the current segment plus one lookahead segment via
   `POST /generate-and-download-tts`.
5. Decode MP3 to 24 kHz mono signed 16-bit PCM with `ffmpeg`.
6. Send bounded PCM frames over the Speech Dispatcher server-audio protocol.
7. Emit `BEGIN`, index-mark, and `END` events at the matching boundaries.

### Settings mapping

| Speech Dispatcher | Range | free-tts / Edge |
|---|---|---|
| `rate` | -100..0 | `-50%`..`+0%` (linear) |
| `rate` | 0..100 | `+0%`..`+200%` (linear) |
| `pitch` | -100..100 | `-50Hz`..`+50Hz` |
| `volume` | -100..100 | 0..100% PCM gain, applied after decode |
| `pitch_range` | any | accepted, ignored (no Edge equivalent) |

Punctuation, spelling, and capital-letter preprocessing remain Speech
Dispatcher's responsibility (`SymbolsPreproc`).

### Controls

- **STOP**: invalidates the current generation token, cancels current and
  lookahead HTTP work, stops the decoder, discards queued PCM, emits `STOP`.
  Stale workers cannot emit audio afterwards.
- **PAUSE**: finishes the current marked segment, reports its index mark,
  discards lookahead, emits `PAUSE`.
- **Resume**: handled by Speech Dispatcher, which issues a new `SPEAK` starting
  at the last reported mark. The adapter keeps no hidden resume buffer.
- If no usable mark exists, pause degrades to stopping at the nearest internal
  chunk boundary and may repeat part of that chunk on resume rather than losing
  text.

## Failure handling

- Refused connection to the backend triggers the serialized on-demand startup.
- A healthy response with wrong identity or unsupported API version is treated
  as a port conflict: no second server is started, nothing is killed, no
  synthesis data is sent to it.
- Startup readiness has a bounded timeout. Early exit and timeout errors include
  the spawned backend's log path.
- Voice-load failure returns Speech Dispatcher's `304 CANT LIST VOICES`. No
  stale or invented voices.
- Invalid voice or settings values fail at protocol level before speech starts.
- Synthesis failure before acceptance returns `301 ERROR CANT SPEAK`. Failure
  after asynchronous acceptance stops that message, clears prefetched work, and
  logs the detailed cause.
- HTTP 503 honors `Retry-After` for one bounded retry, unless stop or pause was
  requested.
- Missing or non-functional `ffmpeg` is detected during module initialization; no
  backend startup is attempted in that case.
- A broken Speech Dispatcher pipe terminates adapter workers and decoders. An
  adapter-started backend is still governed by its own idle timer; an externally
  started backend is untouched.

### Logging

- Adapter diagnostics land in Speech Dispatcher's per-user log directory.
- An adapter-started backend logs to an XDG cache path, rotated at a small fixed
  size before startup.
- Logs carry request IDs and state transitions, never full spoken text.
- Protocol responses stay concise; network and provider detail stays in logs.

## Installation safety

- Preflight: Python >= 3.11, `speech-dispatcher`, `libspeechd`, `ffmpeg`.
- Runtime files and the private virtualenv are staged, then moved into place.
- Speech Dispatcher config is edited only inside a clearly marked managed block,
  with a backup taken before the first edit.
- Re-running install upgrades runtime files and preserves
  `~/.config/free-tts/config.json`.
- Uninstall removes only the managed block and files recorded in the install
  manifest. It does not remove Speech Dispatcher, unrelated user config, or a
  manually started backend.
- The user's Speech Dispatcher is restarted after install/uninstall; Qt
  applications must be reopened.

## Testing and acceptance

Automated:

- **Protocol unit tests**: command parsing, multiline and dot-stuffed messages,
  settings validation, voice filtering, exact response codes, event ordering,
  binary PCM escaping.
- **Voice tests**: every `/voices` entry exposed with exact name and locale,
  `variant=none`, symbolic gender fallback from retained metadata.
- **Backend controller tests**: reuse healthy backend, reject identity/version
  mismatch, serialized concurrent startup, readiness timeout, idle setting passed
  only to spawned instances.
- **Server tests**: health identity, cancellation, active-request accounting, no
  idle shutdown mid-synthesis, shutdown after the configured idle window,
  idle disabled for ordinary launches.
- **Pipeline tests**: SSML/index-mark parsing, chunk ordering, one-segment
  lookahead, rate/pitch/volume mapping, decoder failure, stale-worker
  suppression, stop cancellation, pause at reported marks.
- **Installer tests**: isolated fake `HOME`/XDG dirs, first install, idempotent
  upgrade, preserved config, managed-block edits, non-destructive uninstall.
- **Protocol integration tests**: run the adapter as a subprocess against a fake
  backend, replaying full `INIT`, `AUDIO`, `LIST VOICES`, `SET`, `SPEAK`,
  `STOP`, `PAUSE`, `QUIT` transcripts.
- **Regression**: existing Flask, frontend, media-session, and extension tests
  keep passing.

Opt-in live smoke (real Speech Dispatcher, real Edge service):

```bash
spd-say -o free-tts -L
spd-say -w -o free-tts -y en-US-AvaMultilingualNeural "Desktop speech is working."
```

A small Qt TextToSpeech probe verifies the `speechd` engine enumerates the
expected names/locales and reports `Ready` -> `Speaking` -> `Paused` -> `Ready`.
Gender stays `Unknown`, as accepted.

Manual acceptance in Okular:

1. Install per-user integration with no Flask server running.
2. Invoke Okular's Speak action; backend starts on demand.
3. Verify speech, rate/pitch/volume, pause/resume, stop.
4. Start the backend manually, speak again, confirm reuse.
5. Confirm the manually started process survives inactivity.
6. Confirm an adapter-started backend exits after the idle window.
7. Confirm web frontend and Chrome extension still work.

## Environment verified during design

Arch Linux, KDE/X11, Okular 26.04.3, qt6-speech 6.11.1 with
`libqtexttospeech_speechd.so` present but `libspeechd.so.2` **not installed**
(`speech-dispatcher` must be installed as a prerequisite). `ffmpeg`, `mpv`,
`mpg123`, PipeWire with `pipewire-pulse` are present.
