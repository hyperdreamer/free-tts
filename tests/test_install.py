"""Per-user server installer: paths, ownership, staging, unit, and CLI.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
"""

import json
import http.client
import os
import pathlib
import re
import shutil
import stat
import subprocess
import sys
import urllib.error

import pytest

import desktop.install
import install


@pytest.fixture(autouse=True)
def isolated_server_runtime(tmp_path, monkeypatch):
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(runtime))
    return runtime / "free-tts-installer.lock"


def test_server_transaction_lock_rejects_a_second_open(isolated_server_runtime):
    with install._server_transaction_lock():
        with pytest.raises(install.PreflightError, match="another server installer"):
            with install._server_transaction_lock():
                pytest.fail("contender entered the critical section")

    with install._server_transaction_lock():
        pass


def test_server_transaction_lock_preserves_body_oserror_and_releases_lock():
    body_error = OSError("body disk failure")

    with pytest.raises(OSError, match="body disk failure") as excinfo:
        with install._server_transaction_lock():
            raise body_error

    assert excinfo.value is body_error
    with install._server_transaction_lock():
        pass


def test_server_transaction_lock_reuses_stale_unlocked_file(
    isolated_server_runtime,
):
    isolated_server_runtime.write_text("stale diagnostic\n")
    isolated_server_runtime.chmod(0o600)

    with install._server_transaction_lock():
        assert isolated_server_runtime.is_file()


@pytest.mark.parametrize("kind", ("symlink", "directory", "insecure-file"))
def test_server_transaction_lock_rejects_unsafe_lock_path(
    kind, isolated_server_runtime, tmp_path
):
    if kind == "symlink":
        target = tmp_path / "foreign-lock"
        target.write_text("foreign\n")
        isolated_server_runtime.symlink_to(target)
    elif kind == "directory":
        isolated_server_runtime.mkdir()
    else:
        isolated_server_runtime.write_text("insecure\n")
        isolated_server_runtime.chmod(0o644)

    with pytest.raises(install.PreflightError, match="installer lock"):
        with install._server_transaction_lock():
            pytest.fail("unsafe lock entered the critical section")


def test_server_transaction_lock_rejects_foreign_owner(
    isolated_server_runtime, monkeypatch
):
    real_fstat = install.os.fstat

    def foreign_fstat(fd):
        values = list(real_fstat(fd))
        values[4] = os.geteuid() + 1
        return os.stat_result(values)

    monkeypatch.setattr(install.os, "fstat", foreign_fstat)

    with pytest.raises(install.PreflightError, match="owned by the current user"):
        with install._server_transaction_lock():
            pytest.fail("foreign lock entered the critical section")


def test_server_lock_path_rejects_symlinked_runtime(
    isolated_server_runtime, tmp_path, monkeypatch
):
    real_runtime = tmp_path / "real-runtime"
    real_runtime.mkdir()
    linked_runtime = tmp_path / "linked-runtime"
    linked_runtime.symlink_to(real_runtime, target_is_directory=True)
    monkeypatch.setenv("XDG_RUNTIME_DIR", str(linked_runtime))

    with pytest.raises(install.PreflightError, match="XDG_RUNTIME_DIR"):
        install._server_lock_path()


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


def test_uninstall_server_rejects_oversized_manifest_without_mutation(tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    manifest = root / install.MANIFEST_NAME
    manifest.write_text(
        '{"component": "server", "version": ' + ("9" * 5000) + "}"
    )
    unit = unit_dir / install.UNIT_NAME
    unit.write_text("keep unit\n")
    before_root = snapshot_tree(root)
    before_unit_dir = snapshot_tree(unit_dir)
    calls = []

    with pytest.raises(install.OwnershipError) as excinfo:
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=fake_systemctl(calls=calls),
        )

    assert str(manifest) in str(excinfo.value)
    assert snapshot_tree(root) == before_root
    assert snapshot_tree(unit_dir) == before_unit_dir
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


def test_read_manifest_tolerates_oversized_unquoted_json_integer(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    (root / install.MANIFEST_NAME).write_text(
        '{"component": "server", "version": ' + ("9" * 5000) + "}"
    )

    assert install.read_manifest(root) is None


def test_tolerant_json_readers_contain_recursion_error(tmp_path, monkeypatch):
    root = tmp_path / "root"
    root.mkdir()
    server_manifest = root / install.MANIFEST_NAME
    server_manifest.write_text("{}")
    desktop_manifest = tmp_path / install.DESKTOP_MANIFEST_NAME
    desktop_manifest.write_text("{}")

    def fail_json_loads(payload):
        raise RecursionError("nested JSON")

    monkeypatch.setattr(install.json, "loads", fail_json_loads)

    assert install.read_manifest(root) is None
    assert install._read_json(desktop_manifest) is None


def test_load_manifest_normalizes_recursion_error_with_path(tmp_path, monkeypatch):
    root = tmp_path / "root"
    unit_dir = tmp_path / "systemd" / "user"
    root.mkdir()
    manifest = root / install.MANIFEST_NAME
    manifest.write_text("{}")

    def fail_json_loads(payload):
        raise RecursionError("nested JSON")

    monkeypatch.setattr(install.json, "loads", fail_json_loads)

    with pytest.raises(install.OwnershipError) as excinfo:
        install._load_manifest(
            root,
            install._expected_manifest(root, unit_dir),
            missing_ok=False,
        )

    assert str(manifest) in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RecursionError)


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


def test_systemd_artifacts_publish_known_unit_and_enablement_inodes(tmp_path):
    unit_dir = tmp_path / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    artifacts = install._SystemdArtifacts.capture_for_install(
        unit, link, upgrading=False
    )

    artifacts.publish_unit("[Unit]\nDescription=test\n")
    artifacts.ensure_enablement()

    assert artifacts.unit_expected.identity == (
        os.lstat(unit).st_dev,
        os.lstat(unit).st_ino,
    )
    assert artifacts.enablement_expected.identity == (
        os.lstat(link).st_dev,
        os.lstat(link).st_ino,
    )
    assert os.readlink(link) == f"../{install.UNIT_NAME}"


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


class EnablementSystemctl(StatefulSystemctl):
    """Stateful systemctl model paired with a real wants-path filesystem."""

    def __init__(
        self,
        unit_dir,
        *,
        active=False,
        enabled=False,
        fail_once=None,
    ):
        super().__init__(
            active=active,
            enabled=enabled,
            fail_once=fail_once,
        )
        self.unit = pathlib.Path(unit_dir) / install.UNIT_NAME
        self.link = (
            pathlib.Path(unit_dir)
            / "default.target.wants"
            / install.UNIT_NAME
        )


def write_foreign_enablement(link, kind, unit):
    """Create a literal unowned wants-path entry for collision tests."""
    link.parent.mkdir(parents=True, exist_ok=True)
    if kind == "file":
        link.write_text("foreign\n")
    elif kind == "directory":
        link.mkdir()
    elif kind == "foreign-symlink":
        link.symlink_to(link.parent / "foreign.service")
    elif kind == "owned-target-symlink":
        link.symlink_to(pathlib.Path("..") / unit.name)
    else:
        raise AssertionError(f"unknown enablement entry kind: {kind}")


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


def test_probe_health_contains_recursion_error(monkeypatch):
    def fail_json_loads(payload):
        raise RecursionError("nested JSON")

    def fetch(url, timeout):
        return install.json.loads("{}")

    monkeypatch.setattr(install.json, "loads", fail_json_loads)

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


def test_load_service_endpoint_rejects_oversized_unquoted_json_integer(tmp_path):
    config = tmp_path / "config.json"
    config.write_text('{"port": ' + ("9" * 5000) + "}\n")

    with pytest.raises(install.PreflightError) as exc:
        install._load_service_endpoint(config)

    assert str(config) in str(exc.value)
    assert "JSON" in str(exc.value)


def test_load_service_endpoint_normalizes_recursion_error(tmp_path, monkeypatch):
    config = tmp_path / "config.json"
    config.write_text("{}")

    def fail_json_loads(payload):
        raise RecursionError("nested JSON")

    monkeypatch.setattr(install.json, "loads", fail_json_loads)

    with pytest.raises(install.PreflightError) as excinfo:
        install._load_service_endpoint(config)

    assert str(config) in str(excinfo.value)
    assert isinstance(excinfo.value.__cause__, RecursionError)


@pytest.mark.parametrize(
    "port",
    (
        "\N{SUPERSCRIPT TWO}",
        "9"
        * (
            sys.get_int_max_str_digits() + 1
            if sys.get_int_max_str_digits()
            else 10_000
        ),
    ),
)
def test_load_service_endpoint_rejects_unparseable_unicode_and_oversized_ports(
    tmp_path, port
):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"port": port}))

    with pytest.raises(
        install.PreflightError, match=re.escape(str(config))
    ):
        install._load_service_endpoint(config)


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
    link = install._enablement_path(unit.parent)
    assert manifest["component"] == "server"
    assert manifest["root"] == str(root)
    assert manifest["unit"] == str(unit)
    assert manifest["version"] == "2.1.0"
    assert install.read_manifest(root) == manifest
    assert (root / "config.json").exists()
    assert str(root) in unit.read_text()
    assert venv == [root]
    assert ["daemon-reload"] in calls
    assert ["enable", install.UNIT_NAME] not in calls
    assert link.is_symlink()
    assert os.readlink(link) == str(pathlib.Path("..") / install.UNIT_NAME)
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


def test_contended_install_does_not_run_preflight_or_touch_targets(
    checkout, tmp_path, isolated_server_runtime, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    calls = []
    ownership_reads = []
    monkeypatch.setattr(
        install,
        "_check_root_ownership",
        lambda *args: ownership_reads.append(args),
    )

    with install._server_transaction_lock():
        with pytest.raises(install.PreflightError, match="another server installer"):
            install.install_server(
                checkout,
                root=root,
                unit_dir=unit_dir,
                preflight=lambda: calls.append("preflight"),
            )

    assert calls == []
    assert ownership_reads == []
    assert not root.exists()
    assert not unit_dir.exists()


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


def test_install_server_preserves_venv_oserror_rolls_back_and_releases_lock(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = StatefulSystemctl()
    venv_error = OSError("venv disk failure")

    def failing_builder(target):
        assert target == root
        assert (target / install.MANIFEST_NAME).is_file()
        raise venv_error

    with pytest.raises(OSError, match="venv disk failure") as excinfo:
        _transaction_install(
            checkout,
            root,
            unit_dir,
            runner,
            failing_builder,
            healthy_fetch,
        )

    assert excinfo.value is venv_error
    assert not os.path.lexists(root)
    assert not os.path.lexists(unit)
    assert runner.calls == []
    with install._server_transaction_lock():
        pass


def test_install_server_rejects_oversized_upgrade_manifest_without_mutation(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    root.mkdir(parents=True)
    unit_dir.mkdir(parents=True)
    manifest = root / install.MANIFEST_NAME
    manifest.write_text(
        '{"component": "server", "version": ' + ("9" * 5000) + "}"
    )
    (root / "keep.txt").write_text("keep runtime\n")
    unit = unit_dir / install.UNIT_NAME
    unit.write_text("keep unit\n")
    before_root = snapshot_tree(root)
    before_unit_dir = snapshot_tree(unit_dir)
    systemctl_calls = []
    venv_calls = []
    network_calls = []

    def occupancy_probe(host, port, timeout):
        network_calls.append((host, port, timeout))
        return False

    with pytest.raises(install.OwnershipError) as excinfo:
        install.install_server(
            checkout,
            root=root,
            unit_dir=unit_dir,
            venv_builder=lambda target: venv_calls.append(target),
            systemctl=fake_systemctl(calls=systemctl_calls),
            fetch=lambda url, timeout: pytest.fail("health fetch must not run"),
            occupancy_probe=occupancy_probe,
            preflight=lambda: None,
        )

    assert str(manifest) in str(excinfo.value)
    assert snapshot_tree(root) == before_root
    assert snapshot_tree(unit_dir) == before_unit_dir
    assert systemctl_calls == []
    assert venv_calls == []
    assert network_calls == []


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


@pytest.mark.parametrize(
    "kind", ("file", "directory", "foreign-symlink", "owned-target-symlink")
)
def test_install_server_rejects_fresh_enablement_collision_before_mutation(
    kind, checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    write_foreign_enablement(runner.link, kind, unit)
    before_entry = snapshot_tree(runner.link.parent)
    builds = []

    with pytest.raises(install.OwnershipError, match="enablement"):
        _transaction_install(
            checkout,
            root,
            unit_dir,
            runner,
            lambda target: builds.append(target),
            healthy_fetch,
        )

    assert snapshot_tree(runner.link.parent) == before_entry
    assert not root.exists()
    assert not unit.exists()
    assert builds == []
    assert runner.calls == []


@pytest.mark.parametrize("kind", ("file", "directory", "foreign-symlink"))
def test_install_server_rejects_upgrade_enablement_collision_before_mutation(
    kind, checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    runner.link.unlink()
    write_foreign_enablement(runner.link, kind, unit)
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    before_entry = snapshot_tree(runner.link.parent)
    runner.calls.clear()

    with pytest.raises(install.OwnershipError, match="enablement"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert snapshot_tree(runner.link.parent) == before_entry
    assert runner.calls == []


def test_install_server_retains_unit_collision_at_no_replace_publication(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    real_link = install.os.link
    collided = False

    def collide_at_link(source, destination, *args, **kwargs):
        nonlocal collided
        destination = pathlib.Path(destination)
        if destination == unit and not collided:
            collided = True
            unit.write_text("foreign unit\n")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(install.os, "link", collide_at_link)

    with pytest.raises(install.OwnershipError, match="service unit"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert collided is True
    assert unit.read_text() == "foreign unit\n"
    assert not root.exists()
    assert not os.path.lexists(runner.link)
    assert list(root.parent.glob(".free-tts-server-failed-*")) == []


def test_install_server_retains_enablement_collision_at_no_replace_publication(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = unit_dir / "default.target.wants" / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    real_link = install.os.link
    collided = False

    def collide_at_link(source, destination, *args, **kwargs):
        nonlocal collided
        destination = pathlib.Path(destination)
        if destination == link and not collided:
            collided = True
            link.write_text("foreign enablement\n")
        return real_link(source, destination, *args, **kwargs)

    monkeypatch.setattr(install.os, "link", collide_at_link)

    with pytest.raises(install.OwnershipError, match="service enablement"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert collided is True
    assert link.read_text() == "foreign enablement\n"
    assert not unit.exists()
    assert not root.exists()
    assert list(root.parent.glob(".free-tts-server-failed-*")) == []


def test_install_server_fresh_restart_failure_restores_absent_enablement(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir, fail_once="restart")

    with pytest.raises(install.InstallError, match="restart failed"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert not os.path.lexists(runner.link)
    assert not root.exists()
    assert not unit.exists()
    assert runner.calls == [
        ["daemon-reload"],
        ["restart", install.UNIT_NAME],
        ["stop", install.UNIT_NAME],
        ["daemon-reload"],
    ]


def test_install_server_upgrade_restart_failure_restores_exact_enablement(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)
    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    runner.link.unlink()
    runner.link.symlink_to(unit)
    previous_target = os.readlink(runner.link)
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner.fail_once = "restart"
    runner.failed = False
    runner.calls.clear()

    with pytest.raises(install.InstallError, match="restart failed"):
        _transaction_install(
            checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert runner.link.is_symlink()
    assert os.readlink(runner.link) == previous_target
    assert runner.active is True
    assert ["enable", install.UNIT_NAME] not in runner.calls


def test_install_server_success_creates_repairs_and_keeps_owned_enablement(
    checkout, tmp_path
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    runner = EnablementSystemctl(unit_dir)

    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    assert runner.link.is_symlink()
    assert runner.link.resolve(strict=False) == unit.resolve(strict=False)

    runner.link.unlink()
    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    assert runner.link.is_symlink()
    assert runner.link.resolve(strict=False) == unit.resolve(strict=False)

    _transaction_install(
        checkout, root, unit_dir, runner, fake_venv_builder, healthy_fetch
    )
    assert runner.link.is_symlink()
    assert runner.link.resolve(strict=False) == unit.resolve(strict=False)


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
        real_publish_unit = install._SystemdArtifacts.publish_unit

        def failing_unit(artifacts, text):
            fail_once("unit boundary failed")
            return real_publish_unit(artifacts, text)

        monkeypatch.setattr(
            install._SystemdArtifacts,
            "publish_unit",
            failing_unit,
        )
    elif boundary == "enablement":
        real_ensure_enablement = install._SystemdArtifacts.ensure_enablement

        def failing_enablement(artifacts):
            fail_once("enablement boundary failed")
            return real_ensure_enablement(artifacts)

        monkeypatch.setattr(
            install._SystemdArtifacts,
            "ensure_enablement",
            failing_enablement,
        )
    elif boundary in {"daemon-reload", "restart"}:
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
        "enablement",
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
    link = install._enablement_path(unit_dir)
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
    assert not os.path.lexists(link)
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
    assert link.is_symlink()
    assert os.readlink(link) == str(pathlib.Path("..") / install.UNIT_NAME)
    assert_no_transaction_artifacts(root, unit_dir)


@pytest.mark.parametrize(
    "boundary",
    (
        "manifest",
        "publish",
        "unit",
        "daemon-reload",
        "enablement",
        "restart",
        "health",
    ),
)
def test_install_server_upgrade_failure_restores_exact_install_and_retries(
    boundary, checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    link = install._enablement_path(unit_dir)
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
    unit.write_text(
        "[Unit]\n"
        "Description=legacy free-tts server\n"
        "\n"
        "[Service]\n"
        "Type=simple\n"
        "ExecStart=/bin/true\n"
        "\n"
        "[Install]\n"
        "WantedBy=default.target\n"
    )
    unit.chmod(0o640)
    before_root = snapshot_tree(root)
    before_unit = (unit.read_bytes(), stat.S_IMODE(unit.stat().st_mode))
    before_enablement = os.readlink(link)
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
    assert link.is_symlink()
    assert os.readlink(link) == before_enablement
    assert_no_transaction_artifacts(root, unit_dir)

    release()
    manifest = _transaction_install(
        checkout, root, unit_dir, runner, builder, fetch
    )

    assert manifest["version"] == "2.2.0"
    assert (root / "server.py").read_text() == "# server v2\n"
    assert (root / "config.json").read_text() == '{"port": 6123}\n'
    assert runner.active is True
    assert link.is_symlink()
    assert os.readlink(link) == before_enablement
    assert_no_transaction_artifacts(root, unit_dir)


def test_contended_uninstall_does_not_read_manifest_or_call_systemctl(
    tmp_path, isolated_server_runtime, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    calls = []
    manifest_reads = []
    monkeypatch.setattr(
        install,
        "_load_manifest",
        lambda *args, **kwargs: manifest_reads.append((args, kwargs)),
    )

    def runner(args, check=False):
        calls.append(args)
        return FakeCompleted()

    with install._server_transaction_lock():
        with pytest.raises(install.PreflightError, match="another server installer"):
            install.uninstall_server(
                root=root, unit_dir=unit_dir, systemctl=runner
            )

    assert manifest_reads == []
    assert calls == []
    assert not root.exists()
    assert not unit_dir.exists()


def test_uninstall_server_preserves_manifest_oserror_and_releases_lock(
    tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    calls = []
    manifest_error = OSError("manifest disk failure")

    def failing_manifest_load(*args, **kwargs):
        raise manifest_error

    monkeypatch.setattr(install, "_load_manifest", failing_manifest_load)

    with pytest.raises(OSError, match="manifest disk failure") as excinfo:
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=fake_systemctl(calls=calls),
        )

    assert excinfo.value is manifest_error
    assert calls == []
    assert not root.exists()
    assert not unit_dir.exists()
    with install._server_transaction_lock():
        pass


def test_uninstall_server_removes_unit_and_root(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    calls = []

    removed = install.uninstall_server(
        root=tmp_path / "share" / "free-tts-server",
        unit_dir=tmp_path / "config" / "systemd" / "user",
        systemctl=fake_systemctl(
            {
                "is-active": FakeCompleted(
                    returncode=3, stdout="inactive\n"
                )
            },
            calls=calls,
        ),
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
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / "free-tts.service"
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = StatefulSystemctl(active=True, enabled=True, fail_once="disable")
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

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
    assert runner.calls == [
        ["disable", "--now", "free-tts.service"],
        *([["is-active", "free-tts.service"]] * 10),
    ]

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
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

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
    if not os.path.lexists(link):
        link.symlink_to(pathlib.Path("..") / install.UNIT_NAME)
    assert link.is_symlink()
    assert os.readlink(link) == str(pathlib.Path("..") / install.UNIT_NAME)
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


class DisableRemovesEnablementSystemctl(StatefulSystemctl):
    """Real systemd semantics: a successful disable deletes the .wants link."""

    def __init__(self, link):
        super().__init__(active=True, enabled=True)
        self.link = link

    def __call__(self, args, check=True):
        if args[0] == "disable" and os.path.lexists(self.link):
            self.link.unlink()
        return super().__call__(args, check=check)


class DisableReplacesEnablementSystemctl(StatefulSystemctl):
    """Hostile disable that swaps the owned link for a foreign entry."""

    def __init__(self, link):
        super().__init__(active=True, enabled=True)
        self.link = link

    def __call__(self, args, check=True):
        if args[0] == "disable" and os.path.lexists(self.link):
            self.link.unlink()
            self.link.write_text("foreign replacement\n")
        return super().__call__(args, check=check)


class DisableLeavesActiveSystemctl(StatefulSystemctl):
    """A successful-looking disable that fails to stop the service."""

    def __init__(self):
        super().__init__(active=True, enabled=True)

    def __call__(self, args, check=True):
        args = list(args)
        if args[0] == "disable":
            self.calls.append(args)
            self.enabled = False
            return FakeCompleted()
        return super().__call__(args, check=check)


class DisableActivitySequenceSystemctl:
    """Disable removes the wants link, then reports a scripted activity sequence."""

    def __init__(
        self,
        link,
        states,
        *,
        disable_returncode=0,
        reset_returncode=0,
        replacement=None,
    ):
        self.link = link
        self.states = list(states)
        self.last_state = self.states[-1]
        self.disable_returncode = disable_returncode
        self.reset_returncode = reset_returncode
        self.replacement = replacement
        self.calls = []

    def __call__(self, args, check=True):
        args = list(args)
        self.calls.append(args)
        if args[0] == "disable":
            if os.path.lexists(self.link):
                self.link.unlink()
            if self.replacement == "file":
                self.link.write_text("foreign replacement\n")
            return FakeCompleted(
                returncode=self.disable_returncode,
                stderr="disable failed" if self.disable_returncode else "",
            )
        if args[0] == "is-active":
            state = self.states.pop(0) if self.states else self.last_state
            return FakeCompleted(
                returncode=0 if state == "active" else 3,
                stdout=f"{state}\n",
            )
        if args[0] == "reset-failed":
            return FakeCompleted(
                returncode=self.reset_returncode,
                stderr="reset failed" if self.reset_returncode else "",
            )
        return FakeCompleted()


class DisableCreatesOwnedEnablementSystemctl:
    """Disable creates an owned wants link while the unit remains active."""

    def __init__(self, link):
        self.link = link
        self.calls = []

    def __call__(self, args, check=True):
        args = list(args)
        self.calls.append(args)
        if args[0] == "disable":
            self.link.parent.mkdir(parents=True, exist_ok=True)
            self.link.symlink_to(pathlib.Path("..") / install.UNIT_NAME)
            return FakeCompleted()
        if args[0] == "is-active":
            return FakeCompleted(returncode=0, stdout="active\n")
        return FakeCompleted()


def assert_owned_enablement_restored(link, target):
    assert link.is_symlink()
    assert os.readlink(link) == target


def test_uninstall_failed_stop_restores_missing_snapshot_after_disable_creates_link(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = install._enablement_path(unit_dir)
    link.unlink()
    runner = DisableCreatesOwnedEnablementSystemctl(link)
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError, match="did not become inactive") as exc:
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert not os.path.lexists(link)
    assert "service enablement restored for retry" in str(exc.value)
    assert root.is_dir() and unit.is_file()


def test_uninstall_rejects_successful_disable_when_unit_stays_active(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    previous_target = os.readlink(link)
    runner = DisableLeavesActiveSystemctl()
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError, match="did not become inactive"):
        install.uninstall_server(
            root=root, unit_dir=unit_dir, systemctl=runner
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert link.is_symlink()
    assert os.readlink(link) == previous_target
    assert runner.calls == [
        ["disable", "--now", install.UNIT_NAME],
        *([["is-active", install.UNIT_NAME]] * 10),
    ]


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


def test_uninstall_succeeds_when_disable_removes_enablement(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    runner = DisableRemovesEnablementSystemctl(link)

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )

    assert not os.path.lexists(link)
    assert not unit.exists()
    assert not root.exists()
    assert str(unit) in removed
    assert str(root) in removed


def test_uninstall_rejects_replaced_enablement_after_disable(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    runner = DisableReplacesEnablementSystemctl(link)

    with pytest.raises(install.OwnershipError, match="enablement"):
        install.uninstall_server(
            root=root, unit_dir=unit_dir, systemctl=runner
        )

    assert link.read_text() == "foreign replacement\n"
    assert unit.exists()
    assert root.is_dir()


def test_systemd_artifacts_uninstall_validates_both_before_removal(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    artifacts = install._SystemdArtifacts.capture_for_uninstall(unit, link)
    unit.write_text("changed before removal\n")

    with pytest.raises(install.InstallError, match="service unit changed"):
        artifacts.remove_for_uninstall()

    assert link.is_symlink()
    assert unit.read_text() == "changed before removal\n"
    assert root.is_dir()


def test_uninstall_retains_unit_changed_during_disable_before_cleanup(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)

    class UnitChangingDisable(StatefulSystemctl):
        def __call__(self, args, check=False):
            result = super().__call__(args, check=check)
            if args == ["disable", "--now", install.UNIT_NAME]:
                unit.write_text("changed during disable\n")
            return result

    runner = UnitChangingDisable(active=True, enabled=True)

    with pytest.raises(install.InstallError, match="service unit changed"):
        install.uninstall_server(
            root=root, unit_dir=unit_dir, systemctl=runner
        )

    assert unit.read_text() == "changed during disable\n"
    assert root.is_dir()


def test_uninstall_artifact_removal_reports_enablement_unit_and_root(
    checkout, tmp_path
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    runner = StatefulSystemctl(active=True, enabled=True)

    removed = install.uninstall_server(
        root=root, unit_dir=unit_dir, systemctl=runner
    )

    assert removed == [str(link), str(unit), str(root)]
    assert not os.path.lexists(link)
    assert not unit.exists()
    assert not root.exists()


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
    link.unlink()
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


@pytest.mark.parametrize("state", ("active", "activating", "reloading", "deactivating"))
def test_uninstall_noninactive_state_restores_enablement_and_keeps_files(
    state, checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    target = os.readlink(link)
    before_root = snapshot_tree(root)
    before_unit = unit.read_bytes()
    runner = DisableActivitySequenceSystemctl(link, [state, state])
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError, match="did not become inactive"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert snapshot_tree(root) == before_root
    assert unit.read_bytes() == before_unit
    assert_owned_enablement_restored(link, target)


def test_uninstall_waits_from_deactivating_to_inactive(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    sleeps = []
    runner = DisableActivitySequenceSystemctl(link, ["deactivating", "inactive"])
    monkeypatch.setattr(install.time, "sleep", sleeps.append)

    removed = install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=runner,
    )

    assert sleeps == [0.2]
    assert not os.path.lexists(link)
    assert not unit.exists()
    assert not root.exists()
    assert str(unit) in removed and str(root) in removed


def test_uninstall_resets_failed_state_before_cleanup(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    link = write_enablement_link(unit_dir)
    runner = DisableActivitySequenceSystemctl(link, ["failed", "inactive"])
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    install.uninstall_server(
        root=root,
        unit_dir=unit_dir,
        systemctl=runner,
    )

    assert ["reset-failed", install.UNIT_NAME] in runner.calls
    assert not root.exists()


def test_uninstall_reset_failure_restores_enablement(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    target = os.readlink(link)
    runner = DisableActivitySequenceSystemctl(
        link, ["failed"], reset_returncode=7
    )

    with pytest.raises(install.InstallError, match="reset-failed"):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert root.is_dir() and unit.is_file()
    assert_owned_enablement_restored(link, target)


def test_uninstall_compensation_retains_foreign_replacement(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    runner = DisableActivitySequenceSystemctl(
        link, ["active"], replacement="file"
    )
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError) as excinfo:
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert link.read_text() == "foreign replacement\n"
    assert "could not restore service enablement" in str(excinfo.value)
    assert root.is_dir() and unit.is_file()


def test_uninstall_failed_stop_preserves_absent_enablement(
    checkout, tmp_path, monkeypatch
):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = install._enablement_path(unit_dir)
    link.unlink()
    assert not os.path.lexists(link)
    runner = DisableActivitySequenceSystemctl(link, ["deactivating"])
    monkeypatch.setattr(install.time, "sleep", lambda delay: None)

    with pytest.raises(install.InstallError):
        install.uninstall_server(
            root=root,
            unit_dir=unit_dir,
            systemctl=runner,
        )

    assert not os.path.lexists(link)
    assert root.is_dir() and unit.is_file()


def test_uninstall_unknown_activity_restores_enablement(checkout, tmp_path):
    _install_server(checkout, tmp_path)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit = unit_dir / install.UNIT_NAME
    link = write_enablement_link(unit_dir)
    target = os.readlink(link)
    runner = DisableActivitySequenceSystemctl(link, ["unknown"])

    with pytest.raises(install.InstallError, match="unrecognized state"):
        install.uninstall_server(root=root, unit_dir=unit_dir, systemctl=runner)

    assert_owned_enablement_restored(link, target)
    assert root.is_dir() and unit.is_file()


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


def test_status_remains_unlocked_without_xdg_runtime(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_RUNTIME_DIR", raising=False)

    report = install.status(
        root=tmp_path / "missing",
        desktop_root=tmp_path / "desktop",
        unit_dir=tmp_path / "systemd" / "user",
        systemctl=fake_systemctl(),
    )

    assert report["server"]["installed"] is False


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


def test_status_tolerates_oversized_unquoted_desktop_manifest(tmp_path):
    desktop_root = tmp_path / "share" / "free-tts"
    desktop_root.mkdir(parents=True)
    (desktop_root / install.DESKTOP_MANIFEST_NAME).write_text(
        '{"module": "free-tts", "version": ' + ("9" * 5000) + "}"
    )

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


def test_main_status_tolerates_oversized_server_manifest_without_traceback(
    tmp_path, monkeypatch, capsys
):
    root = tmp_path / "share" / "free-tts-server"
    root.mkdir(parents=True)
    (root / install.MANIFEST_NAME).write_text(
        '{"component": "server", "version": ' + ("9" * 5000) + "}"
    )
    desktop_root = tmp_path / "share" / "free-tts"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    monkeypatch.setattr(install, "server_root", lambda: root)
    monkeypatch.setattr(install, "systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(install, "_desktop_root", lambda: desktop_root)
    monkeypatch.setattr(install, "default_systemctl", fake_systemctl())

    code = install.main(["status"])

    captured = capsys.readouterr()
    assert code == 0
    assert "server   not installed" in captured.out
    assert "Traceback" not in captured.out + captured.err


@pytest.mark.parametrize(
    "raw_config",
    (
        json.dumps({"port": "\N{SUPERSCRIPT TWO}"}),
        '{"port": ' + ("9" * 5000) + "}",
    ),
)
def test_main_install_server_aggregates_malformed_port_without_traceback(
    raw_config, checkout, tmp_path, monkeypatch, capsys
):
    config = checkout / "config.example.json"
    config.write_text(raw_config)
    root = tmp_path / "share" / "free-tts-server"
    unit_dir = tmp_path / "config" / "systemd" / "user"
    monkeypatch.setattr(install, "checkout_root", lambda: checkout)
    monkeypatch.setattr(install, "server_root", lambda: root)
    monkeypatch.setattr(install, "systemd_user_dir", lambda: unit_dir)
    monkeypatch.setattr(
        install,
        "default_systemctl",
        fake_systemctl(
            {"is-system-running": FakeCompleted(stdout="running\n")}
        ),
    )

    code = install.main(["install", "server"])

    captured = capsys.readouterr()
    assert code == 1
    assert str(config) in captured.out
    assert "Traceback" not in captured.out + captured.err
    assert not root.exists()


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
