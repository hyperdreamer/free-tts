# Server Installer + Systemd Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use the deterministic
> subagent-driven-development controller to implement this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a per-user installer (`install.py`) that installs the free-tts server into `~/.local/share/free-tts-server` with a private venv and a systemd user service, wraps the existing desktop module installer, and supports uninstall and status.

**Architecture:** One stdlib-only module at the repo root. The server component is new code owning its own root, manifest, config, and systemd unit; the desktop component delegates to `desktop.install` untouched. All side effects (venv build, `systemctl`, health probe) are injected so tests never touch the developer's live systemd session.

**Tech Stack:** Python 3.11+ standard library only (`argparse`, `json`, `pathlib`, `shutil`, `subprocess`, `tempfile`, `urllib`), pytest for tests, systemd user units.

## Global Constraints

- Python floor is 3.11; the development interpreter is 3.14.6.
- `install.py` and `tests/test_install.py` must be stdlib-only. No new entries in `requirements.txt`.
- Installer tests run with `python3 -m pytest tests/test_install.py -q` from the repo root (stdlib-only, needs no venv).
- The full suite runs with `.venv/bin/python -m pytest -q` and passes with no failures (469 tests at time of writing; a legitimately different total is fine as long as there are no failures).
- Tests must never invoke real `systemctl`, real `python -m venv`, or real network calls. Inject `unit_dir`, `systemctl`, `venv_builder`, and `fetch` in every test.
- Never modify `tests/test_extension_split_sentences.py` or `tests/test_media_session.py`.
- Never edit `desktop/install.py`. The desktop component is delegation only.
- New files carry the AGPL-3.0 note used in `server.py`: `Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).`
- Follow `desktop/` conventions: dependency injection for side effects, atomic writes via temp file plus `os.replace`, ownership manifests before mutating a directory.
- Server install root is `~/.local/share/free-tts-server`; the desktop root `~/.local/share/free-tts` is never touched by server code.

## Task 1: Server paths, manifest, and runtime staging

**Implementer tier:** Standard

**Files:**

- Create: `install.py`
- Test: `tests/test_install.py`

**Interfaces:**

- Consumes: nothing; this is the first task.
- Produces:
  - `server_root() -> pathlib.Path` (honours `FREE_TTS_SERVER_HOME`, then `XDG_DATA_HOME`, else `~/.local/share/free-tts-server`)
  - `systemd_user_dir() -> pathlib.Path` (honours `XDG_CONFIG_HOME`, else `~/.config`, plus `systemd/user`)
  - `checkout_root() -> pathlib.Path`
  - `read_manifest(root: pathlib.Path) -> dict | None` (tolerant; returns None on missing/corrupt/foreign)
  - `publish_runtime(source_root: pathlib.Path, root: pathlib.Path) -> bool` (True when it upgraded an existing install)
  - `read_version(source_root: pathlib.Path) -> str`
  - `manifest_path(root: pathlib.Path) -> pathlib.Path`
  - `_atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o644) -> None`
  - `InstallError`, `OwnershipError` exceptions
  - Constants `MANIFEST_NAME = "server-manifest.json"`, `COMPONENT = "server"`, `RUNTIME_ENTRIES = ("server.py", "requirements.txt", "config.example.json")`, `PRESERVED = (".venv", "config.json")`

- [ ] **Step 1: Write the failing test**

Create `tests/test_install.py`:

```python
"""Per-user server installer: paths, ownership, staging, unit, and CLI."""

import json

import pytest

import install


@pytest.fixture
def checkout(tmp_path):
    """A stand-in checkout holding the files the installer copies."""
    root = tmp_path / "checkout"
    root.mkdir()
    (root / "server.py").write_text("# server\n")
    (root / "requirements.txt").write_text("flask\n")
    (root / "config.example.json").write_text('{"port": 5000}\n')
    (root / "VERSION").write_text("2.1.0\n")
    return root


def test_server_root_prefers_explicit_home(tmp_path, monkeypatch):
    monkeypatch.setenv("FREE_TTS_SERVER_HOME", str(tmp_path / "explicit"))
    assert install.server_root() == tmp_path / "explicit"


def test_server_root_falls_back_to_xdg_data_home(tmp_path, monkeypatch):
    monkeypatch.delenv("FREE_TTS_SERVER_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "share"))
    assert install.server_root() == tmp_path / "share" / "free-tts-server"


def test_systemd_user_dir_honours_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    assert install.systemd_user_dir() == tmp_path / "config" / "systemd" / "user"


def test_publish_runtime_stages_entries(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"

    upgraded = install.publish_runtime(checkout, root)

    assert upgraded is False
    assert (root / "server.py").read_text() == "# server\n"
    assert (root / "requirements.txt").exists()
    assert (root / "config.example.json").exists()


def test_publish_runtime_refuses_unowned_root(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    root.mkdir(parents=True)
    (root / "important.txt").write_text("not ours\n")

    with pytest.raises(install.OwnershipError):
        install.publish_runtime(checkout, root)

    assert (root / "important.txt").exists()


def test_publish_runtime_preserves_venv_and_config_on_upgrade(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    install.publish_runtime(checkout, root)
    (root / install.MANIFEST_NAME).write_text(
        json.dumps({"component": "server", "root": str(root)}), encoding="utf-8"
    )
    (root / "config.json").write_text('{"port": 6000}\n')
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (checkout / "server.py").write_text("# server v2\n")

    upgraded = install.publish_runtime(checkout, root)

    assert upgraded is True
    assert (root / "server.py").read_text() == "# server v2\n"
    assert (root / "config.json").read_text() == '{"port": 6000}\n'
    assert (root / ".venv" / "bin" / "python").exists()


def test_publish_runtime_rejects_incomplete_checkout(tmp_path):
    source = tmp_path / "bare"
    source.mkdir()

    with pytest.raises(install.InstallError):
        install.publish_runtime(source, tmp_path / "share" / "free-tts-server")


def test_read_manifest_tolerates_corrupt_and_foreign(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    assert install.read_manifest(root) is None

    (root / install.MANIFEST_NAME).write_text("{not json", encoding="utf-8")
    assert install.read_manifest(root) is None

    (root / install.MANIFEST_NAME).write_text(
        json.dumps({"component": "something-else"}), encoding="utf-8"
    )
    assert install.read_manifest(root) is None


def test_read_version_falls_back_when_missing(tmp_path, checkout):
    assert install.read_version(checkout) == "2.1.0"
    bare = tmp_path / "bare"
    bare.mkdir()
    assert install.read_version(bare) == "unknown"
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'install'`.

- [ ] **Step 3: Write the minimal implementation**

Create `install.py`:

```python
#!/usr/bin/env python3
"""Per-user installer for the free-tts server and desktop module.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
See the LICENSE file at the repository root for the full license text.

The server is installed into ~/.local/share/free-tts-server with a private
virtualenv and a systemd user service. The Speech Dispatcher module is
delegated to desktop.install, which owns ~/.local/share/free-tts exclusively;
the two roots never overlap, so either component can be removed on its own.

Stdlib-only: the installer runs before any dependency exists.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import pathlib
import shutil
import tempfile

logger = logging.getLogger("free-tts.install")

MANIFEST_NAME = "server-manifest.json"
COMPONENT = "server"
RUNTIME_ENTRIES = ("server.py", "requirements.txt", "config.example.json")
PRESERVED = (".venv", "config.json")


class InstallError(Exception):
    """The install cannot proceed, with a reason safe to log."""


class OwnershipError(InstallError):
    """A target path exists and is not owned by this installer."""


def checkout_root() -> pathlib.Path:
    """The checkout this installer was run from."""
    return pathlib.Path(__file__).resolve().parent


def server_root() -> pathlib.Path:
    """Directory holding the installed server runtime."""
    explicit = os.environ.get("FREE_TTS_SERVER_HOME")
    if explicit:
        return pathlib.Path(explicit)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    return base / "free-tts-server"


def systemd_user_dir() -> pathlib.Path:
    """Where systemd looks for a user's unit files."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return base / "systemd" / "user"


def manifest_path(root: pathlib.Path) -> pathlib.Path:
    """Path of the ownership manifest inside an install root."""
    return root / MANIFEST_NAME


def read_manifest(root: pathlib.Path) -> dict | None:
    """Return our manifest, or None when missing, corrupt, or foreign."""
    path = manifest_path(root)
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or payload.get("component") != COMPONENT:
        return None
    return payload


def read_version(source_root: pathlib.Path) -> str:
    """Version recorded in the checkout, or 'unknown'."""
    path = pathlib.Path(source_root) / "VERSION"
    try:
        return path.read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o644) -> None:
    """Write a file by staging a sibling and renaming it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    handle, name = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.")
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
        os.chmod(name, mode)
        os.replace(name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name)
        raise


def _check_root_ownership(root: pathlib.Path) -> dict | None:
    """Return the owned manifest, or raise when the root is not ours."""
    if not os.path.lexists(root):
        return None
    if root.is_symlink() or not root.is_dir():
        raise OwnershipError(f"refusing to replace non-directory install root {root}")
    manifest = read_manifest(root)
    if manifest is None:
        raise OwnershipError(
            f"refusing to write into {root}: it exists without a valid "
            f"{MANIFEST_NAME}. Move it aside, then install again."
        )
    return manifest


def _stage_runtime(source_root: pathlib.Path, staging: pathlib.Path) -> None:
    for name in RUNTIME_ENTRIES:
        source = source_root / name
        if not source.is_file():
            raise InstallError(f"missing runtime entry in checkout: {source}")
        shutil.copy2(source, staging / name)


def _copy_preserved(source: pathlib.Path, target: pathlib.Path) -> None:
    if source.is_symlink():
        target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    elif source.exists():
        shutil.copy2(source, target)


def publish_runtime(source_root: pathlib.Path, root: pathlib.Path) -> bool:
    """Stage the runtime and swap it in, preserving the venv and config.

    Returns True when an existing owned install was upgraded.
    """
    source_root = pathlib.Path(source_root)
    root = pathlib.Path(root)
    existing = _check_root_ownership(root)
    root.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=".free-tts-server-stage-", dir=root.parent)
    )
    rollback: pathlib.Path | None = None
    try:
        _stage_runtime(source_root, staging)
        if existing is not None:
            for name in PRESERVED:
                _copy_preserved(root / name, staging / name)
            _copy_preserved(manifest_path(root), manifest_path(staging))
            rollback = pathlib.Path(
                tempfile.mkdtemp(prefix=".free-tts-server-rollback-", dir=root.parent)
            )
            rollback.rmdir()
            os.replace(root, rollback)
        try:
            os.replace(staging, root)
        except BaseException:
            if rollback is not None and os.path.lexists(rollback):
                os.replace(rollback, root)
                rollback = None
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if rollback is not None and os.path.lexists(rollback):
            shutil.rmtree(rollback, ignore_errors=True)
    logger.info("Published server runtime into %s", root)
    return existing is not None
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: PASS with no failures, 9 tests.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): server paths, ownership manifest, runtime staging"
```

## Task 2: Config bootstrap and systemd unit generation

**Implementer tier:** Fast

**Files:**

- Modify: `install.py`
- Test: `tests/test_install.py`

**Interfaces:**

- Consumes from Task 1, in `install.py`: `_atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o644) -> None`, constant `PRESERVED = (".venv", "config.json")`, exception `InstallError`.
- Produces:
  - `UNIT_NAME = "free-tts.service"`
  - `bootstrap_config(root: pathlib.Path) -> bool` (True when it created the file)
  - `render_unit(root: pathlib.Path, python: pathlib.Path | str | None = None) -> str`
  - `write_unit(text: str, unit_dir: pathlib.Path) -> pathlib.Path`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install.py`:

```python
def test_bootstrap_config_copies_example_once(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.example.json").write_text('{"port": 5000}\n')

    assert install.bootstrap_config(root) is True
    assert (root / "config.json").read_text() == '{"port": 5000}\n'

    (root / "config.json").write_text('{"port": 6000}\n')
    assert install.bootstrap_config(root) is False
    assert (root / "config.json").read_text() == '{"port": 6000}\n'


def test_bootstrap_config_requires_example(tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    with pytest.raises(install.InstallError):
        install.bootstrap_config(root)


def test_render_unit_embeds_paths_and_idle_warning(tmp_path):
    root = tmp_path / "share" / "free-tts-server"

    text = install.render_unit(root)

    assert f"ExecStart={root / '.venv' / 'bin' / 'python'} {root / 'server.py'}" in text
    assert f"WorkingDirectory={root}" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert "idle_timeout" in text
    assert "Environment=TTS_CONFIG" not in text


def test_render_unit_accepts_explicit_python(tmp_path):
    text = install.render_unit(tmp_path / "root", python="/usr/bin/python3")

    assert "ExecStart=/usr/bin/python3 " in text


def test_write_unit_creates_then_overwrites(tmp_path):
    unit_dir = tmp_path / "config" / "systemd" / "user"

    path = install.write_unit("[Unit]\nDescription=one\n", unit_dir)
    assert path == unit_dir / install.UNIT_NAME
    assert path.read_text() == "[Unit]\nDescription=one\n"

    install.write_unit("[Unit]\nDescription=two\n", unit_dir)
    assert path.read_text() == "[Unit]\nDescription=two\n"
    assert sorted(p.name for p in unit_dir.iterdir()) == [install.UNIT_NAME]
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: FAIL with `AttributeError: module 'install' has no attribute 'bootstrap_config'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `install.py`:

```python
UNIT_NAME = "free-tts.service"

UNIT_TEMPLATE = """\
# free-tts server. Managed by `python install.py install server`.
#
# Keep `idle_timeout` at 0 in {root}/config.json: the server arms its
# idle-shutdown watchdog only when TTS_IDLE_TIMEOUT > 0, and a persistent
# service must never exit on its own.
[Unit]
Description=free-tts local TTS server (edge-tts)
After=default.target

[Service]
Type=simple
ExecStart={python} {server}
WorkingDirectory={root}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def bootstrap_config(root: pathlib.Path) -> bool:
    """Seed config.json from the example. True when it created the file."""
    root = pathlib.Path(root)
    config = root / "config.json"
    if config.exists():
        return False
    example = root / "config.example.json"
    if not example.is_file():
        raise InstallError(f"missing config template: {example}")
    shutil.copy2(example, config)
    logger.info("Wrote default config to %s", config)
    return True


def render_unit(
    root: pathlib.Path, python: pathlib.Path | str | None = None
) -> str:
    """Render the systemd user unit for an install root."""
    root = pathlib.Path(root)
    interpreter = (
        root / ".venv" / "bin" / "python" if python is None else pathlib.Path(python)
    )
    return UNIT_TEMPLATE.format(
        python=interpreter, server=root / "server.py", root=root
    )


def write_unit(text: str, unit_dir: pathlib.Path) -> pathlib.Path:
    """Atomically install the unit file and return its path."""
    unit_dir = pathlib.Path(unit_dir)
    unit_dir.mkdir(parents=True, exist_ok=True)
    path = unit_dir / UNIT_NAME
    _atomic_write(path, text.encode("utf-8"), 0o644)
    return path
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: PASS with no failures, 14 tests.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): config bootstrap and systemd unit generation"
```

## Task 3: Pre-flight checks

**Implementer tier:** Standard

**Files:**

- Modify: `install.py`
- Test: `tests/test_install.py`

**Interfaces:**

- Consumes from Task 1, in `install.py`: exception `InstallError`. From Task 2: `UNIT_NAME = "free-tts.service"`.
- Produces:
  - `PreflightError` (subclass of `InstallError`)
  - `MIN_PYTHON = (3, 11)`, `DEFAULT_PORT = 5000`
  - `check_python(version: tuple[int, ...] | None = None) -> None`
  - `check_systemd(*, systemctl: Callable[[list[str]], subprocess.CompletedProcess]) -> None`
  - `probe_health(port: int = DEFAULT_PORT, *, fetch: Callable[[str, float], object] | None = None) -> dict | None`
  - `check_port(*, port: int = DEFAULT_PORT, force: bool = False, fetch=None, systemctl=None) -> str` returning one of `"free"`, `"ours"`, `"forced"`
  - `unit_is_active(systemctl) -> bool`

The `systemctl` callable takes a list of arguments (without the `systemctl --user` prefix) and returns an object with `returncode: int` and `stdout: str`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install.py`:

```python
class FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def fake_systemctl(responses=None, calls=None):
    """Build a systemctl double returning canned answers per first argument."""
    responses = responses or {}

    def run(args, check=True):
        if calls is not None:
            calls.append(list(args))
        return responses.get(args[0], FakeCompleted())

    return run


def test_check_python_rejects_old_interpreter():
    with pytest.raises(install.PreflightError):
        install.check_python((3, 10, 0))
    install.check_python((3, 11, 0))


def test_check_systemd_raises_with_linger_hint():
    failing = fake_systemctl({"is-system-running": FakeCompleted(returncode=4)})

    with pytest.raises(install.PreflightError) as excinfo:
        install.check_systemd(systemctl=failing)

    assert "linger" in str(excinfo.value)


def test_check_systemd_accepts_degraded_session():
    degraded = fake_systemctl(
        {"is-system-running": FakeCompleted(returncode=1, stdout="degraded\n")}
    )

    install.check_systemd(systemctl=degraded)


def test_probe_health_returns_none_when_unreachable():
    def fetch(url, timeout):
        raise OSError("connection refused")

    assert install.probe_health(fetch=fetch) is None


def test_check_port_free_when_nothing_answers():
    def fetch(url, timeout):
        raise OSError("connection refused")

    assert install.check_port(fetch=fetch, systemctl=fake_systemctl()) == "free"


def test_check_port_accepts_our_own_active_unit():
    def fetch(url, timeout):
        return {"service": "free-tts", "status": "ok"}

    active = fake_systemctl({"is-active": FakeCompleted(stdout="active\n")})

    assert install.check_port(fetch=fetch, systemctl=active) == "ours"


def test_check_port_rejects_foreign_free_tts_owner():
    def fetch(url, timeout):
        return {"service": "free-tts", "status": "ok"}

    inactive = fake_systemctl(
        {"is-active": FakeCompleted(returncode=3, stdout="inactive\n")}
    )

    with pytest.raises(install.PreflightError) as excinfo:
        install.check_port(fetch=fetch, systemctl=inactive)

    assert "--force" in str(excinfo.value)
    assert (
        install.check_port(fetch=fetch, systemctl=inactive, force=True) == "forced"
    )


def test_check_port_rejects_unrelated_service():
    def fetch(url, timeout):
        return {"service": "something-else"}

    with pytest.raises(install.PreflightError):
        install.check_port(fetch=fetch, systemctl=fake_systemctl())
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: FAIL with `AttributeError: module 'install' has no attribute 'PreflightError'`.

- [ ] **Step 3: Write the minimal implementation**

Add these imports to the import block in `install.py`: `subprocess`, `sys`, `urllib.error`, `urllib.request`, and `from collections.abc import Callable`. Then append:

```python
MIN_PYTHON = (3, 11)
DEFAULT_PORT = 5000
_PROBE_TIMEOUT = 3.0


class PreflightError(InstallError):
    """A pre-flight check failed; nothing has been changed yet."""


def default_systemctl(
    args: list[str], check: bool = True
) -> subprocess.CompletedProcess:
    """Run `systemctl --user <args>` and capture its output."""
    return subprocess.run(
        ["systemctl", "--user", *args],
        check=check,
        capture_output=True,
        text=True,
    )


def check_python(version: tuple[int, ...] | None = None) -> None:
    """Reject interpreters older than the project floor."""
    current = tuple(sys.version_info[:3]) if version is None else tuple(version)
    if current[:2] < MIN_PYTHON:
        raise PreflightError(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]}+ is required; this "
            f"interpreter is {current[0]}.{current[1]}. Re-run with a newer "
            "python3."
        )


def check_systemd(*, systemctl: Callable[..., object] | None = None) -> None:
    """Confirm a systemd user session is reachable."""
    runner = systemctl or default_systemctl
    try:
        result = runner(["is-system-running"], check=False)
    except OSError as exc:
        raise PreflightError(f"cannot run systemctl --user: {exc}") from exc
    output = str(getattr(result, "stdout", "") or "").strip()
    if output in {"running", "degraded", "starting", "maintenance", "stopping"}:
        return
    raise PreflightError(
        "no systemd user session is reachable (is-system-running said "
        f"{output or 'nothing'!r}). Log in graphically, or enable a lingering "
        "session with `loginctl enable-linger $USER`, then install again."
    )


def _fetch_json(url: str, timeout: float) -> object:
    with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310
        return json.loads(response.read().decode("utf-8"))


def probe_health(
    port: int = DEFAULT_PORT,
    *,
    fetch: Callable[[str, float], object] | None = None,
) -> dict | None:
    """Return the /health payload from a local server, or None."""
    getter = fetch or _fetch_json
    url = f"http://127.0.0.1:{port}/health"
    try:
        payload = getter(url, _PROBE_TIMEOUT)
    except (OSError, urllib.error.URLError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def unit_is_active(systemctl: Callable[..., object] | None = None) -> bool:
    """True when our own unit is the active service."""
    runner = systemctl or default_systemctl
    try:
        result = runner(["is-active", UNIT_NAME], check=False)
    except OSError:
        return False
    return str(getattr(result, "stdout", "") or "").strip() == "active"


def check_port(
    *,
    port: int = DEFAULT_PORT,
    force: bool = False,
    fetch: Callable[[str, float], object] | None = None,
    systemctl: Callable[..., object] | None = None,
) -> str:
    """Classify who owns the server port before we bind it."""
    payload = probe_health(port, fetch=fetch)
    if payload is None:
        return "free"
    if payload.get("service") == "free-tts":
        if unit_is_active(systemctl):
            return "ours"
        if force:
            return "forced"
        raise PreflightError(
            f"port {port} already serves free-tts, but not through "
            f"{UNIT_NAME}. Another supervisor probably owns it; stop that "
            "owner first, or pass --force to install anyway."
        )
    if force:
        return "forced"
    raise PreflightError(
        f"port {port} is already used by another service; free the port or "
        "pass --force to install anyway."
    )
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: PASS with no failures, 22 tests.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): pre-flight python, systemd, and port checks"
```

## Task 4: Server install and uninstall orchestration

**Implementer tier:** Standard

**Files:**

- Modify: `install.py`
- Test: `tests/test_install.py`

**Interfaces:**

- Consumes from earlier tasks, all in `install.py`:
  - `server_root()`, `systemd_user_dir()`, `checkout_root()`, `read_manifest(root)`, `read_version(source_root)`, `publish_runtime(source_root, root) -> bool`, `manifest_path(root)`, `_atomic_write(path, data, mode=0o644)`, `MANIFEST_NAME`, `COMPONENT = "server"`, `InstallError`, `OwnershipError`
  - `bootstrap_config(root) -> bool`, `render_unit(root, python=None) -> str`, `write_unit(text, unit_dir) -> pathlib.Path`, `UNIT_NAME`
  - `check_python()`, `check_systemd(*, systemctl)`, `check_port(*, port, force, fetch, systemctl) -> str`, `default_systemctl(args, check=True)`, `DEFAULT_PORT`
- Consumes from the existing `tests/test_install.py`, already written by earlier tasks: the `checkout` fixture (a `tmp_path` checkout holding `server.py`, `requirements.txt`, `config.example.json`, and `VERSION` containing `2.1.0`), the `FakeCompleted(returncode=0, stdout="")` class, and the `fake_systemctl(responses=None, calls=None)` helper whose returned callable has signature `run(args: list[str], check: bool = True)` and appends each `args` list to `calls`. Do not redefine them; append the new tests below the existing ones.
- Produces:
  - `default_venv_builder(root: pathlib.Path) -> None`
  - `install_server(source_root=None, *, root=None, unit_dir=None, venv_builder=None, systemctl=None, fetch=None, force=False, preflight=None) -> dict`
  - `uninstall_server(*, root=None, unit_dir=None, systemctl=None) -> list[str]`

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install.py`:

```python
def _install_server(checkout, tmp_path, *, calls=None, force=False, venv=None):
    """Install with every side effect injected."""
    def venv_builder(root):
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        if venv is not None:
            venv.append(root)

    return install.install_server(
        checkout,
        root=tmp_path / "share" / "free-tts-server",
        unit_dir=tmp_path / "config" / "systemd" / "user",
        venv_builder=venv_builder,
        systemctl=fake_systemctl(calls=calls),
        preflight=lambda: None,
        force=force,
    )


def test_install_server_writes_manifest_config_and_unit(checkout, tmp_path):
    calls = []
    venv = []

    manifest = _install_server(checkout, tmp_path, calls=calls, venv=venv)

    root = tmp_path / "share" / "free-tts-server"
    unit = tmp_path / "config" / "systemd" / "user" / install.UNIT_NAME
    assert manifest["component"] == "server"
    assert manifest["root"] == str(root)
    assert manifest["unit"] == str(unit)
    assert manifest["version"] == "2.1.0"
    assert install.read_manifest(root) == manifest
    assert (root / "config.json").exists()
    assert str(root) in unit.read_text()
    assert venv == [root]
    assert ["daemon-reload"] in calls
    assert ["enable", install.UNIT_NAME] in calls
    assert ["restart", install.UNIT_NAME] in calls


def test_install_server_reinstall_preserves_config_and_venv(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    (root / "config.json").write_text('{"port": 6000}\n')
    venv = []

    _install_server(checkout, tmp_path, venv=venv)

    assert (root / "config.json").read_text() == '{"port": 6000}\n'
    assert venv == []


def test_install_server_runs_preflight_before_touching_disk(checkout, tmp_path):
    def preflight():
        raise install.PreflightError("nope")

    with pytest.raises(install.PreflightError):
        install.install_server(
            checkout,
            root=tmp_path / "share" / "free-tts-server",
            unit_dir=tmp_path / "config" / "systemd" / "user",
            venv_builder=lambda root: None,
            systemctl=fake_systemctl(),
            preflight=preflight,
        )

    assert not (tmp_path / "share").exists()


def test_uninstall_server_removes_unit_and_root(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    calls = []

    removed = install.uninstall_server(
        root=tmp_path / "share" / "free-tts-server",
        unit_dir=tmp_path / "config" / "systemd" / "user",
        systemctl=fake_systemctl(calls=calls),
    )

    root = tmp_path / "share" / "free-tts-server"
    unit = tmp_path / "config" / "systemd" / "user" / install.UNIT_NAME
    assert str(root) in removed
    assert str(unit) in removed
    assert not root.exists()
    assert not unit.exists()
    assert ["disable", "--now", install.UNIT_NAME] in calls


def test_uninstall_server_is_idempotent(tmp_path):
    removed = install.uninstall_server(
        root=tmp_path / "missing",
        unit_dir=tmp_path / "config" / "systemd" / "user",
        systemctl=fake_systemctl(),
    )

    assert removed == []


def test_uninstall_server_keeps_unowned_root(tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    root.mkdir(parents=True)
    (root / "important.txt").write_text("not ours\n")

    removed = install.uninstall_server(
        root=root,
        unit_dir=tmp_path / "config" / "systemd" / "user",
        systemctl=fake_systemctl(),
    )

    assert removed == []
    assert (root / "important.txt").exists()
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: FAIL with `AttributeError: module 'install' has no attribute 'install_server'`.

- [ ] **Step 3: Write the minimal implementation**

Append to `install.py`:

```python
def default_venv_builder(root: pathlib.Path) -> None:
    """Create the private virtualenv and install the server requirements."""
    venv = pathlib.Path(root) / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run(
        [
            str(venv / "bin" / "pip"),
            "install",
            "--quiet",
            "-r",
            str(pathlib.Path(root) / "requirements.txt"),
        ],
        check=True,
    )


def install_server(
    source_root: pathlib.Path | None = None,
    *,
    root: pathlib.Path | None = None,
    unit_dir: pathlib.Path | None = None,
    venv_builder: Callable[[pathlib.Path], None] | None = None,
    systemctl: Callable[..., object] | None = None,
    fetch: Callable[[str, float], object] | None = None,
    force: bool = False,
    preflight: Callable[[], None] | None = None,
) -> dict:
    """Install or refresh the server plus its systemd user service."""
    source_root = checkout_root() if source_root is None else pathlib.Path(source_root)
    root = server_root() if root is None else pathlib.Path(root)
    unit_dir = systemd_user_dir() if unit_dir is None else pathlib.Path(unit_dir)
    build_venv = venv_builder or default_venv_builder
    runner = systemctl or default_systemctl

    def _default_preflight() -> None:
        check_python()
        check_systemd(systemctl=runner)
        check_port(force=force, fetch=fetch, systemctl=runner)

    (preflight or _default_preflight)()

    publish_runtime(source_root, root)
    if not (root / ".venv").exists():
        build_venv(root)
    bootstrap_config(root)
    unit_path = write_unit(render_unit(root), unit_dir)
    manifest = {
        "component": COMPONENT,
        "root": str(root),
        "unit": str(unit_path),
        "config": str(root / "config.json"),
        "python": str(root / ".venv" / "bin" / "python"),
        "version": read_version(source_root),
    }
    _atomic_write(
        manifest_path(root),
        json.dumps(manifest, indent=2).encode("utf-8"),
        0o644,
    )
    runner(["daemon-reload"])
    runner(["enable", UNIT_NAME])
    runner(["restart", UNIT_NAME])
    logger.info("Installed free-tts server into %s", root)
    return manifest


def uninstall_server(
    *,
    root: pathlib.Path | None = None,
    unit_dir: pathlib.Path | None = None,
    systemctl: Callable[..., object] | None = None,
) -> list[str]:
    """Remove the service and the server root. Returns removed paths."""
    root = server_root() if root is None else pathlib.Path(root)
    unit_dir = systemd_user_dir() if unit_dir is None else pathlib.Path(unit_dir)
    runner = systemctl or default_systemctl
    removed: list[str] = []

    unit_path = unit_dir / UNIT_NAME
    if os.path.lexists(unit_path):
        runner(["disable", "--now", UNIT_NAME], check=False)
        unit_path.unlink()
        removed.append(str(unit_path))
        runner(["daemon-reload"], check=False)

    if not os.path.lexists(root):
        return removed
    if read_manifest(root) is None:
        logger.warning(
            "No valid %s at %s; leaving the directory untouched.",
            MANIFEST_NAME,
            root,
        )
        return removed
    shutil.rmtree(root)
    removed.append(str(root))
    return removed
```

- [ ] **Step 4: Run the test and confirm it passes**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: PASS with no failures, 28 tests.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): server install and uninstall orchestration"
```

## Task 5: CLI dispatch, desktop delegation, and status

**Implementer tier:** Advanced

**Files:**

- Modify: `install.py`
- Test: `tests/test_install.py`

**Interfaces:**

- Consumes from earlier tasks, all in `install.py`: `install_server(source_root=None, *, root=None, unit_dir=None, venv_builder=None, systemctl=None, fetch=None, force=False, preflight=None) -> dict`, `uninstall_server(*, root=None, unit_dir=None, systemctl=None) -> list[str]`, `read_manifest(root) -> dict | None`, `server_root()`, `systemd_user_dir()`, `checkout_root()`, `default_systemctl(args, check=True)`, `unit_is_active(systemctl)`, `UNIT_NAME`, `InstallError`.
- Consumes from the existing untouched module `desktop/install.py`: `install(source_root: pathlib.Path, *, root=None, launcher=None, config_dir=None, venv_builder=None, preflight=None) -> dict[str, object]`, `uninstall(*, root=None, launcher=None, config_dir=None) -> list[str]`, `restart_speech_dispatcher(*, opener=..., sender=..., identity=...) -> bool`, and `install_root() -> pathlib.Path` from `desktop/backend.py`.
- Produces:
  - `install_desktop(source_root=None, *, delegate=None) -> dict`
  - `uninstall_desktop(*, delegate=None) -> list[str]`
  - `status(*, root=None, desktop_root=None, unit_dir=None, systemctl=None) -> dict`
  - `main(argv: list[str] | None = None) -> int`
- Consumes from the existing `tests/test_install.py`, already written by earlier tasks: the `checkout` fixture, the `FakeCompleted(returncode=0, stdout="")` class, the `fake_systemctl(responses=None, calls=None)` helper, and `_install_server(checkout, tmp_path, *, calls=None, force=False, venv=None)` which installs into `tmp_path / "share" / "free-tts-server"` with `unit_dir=tmp_path / "config" / "systemd" / "user"` and every side effect injected. Do not redefine them; append the new tests below the existing ones.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_install.py`:

```python
class FakeDesktop:
    """Stand-in for the desktop.install module."""

    def __init__(self):
        self.calls = []

    def install(self, source_root, **kwargs):
        self.calls.append("install")
        return {"module": "free-tts", "root": str(source_root)}

    def uninstall(self, **kwargs):
        self.calls.append("uninstall")
        return ["/removed/path"]

    def restart_speech_dispatcher(self):
        self.calls.append("restart")
        return True


def test_install_desktop_restarts_speech_dispatcher(checkout):
    delegate = FakeDesktop()

    manifest = install.install_desktop(checkout, delegate=delegate)

    assert manifest["module"] == "free-tts"
    assert delegate.calls == ["install", "restart"]


def test_uninstall_desktop_restarts_speech_dispatcher():
    delegate = FakeDesktop()

    removed = install.uninstall_desktop(delegate=delegate)

    assert removed == ["/removed/path"]
    assert delegate.calls == ["uninstall", "restart"]


def test_status_reports_both_components(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    desktop_root = tmp_path / "share" / "free-tts"
    desktop_root.mkdir(parents=True)
    (desktop_root / "install-manifest.json").write_text(
        json.dumps({"module": "free-tts", "root": str(desktop_root)}),
        encoding="utf-8",
    )

    report = install.status(
        root=tmp_path / "share" / "free-tts-server",
        desktop_root=desktop_root,
        unit_dir=tmp_path / "config" / "systemd" / "user",
        systemctl=fake_systemctl(
            {
                "is-active": FakeCompleted(stdout="active\n"),
                "is-enabled": FakeCompleted(stdout="enabled\n"),
            }
        ),
    )

    assert report["server"]["installed"] is True
    assert report["server"]["version"] == "2.1.0"
    assert report["server"]["active"] == "active"
    assert report["server"]["enabled"] == "enabled"
    assert report["desktop"]["installed"] is True


def test_status_tolerates_corrupt_desktop_manifest(tmp_path):
    desktop_root = tmp_path / "share" / "free-tts"
    desktop_root.mkdir(parents=True)
    (desktop_root / "install-manifest.json").write_text("{broken", encoding="utf-8")

    report = install.status(
        root=tmp_path / "missing",
        desktop_root=desktop_root,
        unit_dir=tmp_path / "config" / "systemd" / "user",
        systemctl=fake_systemctl(),
    )

    assert report["server"]["installed"] is False
    assert report["desktop"]["installed"] is False


def test_main_rejects_unknown_command(capsys):
    assert install.main(["frobnicate"]) == 2


def test_main_status_prints_a_report(checkout, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        install, "status", lambda **kwargs: {
            "server": {"installed": False},
            "desktop": {"installed": False},
        }
    )

    assert install.main(["status"]) == 0
    assert "server" in capsys.readouterr().out


def test_main_install_all_reports_each_component(monkeypatch, capsys):
    performed = []
    monkeypatch.setattr(
        install,
        "install_server",
        lambda **kwargs: performed.append("server") or {"root": "/srv"},
    )

    def failing_desktop(**kwargs):
        performed.append("desktop")
        raise install.InstallError("speech-dispatcher is missing")

    monkeypatch.setattr(install, "install_desktop", failing_desktop)

    code = install.main(["install", "all"])

    assert performed == ["server", "desktop"]
    assert code == 1
    output = capsys.readouterr().out
    assert "speech-dispatcher is missing" in output


def test_main_install_server_passes_force(monkeypatch):
    seen = {}
    monkeypatch.setattr(
        install,
        "install_server",
        lambda **kwargs: seen.update(kwargs) or {"root": "/srv"},
    )

    assert install.main(["install", "server", "--force"]) == 0
    assert seen["force"] is True
```

- [ ] **Step 2: Run the test and confirm it fails**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: FAIL with `AttributeError: module 'install' has no attribute 'install_desktop'`.

- [ ] **Step 3: Write the minimal implementation**

Add `import argparse` to the import block in `install.py`, then append:

```python
DESKTOP_MANIFEST_NAME = "install-manifest.json"


def _desktop_delegate():
    """Import the desktop installer lazily so the CLI works without it."""
    from desktop import install as desktop_install

    return desktop_install


def install_desktop(
    source_root: pathlib.Path | None = None, *, delegate: object | None = None
) -> dict:
    """Install the Speech Dispatcher module and reload speechd."""
    module = delegate or _desktop_delegate()
    source_root = checkout_root() if source_root is None else pathlib.Path(source_root)
    manifest = module.install(source_root)
    module.restart_speech_dispatcher()
    return dict(manifest)


def uninstall_desktop(*, delegate: object | None = None) -> list[str]:
    """Remove the Speech Dispatcher module and reload speechd."""
    module = delegate or _desktop_delegate()
    removed = list(module.uninstall())
    module.restart_speech_dispatcher()
    return removed


def _desktop_root() -> pathlib.Path:
    from desktop.backend import install_root

    return install_root()


def _read_json(path: pathlib.Path) -> dict | None:
    """Read a JSON object tolerantly; None on any problem."""
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def status(
    *,
    root: pathlib.Path | None = None,
    desktop_root: pathlib.Path | None = None,
    unit_dir: pathlib.Path | None = None,
    systemctl: Callable[..., object] | None = None,
) -> dict:
    """Describe what is installed, without raising on foreign state."""
    root = server_root() if root is None else pathlib.Path(root)
    unit_dir = systemd_user_dir() if unit_dir is None else pathlib.Path(unit_dir)
    runner = systemctl or default_systemctl
    try:
        desktop_dir = (
            _desktop_root() if desktop_root is None else pathlib.Path(desktop_root)
        )
    except ImportError:
        desktop_dir = pathlib.Path.home() / ".local" / "share" / "free-tts"

    manifest = read_manifest(root)
    server: dict[str, object] = {
        "installed": manifest is not None,
        "root": str(root),
        "unit": str(unit_dir / UNIT_NAME),
        "version": manifest.get("version") if manifest else None,
    }
    for field, args in (
        ("active", ["is-active", UNIT_NAME]),
        ("enabled", ["is-enabled", UNIT_NAME]),
    ):
        try:
            result = runner(args, check=False)
            server[field] = str(getattr(result, "stdout", "") or "").strip()
        except OSError as exc:
            server[field] = f"unknown ({exc})"

    desktop_manifest = _read_json(desktop_dir / DESKTOP_MANIFEST_NAME)
    desktop = {
        "installed": bool(
            desktop_manifest and desktop_manifest.get("module") == "free-tts"
        ),
        "root": str(desktop_dir),
    }
    return {"server": server, "desktop": desktop}


def _print_status(report: dict) -> None:
    for name in ("server", "desktop"):
        section = report[name]
        state = "installed" if section.get("installed") else "not installed"
        print(f"{name:8} {state} root={section.get('root')}")
        if name == "server" and section.get("installed"):
            print(
                f"         version={section.get('version')} "
                f"unit={section.get('active')}/{section.get('enabled')}"
            )


def main(argv: list[str] | None = None) -> int:
    """``python install.py install|uninstall|status [server|desktop|all]``."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(
        prog="install.py",
        description="Install the free-tts server and desktop TTS module.",
    )
    parser.add_argument("command", choices=("install", "uninstall", "status"))
    parser.add_argument(
        "component",
        nargs="?",
        default="all",
        choices=("server", "desktop", "all"),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="install even when another service already owns the port",
    )
    try:
        args = parser.parse_args(argv)
    except SystemExit:
        return 2

    if args.command == "status":
        _print_status(status())
        return 0

    components = (
        ("server", "desktop") if args.component == "all" else (args.component,)
    )
    failures = 0
    for component in components:
        try:
            if args.command == "install":
                if component == "server":
                    manifest = install_server(force=args.force)
                    print(f"Installed server into {manifest['root']}")
                else:
                    manifest = install_desktop()
                    print(f"Installed desktop module into {manifest['root']}")
            else:
                removed = (
                    uninstall_server()
                    if component == "server"
                    else uninstall_desktop()
                )
                for path in removed:
                    print(f"Removed {path}")
                if not removed:
                    print(f"Nothing to remove for {component}")
        except (InstallError, OSError, ImportError, subprocess.SubprocessError) as exc:
            failures += 1
            print(f"{component}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Run the tests and confirm they pass**

Run: `python3 -m pytest tests/test_install.py -q`
Expected: PASS with no failures, 36 tests.

Run: `.venv/bin/python -m pytest -q`
Expected: PASS with no failures (469 tests at time of writing, plus the new installer tests). If `.venv` is absent, create it first with `python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt pytest`.

- [ ] **Step 5: Commit**

```bash
git add install.py tests/test_install.py
git commit -m "feat(install): CLI dispatch, desktop delegation, and status"
```

## Task 6: Document the installer

**Implementer tier:** Fast

**Files:**

- Modify: `README.md`
- Modify: `docs/desktop-tts.md`

**Interfaces:**

- Consumes from Task 5, in `install.py`: the CLI `python install.py install|uninstall|status [server|desktop|all]` with a `--force` flag.
- Produces: no code. Documentation only.

- [ ] **Step 1: Add the Installation section to README.md**

Insert this section immediately before the existing `## Quick Start` heading in `README.md`:

````markdown
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
directory it does not own, and it checks before installing that the interpreter
is Python 3.11+, that a systemd user session is reachable, and that port 5000
is free or already served by its own unit. Pass `--force` to install anyway
when another service holds the port.

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
````

- [ ] **Step 2: Note the unified entry point in docs/desktop-tts.md**

Add this paragraph directly below the top-level heading of `docs/desktop-tts.md`:

```markdown
The commands below install the Speech Dispatcher module on its own. The
repo-root installer wraps them as `python install.py install desktop` (and
`python install.py install all` to add the server's systemd user service);
both routes call the same code and reload Speech Dispatcher afterwards.
```

- [ ] **Step 3: Verify the docs match the shipped CLI**

Run: `python3 install.py --help`
Expected: usage line listing `{install,uninstall,status}`, the optional
`{server,desktop,all}` component, and `--force`, matching the README text.

Run: `python3 -m pytest tests/test_install.py -q`
Expected: PASS with no failures, 36 tests (docs changes must not alter behavior).

- [ ] **Step 4: Commit**

```bash
git add README.md docs/desktop-tts.md
git commit -m "docs: document the per-user installer and systemd service"
```
