"""Backend health probing, ownership rules, and on-demand startup."""

import contextlib
import dataclasses

import pytest

from desktop import backend, settings


@contextlib.contextmanager
def _noop_lock():
    yield


def _config(**overrides):
    return dataclasses.replace(settings.DEFAULTS, **overrides)


HEALTHY = {
    "status": "ok",
    "service": "free-tts",
    "api_version": 1,
    "voice_cache_ready": True,
}


class _FakeProc:
    def __init__(self, exit_code=None):
        self._exit_code = exit_code
        self.pid = 4242

    def poll(self):
        return self._exit_code


def _controller(responses, *, config=None, spawn=None, clock=None):
    """Build a controller whose probe answers come from a scripted list."""
    calls = {"fetch": 0, "spawn": []}
    queue = list(responses)

    def fetch(url, timeout):
        calls["fetch"] += 1
        item = queue.pop(0) if queue else queue_last[0]
        queue_last[0] = item
        if isinstance(item, Exception):
            raise item
        return item

    queue_last = [responses[-1] if responses else OSError("refused")]

    def default_spawn(command, env, log_path):
        calls["spawn"].append((command, env, log_path))
        return _FakeProc()

    ticks = [0.0]

    def default_clock():
        ticks[0] += 1.0
        return ticks[0]

    ctrl = backend.BackendController(
        config or _config(),
        fetch=fetch,
        spawn=spawn or default_spawn,
        sleep=lambda _seconds: None,
        clock=clock or default_clock,
        lock_factory=_noop_lock,
    )
    return ctrl, calls


class TestProbe:
    def test_healthy_backend(self):
        ctrl, _ = _controller([HEALTHY])
        health = ctrl.probe()
        assert health.reachable is True
        assert health.service_ok is True
        assert health.voice_cache_ready is True

    def test_unreachable_backend(self):
        ctrl, _ = _controller([OSError("connection refused")])
        health = ctrl.probe()
        assert health.reachable is False
        assert health.service_ok is False

    def test_wrong_service_is_not_ok(self):
        ctrl, _ = _controller([{"status": "ok", "service": "grafana"}])
        health = ctrl.probe()
        assert health.reachable is True
        assert health.service_ok is False
        assert "service" in health.detail

    def test_unsupported_api_version_is_not_ok(self):
        ctrl, _ = _controller(
            [{"status": "ok", "service": "free-tts", "api_version": 99}]
        )
        health = ctrl.probe()
        assert health.service_ok is False
        assert "api_version" in health.detail

    def test_non_dict_payload_is_not_ok(self):
        ctrl, _ = _controller([["not", "a", "dict"]])
        assert ctrl.probe().service_ok is False


class TestEnsureReady:
    def test_reuses_healthy_backend_without_spawning(self):
        ctrl, calls = _controller([HEALTHY])
        ctrl.ensure_ready()
        assert calls["spawn"] == []
        assert ctrl.started_by_adapter is False

    def test_starts_backend_when_unreachable(self):
        ctrl, calls = _controller(
            [OSError("refused"), OSError("refused"), HEALTHY]
        )
        ctrl.ensure_ready()
        assert len(calls["spawn"]) == 1
        assert ctrl.started_by_adapter is True

    def test_spawn_passes_idle_timeout(self):
        ctrl, calls = _controller([OSError("refused"), OSError("refused"), HEALTHY])
        ctrl.ensure_ready()
        _command, env, _log = calls["spawn"][0]
        assert env["TTS_IDLE_TIMEOUT"] == "300"

    def test_spawn_omitted_when_autostart_disabled(self):
        ctrl, calls = _controller(
            [OSError("refused")], config=_config(autostart=False)
        )
        with pytest.raises(backend.BackendUnavailable, match="autostart"):
            ctrl.ensure_ready()
        assert calls["spawn"] == []

    def test_port_conflict_never_spawns_or_speaks(self):
        ctrl, calls = _controller([{"status": "ok", "service": "other"}])
        with pytest.raises(backend.BackendUnavailable, match="another service"):
            ctrl.ensure_ready()
        assert calls["spawn"] == []
        assert ctrl.started_by_adapter is False

    def test_second_call_skips_probe_once_ready(self):
        ctrl, calls = _controller([HEALTHY])
        ctrl.ensure_ready()
        first = calls["fetch"]
        ctrl.ensure_ready()
        assert calls["fetch"] == first

    def test_recheck_under_lock_adopts_backend_started_by_racer(self):
        """Unreachable before the lock, healthy inside it: do not spawn."""
        ctrl, calls = _controller([OSError("refused"), HEALTHY])
        ctrl.ensure_ready()
        assert calls["spawn"] == []
        assert ctrl.started_by_adapter is False

    def test_startup_timeout_reports_log_path(self):
        ctrl, _calls = _controller(
            [OSError("refused")], config=_config(startup_timeout=3)
        )
        with pytest.raises(backend.BackendUnavailable) as excinfo:
            ctrl.ensure_ready()
        assert "log" in str(excinfo.value).lower()

    def test_early_exit_reports_exit_code(self):
        def dying_spawn(command, env, log_path):
            return _FakeProc(exit_code=1)

        ctrl, _calls = _controller([OSError("refused")], spawn=dying_spawn)
        with pytest.raises(backend.BackendUnavailable, match="exited"):
            ctrl.ensure_ready()

    def test_never_stops_backend_it_did_not_start(self):
        ctrl, _ = _controller([HEALTHY])
        ctrl.ensure_ready()
        assert not hasattr(ctrl, "stop")
        assert ctrl.started_by_adapter is False


class TestPaths:
    def test_install_root_prefers_explicit_env(self, monkeypatch, tmp_path):
        monkeypatch.setenv("FREE_TTS_HOME", str(tmp_path / "custom"))
        assert backend.install_root() == tmp_path / "custom"

    def test_install_root_uses_xdg_data_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("FREE_TTS_HOME", raising=False)
        monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
        assert backend.install_root() == tmp_path / "free-tts"

    def test_lock_path_uses_runtime_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        assert backend.lock_path() == tmp_path / "free-tts" / "startup.lock"

    def test_real_file_lock_round_trips(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_RUNTIME_DIR", str(tmp_path))
        with backend.file_lock():
            assert backend.lock_path().exists()
