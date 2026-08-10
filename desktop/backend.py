"""Backend discovery, ownership, and on-demand startup.

Ownership rule, enforced structurally: this module offers no way to stop a
backend. A backend found already running is used and left alone; one this
adapter starts is given a self-managed idle timeout instead of being supervised.
"""

from __future__ import annotations

import contextlib
import fcntl
import http.client
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator
from dataclasses import dataclass

from desktop.settings import (
    AdapterConfig,
    ConfigError,
    validate_backend_url,
)

logger = logging.getLogger("free-tts.backend")

EXPECTED_API_VERSION = 1
_SERVICE_NAME = "free-tts"
_PROBE_TIMEOUT = 3.0
_MAX_LOG_BYTES = 1 << 20


class BackendUnavailable(Exception):
    """The backend cannot be used, with a reason safe to log."""


@dataclass(frozen=True)
class Health:
    """Outcome of one /health probe."""

    reachable: bool
    service_ok: bool
    voice_cache_ready: bool
    detail: str = ""
    status_ok: bool = False

    @property
    def ready(self) -> bool:
        """True only when the expected service is ready to serve voices."""
        return self.service_ok and self.status_ok and self.voice_cache_ready


def install_root() -> pathlib.Path:
    """Directory holding the installed runtime copy."""
    explicit = os.environ.get("FREE_TTS_HOME")
    if explicit:
        return pathlib.Path(explicit)
    xdg = os.environ.get("XDG_DATA_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "share"
    return base / "free-tts"


def runtime_log_path() -> pathlib.Path:
    """Log file for a backend this adapter starts."""
    xdg = os.environ.get("XDG_CACHE_HOME")
    base = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".cache"
    return base / "free-tts" / "backend.log"


def lock_path() -> pathlib.Path:
    """Startup lock, so concurrent first requests start at most one backend."""
    runtime = os.environ.get("XDG_RUNTIME_DIR")
    base = (
        pathlib.Path(runtime)
        if runtime
        else pathlib.Path(os.environ.get("TMPDIR", "/tmp"))
    )
    return base / "free-tts" / "startup.lock"


@contextlib.contextmanager
def file_lock() -> Iterator[None]:
    """Hold an exclusive advisory lock for the duration of the block."""
    path = lock_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(handle, fcntl.LOCK_UN)
        finally:
            os.close(handle)


def _fetch_json(url: str, timeout: float) -> object:
    """GET and parse JSON. Raises OSError when the endpoint is unreachable."""
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return {"status": "error", "http_status": exc.code}
    except http.client.InvalidURL:
        # A malformed URL is a configuration fault, not a transfer fault: the
        # caller maps it to ConfigError, so it must not be normalised here.
        raise
    except (
        urllib.error.URLError,
        http.client.HTTPException,
        TimeoutError,
        json.JSONDecodeError,
    ) as exc:
        raise OSError(str(exc)) from exc


def _spawn_backend(
    command: list[str], env: dict[str, str], log_path: pathlib.Path
) -> subprocess.Popen:
    """Start the backend detached, with output appended to ``log_path``."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    if log_path.exists() and log_path.stat().st_size > _MAX_LOG_BYTES:
        log_path.write_bytes(b"")
    handle = log_path.open("ab", buffering=0)
    return subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=handle,
        stderr=handle,
        env=env,
        start_new_session=True,
        cwd=str(install_root()),
    )


class BackendController:
    """Finds, validates, and if needed starts the synthesis backend."""

    def __init__(
        self,
        config: AdapterConfig,
        *,
        fetch: Callable[[str, float], object] | None = None,
        spawn: Callable[[list[str], dict[str, str], pathlib.Path], object]
        | None = None,
        sleep: Callable[[float], None] = time.sleep,
        clock: Callable[[], float] = time.monotonic,
        lock_factory: Callable[[], object] | None = None,
    ) -> None:
        self._config = config
        self._fetch = fetch or _fetch_json
        self._spawn = spawn or _spawn_backend
        self._sleep = sleep
        self._clock = clock
        self._lock_factory = lock_factory or file_lock
        self._ready = False
        self._started_by_adapter = False

    @property
    def started_by_adapter(self) -> bool:
        """True only when this adapter launched the running backend."""
        return self._started_by_adapter

    def probe(self) -> Health:
        """Check the backend's identity and readiness."""
        url = f"{self._config.backend_url}/health"
        try:
            payload = self._fetch(url, _PROBE_TIMEOUT)
        except OSError as exc:
            return Health(False, False, False, str(exc))
        except (ValueError, UnicodeError, http.client.InvalidURL) as exc:
            # URL construction can fail below urllib.parse, including while
            # http.client converts the port.
            return Health(False, False, False, f"invalid backend_url: {exc}")
        if not isinstance(payload, dict):
            return Health(True, False, False, "health response was not an object")
        if payload.get("service") != _SERVICE_NAME:
            return Health(
                True,
                False,
                False,
                f"unexpected service {payload.get('service')!r} on this port",
            )
        try:
            version = int(payload.get("api_version", 0))
        except (TypeError, ValueError):
            version = 0
        if version != EXPECTED_API_VERSION:
            return Health(
                True, False, False, f"unsupported api_version {version!r}"
            )
        status_ok = payload.get("status") == "ok"
        voice_cache_ready = payload.get("voice_cache_ready") is True
        detail = ""
        if not status_ok:
            detail = f"backend status is {payload.get('status')!r}"
        elif not voice_cache_ready:
            detail = "backend voice cache is not ready"
        return Health(
            True,
            True,
            voice_cache_ready,
            detail,
            status_ok=status_ok,
        )

    def ensure_ready(self) -> None:
        """Guarantee a usable backend, revalidating it on every boundary."""
        self._ready = False
        try:
            validate_backend_url(self._config.backend_url)
        except ConfigError as exc:
            raise BackendUnavailable(f"invalid backend_url: {exc}") from exc
        health = self.probe()
        if health.ready:
            logger.info("Reusing the backend already running; leaving it alone.")
            self._ready = True
            return
        if not health.reachable:
            self._started_by_adapter = False
        self._reject_if_occupied(health)
        if not self._config.autostart and not health.service_ok:
            raise BackendUnavailable(
                "backend is not running and autostart is disabled"
            )
        with self._lock_factory():  # type: ignore[union-attr]
            health = self.probe()
            if health.ready:
                logger.info("Backend appeared while waiting for the lock.")
                self._ready = True
                return
            self._reject_if_occupied(health)
            if health.service_ok:
                self._wait_until_ready(process=None, started_by_adapter=False)
                return
            if not self._config.autostart:
                raise BackendUnavailable(
                    "backend is not running and autostart is disabled"
                )
            self._start_and_wait()

    def _reject_if_occupied(self, health: Health) -> None:
        if health.reachable and not health.service_ok:
            raise BackendUnavailable(
                f"{self._config.backend_url} is served by another service; "
                f"refusing to start a second backend or speak to it "
                f"({health.detail})"
            )

    def _server_command(self) -> list[str]:
        root = install_root()
        venv_python = root / ".venv" / "bin" / "python"
        interpreter = str(venv_python) if venv_python.exists() else sys.executable
        return [interpreter, str(root / "server.py")]

    def _spawn_env(self) -> dict[str, str]:
        parsed = urllib.parse.urlparse(self._config.backend_url)
        env = dict(os.environ)
        env["TTS_IDLE_TIMEOUT"] = str(self._config.idle_timeout)
        if parsed.hostname:
            env["TTS_HOST"] = parsed.hostname
        env["TTS_PORT"] = str(parsed.port or 5000)
        env.pop("FLASK_DEBUG", None)
        return env

    def _start_and_wait(self) -> None:
        log_path = runtime_log_path()
        command = self._server_command()
        logger.info("Starting backend on demand: %s", " ".join(command))
        process = self._spawn(command, self._spawn_env(), log_path)
        self._wait_until_ready(process=process, started_by_adapter=True)

    def _wait_until_ready(
        self, *, process: object | None, started_by_adapter: bool
    ) -> None:
        log_path = runtime_log_path()
        deadline = self._clock() + self._config.startup_timeout
        while self._clock() < deadline:
            if process is not None:
                exit_code = process.poll()  # type: ignore[attr-defined]
                if exit_code is not None:
                    raise BackendUnavailable(
                        f"backend exited with code {exit_code} during startup; "
                        f"see log {log_path}"
                    )
            health = self.probe()
            if health.ready:
                self._ready = True
                self._started_by_adapter = started_by_adapter
                logger.info("Backend ready; idle shutdown handled by the backend.")
                return
            self._reject_if_occupied(health)
            self._sleep(0.25)
        raise BackendUnavailable(
            f"backend did not become ready within "
            f"{self._config.startup_timeout}s; see log {log_path}"
        )
