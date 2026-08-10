"""Per-user server installer: paths, ownership, staging, unit, and CLI.

Licensed under the GNU Affero General Public License v3.0 (AGPL-3.0).
"""

import json
import os

import pytest

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

    upgraded = install.publish_runtime(checkout, root)

    assert upgraded is False
    assert (root / "server.py").read_text() == "# server\n"
    assert (root / "requirements.txt").exists()
    assert (root / "config.example.json").exists()


def test_publish_runtime_refuses_unowned_root(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    root.mkdir(parents=True)
    (root / "important.txt").write_text("not ours\n")

    with pytest.raises(install.OwnershipError):
        install.publish_runtime(checkout, root)

    assert (root / "important.txt").exists()


def test_publish_runtime_preserves_venv_and_config_on_upgrade(checkout, tmp_path):
    root = tmp_path / "share" / "free-tts-server"
    install.publish_runtime(checkout, root)
    (root / install.MANIFEST_NAME).write_text(
        json.dumps({"component": "server", "root": str(root)}), encoding="utf-8"
    )
    (root / "config.json").write_text('{"port": 6000}\n')
    (root / ".venv" / "bin").mkdir(parents=True)
    (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
    (checkout / "server.py").write_text("# server v2\n")

    upgraded = install.publish_runtime(checkout, root)

    assert upgraded is True
    assert (root / "server.py").read_text() == "# server v2\n"
    assert (root / "config.json").read_text() == '{"port": 6000}\n'
    assert (root / ".venv" / "bin" / "python").exists()


def test_publish_runtime_keeps_previous_install_when_restore_fails(
    checkout, tmp_path, monkeypatch
):
    root = tmp_path / "share" / "free-tts-server"
    install.publish_runtime(checkout, root)
    (root / install.MANIFEST_NAME).write_text(
        json.dumps({"component": "server", "root": str(root)}), encoding="utf-8"
    )
    (root / "config.json").write_text('{"port": 6000}\n')

    real_replace = os.replace
    calls = {"count": 0}

    def failing_replace(src, dst):
        # First rename moves the old root aside; the staging swap and the
        # compensating restore both fail afterwards.
        calls["count"] += 1
        if calls["count"] > 1:
            raise OSError("simulated rename failure")
        return real_replace(src, dst)

    monkeypatch.setattr(install.os, "replace", failing_replace)

    with pytest.raises(install.InstallError) as excinfo:
        install.publish_runtime(checkout, root)

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
        install.publish_runtime(source, tmp_path / "share" / "free-tts-server")


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


def test_render_unit_embeds_paths_and_idle_warning(tmp_path):
    root = tmp_path / "share" / "free-tts-server"

    text = install.render_unit(root)

    assert f"ExecStart={root / '.venv' / 'bin' / 'python'} {root / 'server.py'}" in text
    assert f"WorkingDirectory={root}" in text
    assert "Restart=on-failure" in text
    assert "WantedBy=default.target" in text
    assert "idle_timeout" in text
    assert "Environment=TTS_CONFIG" not in text


def test_render_unit_accepts_explicit_python(tmp_path):
    text = install.render_unit(tmp_path / "root", python="/usr/bin/python3")

    assert "ExecStart=/usr/bin/python3 " in text


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

    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


def fake_systemctl(responses=None, calls=None):
    """Build a systemctl double returning canned answers per first argument."""
    responses = responses or {}

    def run(args, check=True):
        if calls is not None:
            calls.append(list(args))
        return responses.get(args[0], FakeCompleted())

    return run


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


def test_check_port_free_when_nothing_answers():
    def fetch(url, timeout):
        raise OSError("connection refused")

    assert install.check_port(fetch=fetch, systemctl=fake_systemctl()) == "free"


def test_check_port_accepts_our_own_active_unit():
    def fetch(url, timeout):
        return {"service": "free-tts", "status": "ok"}

    active = fake_systemctl({"is-active": FakeCompleted(stdout="active\n")})

    assert install.check_port(fetch=fetch, systemctl=active) == "ours"


def test_check_port_rejects_foreign_free_tts_owner():
    def fetch(url, timeout):
        return {"service": "free-tts", "status": "ok"}

    inactive = fake_systemctl(
        {"is-active": FakeCompleted(returncode=3, stdout="inactive\n")}
    )

    with pytest.raises(install.PreflightError) as excinfo:
        install.check_port(fetch=fetch, systemctl=inactive)

    assert "--force" in str(excinfo.value)
    assert (
        install.check_port(fetch=fetch, systemctl=inactive, force=True) == "forced"
    )


def test_check_port_rejects_unrelated_service():
    def fetch(url, timeout):
        return {"service": "something-else"}

    with pytest.raises(install.PreflightError):
        install.check_port(fetch=fetch, systemctl=fake_systemctl())


def _install_server(checkout, tmp_path, *, calls=None, force=False, venv=None):
    """Install with every side effect injected."""
    def venv_builder(root):
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")
        if venv is not None:
            venv.append(root)

    return install.install_server(
        checkout,
        root=tmp_path / "share" / "free-tts-server",
        unit_dir=tmp_path / "config" / "systemd" / "user",
        venv_builder=venv_builder,
        systemctl=fake_systemctl(calls=calls),
        preflight=lambda: None,
        force=force,
    )


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
        raise install.InstallError("speech-dispatcher is missing")

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
