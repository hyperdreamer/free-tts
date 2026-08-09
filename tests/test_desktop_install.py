"""Per-user install, idempotent upgrade, and non-destructive uninstall."""

import json
import pathlib

import pytest

from desktop import install, speechd_config as sc


@pytest.fixture
def source_root(tmp_path):
    """A stand-in checkout with the files the installer copies."""
    root = tmp_path / "checkout"
    (root / "desktop").mkdir(parents=True)
    (root / "server.py").write_text("# server\n")
    (root / "requirements.txt").write_text("flask\n")
    (root / "config.example.json").write_text("{}\n")
    (root / "desktop" / "__init__.py").write_text("")
    (root / "desktop" / "module.py").write_text("# module\n")
    (root / "desktop" / "free-tts.conf").write_text("# module conf\n")
    return root


@pytest.fixture
def paths(tmp_path):
    return {
        "root": tmp_path / "share" / "free-tts",
        "launcher": tmp_path / "libexec" / "speech-dispatcher-modules",
        "config_dir": tmp_path / "config" / "speech-dispatcher",
    }


def _install(source_root, paths, venv_calls=None):
    def venv_builder(root):
        if venv_calls is not None:
            venv_calls.append(root)
        (root / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (root / ".venv" / "bin" / "python").write_text("#!/bin/sh\n")

    return install.install(
        source_root,
        root=paths["root"],
        launcher=paths["launcher"],
        config_dir=paths["config_dir"],
        venv_builder=venv_builder,
    )


class TestInstall:
    def test_copies_runtime_files(self, source_root, paths):
        _install(source_root, paths)
        assert (paths["root"] / "server.py").is_file()
        assert (paths["root"] / "desktop" / "module.py").is_file()
        assert (paths["root"] / "requirements.txt").is_file()

    def test_creates_executable_launcher(self, source_root, paths):
        _install(source_root, paths)
        launcher = paths["launcher"] / install.LAUNCHER_NAME
        assert launcher.is_file()
        assert launcher.stat().st_mode & 0o111
        body = launcher.read_text()
        assert str(paths["root"]) in body
        assert "desktop.module" in body

    def test_installs_module_conf(self, source_root, paths):
        _install(source_root, paths)
        assert (paths["config_dir"] / "modules" / install.MODULE_CONF_NAME).is_file()

    def test_registers_module_in_speechd_conf(self, source_root, paths):
        _install(source_root, paths)
        text = (paths["config_dir"] / "speechd.conf").read_text()
        assert 'AddModule "free-tts"' in text
        assert "DefaultModule free-tts" in text

    def test_builds_the_private_venv(self, source_root, paths):
        calls = []
        _install(source_root, paths, venv_calls=calls)
        assert calls == [paths["root"]]
        assert (paths["root"] / ".venv" / "bin" / "python").is_file()

    def test_writes_manifest(self, source_root, paths):
        _install(source_root, paths)
        manifest = json.loads((paths["root"] / "install-manifest.json").read_text())
        assert manifest["root"] == str(paths["root"])
        assert manifest["launcher"].endswith(install.LAUNCHER_NAME)

    def test_backs_up_existing_speechd_conf_once(self, source_root, paths):
        paths["config_dir"].mkdir(parents=True)
        conf = paths["config_dir"] / "speechd.conf"
        conf.write_text("LogLevel 3\n")
        _install(source_root, paths)
        backup = paths["config_dir"] / "speechd.conf.free-tts.bak"
        assert backup.read_text() == "LogLevel 3\n"
        conf.write_text(conf.read_text() + "# later edit\n")
        _install(source_root, paths)
        assert backup.read_text() == "LogLevel 3\n"

    def test_upgrade_is_idempotent(self, source_root, paths):
        _install(source_root, paths)
        first = (paths["config_dir"] / "speechd.conf").read_text()
        _install(source_root, paths)
        assert (paths["config_dir"] / "speechd.conf").read_text() == first

    def test_upgrade_replaces_stale_runtime_file(self, source_root, paths):
        _install(source_root, paths)
        (paths["root"] / "desktop" / "stale.py").write_text("# gone next time\n")
        _install(source_root, paths)
        assert not (paths["root"] / "desktop" / "stale.py").exists()

    def test_upgrade_preserves_user_config(self, source_root, paths):
        _install(source_root, paths)
        user_config = paths["root"] / "config.json"
        user_config.write_text('{"port": 5001}\n')
        _install(source_root, paths)
        assert user_config.read_text() == '{"port": 5001}\n'

    def test_upgrade_preserves_existing_venv(self, source_root, paths):
        _install(source_root, paths)
        marker = paths["root"] / ".venv" / "marker"
        marker.write_text("keep me\n")
        calls = []
        _install(source_root, paths, venv_calls=calls)
        assert marker.read_text() == "keep me\n"
        assert calls == []

    def test_missing_source_file_is_reported(self, tmp_path, paths):
        empty = tmp_path / "empty"
        empty.mkdir()
        with pytest.raises(FileNotFoundError):
            _install(empty, paths)


class TestUninstall:
    def _uninstall(self, paths):
        return install.uninstall(
            root=paths["root"],
            launcher=paths["launcher"],
            config_dir=paths["config_dir"],
        )

    def test_removes_runtime_launcher_and_block(self, source_root, paths):
        _install(source_root, paths)
        self._uninstall(paths)
        assert not paths["root"].exists()
        assert not (paths["launcher"] / install.LAUNCHER_NAME).exists()
        text = (paths["config_dir"] / "speechd.conf").read_text()
        assert sc.BEGIN_MARKER not in text
        assert "DefaultModule free-tts" not in text

    def test_preserves_unrelated_user_config(self, source_root, paths):
        paths["config_dir"].mkdir(parents=True)
        (paths["config_dir"] / "speechd.conf").write_text("LogLevel 3\n")
        (paths["config_dir"] / "unrelated.conf").write_text("keep\n")
        _install(source_root, paths)
        self._uninstall(paths)
        assert (paths["config_dir"] / "unrelated.conf").read_text() == "keep\n"
        assert "LogLevel 3" in (paths["config_dir"] / "speechd.conf").read_text()

    def test_restores_previous_default_module(self, source_root, paths):
        paths["config_dir"].mkdir(parents=True)
        (paths["config_dir"] / "speechd.conf").write_text("DefaultModule espeak-ng\n")
        _install(source_root, paths)
        self._uninstall(paths)
        assert (
            (paths["config_dir"] / "speechd.conf").read_text()
            == "DefaultModule espeak-ng\n"
        )

    def test_is_idempotent(self, source_root, paths):
        _install(source_root, paths)
        self._uninstall(paths)
        assert self._uninstall(paths) == []

    def test_without_install_is_a_no_op(self, paths):
        assert self._uninstall(paths) == []


class TestPaths:
    def test_launcher_dir_is_user_libexec(self, monkeypatch, tmp_path):
        monkeypatch.setenv("HOME", str(tmp_path))
        assert install.launcher_dir() == (
            tmp_path / ".local" / "libexec" / "speech-dispatcher-modules"
        )

    def test_speechd_config_dir_respects_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert install.speechd_config_dir() == tmp_path / "speech-dispatcher"


class TestRestart:
    def test_failure_is_tolerated(self):
        def runner(*args, **kwargs):
            raise OSError("pkill missing")

        install.restart_speech_dispatcher(runner=runner)

    def test_runs_a_command(self):
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            return type("R", (), {"returncode": 0})()

        install.restart_speech_dispatcher(runner=runner)
        assert calls
