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

from collections.abc import Callable
import argparse
import contextlib
import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request

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
    root_in_place = False
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
            root_in_place = True
        except BaseException:
            if rollback is not None and os.path.lexists(rollback):
                try:
                    os.replace(rollback, root)
                except BaseException as restore_error:
                    logger.error(
                        "publish failed and the previous install could not be "
                        "restored; it is kept at %s",
                        rollback,
                    )
                    raise InstallError(
                        f"publish failed and the previous install could not be "
                        f"restored; it is kept at {rollback}"
                    ) from restore_error
                rollback = None
                root_in_place = True
            raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        if root_in_place and rollback is not None and os.path.lexists(rollback):
            shutil.rmtree(rollback, ignore_errors=True)
    logger.info("Published server runtime into %s", root)
    return existing is not None


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
