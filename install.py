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

from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
import argparse
import contextlib
import errno
import fcntl
import functools
import http.client
import json
import logging
import os
import pathlib
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

logger = logging.getLogger("free-tts.install")

MANIFEST_NAME = "server-manifest.json"
COMPONENT = "server"
UNIT_NAME = "free-tts.service"
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
    except (OSError, json.JSONDecodeError, ValueError, RecursionError):
        return None
    if not isinstance(payload, dict) or payload.get("component") != COMPONENT:
        return None
    return payload


def _canonical(path: pathlib.Path) -> pathlib.Path:
    return pathlib.Path(path).expanduser().resolve(strict=False)


def _expected_manifest(
    root: pathlib.Path, unit_dir: pathlib.Path
) -> dict[str, str]:
    root = pathlib.Path(root)
    unit = pathlib.Path(unit_dir) / UNIT_NAME
    return {
        "component": COMPONENT,
        "root": str(_canonical(root)),
        "unit": str(_canonical(unit)),
        "config": str(_canonical(root / "config.json")),
        "python": str(_canonical(root / ".venv" / "bin" / "python")),
    }


def _validate_manifest(payload: object, expected: dict[str, str]) -> dict:
    if not isinstance(payload, dict):
        raise OwnershipError("server manifest is not a JSON object")
    if payload.get("component") != COMPONENT:
        raise OwnershipError("server manifest has the wrong component owner")
    for key in ("root", "unit", "config", "python"):
        recorded = payload.get(key)
        if not isinstance(recorded, str):
            raise OwnershipError(f"server manifest is missing {key!r}")
        if _canonical(pathlib.Path(recorded)) != _canonical(
            pathlib.Path(expected[key])
        ):
            raise OwnershipError(
                f"server manifest path {key!r} is outside the expected user target"
            )
    return dict(payload)


def _load_manifest(
    root: pathlib.Path,
    expected: dict[str, str],
    *,
    missing_ok: bool,
) -> dict | None:
    path = manifest_path(root)
    if not os.path.lexists(path):
        if missing_ok:
            return None
        raise OwnershipError(
            f"cannot establish ownership: server manifest is missing at {path}"
        )
    if root.is_symlink() or not root.is_dir() or path.is_symlink() or not path.is_file():
        raise OwnershipError("server manifest must be a regular owned file")
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (
        OSError,
        json.JSONDecodeError,
        ValueError,
        RecursionError,
    ) as exc:
        raise OwnershipError(
            f"server manifest is corrupt at {path}: {exc}"
        ) from exc
    return _validate_manifest(payload, expected)


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


def _atomic_copy(source: pathlib.Path, target: pathlib.Path) -> None:
    """Copy metadata and contents to a sibling, then publish atomically."""
    handle, name = tempfile.mkstemp(
        dir=str(target.parent), prefix=f".{target.name}."
    )
    os.close(handle)
    temporary = pathlib.Path(name)
    try:
        shutil.copy2(source, temporary)
        os.replace(temporary, target)
    finally:
        if os.path.lexists(temporary):
            temporary.unlink()


def _check_root_ownership(
    root: pathlib.Path, unit_dir: pathlib.Path
) -> dict | None:
    """Return the owned manifest, or raise when the root is not ours."""
    expected = _expected_manifest(root, unit_dir)
    if not os.path.lexists(root):
        unit = pathlib.Path(expected["unit"])
        if os.path.lexists(unit):
            raise OwnershipError(
                f"refusing to overwrite unowned service unit at {unit}"
            )
        return None
    if root.is_symlink() or not root.is_dir():
        raise OwnershipError(f"refusing to replace non-directory install root {root}")
    return _load_manifest(root, expected, missing_ok=False)


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


@dataclass(frozen=True)
class _PathSnapshot:
    kind: str
    data: bytes | str | None = None
    mode: int = 0
    identity: tuple[int, int] | None = None


def _observe_unit(path: pathlib.Path) -> _PathSnapshot:
    try:
        before = os.lstat(path)
    except FileNotFoundError:
        return _PathSnapshot("missing")
    except OSError as exc:
        raise InstallError(f"could not inspect service unit {path}: {exc}") from exc
    if not stat.S_ISREG(before.st_mode):
        raise OwnershipError(
            f"owned service unit must be a regular non-symlink file: {path}"
        )
    try:
        data = path.read_bytes()
        after = os.lstat(path)
    except OSError as exc:
        raise InstallError(f"could not read service unit {path}: {exc}") from exc
    if _lstat_identity(before) != _lstat_identity(after):
        raise OwnershipError(f"service unit changed during observation: {path}")
    return _PathSnapshot(
        "file",
        data,
        stat.S_IMODE(after.st_mode),
        _lstat_identity(after),
    )


def _snapshot_matches(expected: _PathSnapshot, observed: _PathSnapshot) -> bool:
    if expected.kind != observed.kind:
        return False
    if expected.kind == "missing":
        return True
    return (
        expected.data == observed.data
        and expected.mode == observed.mode
        and expected.identity == observed.identity
    )


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


def _reserve_sibling(parent: pathlib.Path, prefix: str) -> pathlib.Path:
    path = pathlib.Path(tempfile.mkdtemp(prefix=prefix, dir=parent))
    path.rmdir()
    return path


def publish_runtime(
    source_root: pathlib.Path,
    root: pathlib.Path,
    *,
    unit_dir: pathlib.Path | None = None,
) -> bool:
    """Stage the runtime and swap it in, preserving the venv and config.

    Returns True when an existing owned install was upgraded.
    """
    source_root = pathlib.Path(source_root)
    root = pathlib.Path(root)
    unit_dir = systemd_user_dir() if unit_dir is None else pathlib.Path(unit_dir)
    existing = _check_root_ownership(root, unit_dir)
    manifest = {
        **_expected_manifest(root, unit_dir),
        "version": read_version(source_root),
    }
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
        _atomic_write(
            manifest_path(staging),
            json.dumps(manifest, indent=2).encode("utf-8"),
            0o644,
        )
        if existing is not None:
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


UNIT_TEMPLATE = """\
# free-tts server. Managed by `python install.py install server`.
#
# Keep `idle_timeout` at 0 in config.json: the server arms its
# idle-shutdown watchdog only when TTS_IDLE_TIMEOUT > 0, and a persistent
# service must never exit on its own.
[Unit]
Description=free-tts local TTS server (edge-tts)
After=default.target

[Service]
Type=simple
Environment=FREE_TTS_CONFIG_ONLY=1
UnsetEnvironment=FLASK_DEBUG
ExecStart=:/usr/bin/env -- {python} {server}
WorkingDirectory={working_directory}
Restart=on-failure
RestartSec=2

[Install]
WantedBy=default.target
"""


def bootstrap_config(root: pathlib.Path) -> bool:
    """Seed config.json from the example. True when it created the file."""
    root = pathlib.Path(root)
    config = root / "config.json"
    if os.path.lexists(config):
        return False
    example = root / "config.example.json"
    if not example.is_file():
        raise InstallError(f"missing config template: {example}")
    _atomic_copy(example, config)
    logger.info("Wrote default config to %s", config)
    return True


def _systemd_quote(path: pathlib.Path) -> str:
    """Quote one systemd value and neutralize unit specifier expansion."""
    value = str(path)
    if any(character in value for character in ("\0", "\n", "\r")):
        raise InstallError("systemd paths cannot contain NUL or line breaks")
    escaped = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{escaped}"'


def _systemd_path_value(path: pathlib.Path) -> str:
    """Escape a path for a systemd directive that does not accept quoting."""
    value = str(path)
    if any(character in value for character in ("\0", "\n", "\r")):
        raise InstallError("systemd paths cannot contain NUL or line breaks")
    return (
        value.replace("\\", "\\\\")
        .replace(" ", "\\x20")
        .replace('"', '\\"')
        .replace("%", "%%")
    )


def render_unit(
    root: pathlib.Path, python: pathlib.Path | str | None = None
) -> str:
    """Render the systemd user unit for an install root."""
    root = pathlib.Path(root)
    interpreter = (
        root / ".venv" / "bin" / "python" if python is None else pathlib.Path(python)
    )
    return UNIT_TEMPLATE.format(
        python=_systemd_quote(interpreter),
        server=_systemd_quote(root / "server.py"),
        working_directory=_systemd_path_value(root),
    )


MIN_PYTHON = (3, 11)
DEFAULT_PORT = 5000
_PROBE_TIMEOUT = 3.0


@dataclass(frozen=True)
class ServiceEndpoint:
    """Immutable address of the server governed by the installed config.

    ``bind_host`` is what the server listens on; ``probe_host`` is the
    loopback address used to verify it locally (wildcard binds map to the
    matching loopback address).
    """

    bind_host: str
    probe_host: str
    port: int

    @property
    def health_url(self) -> str:
        host = f"[{self.probe_host}]" if ":" in self.probe_host else self.probe_host
        return f"http://{host}:{self.port}/health"


@dataclass(frozen=True)
class UnitIdentity:
    """systemd identity of the running unit: MainPID and InvocationID."""

    main_pid: int
    invocation_id: str


DEFAULT_ENDPOINT = ServiceEndpoint("127.0.0.1", "127.0.0.1", DEFAULT_PORT)


def _probe_host(bind_host: str) -> str:
    """Loopback address used to verify a wildcard or concrete bind."""
    if bind_host == "0.0.0.0":
        return "127.0.0.1"
    if bind_host == "::":
        return "::1"
    return bind_host


class PreflightError(InstallError):
    """A pre-flight check failed; nothing has been changed yet."""


def _server_lock_path() -> pathlib.Path:
    raw_runtime = os.environ.get("XDG_RUNTIME_DIR")
    if not raw_runtime:
        raise PreflightError(
            "XDG_RUNTIME_DIR is required for the server installer lock"
        )
    runtime = pathlib.Path(raw_runtime)
    if not runtime.is_absolute():
        raise PreflightError(
            f"XDG_RUNTIME_DIR must be absolute for the installer lock: {runtime}"
        )
    try:
        metadata = os.lstat(runtime)
    except OSError as exc:
        raise PreflightError(
            f"XDG_RUNTIME_DIR is not accessible for the installer lock: "
            f"{runtime}: {exc}"
        ) from exc
    if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
        raise PreflightError(
            f"XDG_RUNTIME_DIR must be a non-symlink directory: {runtime}"
        )
    if metadata.st_uid != os.geteuid():
        raise PreflightError(
            f"XDG_RUNTIME_DIR is not owned by the current user: {runtime}"
        )
    return runtime / "free-tts-installer.lock"


@contextlib.contextmanager
def _server_transaction_lock(
    path: pathlib.Path | None = None,
) -> Iterator[None]:
    lock_path = _server_lock_path() if path is None else pathlib.Path(path)
    descriptor = None
    locked = False
    try:
        try:
            descriptor = os.open(
                lock_path,
                os.O_CREAT
                | os.O_RDWR
                | os.O_CLOEXEC
                | os.O_NOFOLLOW,
                0o600,
            )
            metadata = os.fstat(descriptor)
            if not stat.S_ISREG(metadata.st_mode):
                raise PreflightError(
                    f"installer lock must be a regular file: {lock_path}"
                )
            if metadata.st_uid != os.geteuid():
                raise PreflightError(
                    f"installer lock is not owned by the current user: {lock_path}"
                )
            if stat.S_IMODE(metadata.st_mode) & 0o077:
                raise PreflightError(
                    f"installer lock has insecure permissions: {lock_path}"
                )
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise PreflightError(
                    "another server installer operation is active; "
                    f"lock: {lock_path}"
                ) from exc
            locked = True
        except PreflightError:
            raise
        except OSError as exc:
            raise PreflightError(
                f"could not open or lock installer lock {lock_path}: {exc}"
            ) from exc

        yield
    finally:
        if descriptor is not None:
            if locked:
                with contextlib.suppress(OSError):
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
            os.close(descriptor)


def _serialized_server_transaction(operation: Callable) -> Callable:
    @functools.wraps(operation)
    def serialized(*args, **kwargs):
        with _server_transaction_lock():
            return operation(*args, **kwargs)

    return serialized


def _load_service_endpoint(path: pathlib.Path) -> ServiceEndpoint:
    """Parse and validate the endpoint that the config at ``path`` governs.

    Read-only and raised before any install mutation, so preflight, startup
    verification, and the server configuration cannot drift.
    """
    path = pathlib.Path(path)
    if path.is_symlink() or not path.is_file():
        raise PreflightError(
            f"endpoint config must be a regular non-symlink file: {path}"
        )
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, ValueError, RecursionError) as exc:
        raise PreflightError(
            f"endpoint config is not readable JSON: {path}: {exc}"
        ) from exc
    if not isinstance(payload, dict):
        raise PreflightError(f"endpoint config must be a JSON object: {path}")
    host = payload.get("host", "127.0.0.1")
    if not isinstance(host, str) or not host:
        raise PreflightError(
            f"endpoint config host must be a non-empty string: {path}"
        )
    port = payload.get("port", DEFAULT_PORT)
    if isinstance(port, bool) or not isinstance(port, (int, str)):
        raise PreflightError(
            "endpoint config port must be an integer or integer-like "
            f"string: {path}"
        )
    if isinstance(port, str):
        port = port.strip()
        try:
            if not port.isascii() or not port.isdigit():
                raise ValueError("port is not an ASCII decimal integer")
            port = int(port)
        except (TypeError, ValueError) as exc:
            raise PreflightError(
                "endpoint config port must be an integer or integer-like "
                f"string: {path}"
            ) from exc
    if not 1 <= port <= 65535:
        raise PreflightError(
            f"endpoint config port must be between 1 and 65535: {path}"
        )
    return ServiceEndpoint(bind_host=host, probe_host=_probe_host(host), port=port)


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


def _systemctl_error(
    runner: Callable[..., object], args: list[str]
) -> str | None:
    """Run one systemctl action and return an actionable failure, if any."""
    try:
        result = runner(args, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"systemctl --user {' '.join(args)} could not run: {exc}"
    returncode = getattr(result, "returncode", 0)
    if returncode == 0:
        return None
    detail = str(
        getattr(result, "stderr", "")
        or getattr(result, "stdout", "")
        or "no diagnostic output"
    ).strip()
    return (
        f"systemctl --user {' '.join(args)} failed with status "
        f"{returncode}: {detail}"
    )


def _run_systemctl(runner: Callable[..., object], args: list[str]) -> None:
    failure = _systemctl_error(runner, args)
    if failure is not None:
        raise InstallError(failure)


def _query_unit_state(
    runner: Callable[..., object], command: str, expected: str
) -> bool:
    try:
        result = runner([command, UNIT_NAME], check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(
            f"systemctl --user {command} {UNIT_NAME} could not run: {exc}"
        ) from exc
    output = str(getattr(result, "stdout", "") or "").strip()
    returncode = getattr(result, "returncode", 0)
    if returncode == 0 and output == expected:
        return True
    known_negative = {
        "is-active": {
            "activating",
            "deactivating",
            "failed",
            "inactive",
            "maintenance",
            "reloading",
            "unknown",
        },
        "is-enabled": {
            "bad",
            "disabled",
            "generated",
            "indirect",
            "masked",
            "masked-runtime",
            "not-found",
            "static",
            "transient",
        },
    }
    if output in known_negative.get(command, set()):
        return False
    detail = str(getattr(result, "stderr", "") or output or "no diagnostic output")
    raise InstallError(
        f"systemctl --user {command} {UNIT_NAME} failed with status "
        f"{returncode}: {detail.strip()}"
    )


_UNIT_ACTIVITY_STATES = frozenset(
    {
        "active",
        "activating",
        "deactivating",
        "failed",
        "inactive",
        "maintenance",
        "reloading",
        "unknown",
    }
)


def _query_unit_active_state(runner: Callable[..., object]) -> str:
    args = ["is-active", UNIT_NAME]
    try:
        result = runner(args, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(
            f"systemctl --user is-active {UNIT_NAME} could not run: {exc}"
        ) from exc
    state = str(getattr(result, "stdout", "") or "").strip()
    if state not in _UNIT_ACTIVITY_STATES or state == "unknown":
        detail = str(getattr(result, "stderr", "") or state or "no diagnostic output")
        raise InstallError(
            f"systemctl --user is-active {UNIT_NAME} returned unrecognized state: "
            f"{detail.strip()}"
        )
    return state


def _wait_for_unit_inactive(
    runner: Callable[..., object],
    *,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> tuple[str, ...]:
    history = []
    reset_attempted = False
    for attempt in range(max(1, attempts)):
        state = _query_unit_active_state(runner)
        history.append(state)
        if state == "inactive":
            return tuple(history)
        if state == "failed" and not reset_attempted:
            _run_systemctl(runner, ["reset-failed", UNIT_NAME])
            reset_attempted = True
        if attempt + 1 < max(1, attempts):
            sleeper(delay)
    raise InstallError(
        f"{UNIT_NAME} did not become inactive; observed: {', '.join(history)}"
    )


_HEX_CHARS = frozenset("0123456789abcdefABCDEF")


def _query_unit_identity(
    runner: Callable[..., object],
) -> UnitIdentity | None:
    """Return the unit's systemd identity, or None while it is not ready.

    A None result means the unit is transitional/inactive or its MainPID or
    InvocationID is not yet valid. Command execution failures and
    unrecognized output raise instead.
    """
    args = [
        "show",
        UNIT_NAME,
        "-p",
        "ActiveState",
        "-p",
        "MainPID",
        "-p",
        "InvocationID",
    ]
    try:
        result = runner(args, check=False)
    except (OSError, subprocess.SubprocessError) as exc:
        raise InstallError(
            f"systemctl --user show {UNIT_NAME} could not run: {exc}"
        ) from exc
    output = str(getattr(result, "stdout", "") or "")
    returncode = getattr(result, "returncode", 0)
    if returncode != 0:
        detail = str(
            getattr(result, "stderr", "") or output or "no diagnostic output"
        ).strip()
        raise InstallError(
            f"systemctl --user show {UNIT_NAME} failed with status "
            f"{returncode}: {detail}"
        )
    fields: dict[str, str] = {}
    for line in output.splitlines():
        key, sep, value = line.partition("=")
        if sep and key.strip():
            fields[key.strip()] = value.strip()
    if "ActiveState" not in fields:
        raise InstallError(
            f"systemctl --user show {UNIT_NAME} returned unrecognized output: "
            f"{output.strip() or '(empty)'}"
        )
    if fields.get("ActiveState") != "active":
        return None
    main_pid = fields.get("MainPID")
    invocation_id = fields.get("InvocationID")
    if main_pid is None or invocation_id is None:
        return None
    try:
        pid = int(main_pid)
    except ValueError:
        return None
    if pid < 1:
        return None
    if len(invocation_id) != 32 or any(
        char not in _HEX_CHARS for char in invocation_id
    ):
        return None
    return UnitIdentity(main_pid=pid, invocation_id=invocation_id)


def _verify_service(
    runner: Callable[..., object],
    fetch: Callable[[str, float], object] | None,
    *,
    endpoint: ServiceEndpoint,
    attempts: int,
    delay: float,
    sleeper: Callable[[float], None],
) -> None:
    """Require the active unit and the expected health identity to match.

    The configured endpoint must report the same PID and invocation ID that
    systemd reports for the unit both before and after the health probe, so a
    foreign responder or a unit that restarted mid-check cannot commit the
    install.
    """
    last_failure = (
        "the unit did not report a stable active identity matching "
        f"{endpoint.health_url}"
    )
    for attempt in range(max(1, attempts)):
        before = _query_unit_identity(runner)
        payload = (
            probe_health(endpoint, fetch=fetch) if before is not None else None
        )
        after = _query_unit_identity(runner) if before is not None else None
        if (
            before is not None
            and after == before
            and payload is not None
            and payload.get("service") == "free-tts"
            and not isinstance(payload.get("pid"), bool)
            and payload.get("pid") == before.main_pid
            and payload.get("invocation_id") == before.invocation_id
        ):
            return
        if attempt + 1 < max(1, attempts):
            sleeper(delay)
    raise InstallError(f"service verification failed: {last_failure}")


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
    endpoint: ServiceEndpoint | None = None,
    *,
    fetch: Callable[[str, float], object] | None = None,
) -> dict | None:
    """Return the /health payload from a local server, or None."""
    getter = fetch or _fetch_json
    url = (endpoint or DEFAULT_ENDPOINT).health_url
    try:
        payload = getter(url, _PROBE_TIMEOUT)
    except (
        OSError,
        urllib.error.URLError,
        ValueError,
        RecursionError,
        http.client.HTTPException,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def _tcp_port_occupied(host: str, port: int, timeout: float) -> bool:
    """Return False only when the probe host explicitly refuses the connection."""
    try:
        connection = socket.create_connection((host, port), timeout=timeout)
    except ConnectionRefusedError:
        return False
    except OSError as exc:
        if exc.errno == errno.ECONNREFUSED:
            return False
        return True
    connection.close()
    return True


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
    endpoint: ServiceEndpoint | None = None,
    force: bool = False,
    fetch: Callable[[str, float], object] | None = None,
    occupancy_probe: Callable[[str, int, float], bool] | None = None,
    systemctl: Callable[..., object] | None = None,
) -> str:
    """Classify who owns the server port before we bind it."""
    endpoint = endpoint or DEFAULT_ENDPOINT
    probe = occupancy_probe or _tcp_port_occupied
    try:
        occupied = probe(endpoint.probe_host, endpoint.port, _PROBE_TIMEOUT)
    except OSError as exc:
        if force:
            return "forced"
        raise PreflightError(
            f"could not prove port {endpoint.port} is free ({exc}); treating it "
            "as occupied. Pass --force to install anyway."
        ) from exc
    if not occupied:
        return "free"

    payload = probe_health(endpoint, fetch=fetch)
    if payload is not None and payload.get("service") == "free-tts":
        if unit_is_active(systemctl):
            return "ours"
        if force:
            return "forced"
        raise PreflightError(
            f"port {endpoint.port} already serves free-tts, but not through "
            f"{UNIT_NAME}. Another supervisor probably owns it; stop that "
            "owner first, or pass --force to install anyway."
        )
    if force:
        return "forced"
    raise PreflightError(
        f"port {endpoint.port} is already used by another service; free the "
        "port or pass --force to install anyway."
    )


def _publish_staged_runtime(
    staging: pathlib.Path, root: pathlib.Path, *, upgrading: bool
) -> pathlib.Path | None:
    rollback: pathlib.Path | None = None
    if upgrading:
        rollback = _reserve_sibling(root.parent, ".free-tts-server-rollback-")
        os.replace(root, rollback)
    try:
        os.replace(staging, root)
    except BaseException:
        if rollback is not None and os.path.lexists(rollback):
            try:
                os.replace(rollback, root)
            except BaseException as restore_error:
                raise InstallError(
                    "runtime publication failed and the previous install could "
                    f"not be restored; it is kept at {rollback}"
                ) from restore_error
        raise
    return rollback


def _rollback_install(
    *,
    root: pathlib.Path,
    rollback: pathlib.Path | None,
    published: bool,
    artifacts: _SystemdArtifacts,
    service_touched: bool,
    runner: Callable[..., object],
    previous_active: bool,
) -> list[str]:
    """Restore pre-install files and service state, retaining failed data on error."""
    errors: list[str] = []
    failed_tree: pathlib.Path | None = None

    if service_touched:
        failure = _systemctl_error(runner, ["stop", UNIT_NAME])
        if failure is not None:
            errors.append(failure)

    if published and os.path.lexists(root):
        try:
            failed_tree = _reserve_sibling(
                root.parent, ".free-tts-server-failed-"
            )
            os.replace(root, failed_tree)
        except BaseException as exc:
            errors.append(f"could not move the failed runtime aside: {exc}")

    if rollback is not None and os.path.lexists(rollback):
        if os.path.lexists(root):
            errors.append(
                f"could not restore the previous runtime from {rollback}: "
                f"the target {root} is still occupied"
            )
        else:
            try:
                os.replace(rollback, root)
            except BaseException as exc:
                errors.append(
                    f"could not restore the previous runtime from {rollback}: {exc}"
                )

    errors.extend(artifacts.restore())

    if service_touched:
        failure = _systemctl_error(runner, ["daemon-reload"])
        if failure is not None:
            errors.append(failure)

    if service_touched:
        if previous_active:
            failure = _systemctl_error(runner, ["restart", UNIT_NAME])
            if failure is not None:
                errors.append(failure)

    if failed_tree is not None and os.path.lexists(failed_tree):
        if errors:
            errors.append(f"failed runtime retained at {failed_tree}")
        else:
            try:
                shutil.rmtree(failed_tree)
            except OSError as exc:
                errors.append(f"could not remove failed runtime {failed_tree}: {exc}")
    return errors


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


@_serialized_server_transaction
def install_server(
    source_root: pathlib.Path | None = None,
    *,
    root: pathlib.Path | None = None,
    unit_dir: pathlib.Path | None = None,
    venv_builder: Callable[[pathlib.Path], None] | None = None,
    systemctl: Callable[..., object] | None = None,
    fetch: Callable[[str, float], object] | None = None,
    occupancy_probe: Callable[[str, int, float], bool] | None = None,
    force: bool = False,
    preflight: Callable[[], None] | None = None,
    verify_attempts: int = 10,
    verify_delay: float = 0.5,
    sleeper: Callable[[float], None] = time.sleep,
) -> dict:
    """Transactionally install or refresh the server and its user service."""
    source_root = checkout_root() if source_root is None else pathlib.Path(source_root)
    root = server_root() if root is None else pathlib.Path(root)
    unit_dir = systemd_user_dir() if unit_dir is None else pathlib.Path(unit_dir)
    build_venv = venv_builder or default_venv_builder
    runner = systemctl or default_systemctl

    def _default_preflight() -> None:
        check_python()
        check_systemd(systemctl=runner)

    (preflight or _default_preflight)()

    expected = _expected_manifest(root, unit_dir)
    existing = _check_root_ownership(root, unit_dir)
    upgrading = existing is not None
    unit_path = pathlib.Path(expected["unit"])
    enablement_path = _enablement_path(unit_dir)
    artifacts = _SystemdArtifacts.capture_for_install(
        unit_path, enablement_path, upgrading
    )
    previous_active = (
        _query_unit_state(runner, "is-active", "active") if upgrading else False
    )
    manifest = {
        **expected,
        "version": read_version(source_root),
    }
    _validate_manifest(manifest, expected)
    unit_text = render_unit(root)
    config_path = (
        root / "config.json"
        if upgrading and os.path.lexists(root / "config.json")
        else source_root / "config.example.json"
    )
    endpoint = _load_service_endpoint(config_path)
    check_port(
        endpoint=endpoint,
        force=force,
        fetch=fetch,
        occupancy_probe=occupancy_probe,
        systemctl=runner,
    )
    created_directories: list[pathlib.Path] = []
    staging: pathlib.Path | None = None
    rollback: pathlib.Path | None = None
    published = False
    service_touched = False

    try:
        _ensure_directory(root.parent, created_directories)
        staging = pathlib.Path(
            tempfile.mkdtemp(prefix=".free-tts-server-stage-", dir=root.parent)
        )
        _stage_runtime(source_root, staging)
        if upgrading:
            for name in PRESERVED:
                _copy_preserved(root / name, staging / name)
        bootstrap_config(staging)
        _atomic_write(
            manifest_path(staging),
            json.dumps(manifest, indent=2).encode("utf-8"),
            0o644,
        )

        rollback = _publish_staged_runtime(staging, root, upgrading=upgrading)
        published = True
        if not (root / ".venv").exists():
            build_venv(root)

        artifacts.publish_unit(unit_text)
        service_touched = True
        _run_systemctl(runner, ["daemon-reload"])
        artifacts.ensure_enablement()
        _run_systemctl(runner, ["restart", UNIT_NAME])
        artifacts.verify_enablement()
        _verify_service(
            runner,
            fetch,
            endpoint=endpoint,
            attempts=verify_attempts,
            delay=verify_delay,
            sleeper=sleeper,
        )
    except BaseException as exc:
        rollback_errors = _rollback_install(
            root=root,
            rollback=rollback,
            published=published,
            artifacts=artifacts,
            service_touched=service_touched,
            runner=runner,
            previous_active=previous_active,
        )
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)
        _remove_empty_directories(created_directories)
        if rollback_errors:
            details = "\n- ".join(rollback_errors)
            raise InstallError(
                f"install failed ({exc}) and rollback was incomplete:\n- {details}"
            ) from exc
        raise
    finally:
        if staging is not None and staging.exists():
            shutil.rmtree(staging, ignore_errors=True)

    if rollback is not None and os.path.lexists(rollback):
        try:
            shutil.rmtree(rollback)
        except OSError as exc:
            logger.warning("Could not remove retained rollback tree %s: %s", rollback, exc)
    logger.info("Installed free-tts server into %s", root)
    return manifest


def _enablement_path(unit_dir: pathlib.Path) -> pathlib.Path:
    """The sole wants path managed by this installer."""
    return pathlib.Path(unit_dir) / "default.target.wants" / UNIT_NAME


def _enablement_target(path: pathlib.Path, unit_path: pathlib.Path) -> str:
    """Return the fixed relative target for the validated wants path."""
    expected = pathlib.Path(unit_path).parent / "default.target.wants" / UNIT_NAME
    if _canonical(path) != _canonical(expected):
        raise InstallError(
            f"enablement path is outside the expected wants directory: {path}"
        )
    return str(pathlib.Path("..") / UNIT_NAME)


def _snapshot_is_owned_enablement(
    path: pathlib.Path,
    unit_path: pathlib.Path,
    snapshot: _PathSnapshot,
) -> bool:
    if snapshot.kind != "symlink" or not isinstance(snapshot.data, str):
        return False
    target = pathlib.Path(snapshot.data)
    resolved = _canonical(target if target.is_absolute() else path.parent / target)
    return resolved == _canonical(unit_path)


def _describe_enablement(snapshot: _PathSnapshot) -> str:
    if snapshot.kind == "symlink":
        return f"symlink with target {snapshot.data!r}"
    return snapshot.kind


_ENABLEMENT_OBSERVATION_ATTEMPTS = 3


def _lstat_identity(metadata: os.stat_result) -> tuple[int, int]:
    return metadata.st_dev, metadata.st_ino


def _observe_enablement(path: pathlib.Path) -> _PathSnapshot:
    """Observe enablement without changing any present entry."""
    for _ in range(_ENABLEMENT_OBSERVATION_ATTEMPTS):
        try:
            before = os.lstat(path)
        except FileNotFoundError:
            return _PathSnapshot("missing")
        except OSError as exc:
            raise InstallError(
                f"could not inspect service enablement {path}: {exc}"
            ) from exc

        if stat.S_ISLNK(before.st_mode):
            try:
                target = os.readlink(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise InstallError(
                    f"could not read service enablement symlink {path}: {exc}"
                ) from exc
            try:
                after = os.lstat(path)
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise InstallError(
                    f"could not confirm service enablement identity {path}: {exc}"
                ) from exc
            identity = _lstat_identity(after)
            if _lstat_identity(before) != identity:
                continue
            return _PathSnapshot("symlink", target, identity=identity)

        try:
            after = os.lstat(path)
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise InstallError(
                f"could not confirm service enablement identity {path}: {exc}"
            ) from exc
        identity = _lstat_identity(after)
        if _lstat_identity(before) != identity:
            continue
        if stat.S_ISREG(after.st_mode):
            return _PathSnapshot("file", identity=identity)
        if stat.S_ISDIR(after.st_mode):
            return _PathSnapshot("directory", identity=identity)
        return _PathSnapshot("other", identity=identity)

    raise InstallError(
        f"could not obtain a stable service enablement observation at {path}"
    )


@dataclass
class _SystemdArtifacts:
    unit_path: pathlib.Path
    enablement_path: pathlib.Path
    unit_initial: _PathSnapshot
    enablement_initial: _PathSnapshot
    unit_expected: _PathSnapshot
    enablement_expected: _PathSnapshot
    operation: str
    unit_touched: bool = False
    enablement_touched: bool = False
    created_directories: list[pathlib.Path] = field(default_factory=list)

    @classmethod
    def capture_for_install(
        cls,
        unit_path: pathlib.Path,
        enablement_path: pathlib.Path,
        upgrading: bool,
    ) -> "_SystemdArtifacts":
        unit_path = pathlib.Path(unit_path)
        enablement_path = pathlib.Path(enablement_path)
        unit = _observe_unit(unit_path)
        enablement = _observe_enablement(enablement_path)
        if enablement.kind != "missing" and not _snapshot_is_owned_enablement(
            enablement_path, unit_path, enablement
        ):
            raise OwnershipError(
                f"enablement path is foreign: {enablement_path}; retained "
                f"{_describe_enablement(enablement)}"
            )
        if not upgrading and unit.kind != "missing":
            raise OwnershipError(
                f"refusing to overwrite unowned service unit at {unit_path}"
            )
        if not upgrading and enablement.kind != "missing":
            raise OwnershipError(
                f"refusing to overwrite unowned enablement path {enablement_path}"
            )
        return cls(
            unit_path,
            enablement_path,
            unit,
            enablement,
            unit,
            enablement,
            "install",
        )

    @classmethod
    def capture_for_uninstall(
        cls,
        unit_path: pathlib.Path,
        enablement_path: pathlib.Path,
    ) -> "_SystemdArtifacts":
        unit_path = pathlib.Path(unit_path)
        enablement_path = pathlib.Path(enablement_path)
        unit = _observe_unit(unit_path)
        enablement = _observe_enablement(enablement_path)
        if enablement.kind != "missing" and not _snapshot_is_owned_enablement(
            enablement_path, unit_path, enablement
        ):
            raise OwnershipError(
                f"enablement path is foreign: {enablement_path}; retained "
                f"{_describe_enablement(enablement)}"
            )
        return cls(
            unit_path,
            enablement_path,
            unit,
            enablement,
            unit,
            enablement,
            "uninstall",
        )

    def _assert_unit_expected(self) -> None:
        observed = _observe_unit(self.unit_path)
        if not _snapshot_matches(self.unit_expected, observed):
            raise InstallError(
                f"service unit changed before mutation: {self.unit_path}; retained"
            )

    def _assert_enablement_expected(self) -> None:
        observed = _observe_enablement(self.enablement_path)
        if not _snapshot_matches(self.enablement_expected, observed):
            raise InstallError(
                f"service enablement changed before mutation: "
                f"{self.enablement_path}; retained "
                f"{_describe_enablement(observed)}"
            )

    def _stage_unit(
        self, data: bytes, mode: int
    ) -> tuple[pathlib.Path, _PathSnapshot]:
        descriptor, name = tempfile.mkstemp(
            dir=str(self.unit_path.parent),
            prefix=f".{UNIT_NAME}.staged-",
        )
        staged = pathlib.Path(name)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(data)
            os.chmod(staged, mode)
            return staged, _observe_unit(staged)
        except BaseException:
            with contextlib.suppress(OSError):
                os.close(descriptor)
            with contextlib.suppress(OSError):
                staged.unlink()
            raise

    def _stage_enablement(
        self, target: str
    ) -> tuple[pathlib.Path, _PathSnapshot]:
        staged = _reserve_sibling(
            self.enablement_path.parent,
            f".{UNIT_NAME}.staged-enablement-",
        )
        try:
            os.symlink(target, staged)
            return staged, _observe_enablement(staged)
        except BaseException:
            with contextlib.suppress(OSError):
                staged.unlink()
            raise

    def publish_unit(self, unit_text: str) -> None:
        _ensure_directory(self.unit_path.parent, self.created_directories)
        staged, staged_snapshot = self._stage_unit(
            unit_text.encode("utf-8"), 0o644
        )
        try:
            if self.unit_expected.kind == "missing":
                try:
                    os.link(staged, self.unit_path, follow_symlinks=False)
                except FileExistsError as exc:
                    observed = _observe_unit(self.unit_path)
                    raise OwnershipError(
                        f"refusing to replace service unit {self.unit_path}; "
                        f"retained {observed.kind}"
                    ) from exc
            else:
                self._assert_unit_expected()
                os.replace(staged, self.unit_path)
                staged = None
            self.unit_expected = staged_snapshot
            self.unit_touched = True
        finally:
            if staged is not None and os.path.lexists(staged):
                staged.unlink()

    def ensure_enablement(self) -> None:
        if self.enablement_expected.kind != "missing":
            self._assert_enablement_expected()
            return
        _ensure_directory(self.enablement_path.parent, self.created_directories)
        staged, staged_snapshot = self._stage_enablement(
            _enablement_target(self.enablement_path, self.unit_path)
        )
        try:
            try:
                os.link(
                    staged,
                    self.enablement_path,
                    follow_symlinks=False,
                )
            except FileExistsError as exc:
                observed = _observe_enablement(self.enablement_path)
                raise OwnershipError(
                    f"refusing to replace service enablement "
                    f"{self.enablement_path}; retained "
                    f"{_describe_enablement(observed)}"
                ) from exc
            self.enablement_expected = staged_snapshot
            self.enablement_touched = True
        finally:
            if os.path.lexists(staged):
                staged.unlink()

    def verify_enablement(self) -> None:
        self._assert_enablement_expected()
        if self.enablement_expected.kind == "missing":
            raise InstallError(
                f"service enablement disappeared after restart: "
                f"{self.enablement_path}"
            )

    def _restore_initial_unit(self) -> None:
        if self.unit_initial.kind != "file" or not isinstance(
            self.unit_initial.data, bytes
        ):
            raise InstallError("saved service unit snapshot is invalid")
        staged, restored = self._stage_unit(
            self.unit_initial.data,
            self.unit_initial.mode,
        )
        try:
            os.replace(staged, self.unit_path)
            staged = None
            self.unit_expected = restored
        finally:
            if staged is not None and os.path.lexists(staged):
                staged.unlink()

    def _restore_initial_enablement(self) -> None:
        target = self.enablement_initial.data
        if self.enablement_initial.kind != "symlink" or not isinstance(
            target, str
        ):
            raise InstallError("saved service enablement snapshot is invalid")
        observed = _observe_enablement(self.enablement_path)
        if (
            observed.kind == "symlink"
            and observed.data == target
            and _snapshot_is_owned_enablement(
                self.enablement_path,
                self.unit_path,
                observed,
            )
        ):
            self.enablement_expected = observed
            return
        if observed.kind != "missing":
            raise InstallError(
                f"refusing to replace service enablement "
                f"{self.enablement_path}; retained "
                f"{_describe_enablement(observed)}"
            )
        _ensure_directory(
            self.enablement_path.parent,
            self.created_directories,
        )
        staged, restored = self._stage_enablement(target)
        try:
            os.link(
                staged,
                self.enablement_path,
                follow_symlinks=False,
            )
            self.enablement_expected = restored
        finally:
            if os.path.lexists(staged):
                staged.unlink()

    def restore(self) -> list[str]:
        errors = []
        if self.operation == "uninstall":
            try:
                current_unit = _observe_unit(self.unit_path)
                if not _snapshot_matches(self.unit_initial, current_unit):
                    raise InstallError(
                        f"service unit changed during failed uninstall: "
                        f"{self.unit_path}"
                    )
                current = _observe_enablement(self.enablement_path)
                if self.enablement_initial.kind == "missing":
                    if current.kind == "missing":
                        return errors
                    if not _snapshot_is_owned_enablement(
                        self.enablement_path,
                        self.unit_path,
                        current,
                    ):
                        raise InstallError(
                            f"foreign enablement retained at "
                            f"{self.enablement_path}: "
                            f"{_describe_enablement(current)}"
                        )
                    self.enablement_expected = current
                    self.enablement_path.unlink()
                    self.enablement_expected = _PathSnapshot("missing")
                    return errors
                target = self.enablement_initial.data
                if (
                    current.kind == "symlink"
                    and current.data == target
                    and _snapshot_is_owned_enablement(
                        self.enablement_path,
                        self.unit_path,
                        current,
                    )
                ):
                    self.enablement_expected = current
                    return errors
                if current.kind != "missing":
                    raise InstallError(
                        f"foreign or changed enablement retained at "
                        f"{self.enablement_path}: "
                        f"{_describe_enablement(current)}"
                    )
                self.enablement_expected = current
                self._restore_initial_enablement()
            except BaseException as exc:
                errors.append(
                    f"could not restore service enablement "
                    f"{self.enablement_path}: {exc}"
                )
            return errors
        if self.operation != "install":
            raise InstallError(
                f"artifact restore mode is not implemented: {self.operation}"
            )
        if self.enablement_touched:
            try:
                self._assert_enablement_expected()
                if self.enablement_initial.kind == "missing":
                    self.enablement_path.unlink()
                    self.enablement_expected = _PathSnapshot("missing")
                else:
                    self._restore_initial_enablement()
            except BaseException as exc:
                errors.append(
                    f"could not restore service enablement "
                    f"{self.enablement_path}: {exc}"
                )
        if self.unit_touched:
            try:
                self._assert_unit_expected()
                if self.unit_initial.kind == "missing":
                    self.unit_path.unlink()
                    self.unit_expected = _PathSnapshot("missing")
                else:
                    self._restore_initial_unit()
            except BaseException as exc:
                errors.append(
                    f"could not restore service unit {self.unit_path}: {exc}"
                )
        _remove_empty_directories(self.created_directories)
        return errors

    def remove_for_uninstall(self) -> list[str]:
        if self.operation != "uninstall":
            raise InstallError(
                f"artifact removal mode is not uninstall: {self.operation}"
            )
        current_enablement = _observe_enablement(self.enablement_path)
        if current_enablement.kind != "missing" and not (
            _snapshot_is_owned_enablement(
                self.enablement_path,
                self.unit_path,
                current_enablement,
            )
        ):
            raise OwnershipError(
                f"enablement path became foreign before cleanup: "
                f"{self.enablement_path}; retained "
                f"{_describe_enablement(current_enablement)}"
            )
        current_unit = _observe_unit(self.unit_path)
        if not _snapshot_matches(self.unit_initial, current_unit):
            raise InstallError(
                f"service unit changed before cleanup: {self.unit_path}"
            )

        removed = []
        if current_enablement.kind != "missing":
            self.enablement_path.unlink()
            removed.append(str(self.enablement_path))
        if current_unit.kind != "missing":
            self.unit_path.unlink()
            removed.append(str(self.unit_path))
        return removed


def _remove_owned_root(root: pathlib.Path, manifest: dict) -> None:
    """Delete a validated install root, restoring ownership on partial failure.

    The root is renamed to a reserved sibling before deletion, so a partial
    rmtree cannot strand an ownerless tree at the canonical path. When a
    partial tree remains, the validated manifest is rewritten into it and it
    is renamed back to the canonical root so a later invocation can retry.
    """
    deleting = _reserve_sibling(root.parent, ".free-tts-server-delete-")
    os.replace(root, deleting)
    try:
        shutil.rmtree(deleting)
    except OSError as delete_error:
        if not os.path.lexists(deleting):
            return
        compensation_errors = []
        try:
            _atomic_write(
                manifest_path(deleting),
                json.dumps(manifest, indent=2).encode("utf-8"),
                0o644,
            )
        except BaseException as exc:
            compensation_errors.append(f"could not restore ownership manifest: {exc}")
        if not compensation_errors:
            try:
                if os.path.lexists(root):
                    raise InstallError(f"canonical root was recreated at {root}")
                os.replace(deleting, root)
            except BaseException as exc:
                compensation_errors.append(f"could not restore canonical root: {exc}")
        if compensation_errors:
            details = "; ".join(compensation_errors)
            raise InstallError(
                f"could not remove {root}: {delete_error}; retained partial tree at "
                f"{deleting}; {details}"
            ) from delete_error
        raise InstallError(
            f"could not remove {root}: {delete_error}; ownership restored for retry"
        ) from delete_error


@_serialized_server_transaction
def uninstall_server(
    *,
    root: pathlib.Path | None = None,
    unit_dir: pathlib.Path | None = None,
    systemctl: Callable[..., object] | None = None,
    stop_attempts: int = 10,
    stop_delay: float = 0.2,
    sleeper: Callable[[float], None] | None = None,
) -> list[str]:
    """Remove the service and the server root. Returns removed paths.

    The unit must reach the exact `inactive` state after `disable --now`;
    transitional and failed states are retried, and enablement ownership is
    restored before anything else is removed when the stop cannot be proven.
    """
    root = server_root() if root is None else pathlib.Path(root)
    unit_dir = systemd_user_dir() if unit_dir is None else pathlib.Path(unit_dir)
    runner = systemctl or default_systemctl
    removed: list[str] = []

    expected = _expected_manifest(root, unit_dir)
    owned = _load_manifest(root, expected, missing_ok=True)
    if owned is None:
        logger.warning(
            "No valid %s at %s; leaving all paths unchanged.",
            MANIFEST_NAME,
            root,
        )
        return removed

    artifacts = _SystemdArtifacts.capture_for_uninstall(
        pathlib.Path(owned["unit"]),
        _enablement_path(unit_dir),
    )
    stop_sleeper = time.sleep if sleeper is None else sleeper

    disable_failure = _systemctl_error(runner, ["disable", "--now", UNIT_NAME])
    try:
        _wait_for_unit_inactive(
            runner,
            attempts=stop_attempts,
            delay=stop_delay,
            sleeper=stop_sleeper,
        )
    except BaseException as stop_error:
        try:
            compensation = artifacts.restore()
        except BaseException as exc:
            compensation = [
                f"could not restore service enablement "
                f"{artifacts.enablement_path}: {exc}"
            ]
        detail = f"server uninstall failed:\n- {stop_error}"
        if disable_failure is not None:
            detail += f"\n- {disable_failure}"
        if compensation:
            detail += "\n- " + "\n- ".join(compensation)
        else:
            detail += "\n- service enablement restored for retry"
        raise InstallError(detail) from stop_error

    removed.extend(artifacts.remove_for_uninstall())

    failure = _systemctl_error(runner, ["daemon-reload"])
    if failure is not None:
        raise InstallError(f"server uninstall failed:\n- {failure}")

    _remove_owned_root(root, owned)
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
    except (OSError, json.JSONDecodeError, ValueError, RecursionError):
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
    except SystemExit as exc:
        # argparse exits 0 for --help/--version and 2 for usage errors.
        return 0 if exc.code in (None, 0) else 2

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
        # RuntimeError covers the desktop hierarchy (desktop.install.InstallError
        # and its PrerequisiteError/InstallOwnershipError subclasses); our own
        # errors are all InstallError subclasses.
        except (
            InstallError,
            RuntimeError,
            OSError,
            ImportError,
            subprocess.SubprocessError,
        ) as exc:
            failures += 1
            print(f"{component}: {exc}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
