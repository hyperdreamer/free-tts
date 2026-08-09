"""Per-user installer for the free-tts Speech Dispatcher module.

Installs into ~/.local and ~/.config only. Runtime files are staged and swapped,
the private virtualenv is created at its final path, and the user's speechd.conf
is edited only inside a marked block that uninstall can remove exactly.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable

from desktop.backend import install_root
from desktop.speechd_config import apply_managed_block, remove_managed_block

logger = logging.getLogger("free-tts.install")

MODULE_NAME = "free-tts"
LAUNCHER_NAME = "sd_free-tts"
MODULE_CONF_NAME = "free-tts.conf"
MANIFEST_NAME = "install-manifest.json"
RUNTIME_ENTRIES = ("server.py", "requirements.txt", "config.example.json", "desktop")
_PRESERVED = (".venv", "config.json", MANIFEST_NAME)

_LAUNCHER_TEMPLATE = """#!/bin/sh
# Managed by free-tts install. Launched by Speech Dispatcher as: {launcher} <conf>
FREE_TTS_HOME={root}
export FREE_TTS_HOME
PYTHONPATH={root}
export PYTHONPATH
exec {python} -m desktop.module "$@"
"""


def launcher_dir() -> pathlib.Path:
    """Where Speech Dispatcher looks for a user's module binaries."""
    return pathlib.Path.home() / ".local" / "libexec" / "speech-dispatcher-modules"


def speechd_config_dir() -> pathlib.Path:
    """The user's Speech Dispatcher configuration directory."""
    xdg = os.environ.get("XDG_CONFIG_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".config"
    return base / "speech-dispatcher"


def manifest_path() -> pathlib.Path:
    """Where the install manifest lives."""
    return install_root() / MANIFEST_NAME


def _default_venv_builder(root: pathlib.Path) -> None:
    """Create the private virtualenv and install the server's requirements."""
    venv = root / ".venv"
    subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
    subprocess.run(
        [
            str(venv / "bin" / "pip"),
            "install",
            "--quiet",
            "-r",
            str(root / "requirements.txt"),
        ],
        check=True,
    )


def _stage_runtime(source_root: pathlib.Path, staging: pathlib.Path) -> None:
    for name in RUNTIME_ENTRIES:
        source = source_root / name
        if not source.exists():
            raise FileNotFoundError(f"missing runtime entry in checkout: {source}")
        target = staging / name
        if source.is_dir():
            shutil.copytree(
                source,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
            )
        else:
            shutil.copy2(source, target)


def install(
    source_root: pathlib.Path,
    *,
    root: pathlib.Path | None = None,
    launcher: pathlib.Path | None = None,
    config_dir: pathlib.Path | None = None,
    venv_builder: Callable[[pathlib.Path], None] | None = None,
) -> dict[str, str]:
    """Install or upgrade the per-user integration. Returns the manifest."""
    source_root = pathlib.Path(source_root)
    root = install_root() if root is None else pathlib.Path(root)
    launcher_directory = launcher_dir() if launcher is None else pathlib.Path(launcher)
    config_directory = (
        speechd_config_dir() if config_dir is None else pathlib.Path(config_dir)
    )
    build_venv = venv_builder or _default_venv_builder

    root.parent.mkdir(parents=True, exist_ok=True)
    staging = pathlib.Path(tempfile.mkdtemp(prefix=".free-tts-stage-", dir=root.parent))
    try:
        _stage_runtime(source_root, staging)
        for name in _PRESERVED:
            existing = root / name
            if existing.exists():
                shutil.move(str(existing), str(staging / name))
        if root.exists():
            shutil.rmtree(root)
        shutil.move(str(staging), str(root))
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    if not (root / ".venv").exists():
        build_venv(root)

    venv_python = root / ".venv" / "bin" / "python"
    python = str(venv_python) if venv_python.exists() else sys.executable
    launcher_directory.mkdir(parents=True, exist_ok=True)
    launcher_file = launcher_directory / LAUNCHER_NAME
    launcher_file.write_text(
        _LAUNCHER_TEMPLATE.format(
            launcher=LAUNCHER_NAME, root=str(root), python=python
        ),
        encoding="utf-8",
    )
    launcher_file.chmod(0o755)

    modules_dir = config_directory / "modules"
    modules_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy2(root / "desktop" / MODULE_CONF_NAME, modules_dir / MODULE_CONF_NAME)

    speechd_conf = config_directory / "speechd.conf"
    original = speechd_conf.read_text(encoding="utf-8") if speechd_conf.is_file() else ""
    backup = config_directory / "speechd.conf.free-tts.bak"
    if original and not backup.exists():
        backup.write_text(original, encoding="utf-8")
    speechd_conf.write_text(
        apply_managed_block(original, LAUNCHER_NAME, MODULE_CONF_NAME),
        encoding="utf-8",
    )

    manifest = {
        "module": MODULE_NAME,
        "root": str(root),
        "launcher": str(launcher_file),
        "module_conf": str(modules_dir / MODULE_CONF_NAME),
        "speechd_conf": str(speechd_conf),
    }
    (root / MANIFEST_NAME).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    logger.info("Installed free-tts module into %s", root)
    return manifest


def uninstall(
    *,
    root: pathlib.Path | None = None,
    launcher: pathlib.Path | None = None,
    config_dir: pathlib.Path | None = None,
) -> list[str]:
    """Remove only what install created. Returns the paths actually removed."""
    root = install_root() if root is None else pathlib.Path(root)
    launcher_directory = launcher_dir() if launcher is None else pathlib.Path(launcher)
    config_directory = (
        speechd_config_dir() if config_dir is None else pathlib.Path(config_dir)
    )

    removed: list[str] = []
    speechd_conf = config_directory / "speechd.conf"
    if speechd_conf.is_file():
        current = speechd_conf.read_text(encoding="utf-8")
        cleaned = remove_managed_block(current)
        if cleaned != current:
            speechd_conf.write_text(cleaned, encoding="utf-8")
            removed.append(str(speechd_conf))

    module_conf = config_directory / "modules" / MODULE_CONF_NAME
    if module_conf.is_file():
        module_conf.unlink()
        removed.append(str(module_conf))

    launcher_file = launcher_directory / LAUNCHER_NAME
    if launcher_file.exists():
        launcher_file.unlink()
        removed.append(str(launcher_file))

    if root.exists():
        shutil.rmtree(root)
        removed.append(str(root))

    return removed


def restart_speech_dispatcher(runner: Callable[..., object] = subprocess.run) -> None:
    """Ask the user's Speech Dispatcher to exit so it reloads configuration."""
    try:
        runner(
            ["pkill", "-u", str(os.getuid()), "-x", "speech-dispatcher"],
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        logger.warning("Could not restart speech-dispatcher automatically: %s", exc)


def main(argv: list[str] | None = None) -> int:
    """``python -m desktop.install [install|uninstall]``."""
    logging.basicConfig(stream=sys.stderr, level=logging.INFO, format="%(message)s")
    args = list(sys.argv[1:] if argv is None else argv)
    action = args[0] if args else "install"
    if action == "install":
        manifest = install(pathlib.Path(__file__).resolve().parent.parent)
        restart_speech_dispatcher()
        print(f"Installed free-tts into {manifest['root']}")
        print("Restart any open Qt applications so they reload the voice list.")
        return 0
    if action == "uninstall":
        removed = uninstall()
        restart_speech_dispatcher()
        for path in removed:
            print(f"Removed {path}")
        return 0
    print(f"Unknown action {action!r}; use install or uninstall.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
