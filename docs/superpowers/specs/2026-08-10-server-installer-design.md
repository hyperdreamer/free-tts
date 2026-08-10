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

- **Self-contained copy** (Option A): the installed server lives at
  `~/.local/share/free-tts`, decoupled from the checkout; the clone can be
  deleted after install. Config lives at `~/.config/free-tts/config.json` —
  the same file the desktop Speech Dispatcher module already reads.
- **Unified entry point** (Option 2): one top-level command dispatches to the
  server installer (new code) and wraps the existing `desktop.install`
  (no rewrite of its internals).
- **Shared root with mutual uninstall guards** (Approach A): both components
  share `~/.local/share/free-tts` (one runtime copy, one venv). Uninstalls are
  ordered; `desktop.install.uninstall()` gains one guarded check before its
  `shutil.rmtree(root)`.
- **Uninstall supported** for server and desktop, with user-data preservation.

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
  directly. Its manifest (`install-manifest.json`) is untouched.
- Server component is new code in `install.py`. Its state is a new
  `server-manifest.json` inside the shared root, recording: unit path, config
  path, source version (from the repo `VERSION` file), owned paths.
- `status` reads both manifests plus `systemctl --user is-enabled` /
  `is-active` and prints what is installed where.
- `python install.py` (repo-root script, not `python -m install`) avoids any
  collision with pip's `install` module name and does not depend on the
  caller's cwd — the checkout root is computed from `__file__`.

## Server component internals

### Base (shared root `~/.local/share/free-tts`)

- Root already has the desktop manifest → base exists (`server.py`,
  `requirements.txt`, venv) → reuse, stage nothing.
- Root exists with *neither* manifest → refuse with a clear message. Never
  clobber unknown data (same ownership philosophy as `desktop.install`).
- Otherwise → create root, stage `server.py` + `requirements.txt` +
  `config.example.json` from the checkout (mirrors `_stage_runtime`), build
  venv: `python -m venv` + `pip install -r requirements.txt` (identical to
  `_default_venv_builder`).

### Config

Copy `config.example.json` → `~/.config/free-tts/config.json` **only if
missing**; an existing config is never overwritten (reported either way). One
config file serves the server and the desktop module.

### Unit generation

Template with placeholders `{python}`, `{root}`, `{config}` → written
atomically to `~/.config/systemd/user/free-tts.service`, then
`systemctl --user daemon-reload` + `enable --now`.

Content (matches the unit validated on this machine):

- `Type=simple`
- `Restart=on-failure`, `RestartSec=2`
- `Environment=TTS_CONFIG=<config>` (works regardless of cwd)
- `WantedBy=default.target`
- Comment: `idle_timeout` must stay 0 for the persistent service (the
  server's idle watchdog arms only when `TTS_IDLE_TIMEOUT > 0`)

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
service. Manifest records the source version.

## Uninstall & ownership guards

`uninstall server`:

1. `systemctl --user disable --now free-tts` (skip if absent), remove the unit
   file, `daemon-reload`.
2. Remove `server-manifest.json`.
3. Remove the shared root **only if the desktop manifest is absent** — with
   desktop installed, root (runtime copy + venv) stays because the module
   needs it. Config is **always kept** (user data; same philosophy as
   `desktop.install` keeping `speechd.conf`).

Guard in `desktop.install.uninstall()` (one small, test-protected change):
before `shutil.rmtree(root)`, check for `server-manifest.json`; if present,
raise an error telling the user to run `python install.py uninstall server`
first.

`uninstall all`: server first, then desktop — with the server manifest gone,
desktop's uninstall removes the root cleanly and restores `speechd.conf` as it
does today.

Failure semantics: every step is idempotent (missing unit/manifest → skip with
a note); non-fatal issues are reported and the command continues; exit 1 if
anything failed, 2 on usage errors. Uninstalls never need `--force` — only
ordering requirements, enforced rather than guessed.

## Migration of this machine

Run `python install.py install server` here. Pre-flight sees port 5000 served
by our own unit → safe path. The installer overwrites the hand-written unit
with the generated one (pointing at `~/.local/share/free-tts` +
`TTS_CONFIG`), stages the base, restarts. No manual steps. The repo `.venv`
becomes unused (stays, gitignored). `ai-backends` was already neutralized
(supervisor entry removed, stale pidfile deleted).

## Testing

New `tests/test_install.py`, hermetic like `tests/test_desktop_install.py`:

- Override hooks for root / config dir / systemd dir (install/uninstall
  functions take path parameters; `desktop.install` already does this, the
  server code follows suit).
- Cases: fresh install (base staged, venv built, unit generated with correct
  paths); reuse when desktop manifest present; config never overwritten on
  reinstall; idempotent reinstall; uninstall ordering guards both directions;
  port-conflict abort vs `--force`; `status` output.
- One new test in `tests/test_desktop_install.py` for the rmtree guard.

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
