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
