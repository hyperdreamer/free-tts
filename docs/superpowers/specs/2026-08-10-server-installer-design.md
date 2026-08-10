# Server Installer + Systemd Support — Design

Date: 2026-08-10
Status: Approved (sections 1–4 reviewed with user)

## Goal

Ship a unified, per-user installer in the free-tts repo so the server can be
installed on any Linux machine (not just this one) with one command: clone,
run, done. Includes a systemd *user* service for the server, and uninstall for
everything. Follows the existing per-user installer philosophy of
`desktop/install.py` (install into `~/.local` + `~/.config`, no root, no system
mutation beyond the user's own systemd session).

## Scope decisions (user-approved)

- **Self-contained copy**: the installed server lives at
  `~/.local/share/free-tts-server/`, decoupled from the checkout; the clone
  can be deleted after install.
- **Unified entry point** (Option 2): one top-level command dispatches to the
  server installer (new code) and wraps the existing `desktop.install`
  (no rewrite of its internals).
- **Separate roots** (Approach B): server and desktop use independent install
  locations (`~/.local/share/free-tts-server/` vs `~/.local/share/free-tts`).
  Zero edits to `desktop.install`; no install or uninstall ordering
  constraints. Cost: two venvs (34MB each) and two runtime copies. Eliminates
  an entire class of cross-component ownership failures.
- **Uninstall supported** for server and desktop, fully independent.

## Entry point & command shape

File: `install.py` at the repo root. Stdlib-only (follows the `desktop/`
convention). Run as:

```
python install.py install|uninstall|status [server|desktop|all]
```

- Component defaults to `all`; no command prints usage.
- `install all` runs each component's pre-flight independently, installs what
  succeeds, and reports a per-component summary (exit 1 if any failed —
  friendly on machines missing optional pieces like speech-dispatcher).
- Desktop component delegates to the existing
  `desktop.install.install()` / `desktop.install.uninstall()` functions
  directly, **followed by `desktop.install.restart_speech_dispatcher()`** —
  which `desktop.install.main()` also does, and without which speechd keeps a
  stale module list. Its manifest (`install-manifest.json`) is untouched.
- Server component is new code in `install.py`. Its state is a new
  `server-manifest.json` in the server root, recording: unit path, config
  path, source version (from the repo `VERSION` file), owned paths.
- `status` reads both manifests **tolerantly** (plain `json.load`, never the
  strict `_load_manifest`, so a foreign or stale manifest cannot crash it)
  plus `systemctl --user is-enabled` / `is-active`, and prints what is
  installed where.
- `python install.py` (repo-root script, not `python -m install`) avoids any
  collision with pip's `install` module name and does not depend on the
  caller's cwd — the checkout root is computed from `__file__`.

## Server component internals

### Base (independent root `~/.local/share/free-tts-server/`)

- If root exists without `server-manifest.json` → refuse with a clear message
  (never clobber unknown data, same ownership philosophy as `desktop.install`).
- Otherwise → create root, stage `server.py` + `requirements.txt` +
  `config.example.json` from the checkout, build venv: `python -m venv` +
  `pip install -r requirements.txt` (mirroring `desktop.install`'s
  `_stage_runtime` and `_default_venv_builder`).

### Config

Copy `config.example.json` → `{root}/config.json` **only if missing**;
existing config is never overwritten (preserved on reinstall, reported either
way). This matches the desktop module's own root-level config pattern
(`_PRESERVED = (".venv", "config.json")`), and `server.py` reads it by default
(`Path(__file__).parent / "config.json"`) — no `TTS_CONFIG` env var needed.

### Unit generation

Template with placeholders `{python}`, `{root}` → written atomically to
`~/.config/systemd/user/free-tts.service`, then `systemctl --user
daemon-reload` + `enable --now`.

Content (matches the unit validated on this machine):

- `ExecStart={python} {root}/server.py`
- `WorkingDirectory={root}`
- `Type=simple`
- `Restart=on-failure`, `RestartSec=2`
- `WantedBy=default.target`
- Comment: `idle_timeout` must stay 0 for the persistent service (the
  server's idle watchdog arms only when `TTS_IDLE_TIMEOUT > 0`)

No `Environment=TTS_CONFIG` needed — `server.py` reads config next to itself.

### Pre-flight (before touching anything)

`--force` bypasses only the port-conflict abort.

1. Interpreter ≥ Python 3.11 (clear error otherwise).
2. `systemctl --user` reachable (sane `XDG_RUNTIME_DIR`); if not, print a hint
   about `loginctl enable-linger` — never auto-enable it.
3. Port 5000 probe (`GET /health`):
   - free-tts responds *and* `systemctl --user is-active free-tts` reports the
     unit active under our own user session (i.e. the responder is our own
     unit) → safe, proceed (restart rebinds).
   - free-tts responds but the active responder is *not* our unit (another
     supervisor owns it) → abort unless `--force`.
   - non-free-tts responder → abort unless `--force`.

### Idempotency

Reinstall refreshes the staged copy (preserving `.venv` and `config.json`,
like `desktop.install`'s `_PRESERVED`), regenerates the unit, restarts the
service. Manifest (`server-manifest.json` in root) records the source version.

## Uninstall (fully independent)

`uninstall server`:

1. `systemctl --user disable --now free-tts` (skip if absent), remove the unit
   file, `daemon-reload`.
2. Remove the entire server root (`~/.local/share/free-tts-server/`), including
   manifest, venv, and config. User backs up config beforehand if desired.

`uninstall desktop`: unchanged — `desktop.install.uninstall()` is never
touched, removes `~/.local/share/free-tts/` as it does today.

`uninstall all`: server + desktop, any order (fully independent now).

Failure semantics: every step is idempotent (missing unit/root → skip with a
note); non-fatal issues are reported and the command continues; exit 1 if
anything failed, 2 on usage errors. No ordering constraints, no guards.

## Migration of this machine

Run `python install.py install server` here. Pre-flight sees port 5000 served
by our own unit → safe path. The installer stages a new base at
`~/.local/share/free-tts-server/`, generates the unit (pointing there),
restarts. The repo `.venv` and `config.json` become unused (stay in repo,
gitignored). `ai-backends` was already neutralized (supervisor entry removed,
stale pidfile deleted). The desktop module (`~/.local/share/free-tts`, already
installed on this machine) is untouched.

## Testing

New `tests/test_install.py`, hermetic like `tests/test_desktop_install.py`:

- Override hooks for root / systemd dir (install/uninstall functions take path
  parameters; `desktop.install` already does this, the server code follows
  suit).
- Cases: fresh install (base staged, venv built, unit generated with correct
  paths); config never overwritten on reinstall; idempotent reinstall;
  port-conflict abort vs `--force`; `status` output (reads manifests
  tolerantly — plain `json.load` rather than strict `_load_manifest`, never
  crashes on foreign/stale manifests).
- Desktop delegation: verify `restart_speech_dispatcher()` is called after
  `desktop.install.install()`/`uninstall()` (otherwise speechd stays stale).
- No cross-component guard tests needed — the two installs are fully
  independent.

## Docs

- README: new "Installation" section — `python install.py install
  server|desktop|all`, what it does, `uninstall`, `status`, the `--force`
  caveat.
- `docs/desktop-tts.md`: note the unified entry point as an alternative to
  `python -m desktop.install`.
- No VERSION bump (release decision is the user's).

## Out of scope

- System-wide (root) installs, distro package installs (speech-dispatcher,
  ffmpeg — documented prerequisites only).
- Auto-enabling linger.
- Rewriting `desktop.install` internals.
- Server-side "update" management beyond idempotent reinstall.
- `--keep-config` flag for uninstall (user backs up config manually if
  desired).

## Known caveat

Two copies of `server.py` exist when both components are installed: the desktop
module's on-demand backend (`~/.local/share/free-tts/server.py`) and the
systemd service (`~/.local/share/free-tts-server/server.py`). These can drift
if installed separately at different times. Impact is low — with the systemd
service always running, `desktop/backend.py` probes `/health`, finds it, and
reuses the active server instead of starting its own copy (the desktop copy
stays dormant). `install all` keeps both in sync.
