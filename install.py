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
from dataclasses import dataclass
import argparse
import contextlib
import errno
import http.client
import json
import logging
import os
import pathlib
import shutil
import socket
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
    except (OSError, json.JSONDecodeError):
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
    except (OSError, json.JSONDecodeError) as exc:
        raise OwnershipError(f"server manifest is corrupt: {exc}") from exc
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


def _snapshot_path(path: pathlib.Path) -> _PathSnapshot:
    if path.is_symlink():
        return _PathSnapshot("symlink", os.readlink(path))
    if path.is_file():
        return _PathSnapshot(
            "file", path.read_bytes(), path.stat().st_mode & 0o7777
        )
    if path.exists():
        raise OwnershipError(f"owned unit path became a directory: {path}")
    return _PathSnapshot("missing")


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
    except (OSError, json.JSONDecodeError) as exc:
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
        if not port.isdigit():
            raise PreflightError(
                "endpoint config port must be an integer or integer-like "
                f"string: {path}"
            )
        port = int(port)
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
    unit_path: pathlib.Path,
    unit_snapshot: _PathSnapshot,
    unit_touched: bool,
    enablement_path: pathlib.Path,
    enablement_snapshot: _PathSnapshot,
    enablement_touched: bool,
    service_touched: bool,
    runner: Callable[..., object],
    previous_active: bool,
    previous_enabled: bool,
) -> list[str]:
    """Restore pre-install files and service state, retaining failed data on error."""
    errors: list[str] = []
    failed_tree: pathlib.Path | None = None

    if service_touched:
        failure = _systemctl_error(runner, ["disable", "--now", UNIT_NAME])
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

    if unit_touched:
        try:
            _restore_path(unit_path, unit_snapshot)
        except BaseException as exc:
            errors.append(f"could not restore service unit {unit_path}: {exc}")

    if service_touched:
        failure = _systemctl_error(runner, ["daemon-reload"])
        if failure is not None:
            errors.append(failure)
        if previous_enabled:
            failure = _systemctl_error(runner, ["enable", UNIT_NAME])
            if failure is not None:
                errors.append(failure)

    if enablement_touched:
        try:
            # Only an absent path or the link we own may be replaced during
            # rollback. A concurrent foreign entry is retained and reported.
            _validate_enablement_link(enablement_path, unit_path)
            _restore_path(enablement_path, enablement_snapshot)
        except BaseException as exc:
            errors.append(
                f"could not restore service enablement {enablement_path}: {exc}"
            )

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
    if upgrading and os.path.lexists(unit_path):
        if unit_path.is_symlink() or not unit_path.is_file():
            raise OwnershipError(
                f"owned service unit must be a regular non-symlink file: {unit_path}"
            )
    unit_snapshot = _snapshot_path(unit_path)
    enablement_path = _enablement_path(unit_dir)
    enablement_present = _validate_enablement_link(enablement_path, unit_path)
    if not upgrading and enablement_present:
        raise OwnershipError(
            f"refusing to overwrite unowned enablement path {enablement_path}"
        )
    enablement_snapshot = _snapshot_path(enablement_path)
    previous_active = (
        _query_unit_state(runner, "is-active", "active") if upgrading else False
    )
    previous_enabled = (
        _query_unit_state(runner, "is-enabled", "enabled") if upgrading else False
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
    unit_touched = False
    enablement_touched = False
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

        _ensure_directory(unit_dir, created_directories)
        written_unit = write_unit(unit_text, unit_dir)
        if _canonical(written_unit) != _canonical(unit_path):
            raise InstallError(
                f"service unit was written outside the expected target: {written_unit}"
            )
        unit_touched = True
        service_touched = True
        _run_systemctl(runner, ["daemon-reload"])
        enablement_touched = True
        _run_systemctl(runner, ["enable", UNIT_NAME])
        _run_systemctl(runner, ["restart", UNIT_NAME])
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
            unit_path=unit_path,
            unit_snapshot=unit_snapshot,
            unit_touched=unit_touched,
            enablement_path=enablement_path,
            enablement_snapshot=enablement_snapshot,
            enablement_touched=enablement_touched,
            service_touched=service_touched,
            runner=runner,
            previous_active=previous_active,
            previous_enabled=previous_enabled,
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
    """The enablement symlink that `systemctl enable` creates for our unit."""
    return pathlib.Path(unit_dir) / "default.target.wants" / UNIT_NAME


def _validate_enablement_link(
    path: pathlib.Path, unit_path: pathlib.Path
) -> bool:
    """Return True for an owned enablement symlink, False when absent.

    A regular file, directory, or symlink resolving outside the validated
    unit path is foreign and raises before any uninstall mutation.
    """
    if not os.path.lexists(path):
        return False
    if not path.is_symlink():
        raise OwnershipError(f"enablement path is not an owned symlink: {path}")
    target = pathlib.Path(os.readlink(path))
    resolved = _canonical(target if target.is_absolute() else path.parent / target)
    if resolved != _canonical(unit_path):
        raise OwnershipError(f"enablement symlink targets a foreign unit: {path}")
    return True


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

    expected = _expected_manifest(root, unit_dir)
    owned = _load_manifest(root, expected, missing_ok=True)
    if owned is None:
        logger.warning(
            "No valid %s at %s; leaving all paths unchanged.",
            MANIFEST_NAME,
            root,
        )
        return removed

    unit_path = pathlib.Path(owned["unit"])
    if os.path.lexists(unit_path):
        if unit_path.is_symlink() or not unit_path.is_file():
            raise OwnershipError(
                f"owned service unit must be a regular non-symlink file: {unit_path}"
            )
    enablement = _enablement_path(unit_dir)
    _validate_enablement_link(enablement, unit_path)

    failure = _systemctl_error(runner, ["disable", "--now", UNIT_NAME])
    if _query_unit_state(runner, "is-active", "active"):
        detail = f"{UNIT_NAME} is still active"
        if failure is not None:
            detail = f"{detail}\n- {failure}"
        raise InstallError(f"server uninstall failed:\n- {detail}")

    if os.path.lexists(enablement):
        # A successful disable normally removes the link itself; only a
        # still-present entry needs removal. Revalidate before unlinking so a
        # foreign replacement is never tolerated or deleted.
        _validate_enablement_link(enablement, unit_path)
        try:
            enablement.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            detail = f"could not remove {enablement}: {exc}"
            if failure is not None:
                detail = f"{detail}\n- {failure}"
            raise InstallError(f"server uninstall failed:\n- {detail}") from exc
        removed.append(str(enablement))

    if failure is not None and os.path.lexists(enablement):
        raise InstallError(
            "server uninstall failed:\n- "
            f"{failure}\n- enablement path remains at {enablement}"
        )

    if os.path.lexists(unit_path):
        try:
            unit_path.unlink()
        except OSError as exc:
            raise InstallError(
                f"server uninstall failed:\n- could not remove {unit_path}: {exc}"
            ) from exc
        removed.append(str(unit_path))

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
