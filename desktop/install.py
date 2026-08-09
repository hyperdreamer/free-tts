"""Per-user installer for the free-tts Speech Dispatcher module.

Installs into ~/.local and ~/.config only. Runtime files are staged and swapped,
the private virtualenv is created at its final path, and the user's speechd.conf
is edited only inside a marked block that uninstall can remove exactly.
"""

from __future__ import annotations

import ctypes.util
import json
import logging
import os
import pathlib
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
from collections.abc import Callable
from dataclasses import dataclass

from desktop.backend import install_root
from desktop.speechd_config import (
    BEGIN_MARKER,
    apply_managed_block,
    remove_managed_block,
)

logger = logging.getLogger("free-tts.install")

MODULE_NAME = "free-tts"
LAUNCHER_NAME = "sd_free-tts"
MODULE_CONF_NAME = "free-tts.conf"
MANIFEST_NAME = "install-manifest.json"
RUNTIME_ENTRIES = ("server.py", "requirements.txt", "config.example.json", "desktop")
_PRESERVED = (".venv", "config.json")

SPEECHD_PROCESS_NAMES = ("speech-dispatcher",)

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


class InstallError(RuntimeError):
    """Base class for actionable installer failures."""


class InstallOwnershipError(InstallError):
    """Raised when existing paths cannot be proven to belong to free-tts."""


class PrerequisiteError(InstallError):
    """Raised when the desktop integration cannot work on this host."""


def check_prerequisites(
    *,
    version_info: object = sys.version_info,
    which: Callable[[str], str | None] = shutil.which,
    library_finder: Callable[[str], str | None] = ctypes.util.find_library,
    runner: Callable[..., object] = subprocess.run,
) -> None:
    """Validate desktop prerequisites without mutating install targets."""
    version = tuple(version_info[:2])  # type: ignore[index]
    errors: list[str] = []
    if version < (3, 11):
        errors.append(
            f"Python 3.11 or newer is required (found {version[0]}.{version[1]})."
        )

    probes = (
        ("speech-dispatcher", "--version"),
        ("ffmpeg", "-version"),
    )
    for executable, version_arg in probes:
        path = which(executable)
        if path is None:
            errors.append(
                f"{executable} is required; install it with your system "
                "package manager."
            )
            continue
        try:
            result = runner(
                [path, version_arg], capture_output=True, check=False
            )
        except OSError as exc:
            errors.append(f"{executable} could not be run: {exc}.")
            continue
        if getattr(result, "returncode", 1) != 0:
            errors.append(f"{executable} failed its version check.")

    if library_finder("speechd") is None:
        errors.append(
            "libspeechd is required for Qt/Speech Dispatcher integration; "
            "install your distribution's libspeechd package."
        )

    if errors:
        detail = "\n- ".join(errors)
        raise PrerequisiteError(
            f"Desktop TTS prerequisites are missing:\n- {detail}"
        )


def _expected_manifest(
    root: pathlib.Path,
    launcher_directory: pathlib.Path,
    config_directory: pathlib.Path,
) -> dict[str, str]:
    return {
        "module": MODULE_NAME,
        "root": str(root),
        "launcher": str(launcher_directory / LAUNCHER_NAME),
        "module_conf": str(
            config_directory / "modules" / MODULE_CONF_NAME
        ),
        "speechd_conf": str(config_directory / "speechd.conf"),
    }


def _canonical(path: pathlib.Path) -> pathlib.Path:
    return path.expanduser().resolve(strict=False)


def _validate_manifest(
    payload: object, expected: dict[str, str]
) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise InstallOwnershipError("install manifest is not a JSON object")
    if payload.get("module") != MODULE_NAME:
        raise InstallOwnershipError("install manifest has the wrong module owner")
    for key in ("root", "launcher", "module_conf", "speechd_conf"):
        recorded = payload.get(key)
        if not isinstance(recorded, str):
            raise InstallOwnershipError(f"install manifest is missing {key!r}")
        if _canonical(pathlib.Path(recorded)) != _canonical(
            pathlib.Path(expected[key])
        ):
            raise InstallOwnershipError(
                f"install manifest path {key!r} is outside the expected user target"
            )

    validated: dict[str, object] = {
        key: str(payload[key]) for key in expected
    }
    has_existence = "speechd_conf_existed" in payload
    has_mode = "speechd_conf_mode" in payload
    if has_existence != has_mode:
        raise InstallOwnershipError(
            "install manifest has incomplete speechd.conf provenance"
        )
    if has_existence:
        existed = payload["speechd_conf_existed"]
        mode = payload["speechd_conf_mode"]
        if not isinstance(existed, bool):
            raise InstallOwnershipError(
                "install manifest has invalid speechd.conf existence provenance"
            )
        if existed:
            if (
                isinstance(mode, bool)
                or not isinstance(mode, int)
                or not 0 <= mode <= 0o7777
            ):
                raise InstallOwnershipError(
                    "install manifest has invalid speechd.conf mode provenance"
                )
        elif mode is not None:
            raise InstallOwnershipError(
                "install manifest records a mode for a previously missing speechd.conf"
            )
        validated["speechd_conf_existed"] = existed
        validated["speechd_conf_mode"] = mode
    return validated


def _load_manifest(
    root: pathlib.Path,
    expected: dict[str, str],
    *,
    missing_ok: bool,
) -> dict[str, object] | None:
    path = root / MANIFEST_NAME
    if not os.path.lexists(path):
        if missing_ok:
            return None
        raise InstallOwnershipError(
            f"cannot establish ownership: install manifest is missing at {path}"
        )
    if root.is_symlink() or path.is_symlink() or not path.is_file():
        raise InstallOwnershipError("install manifest must be a regular owned file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise InstallOwnershipError(f"install manifest is corrupt: {exc}") from exc
    return _validate_manifest(payload, expected)


def _check_install_ownership(
    root: pathlib.Path,
    expected: dict[str, str],
) -> dict[str, object] | None:
    root_exists = os.path.lexists(root)
    if root_exists:
        return _load_manifest(root, expected, missing_ok=False)

    for key in ("launcher", "module_conf"):
        path = pathlib.Path(expected[key])
        if os.path.lexists(path):
            raise InstallOwnershipError(
                f"refusing to overwrite unowned collision at {path}"
            )
    speechd_conf = pathlib.Path(expected["speechd_conf"])
    if speechd_conf.is_file() and BEGIN_MARKER in speechd_conf.read_text(
        encoding="utf-8"
    ):
        raise InstallOwnershipError(
            "managed Speech Dispatcher block exists without an ownership "
            f"manifest at {speechd_conf}"
        )
    return None


def _copy_preserved(source: pathlib.Path, target: pathlib.Path) -> None:
    if source.is_symlink():
        target.symlink_to(os.readlink(source), target_is_directory=source.is_dir())
    elif source.is_dir():
        shutil.copytree(source, target, symlinks=True)
    elif source.exists():
        shutil.copy2(source, target)


@dataclass(frozen=True)
class _PathSnapshot:
    kind: str
    data: bytes | str | None = None
    mode: int = 0


def _snapshot_path(path: pathlib.Path) -> _PathSnapshot:
    if path.is_symlink():
        return _PathSnapshot("symlink", os.readlink(path))
    if path.is_file():
        return _PathSnapshot(
            "file", path.read_bytes(), stat.S_IMODE(path.stat().st_mode)
        )
    if path.exists():
        raise InstallOwnershipError(f"owned file path became a directory: {path}")
    return _PathSnapshot("missing")


def _has_provenance(manifest: dict[str, object]) -> bool:
    """True when the manifest records the original speechd.conf state."""
    return "speechd_conf_existed" in manifest


def _atomic_write(path: pathlib.Path, data: bytes, mode: int = 0o644) -> None:
    handle, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = pathlib.Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(mode)
        os.replace(temporary, path)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _restore_path(path: pathlib.Path, snapshot: _PathSnapshot) -> None:
    if os.path.lexists(path):
        if path.is_dir() and not path.is_symlink():
            raise InstallError(f"cannot restore file snapshot over directory {path}")
        path.unlink()
    if snapshot.kind == "missing":
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    if snapshot.kind == "symlink":
        path.symlink_to(str(snapshot.data))
    else:
        _atomic_write(path, bytes(snapshot.data), snapshot.mode)


def _ensure_directory(path: pathlib.Path, created: list[pathlib.Path]) -> None:
    missing: list[pathlib.Path] = []
    cursor = path
    while not cursor.exists():
        missing.append(cursor)
        if cursor == cursor.parent:
            break
        cursor = cursor.parent
    path.mkdir(parents=True, exist_ok=True)
    created.extend(reversed(missing))


def _remove_empty_directories(created: list[pathlib.Path]) -> None:
    for path in sorted(set(created), key=lambda item: len(item.parts), reverse=True):
        try:
            path.rmdir()
        except (FileNotFoundError, OSError):
            pass


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


def _atomic_copy(source: pathlib.Path, target: pathlib.Path) -> None:
    handle, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", dir=target.parent
    )
    os.close(handle)
    temporary = pathlib.Path(temporary_name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _reserve_sibling(parent: pathlib.Path, prefix: str) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    path.rmdir()
    return path


def install(
    source_root: pathlib.Path,
    *,
    root: pathlib.Path | None = None,
    launcher: pathlib.Path | None = None,
    config_dir: pathlib.Path | None = None,
    venv_builder: Callable[[pathlib.Path], None] | None = None,
    preflight: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Install or transactionally upgrade the per-user integration."""
    source_root = pathlib.Path(source_root)
    root = install_root() if root is None else pathlib.Path(root)
    launcher_directory = launcher_dir() if launcher is None else pathlib.Path(launcher)
    config_directory = (
        speechd_config_dir() if config_dir is None else pathlib.Path(config_dir)
    )
    build_venv = venv_builder or _default_venv_builder

    (preflight or check_prerequisites)()

    expected = _expected_manifest(root, launcher_directory, config_directory)
    owned_manifest = _check_install_ownership(root, expected)
    upgrading = owned_manifest is not None
    launcher_file = pathlib.Path(expected["launcher"])
    module_conf = pathlib.Path(expected["module_conf"])
    speechd_conf = pathlib.Path(expected["speechd_conf"])
    backup = config_directory / "speechd.conf.free-tts.bak"
    if speechd_conf.is_symlink():
        raise InstallOwnershipError(
            f"refusing to replace symlinked Speech Dispatcher config {speechd_conf}"
        )

    external_paths = (launcher_file, module_conf, speechd_conf, backup)
    snapshots = {path: _snapshot_path(path) for path in external_paths}
    if owned_manifest is not None and not _has_provenance(owned_manifest):
        raise InstallError(
            "this installation was made by an older build that did not record "
            "speechd.conf provenance, so its original state cannot be restored. "
            f"Run `python -m desktop.install uninstall` first (it keeps "
            f"{speechd_conf}), then install again."
        )
    snapshot = snapshots[speechd_conf]
    if owned_manifest is not None:
        speechd_conf_existed = owned_manifest["speechd_conf_existed"] is True
        recorded_mode = owned_manifest["speechd_conf_mode"]
        speechd_conf_mode = recorded_mode if isinstance(recorded_mode, int) else None
    else:
        speechd_conf_existed = snapshot.kind == "file"
        speechd_conf_mode = snapshot.mode if speechd_conf_existed else None
    manifest: dict[str, object] = {
        **expected,
        "speechd_conf_existed": speechd_conf_existed,
        "speechd_conf_mode": speechd_conf_mode,
    }
    speechd_write_mode = (
        snapshots[speechd_conf].mode
        if snapshots[speechd_conf].kind == "file"
        else speechd_conf_mode
        if speechd_conf_mode is not None
        else 0o644
    )
    created_directories: list[pathlib.Path] = []
    _ensure_directory(root.parent, created_directories)
    staging = pathlib.Path(
        tempfile.mkdtemp(prefix=".free-tts-stage-", dir=root.parent)
    )
    rollback: pathlib.Path | None = None
    failed_tree: pathlib.Path | None = None
    published = False

    try:
        _stage_runtime(source_root, staging)
        if upgrading:
            for name in _PRESERVED:
                _copy_preserved(root / name, staging / name)
        (staging / MANIFEST_NAME).write_text(
            json.dumps(manifest, indent=2), encoding="utf-8"
        )

        _ensure_directory(launcher_directory, created_directories)
        _ensure_directory(module_conf.parent, created_directories)
        _ensure_directory(config_directory, created_directories)

        if upgrading:
            rollback = _reserve_sibling(root.parent, ".free-tts-rollback-")
            os.replace(root, rollback)
        os.replace(staging, root)
        published = True

        if not (root / ".venv").exists():
            build_venv(root)

        venv_python = root / ".venv" / "bin" / "python"
        python = str(venv_python) if venv_python.exists() else sys.executable
        launcher_body = _LAUNCHER_TEMPLATE.format(
            launcher=LAUNCHER_NAME,
            root=shlex.quote(str(root)),
            python=shlex.quote(python),
        ).encode("utf-8")
        _atomic_write(launcher_file, launcher_body, 0o755)
        _atomic_copy(root / "desktop" / MODULE_CONF_NAME, module_conf)

        original = (
            speechd_conf.read_text(encoding="utf-8")
            if speechd_conf.is_file()
            else ""
        )
        if original and snapshots[backup].kind == "missing":
            _atomic_write(
                backup, original.encode("utf-8"), speechd_write_mode
            )
        managed = apply_managed_block(
            original, LAUNCHER_NAME, MODULE_CONF_NAME
        )
        _atomic_write(
            speechd_conf, managed.encode("utf-8"), speechd_write_mode
        )
    except BaseException:
        try:
            for path in reversed(external_paths):
                _restore_path(path, snapshots[path])
        finally:
            if published and os.path.lexists(root):
                failed_tree = _reserve_sibling(
                    root.parent, ".free-tts-failed-"
                )
                os.replace(root, failed_tree)
                published = False
            if rollback is not None and os.path.lexists(rollback):
                os.replace(rollback, root)
                rollback = None
            if failed_tree is not None and failed_tree.exists():
                shutil.rmtree(failed_tree, ignore_errors=True)
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)
            _remove_empty_directories(created_directories)
        raise
    finally:
        if staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    if rollback is not None and rollback.exists():
        try:
            shutil.rmtree(rollback)
        except OSError as exc:
            logger.warning(
                "Could not remove retained rollback tree %s: %s", rollback, exc
            )

    logger.info("Installed free-tts module into %s", root)
    return manifest


def uninstall(
    *,
    root: pathlib.Path | None = None,
    launcher: pathlib.Path | None = None,
    config_dir: pathlib.Path | None = None,
) -> list[str]:
    """Remove manifest-owned paths only. Returns paths actually changed."""
    root = install_root() if root is None else pathlib.Path(root)
    launcher_directory = launcher_dir() if launcher is None else pathlib.Path(launcher)
    config_directory = (
        speechd_config_dir() if config_dir is None else pathlib.Path(config_dir)
    )
    expected = _expected_manifest(root, launcher_directory, config_directory)
    owned = _load_manifest(root, expected, missing_ok=True)
    if owned is None:
        logger.warning(
            "No valid ownership manifest at %s; leaving all paths unchanged.",
            root / MANIFEST_NAME,
        )
        return []

    speechd_conf = pathlib.Path(owned["speechd_conf"])
    module_conf = pathlib.Path(owned["module_conf"])
    launcher_file = pathlib.Path(owned["launcher"])
    for path in (speechd_conf, module_conf, launcher_file):
        if path.is_symlink():
            if path == speechd_conf:
                raise InstallOwnershipError(
                    f"refusing to edit symlinked Speech Dispatcher config {path}"
                )
            continue
        if path.exists() and not path.is_file():
            raise InstallOwnershipError(
                f"manifest-owned file path became a directory: {path}"
            )

    speechd_snapshot = _snapshot_path(speechd_conf)
    if _has_provenance(owned):
        speechd_conf_existed = owned["speechd_conf_existed"] is True
        recorded_mode = owned["speechd_conf_mode"]
        speechd_conf_mode = recorded_mode if isinstance(recorded_mode, int) else None
    else:
        # Provenance was never recorded, and an installer-created file is now
        # indistinguishable from a pre-existing empty one. Keep the file.
        logger.warning(
            "Ownership manifest has no speechd.conf provenance; keeping %s and "
            "removing only the managed block.",
            speechd_conf,
        )
        speechd_conf_existed = True
        speechd_conf_mode = speechd_snapshot.mode

    removed: list[str] = []
    if speechd_conf.is_file():
        current = speechd_conf.read_text(encoding="utf-8")
        cleaned = remove_managed_block(current)
        if not speechd_conf_existed and not cleaned:
            speechd_conf.unlink()
            removed.append(str(speechd_conf))
        else:
            restored_mode = (
                speechd_conf_mode
                if speechd_conf_mode is not None
                else speechd_snapshot.mode
            )
            if cleaned != current or restored_mode != speechd_snapshot.mode:
                _atomic_write(
                    speechd_conf, cleaned.encode("utf-8"), restored_mode
                )
                removed.append(str(speechd_conf))

    for path in (module_conf, launcher_file):
        if os.path.lexists(path):
            path.unlink()
            removed.append(str(path))

    shutil.rmtree(root)
    removed.append(str(root))
    return removed


def _speechd_pid_path() -> pathlib.Path | None:
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not runtime:
        return None
    return (
        pathlib.Path(runtime)
        / "speech-dispatcher"
        / "pid"
        / "speech-dispatcher.pid"
    )


def _read_speechd_pid(path: pathlib.Path) -> int | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError as exc:
        raise InstallError(
            f"could not inspect Speech Dispatcher PID file {path}: {exc}"
        ) from exc
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid != os.getuid():
        raise InstallError(
            f"Speech Dispatcher PID file is not a regular user-owned file: {path}"
        )
    try:
        raw_pid = path.read_text(encoding="ascii").strip()
        pid = int(raw_pid)
    except (OSError, UnicodeError, ValueError) as exc:
        raise InstallError(
            f"Speech Dispatcher PID file is invalid at {path}: {exc}"
        ) from exc
    if pid <= 0:
        raise InstallError(
            f"Speech Dispatcher PID file contains an invalid PID at {path}: {pid}"
        )
    return pid


def _process_identity(pid: int) -> str | None:
    """Basename of the live process's program, or None if it cannot be read."""
    try:
        return os.path.basename(os.readlink(f"/proc/{pid}/exe"))
    except OSError:
        pass
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as stream:
            argv0 = stream.read().split(b"\0")[0]
    except OSError:
        return None
    if not argv0:
        return None
    return os.path.basename(argv0.decode("utf-8", "replace"))


def restart_speech_dispatcher(
    *,
    opener: Callable[[int, int], int] = os.pidfd_open,
    sender: Callable[[int, int], None] = signal.pidfd_send_signal,
    identity: Callable[[int], str | None] = _process_identity,
) -> bool:
    """Reload the user's running Speech Dispatcher with ``SIGHUP``.

    The recorded PID is pinned with a pidfd before it is signalled, so a PID that
    has already been recycled cannot be reached: signalling a stale pidfd fails
    instead of hitting whatever process now owns that number. A missing PID file,
    an exited daemon, or a process that is not Speech Dispatcher is a no-op,
    because the next daemon start reads the updated configuration anyway.
    """
    pid_path = _speechd_pid_path()
    if pid_path is None:
        logger.info(
            "XDG_RUNTIME_DIR is unset; no running Speech Dispatcher to reload."
        )
        return False
    pid = _read_speechd_pid(pid_path)
    if pid is None:
        logger.info("No running Speech Dispatcher to reload at %s.", pid_path)
        return False

    try:
        descriptor = opener(pid, 0)
    except ProcessLookupError:
        logger.info("Speech Dispatcher PID %d is no longer running.", pid)
        return False
    except (AttributeError, NotImplementedError, OSError) as exc:
        logger.warning(
            "Cannot pin Speech Dispatcher PID %d for a safe reload (%s); "
            "restart Speech Dispatcher manually to load the new configuration.",
            pid,
            exc,
        )
        return False

    try:
        # The pidfd already pins one process, so this check cannot be raced into
        # signalling a different one: if the pinned process exited, the send below
        # fails rather than reaching a recycled PID.
        name = identity(pid)
        if name not in SPEECHD_PROCESS_NAMES:
            logger.warning(
                "PID %d from %s is %s, not Speech Dispatcher; refusing to signal "
                "it. Restart Speech Dispatcher manually if it is running.",
                pid,
                pid_path,
                name if name is not None else "gone",
            )
            return False
        try:
            sender(descriptor, signal.SIGHUP)
        except ProcessLookupError:
            logger.info("Speech Dispatcher PID %d exited before reload.", pid)
            return False
        except (OSError, OverflowError, ValueError) as exc:
            raise InstallError(
                f"could not signal Speech Dispatcher PID {pid} to reload: {exc}"
            ) from exc
    finally:
        os.close(descriptor)

    logger.info("Reloaded Speech Dispatcher configuration for PID %d.", pid)
    return True


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
