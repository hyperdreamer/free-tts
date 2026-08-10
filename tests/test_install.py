"""Per-user server installer: paths, ownership, staging, unit, and CLI.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
"""

import json
import http.client
import os
import pathlib
import shutil
import stat
import subprocess
import urllib.error

import pytest

import desktop.install
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


def write_server_manifest(install_root, unit_dir, *, version="2.1.0", **overrides):
    """Write a complete server ownership manifest with literal expected paths."""
    payload = {
        "component": "server",
        "root": str(install_root),
        "unit": str(unit_dir / "free-tts.service"),
        "config": str(install_root / "config.json"),
        "python": str(install_root / ".venv" / "bin" / "python"),
        "version": version,
    }
    payload.update(overrides)
    (install_root / "server-manifest.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )
    return payload


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
    unit_dir = tmp_path / "config" / "systemd" / "user"

    upgraded = install.publish_runtime(checkout, root, unit_dir=unit_dir)

    assert upgraded is False
    assert (root / "server.py").read_text() == "# server\n"
    assert (root / "requirements.txt").exists()
    assert (root / "config.example.json").exists()


def test_publish_runtime_refuses_unowned_root(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    (root / "important.txt").write_text("not ours\n")

    with pytest.raises(install.OwnershipError):
        install.publish_runtime(checkout, root, unit_dir=unit_dir)

    assert (root / "important.txt").exists()


def test_publish_runtime_preserves_venv_and_config_on_upgrade(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    install.publish_runtime(checkout, root, unit_dir=unit_dir)
    write_server_manifest(root, unit_dir)
    (root / "config.json").write_text('{"port": 6000}\n')
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (checkout / "server.py").write_text("# server v2\n")

    upgraded = install.publish_runtime(checkout, root, unit_dir=unit_dir)

    assert upgraded is True
    assert (root / "server.py").read_text() == "# server v2\n"
    assert (root / "config.json").read_text() == '{"port": 6000}\n'
    assert (root / ".venv" / "bin" / "python").exists()


def test_publish_runtime_keeps_previous_install_when_restore_fails(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    install.publish_runtime(checkout, root, unit_dir=unit_dir)
    write_server_manifest(root, unit_dir)
    (root / "config.json").write_text('{"port": 6000}\n')

    real_replace = os.replace
    def failing_replace(src, dst):
        # Permit atomic writes and moving the old root aside. The staged-tree
        # publication and compensating restore both target root and fail.
        if pathlib.Path(dst) == root:
            raise OSError("simulated rename failure")
        return real_replace(src, dst)

    monkeypatch.setattr(install.os, "replace", failing_replace)

    with pytest.raises(install.InstallError) as excinfo:
        install.publish_runtime(checkout, root, unit_dir=unit_dir)

    rollback_dirs = [
        p
        for p in root.parent.iterdir()
        if p.name.startswith(".free-tts-server-rollback-")
    ]
    assert len(rollback_dirs) == 1
    assert str(rollback_dirs[0]) in str(excinfo.value)
    assert (rollback_dirs[0] / "config.json").read_text() == '{"port": 6000}\n'
    assert (rollback_dirs[0] / install.MANIFEST_NAME).exists()


def test_publish_runtime_rejects_incomplete_checkout(tmp_path):
    source = tmp_path / "bare"
    source.mkdir()

    with pytest.raises(install.InstallError):
        install.publish_runtime(
            source,
            tmp_path / "share" / "free-tts-server",
            unit_dir=tmp_path / "config" / "systemd" / "user",
        )


def test_publish_runtime_rejects_component_only_manifest_without_mutation(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    important = root / "important.txt"
    important.write_text("not installer data\n")
    (root / "server-manifest.json").write_text(
        json.dumps({"component": "server"}), encoding="utf-8"
    )

    with pytest.raises(install.OwnershipError):
        install.publish_runtime(checkout, root, unit_dir=unit_dir)

    assert important.read_text() == "not installer data\n"


def test_publish_runtime_rejects_symlinked_manifest_without_mutation(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    important = root / "important.txt"
    important.write_text("not installer data\n")
    external = tmp_path / "manifest.json"
    external.write_text(
        json.dumps(
            {
                "component": "server",
                "root": str(root),
                "unit": str(unit_dir / "free-tts.service"),
                "config": str(root / "config.json"),
                "python": str(root / ".venv" / "bin" / "python"),
            }
        ),
        encoding="utf-8",
    )
    (root / "server-manifest.json").symlink_to(external)

    with pytest.raises(install.OwnershipError):
        install.publish_runtime(checkout, root, unit_dir=unit_dir)

    assert important.read_text() == "not installer data\n"
    assert (root / "server-manifest.json").is_symlink()


@pytest.mark.parametrize("field", ("root", "unit", "config", "python"))
def test_publish_runtime_rejects_every_manifest_path_drift(
    field, checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    important = root / "important.txt"
    important.write_text("keep this tree\n")
    write_server_manifest(root, unit_dir, **{field: str(tmp_path / "drifted")})

    with pytest.raises(install.OwnershipError, match=field):
        install.publish_runtime(checkout, root, unit_dir=unit_dir)

    assert important.read_text() == "keep this tree\n"


def test_uninstall_server_rejects_malformed_manifest_before_unit_mutation(tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    (root / "server-manifest.json").write_text("{malformed")
    unit = unit_dir / "free-tts.service"
    unit.write_text("foreign unit\n")
    calls = []

    with pytest.raises(install.OwnershipError, match="corrupt"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=fake_systemctl(calls=calls),
        )

    assert unit.read_text() == "foreign unit\n"
    assert calls == []


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


def test_bootstrap_config_preserves_existing_broken_symlink(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / "config.example.json").write_text('{"port": 5000}\n')
    config = root / "config.json"
    config.symlink_to("missing-user-config.json")

    assert install.bootstrap_config(root) is False
    assert config.is_symlink()
    assert os.readlink(config) == "missing-user-config.json"


def test_bootstrap_config_interrupted_copy_is_atomic_and_retryable(
    tmp_path, monkeypatch
):
    root = tmp_path / "root"
    root.mkdir()
    example = root / "config.example.json"
    example.write_text('{"port": 5000, "voice": "complete"}\n')
    real_copy = install.shutil.copy2

    def interrupted_copy(source, target):
        pathlib.Path(target).write_text('{"port":')
        raise OSError("interrupted copy")

    monkeypatch.setattr(install.shutil, "copy2", interrupted_copy)

    with pytest.raises(OSError, match="interrupted copy"):
        install.bootstrap_config(root)

    assert not (root / "config.json").exists()
    assert sorted(path.name for path in root.iterdir()) == ["config.example.json"]

    monkeypatch.setattr(install.shutil, "copy2", real_copy)
    assert install.bootstrap_config(root) is True
    assert (root / "config.json").read_text() == example.read_text()


def test_render_unit_embeds_paths_and_idle_warning(tmp_path):
    root = tmp_path / "share" / "free-tts-server"

    text = install.render_unit(root)

    assert (
        f'ExecStart=:/usr/bin/env -- "{root / ".venv" / "bin" / "python"}" '
        f'"{root / "server.py"}"' in text
    )
    assert f"WorkingDirectory={root}" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert "idle_timeout" in text
    assert "Environment=FREE_TTS_CONFIG_ONLY=1" in text
    assert "UnsetEnvironment=FLASK_DEBUG" in text
    assert "Environment=TTS_CONFIG" not in text


def test_render_unit_accepts_explicit_python(tmp_path):
    text = install.render_unit(tmp_path / "root", python="/usr/bin/python3")

    assert 'ExecStart=:/usr/bin/env -- "/usr/bin/python3" ' in text


def test_render_unit_quotes_and_escapes_every_systemd_path(tmp_path):
    root = tmp_path / 'home space%quote"slash\\semi;dollar$'
    python = root / "venv path" / "python%3"

    text = install.render_unit(root, python=python)

    escaped_root = f'{tmp_path}/home space%%quote\\"slash\\\\semi;dollar$'
    escaped_python = f'{escaped_root}/venv path/python%%3'
    working_root = f'{tmp_path}/home\\x20space%%quote\\"slash\\\\semi;dollar$'
    assert (
        f'ExecStart=:/usr/bin/env -- "{escaped_python}" '
        f'"{escaped_root}/server.py"' in text
    )
    assert f"WorkingDirectory={working_root}" in text


@pytest.mark.skipif(
    shutil.which("systemd-analyze") is None,
    reason="systemd-analyze is unavailable",
)
def test_render_unit_passes_systemd_analyze_with_metacharacter_paths(tmp_path):
    root = tmp_path / 'home space%quote"slash\\semi;dollar$'
    python = root / "venv path" / "python%3"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\nexit 0\n")
    python.chmod(0o755)
    (root / "server.py").write_text("# server\n")
    unit = tmp_path / "quoted.service"
    unit.write_text(install.render_unit(root, python=python))

    result = subprocess.run(
        ["systemd-analyze", "verify", str(unit)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr


def test_write_unit_creates_then_overwrites(tmp_path):
    unit_dir = tmp_path / "config" / "systemd" / "user"

    path = install.write_unit("[Unit]\nDescription=one\n", unit_dir)
    assert path == unit_dir / install.UNIT_NAME
    assert path.read_text() == "[Unit]\nDescription=one\n"

    install.write_unit("[Unit]\nDescription=two\n", unit_dir)
    assert path.read_text() == "[Unit]\nDescription=two\n"
    assert sorted(p.name for p in unit_dir.iterdir()) == [install.UNIT_NAME]


class FakeCompleted:
    """Stand-in for subprocess.CompletedProcess."""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def fake_systemctl(responses=None, calls=None):
    """Build a systemctl double returning canned answers per first argument."""
    responses = responses or {}

    def run(args, check=True):
        if calls is not None:
            calls.append(list(args))
        return responses.get(args[0], FakeCompleted())

    return run


TEST_MAIN_PID = 4242
TEST_INVOCATION_ID = "a" * 32


def healthy_fetch(url, timeout):
    """Complete payload returned by the real server's health endpoint."""
    return {
        "status": "ok",
        "service": "free-tts",
        "api_version": 1,
        "voice_cache_ready": True,
        "pid": TEST_MAIN_PID,
        "invocation_id": TEST_INVOCATION_ID,
    }


class StatefulSystemctl:
    """Hermetic systemctl model with one-shot command failure injection."""

    def __init__(
        self,
        *,
        active=False,
        enabled=False,
        fail_once=None,
        main_pid=TEST_MAIN_PID,
        invocation_id=TEST_INVOCATION_ID,
    ):
        self.active = active
        self.enabled = enabled
        self.fail_once = fail_once
        self.failed = False
        self.hide_active = False
        self.main_pid = main_pid
        self.invocation_id = invocation_id
        self.calls = []

    def health_payload(self):
        return {
            "status": "ok",
            "service": "free-tts",
            "api_version": 1,
            "voice_cache_ready": True,
            "pid": self.main_pid,
            "invocation_id": self.invocation_id,
        }

    def __call__(self, args, check=True):
        args = list(args)
        self.calls.append(args)
        command = args[0]
        if command == self.fail_once and not self.failed:
            self.failed = True
            return FakeCompleted(returncode=7, stderr=f"{command} failed")
        if command == "show":
            state = "active" if self.active and not self.hide_active else "inactive"
            return FakeCompleted(
                stdout=(
                    f"ActiveState={state}\n"
                    f"MainPID={self.main_pid if state == 'active' else 0}\n"
                    f"InvocationID={self.invocation_id if state == 'active' else ''}\n"
                )
            )
        if command == "is-active":
            reported_active = self.active and not self.hide_active
            state = "active" if reported_active else "inactive"
            return FakeCompleted(
                returncode=0 if reported_active else 3,
                stdout=f"{state}\n",
            )
        if command == "is-enabled":
            state = "enabled" if self.enabled else "disabled"
            return FakeCompleted(
                returncode=0 if self.enabled else 1,
                stdout=f"{state}\n",
            )
        if command == "enable":
            self.enabled = True
        elif command == "restart":
            self.active = True
        elif command == "disable":
            self.enabled = False
            if "--now" in args:
                self.active = False
        elif command == "stop":
            self.active = False
        return FakeCompleted()


def snapshot_tree(root):
    """Capture exact file bytes, modes, directories, and symlink targets."""
    snapshot = {}
    for path in [root, *sorted(root.rglob("*"))]:
        relative = "." if path == root else str(path.relative_to(root))
        metadata = path.lstat()
        mode = stat.S_IMODE(metadata.st_mode)
        if path.is_symlink():
            snapshot[relative] = ("symlink", mode, os.readlink(path))
        elif path.is_dir():
            snapshot[relative] = ("directory", mode)
        else:
            snapshot[relative] = ("file", mode, path.read_bytes())
    return snapshot


def assert_no_transaction_artifacts(root, unit_dir):
    for parent in (root.parent, unit_dir):
        if not parent.exists():
            continue
        assert not [
            path
            for path in parent.iterdir()
            if path.name.startswith(
                (
                    ".free-tts-server-stage-",
                    ".free-tts-server-rollback-",
                    ".free-tts-server-failed-",
                    ".server-manifest.json.",
                    ".config.json.",
                    ".free-tts.service.",
                )
            )
        ]


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


def test_probe_health_contains_non_http_protocol_errors():
    def fetch(url, timeout):
        raise http.client.BadStatusLine("not http")

    assert install.probe_health(fetch=fetch) is None


def test_check_port_free_when_nothing_answers():
    def fetch(url, timeout):
        raise AssertionError("health fetch must not run for a proven-free port")

    assert (
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: False,
            systemctl=fake_systemctl(),
        )
        == "free"
    )


def test_check_port_accepts_our_own_active_unit():
    def fetch(url, timeout):
        return {
            "status": "ok",
            "service": "free-tts",
            "api_version": 1,
            "voice_cache_ready": True,
        }

    active = fake_systemctl({"is-active": FakeCompleted(stdout="active\n")})

    assert (
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: True,
            systemctl=active,
        )
        == "ours"
    )


def test_check_port_rejects_foreign_free_tts_owner():
    def fetch(url, timeout):
        return {
            "status": "ok",
            "service": "free-tts",
            "api_version": 1,
            "voice_cache_ready": True,
        }

    inactive = fake_systemctl(
        {"is-active": FakeCompleted(returncode=3, stdout="inactive\n")}
    )

    with pytest.raises(install.PreflightError) as excinfo:
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: True,
            systemctl=inactive,
        )

    assert "--force" in str(excinfo.value)
    assert (
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: True,
            systemctl=inactive,
            force=True,
        )
        == "forced"
    )


def test_check_port_rejects_unrelated_service():
    def fetch(url, timeout):
        return {"service": "something-else"}

    with pytest.raises(install.PreflightError):
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: True,
            systemctl=fake_systemctl(),
        )


@pytest.mark.parametrize(
    "response",
    [
        "<html>not json</html>",
        ["not", "an", "object"],
        urllib.error.HTTPError(
            "http://127.0.0.1:5000/health", 503, "busy", {}, None
        ),
        ConnectionResetError("reset by peer"),
        http.client.BadStatusLine("SSH-2.0-not-http"),
    ],
    ids=("malformed-body", "non-dict", "http-error", "reset", "non-http"),
)
def test_check_port_treats_every_reachable_nonmatch_as_occupied(response):
    def fetch(url, timeout):
        if isinstance(response, BaseException):
            raise response
        return response

    with pytest.raises(install.PreflightError) as excinfo:
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: True,
            systemctl=fake_systemctl(),
        )

    assert "--force" in str(excinfo.value)
    assert (
        install.check_port(
            fetch=fetch,
            occupancy_probe=lambda host, port, timeout: True,
            systemctl=fake_systemctl(),
            force=True,
        )
        == "forced"
    )


def test_check_port_contains_tcp_probe_errors():
    def failing_probe(host, port, timeout):
        raise OSError("probe unavailable")

    with pytest.raises(install.PreflightError) as excinfo:
        install.check_port(
            occupancy_probe=failing_probe,
            systemctl=fake_systemctl(),
        )

    assert "could not prove" in str(excinfo.value)
    assert (
        install.check_port(
            occupancy_probe=failing_probe,
            systemctl=fake_systemctl(),
            force=True,
        )
        == "forced"
    )


def test_load_service_endpoint_accepts_custom_port(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"host": "127.0.0.1", "port": "6123"}\n')

    endpoint = install._load_service_endpoint(config)

    assert endpoint == install.ServiceEndpoint("127.0.0.1", "127.0.0.1", 6123)
    assert endpoint.health_url == "http://127.0.0.1:6123/health"


@pytest.mark.parametrize(
    "bind_host,probe_host,url",
    (
        ("0.0.0.0", "127.0.0.1", "http://127.0.0.1:6123/health"),
        ("::", "::1", "http://[::1]:6123/health"),
        ("::1", "::1", "http://[::1]:6123/health"),
    ),
)
def test_service_endpoint_maps_probe_hosts(bind_host, probe_host, url):
    endpoint = install.ServiceEndpoint(bind_host, probe_host, 6123)
    assert endpoint.health_url == url


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"host": "", "port": 5000},
        {"host": "127.0.0.1", "port": True},
        {"host": "127.0.0.1", "port": 0},
        {"host": "127.0.0.1", "port": 65536},
        {"host": "127.0.0.1", "port": "not-a-port"},
    ),
)
def test_load_service_endpoint_rejects_invalid_config(tmp_path, payload):
    config = tmp_path / "config.json"
    config.write_text(json.dumps(payload))
    with pytest.raises(install.PreflightError):
        install._load_service_endpoint(config)


def test_load_service_endpoint_rejects_symlink(tmp_path):
    target = tmp_path / "target.json"
    target.write_text('{"port": 6123}\n')
    config = tmp_path / "config.json"
    config.symlink_to(target)
    with pytest.raises(install.PreflightError, match="regular non-symlink"):
        install._load_service_endpoint(config)


def test_check_port_uses_configured_probe_host_and_port():
    endpoint = install.ServiceEndpoint("127.0.0.1", "127.0.0.1", 6123)
    seen = []

    def occupancy(host, port, timeout):
        seen.append((host, port))
        return False

    assert install.check_port(endpoint=endpoint, occupancy_probe=occupancy) == "free"
    assert seen == [("127.0.0.1", 6123)]


def _install_server(checkout, tmp_path, *, calls=None, force=False, venv=None):
    """Install with every side effect injected."""
    def venv_builder(root):
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        if venv is not None:
            venv.append(root)

    runner = StatefulSystemctl(active=True, enabled=True)
    manifest = install.install_server(
        checkout,
        root=tmp_path / "share" / "free-tts-server",
        unit_dir=tmp_path / "config" / "systemd" / "user",
        venv_builder=venv_builder,
        systemctl=runner,
        fetch=healthy_fetch,
        occupancy_probe=lambda host, port, timeout: False,
        preflight=lambda: None,
        force=force,
        verify_attempts=1,
        verify_delay=0,
    )
    if calls is not None:
        calls.extend(runner.calls)
    return manifest


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


def test_install_server_reinstall_verifies_preserved_custom_port(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()

    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    (root / "config.json").write_text('{"host": "127.0.0.1", "port": 6123}\n')

    def custom_fetch(url, timeout):
        assert url == "http://127.0.0.1:6123/health"
        return runner.health_payload()

    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, custom_fetch
    )


def test_verify_rejects_foreign_responder_after_forced_install(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()

    def foreign_fetch(url, timeout):
        payload = runner.health_payload()
        payload["pid"] = 9999
        payload["invocation_id"] = "f" * 32
        return payload

    with pytest.raises(install.InstallError, match="identity"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, foreign_fetch
        )
    assert not os.path.lexists(root)


def test_verify_rejects_identity_change_during_health(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()

    def racing_fetch(url, timeout):
        payload = runner.health_payload()
        runner.main_pid = 5252
        runner.invocation_id = "b" * 32
        return payload

    with pytest.raises(install.InstallError, match="identity"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, racing_fetch
        )
    assert not os.path.lexists(root)


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


def test_install_server_refuses_foreign_unit_when_root_is_missing(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit = unit_dir / "free-tts.service"
    unit.write_text("foreign unit\n")
    runner = StatefulSystemctl()
    builds = []

    with pytest.raises(install.OwnershipError, match="unowned service unit"):
        install.install_server(
            checkout,
            root=root,
            unit_dir=unit_dir,
            venv_builder=lambda target: builds.append(target),
            systemctl=runner,
            fetch=healthy_fetch,
            preflight=lambda: None,
            verify_attempts=1,
        )

    assert not root.exists()
    assert unit.read_text() == "foreign unit\n"
    assert builds == []
    assert runner.calls == []


def test_install_server_rejects_unknown_previous_unit_state_before_mutation(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = StatefulSystemctl(active=True, enabled=True, fail_once="is-active")

    with pytest.raises(install.InstallError, match="is-active.*status 7"):
        _transaction_install(
            checkout,
            root,
            unit_dir,
            runner,
            lambda target: None,
            healthy_fetch,
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert runner.calls == [["is-active", "free-tts.service"]]


def _transaction_install(checkout, root, unit_dir, runner, builder, fetch):
    return install.install_server(
        checkout,
        root=root,
        unit_dir=unit_dir,
        venv_builder=builder,
        systemctl=runner,
        fetch=fetch,
        occupancy_probe=lambda host, port, timeout: False,
        preflight=lambda: None,
        verify_attempts=1,
        verify_delay=0,
    )


def fake_venv_builder(target):
    (target / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
    (target / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")


def _arm_install_failure(boundary, monkeypatch, root, runner):
    pending = {"value": True}
    health_blocked = {"value": boundary == "health"}

    def fail_once(message):
        if pending["value"]:
            pending["value"] = False
            raise install.InstallError(message)

    def builder(target):
        if boundary == "venv":
            fail_once("venv boundary failed")
        (target / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (target / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    if boundary == "config":
        real_copy = install.shutil.copy2

        def failing_copy(source, target):
            if pathlib.Path(target).name.startswith(".config.json."):
                pathlib.Path(target).write_text('{"partial":')
                fail_once("config boundary failed")
            return real_copy(source, target)

        monkeypatch.setattr(install.shutil, "copy2", failing_copy)
    elif boundary == "manifest":
        real_atomic_write = install._atomic_write

        def failing_manifest(path, data, mode=0o644):
            if pathlib.Path(path).name == "server-manifest.json":
                fail_once("manifest boundary failed")
            return real_atomic_write(path, data, mode)

        monkeypatch.setattr(install, "_atomic_write", failing_manifest)
    elif boundary == "publish":
        real_replace = install.os.replace

        def failing_publish(source, target):
            if (
                pathlib.Path(target) == root
                and pathlib.Path(source).name.startswith(".free-tts-server-stage-")
            ):
                fail_once("publish boundary failed")
            return real_replace(source, target)

        monkeypatch.setattr(install.os, "replace", failing_publish)
    elif boundary == "unit":
        real_write_unit = install.write_unit

        def failing_unit(text, unit_dir):
            fail_once("unit boundary failed")
            return real_write_unit(text, unit_dir)

        monkeypatch.setattr(install, "write_unit", failing_unit)
    elif boundary in {"daemon-reload", "enable", "restart"}:
        runner.fail_once = boundary
        runner.failed = False
    elif boundary == "active":
        runner.hide_active = True

    def fetch(url, timeout):
        if health_blocked["value"]:
            return "malformed health body"
        return healthy_fetch(url, timeout)

    def release():
        runner.hide_active = False
        health_blocked["value"] = False

    return builder, fetch, release


def test_install_server_publishes_complete_manifest_and_config_in_staged_root(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl()
    observed = []
    real_replace = install.os.replace

    def inspect_publication(source, target):
        source = pathlib.Path(source)
        if target == root and source.name.startswith(".free-tts-server-stage-"):
            payload = json.loads((source / "server-manifest.json").read_text())
            assert payload == {
                "component": "server",
                "root": str(root),
                "unit": str(unit_dir / "free-tts.service"),
                "config": str(root / "config.json"),
                "python": str(root / ".venv" / "bin" / "python"),
                "version": "2.1.0",
            }
            assert (source / "config.json").read_text() == '{"port": 5000}\n'
            observed.append(source)
        return real_replace(source, target)

    monkeypatch.setattr(install.os, "replace", inspect_publication)

    _transaction_install(
        checkout,
        root,
        unit_dir,
        runner,
        lambda target: (
            (target / ".venv" / "bin").mkdir(parents=True),
            (target / ".venv" / "bin" / "python").write_text("#!/bin/sh\n"),
        ),
        healthy_fetch,
    )

    assert len(observed) == 1


@pytest.mark.parametrize(
    "boundary",
    (
        "config",
        "manifest",
        "publish",
        "venv",
        "unit",
        "daemon-reload",
        "enable",
        "restart",
        "active",
        "health",
    ),
)
def test_install_server_fresh_failure_cleans_up_and_retry_succeeds(
    boundary, checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    runner = StatefulSystemctl()
    builder, fetch, release = _arm_install_failure(
        boundary, monkeypatch, root, runner
    )

    with pytest.raises(install.InstallError, match="failed|active|health"):
        _transaction_install(
            checkout, root, unit_dir, runner, builder, fetch
        )

    assert not os.path.lexists(root)
    assert not os.path.lexists(unit)
    assert not root.parent.exists()
    assert not unit_dir.exists()
    assert runner.active is False
    assert runner.enabled is False
    assert_no_transaction_artifacts(root, unit_dir)

    release()
    manifest = _transaction_install(
        checkout, root, unit_dir, runner, builder, fetch
    )

    assert manifest["version"] == "2.1.0"
    assert (root / "server.py").read_text() == "# server\n"
    assert (root / "config.json").read_text() == '{"port": 5000}\n'
    assert unit.is_file()
    assert runner.active is True
    assert runner.enabled is True
    assert_no_transaction_artifacts(root, unit_dir)


@pytest.mark.parametrize(
    "boundary",
    ("manifest", "publish", "unit", "daemon-reload", "enable", "restart", "health"),
)
def test_install_server_upgrade_failure_restores_exact_install_and_retries(
    boundary, checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    runner = StatefulSystemctl()

    def initial_builder(target):
        (target / ".venv" / "bin").mkdir(parents=True)
        (target / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    _transaction_install(
        checkout,
        root,
        unit_dir,
        runner,
        initial_builder,
        healthy_fetch,
    )
    (root / "config.json").write_text('{"port": 6123}\n')
    before_root = snapshot_tree(root)
    before_unit = (unit.read_bytes(), stat.S_IMODE(unit.stat().st_mode))
    (checkout / "server.py").write_text("# server v2\n")
    (checkout / "VERSION").write_text("2.2.0\n")
    builder, fetch, release = _arm_install_failure(
        boundary, monkeypatch, root, runner
    )

    with pytest.raises(install.InstallError, match="failed|health"):
        _transaction_install(
            checkout, root, unit_dir, runner, builder, fetch
        )

    assert snapshot_tree(root) == before_root
    assert (unit.read_bytes(), stat.S_IMODE(unit.stat().st_mode)) == before_unit
    assert runner.active is True
    assert runner.enabled is True
    assert_no_transaction_artifacts(root, unit_dir)

    release()
    manifest = _transaction_install(
        checkout, root, unit_dir, runner, builder, fetch
    )

    assert manifest["version"] == "2.2.0"
    assert (root / "server.py").read_text() == "# server v2\n"
    assert (root / "config.json").read_text() == '{"port": 6123}\n'
    assert runner.active is True
    assert runner.enabled is True
    assert_no_transaction_artifacts(root, unit_dir)


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


def test_uninstall_server_leaves_foreign_unit_when_root_is_missing(tmp_path):
    root = tmp_path / "missing"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit = unit_dir / "free-tts.service"
    unit.write_text("foreign unit\n")
    calls = []

    removed = install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=fake_systemctl(calls=calls),
    )

    assert removed == []
    assert unit.read_text() == "foreign unit\n"
    assert calls == []


def test_uninstall_server_rejects_manifest_unit_drift_before_mutation(tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    important = root / "important.txt"
    important.write_text("keep root\n")
    unit = unit_dir / "free-tts.service"
    unit.write_text("keep unit\n")
    write_server_manifest(root, unit_dir, unit=str(tmp_path / "other.service"))
    calls = []

    with pytest.raises(install.OwnershipError):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=fake_systemctl(calls=calls),
        )

    assert important.read_text() == "keep root\n"
    assert unit.read_text() == "keep unit\n"
    assert calls == []


def test_uninstall_server_rejects_symlinked_root_before_mutation(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    root = tmp_path / "share" / "free-tts-server"
    root.parent.mkdir(parents=True)
    root.symlink_to(target, target_is_directory=True)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True)
    unit = unit_dir / "free-tts.service"
    unit.write_text("keep unit\n")
    calls = []

    removed = install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=fake_systemctl(calls=calls),
    )

    assert removed == []
    assert root.is_symlink()
    assert unit.read_text() == "keep unit\n"
    assert calls == []


def test_uninstall_server_disable_failure_keeps_runtime_and_unit_for_retry(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = StatefulSystemctl(active=True, enabled=True, fail_once="disable")

    with pytest.raises(install.InstallError, match="disable --now.*status 7"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert runner.active is True
    assert runner.enabled is True
    assert runner.calls == [["disable", "--now", "free-tts.service"]]

    removed = install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=runner,
    )

    assert str(unit) in removed
    assert str(root) in removed
    assert not root.exists()
    assert not unit.exists()
    assert runner.active is False
    assert runner.enabled is False


def test_uninstall_server_daemon_reload_failure_keeps_manifest_for_retry(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    runner = StatefulSystemctl(active=True, enabled=True, fail_once="daemon-reload")

    with pytest.raises(install.InstallError, match="daemon-reload.*status 7"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert root.is_dir()
    assert (root / "server-manifest.json").is_file()
    assert not unit.exists()
    assert runner.active is False
    assert runner.enabled is False

    removed = install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=runner,
    )

    assert removed == [str(root)]
    assert not root.exists()
    assert ["daemon-reload"] == runner.calls[-1]


def test_main_uninstall_server_returns_one_on_systemctl_failure(
    checkout, tmp_path, monkeypatch, capsys
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    runner = StatefulSystemctl(active=True, enabled=True, fail_once="disable")
    monkeypatch.setattr(install, "server_root", lambda: root)
    monkeypatch.setattr(install, "systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(install, "default_systemctl", runner)

    code = install.main(["uninstall", "server"])

    assert code == 1
    assert "disable --now" in capsys.readouterr().out
    assert root.is_dir()
    assert unit.is_file()


def test_main_uninstall_all_continues_desktop_after_server_failure(
    monkeypatch, capsys
):
    performed = []

    def failing_server(**kwargs):
        performed.append("server")
        raise install.InstallError("server cleanup failed")

    monkeypatch.setattr(install, "uninstall_server", failing_server)
    monkeypatch.setattr(
        install,
        "uninstall_desktop",
        lambda **kwargs: performed.append("desktop") or ["/desktop/removed"],
    )

    code = install.main(["uninstall", "all"])

    assert code == 1
    assert performed == ["server", "desktop"]
    output = capsys.readouterr().out
    assert "server cleanup failed" in output
    assert "Removed /desktop/removed" in output


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


def write_enablement_link(unit_dir):
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(pathlib.Path("..") / install.UNIT_NAME)
    return link


class MissingFragmentSystemctl:
    def __init__(self):
        self.calls = []

    def __call__(self, args, check=True):
        args = list(args)
        self.calls.append(args)
        if args[0] == "disable":
            return FakeCompleted(returncode=1, stderr="Unit not found")
        if args[0] == "is-active":
            return FakeCompleted(returncode=3, stdout="inactive\n")
        return FakeCompleted()


def test_uninstall_removes_dangling_owned_enablement_without_fragment(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    unit.unlink()
    link = write_enablement_link(unit_dir)
    runner = MissingFragmentSystemctl()

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )

    assert str(link) in removed
    assert str(root) in removed
    assert not os.path.lexists(link)
    assert not root.exists()
    assert ["disable", "--now", install.UNIT_NAME] in runner.calls


@pytest.mark.parametrize("kind", ("file", "directory", "foreign-symlink"))
def test_uninstall_rejects_foreign_enablement_before_mutation(
    kind, checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        link.write_text("foreign\n")
    elif kind == "directory":
        link.mkdir()
    else:
        link.symlink_to(tmp_path / "foreign.service")
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = StatefulSystemctl(active=True, enabled=True)

    with pytest.raises(install.OwnershipError):
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert os.path.lexists(link)
    assert runner.calls == []


def test_uninstall_partial_root_delete_restores_manifest_and_retries(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl(active=True, enabled=True)
    real_rmtree = install.shutil.rmtree
    failed = False

    def partial_delete(path, *args, **kwargs):
        nonlocal failed
        path = pathlib.Path(path)
        if path.name.startswith(".free-tts-server-delete-") and not failed:
            failed = True
            (path / install.MANIFEST_NAME).unlink()
            (path / "server.py").unlink()
            raise OSError("injected partial deletion")
        return real_rmtree(path, *args, **kwargs)

    monkeypatch.setattr(install.shutil, "rmtree", partial_delete)

    with pytest.raises(install.InstallError, match="ownership restored"):
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    expected = install._expected_manifest(root, unit_dir)
    assert (
        install._load_manifest(root, expected, missing_ok=False)["component"]
        == "server"
    )
    assert not (root / "server.py").exists()

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )
    assert removed == [str(root)]
    assert not root.exists()


def test_uninstall_partial_delete_names_retained_tree_when_compensation_fails(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    runner = StatefulSystemctl(active=True, enabled=True)
    real_rmtree = install.shutil.rmtree
    real_atomic_write = install._atomic_write

    def partial_delete(path, *args, **kwargs):
        path = pathlib.Path(path)
        if path.name.startswith(".free-tts-server-delete-"):
            (path / install.MANIFEST_NAME).unlink()
            raise OSError("injected partial deletion")
        return real_rmtree(path, *args, **kwargs)

    def fail_manifest_restore(path, data, mode=0o644):
        path = pathlib.Path(path)
        if (
            path.name == install.MANIFEST_NAME
            and path.parent.name.startswith(".free-tts-server-delete-")
        ):
            raise OSError("injected receipt failure")
        return real_atomic_write(path, data, mode)

    monkeypatch.setattr(install.shutil, "rmtree", partial_delete)
    monkeypatch.setattr(install, "_atomic_write", fail_manifest_restore)

    with pytest.raises(install.InstallError) as excinfo:
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    retained = list(root.parent.glob(".free-tts-server-delete-*"))
    assert len(retained) == 1
    assert str(retained[0]) in str(excinfo.value)
    assert "injected receipt failure" in str(excinfo.value)
    assert not root.exists()


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


def test_main_help_exits_zero(capsys):
    assert install.main(["--help"]) == 0
    assert "usage" in capsys.readouterr().out


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
        raise desktop.install.PrerequisiteError("speech-dispatcher is missing")

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
