"""Per-user install, idempotent upgrade, and non-destructive uninstall."""

import json
import os
import pathlib
import shutil
import subprocess
import sys

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


def _install(source_root, paths, venv_calls=None, *, preflight=lambda: None):
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
        preflight=preflight,
    )


def _snapshot(root):
    if not root.exists():
        return None
    entries = {}
    for path in sorted(root.rglob("*")):
        relative = str(path.relative_to(root))
        if path.is_symlink():
            entries[relative] = ("symlink", os.readlink(path))
        elif path.is_dir():
            entries[relative] = ("dir", path.stat().st_mode & 0o777)
        else:
            entries[relative] = (
                "file",
                path.read_bytes(),
                path.stat().st_mode & 0o777,
            )
    return entries


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

    def test_missing_source_file_is_reported_without_target_mutation(
        self, tmp_path, paths
    ):
        empty = tmp_path / "empty"
        empty.mkdir()
        before = _snapshot(tmp_path)
        with pytest.raises(FileNotFoundError):
            _install(empty, paths)
        assert _snapshot(tmp_path) == before


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


class TestOwnershipSafety:
    @pytest.mark.parametrize("collision", ["root", "launcher", "module_conf"])
    def test_fresh_install_refuses_unowned_collisions(
        self, source_root, paths, collision
    ):
        if collision == "root":
            target = paths["root"] / "sentinel"
        elif collision == "launcher":
            target = paths["launcher"] / install.LAUNCHER_NAME
        else:
            target = paths["config_dir"] / "modules" / install.MODULE_CONF_NAME
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("unowned\n")
        before = _snapshot(source_root.parent)

        with pytest.raises(RuntimeError, match="owned|manifest|collision"):
            _install(source_root, paths)

        assert _snapshot(source_root.parent) == before

    def test_missing_manifest_uninstall_preserves_every_conventional_path(self, paths):
        (paths["root"]).mkdir(parents=True)
        (paths["root"] / "sentinel").write_text("runtime owner\n")
        launcher_file = paths["launcher"] / install.LAUNCHER_NAME
        launcher_file.parent.mkdir(parents=True)
        launcher_file.write_text("launcher owner\n")
        module_conf = paths["config_dir"] / "modules" / install.MODULE_CONF_NAME
        module_conf.parent.mkdir(parents=True)
        module_conf.write_text("module owner\n")
        speechd_conf = paths["config_dir"] / "speechd.conf"
        speechd_conf.write_text(
            sc.apply_managed_block(
                "LogLevel 3\n", install.LAUNCHER_NAME, install.MODULE_CONF_NAME
            )
        )
        before = _snapshot(paths["root"].parents[1])

        removed = install.uninstall(
            root=paths["root"],
            launcher=paths["launcher"],
            config_dir=paths["config_dir"],
        )

        assert removed == []
        assert _snapshot(paths["root"].parents[1]) == before

    def test_corrupt_manifest_aborts_without_deleting_anything(self, paths):
        paths["root"].mkdir(parents=True)
        (paths["root"] / install.MANIFEST_NAME).write_text("{not-json")
        (paths["root"] / "sentinel").write_text("keep\n")
        before = _snapshot(paths["root"].parents[1])

        with pytest.raises(RuntimeError, match="manifest"):
            install.uninstall(
                root=paths["root"],
                launcher=paths["launcher"],
                config_dir=paths["config_dir"],
            )

        assert _snapshot(paths["root"].parents[1]) == before

    def test_manifest_path_escape_aborts_before_removal(self, paths, tmp_path):
        outside = tmp_path / "outside-launcher"
        outside.write_text("keep\n")
        paths["root"].mkdir(parents=True)
        manifest = {
            "module": install.MODULE_NAME,
            "root": str(paths["root"]),
            "launcher": str(outside),
            "module_conf": str(
                paths["config_dir"] / "modules" / install.MODULE_CONF_NAME
            ),
            "speechd_conf": str(paths["config_dir"] / "speechd.conf"),
        }
        (paths["root"] / install.MANIFEST_NAME).write_text(json.dumps(manifest))
        before = _snapshot(tmp_path)

        with pytest.raises(RuntimeError, match="manifest|path"):
            install.uninstall(
                root=paths["root"],
                launcher=paths["launcher"],
                config_dir=paths["config_dir"],
            )

        assert _snapshot(tmp_path) == before


class TestUpgradeRollback:
    def _prepare(self, source_root, paths):
        _install(source_root, paths)
        (paths["root"] / "old-only.txt").write_text("old runtime\n")
        (paths["root"] / "config.json").write_text('{"port": 5001}\n')
        (paths["root"] / ".venv" / "marker").write_text("old venv\n")
        (source_root / "server.py").write_text("# replacement server\n")
        return _snapshot(source_root.parent)

    def test_old_tree_rename_failure_preserves_install_byte_for_byte(
        self, source_root, paths, monkeypatch
    ):
        before = self._prepare(source_root, paths)
        real_replace = os.replace

        def fail_old_rename(source, target):
            if pathlib.Path(source) == paths["root"]:
                raise OSError("injected old-tree rename failure")
            return real_replace(source, target)

        monkeypatch.setattr(install.os, "replace", fail_old_rename)
        with pytest.raises(OSError, match="old-tree"):
            _install(source_root, paths)
        assert _snapshot(source_root.parent) == before

    def test_publish_failure_restores_install_byte_for_byte(
        self, source_root, paths, monkeypatch
    ):
        before = self._prepare(source_root, paths)
        real_replace = os.replace
        failed = False

        def fail_publish(source, target):
            nonlocal failed
            source = pathlib.Path(source)
            target = pathlib.Path(target)
            if not failed and target == paths["root"] and source != paths["root"]:
                failed = True
                raise OSError("injected publish failure")
            return real_replace(source, target)

        monkeypatch.setattr(install.os, "replace", fail_publish)
        with pytest.raises(OSError, match="publish"):
            _install(source_root, paths)
        assert _snapshot(source_root.parent) == before

    def test_post_publish_failure_restores_install_byte_for_byte(
        self, source_root, paths, monkeypatch
    ):
        before = self._prepare(source_root, paths)
        module_conf = paths["config_dir"] / "modules" / install.MODULE_CONF_NAME
        real_copy2 = shutil.copy2

        def fail_module_copy(source, target, *args, **kwargs):
            source = pathlib.Path(source)
            target = pathlib.Path(target)
            installed_conf = paths["root"] / "desktop" / install.MODULE_CONF_NAME
            if source == installed_conf and target.parent == module_conf.parent:
                raise OSError("injected module publish failure")
            return real_copy2(source, target, *args, **kwargs)

        monkeypatch.setattr(install.shutil, "copy2", fail_module_copy)
        with pytest.raises(OSError, match="module publish"):
            _install(source_root, paths)
        assert _snapshot(source_root.parent) == before

    def test_rollback_cleanup_failure_retains_complete_old_tree(
        self, source_root, paths, monkeypatch
    ):
        self._prepare(source_root, paths)
        old_tree = _snapshot(paths["root"])
        real_rmtree = shutil.rmtree

        def fail_rollback_cleanup(path, *args, **kwargs):
            path = pathlib.Path(path)
            if path.name.startswith(".free-tts-rollback-"):
                raise OSError("injected rollback cleanup failure")
            return real_rmtree(path, *args, **kwargs)

        monkeypatch.setattr(install.shutil, "rmtree", fail_rollback_cleanup)
        _install(source_root, paths)

        rollback_trees = list(paths["root"].parent.glob(".free-tts-rollback-*"))
        assert len(rollback_trees) == 1
        assert _snapshot(rollback_trees[0]) == old_tree
        assert (paths["root"] / "server.py").read_text() == "# replacement server\n"
        assert (paths["root"] / "config.json").read_text() == '{"port": 5001}\n'
        assert (paths["root"] / ".venv" / "marker").read_text() == "old venv\n"


class TestPreflight:
    @pytest.mark.parametrize(
        "case,expected",
        [
            ("python", "Python 3.11"),
            ("speech-dispatcher", "speech-dispatcher"),
            ("ffmpeg", "ffmpeg"),
            ("libspeechd", "libspeechd"),
        ],
    )
    def test_missing_prerequisite_has_actionable_error(self, case, expected):
        version = (3, 10) if case == "python" else (3, 11)
        executables = {
            "speech-dispatcher": "/usr/bin/speech-dispatcher",
            "ffmpeg": "/usr/bin/ffmpeg",
        }
        if case in executables:
            executables[case] = None
        library = None if case == "libspeechd" else "libspeechd.so.2"

        with pytest.raises(RuntimeError, match=expected):
            install.check_prerequisites(
                version_info=version,
                which=lambda name: executables.get(name),
                library_finder=lambda _name: library,
                runner=lambda *args, **kwargs: type(
                    "Result", (), {"returncode": 0}
                )(),
            )

    def test_failed_preflight_leaves_all_targets_byte_for_byte_unchanged(
        self, source_root, paths
    ):
        paths["root"].mkdir(parents=True)
        (paths["root"] / "sentinel").write_text("keep\n")
        before = _snapshot(source_root.parent)

        def fail_preflight():
            raise RuntimeError("speech-dispatcher is required")

        with pytest.raises(RuntimeError, match="speech-dispatcher"):
            _install(source_root, paths, preflight=fail_preflight)

        assert _snapshot(source_root.parent) == before


class TestLauncherQuoting:
    def test_launcher_executes_from_shell_metacharacter_paths(
        self, source_root, paths, tmp_path
    ):
        paths["root"] = tmp_path / "share with space;dollar$'quote" / "free tts"
        paths["launcher"] = tmp_path / "launcher with spaces"

        def venv_builder(root):
            python = root / ".venv" / "bin" / "python"
            python.parent.mkdir(parents=True)
            python.symlink_to(sys.executable)

        install.install(
            source_root,
            root=paths["root"],
            launcher=paths["launcher"],
            config_dir=paths["config_dir"],
            venv_builder=venv_builder,
            preflight=lambda: None,
        )
        launcher = paths["launcher"] / install.LAUNCHER_NAME

        result = subprocess.run(
            [str(launcher), "/dev/null"],
            cwd=tmp_path,
            env={"PATH": os.environ.get("PATH", "")},
            capture_output=True,
            check=False,
            timeout=10,
        )

        assert result.returncode == 0, result.stderr.decode("utf-8", "replace")


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
