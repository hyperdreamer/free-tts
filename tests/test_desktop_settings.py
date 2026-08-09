"""Adapter config resolution and Speech Dispatcher parameter mapping."""

import json

import pytest

from desktop import settings


class TestLoadConfig:
    def test_defaults_when_file_missing(self, tmp_path):
        cfg = settings.load_config(tmp_path / "nope.json", env={})
        assert cfg == settings.DEFAULTS
        assert cfg.backend_url == "http://127.0.0.1:5000"
        assert cfg.idle_timeout == 300

    def test_file_overrides_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"backend_url": "http://127.0.0.1:6001"}))
        cfg = settings.load_config(path, env={})
        assert cfg.backend_url == "http://127.0.0.1:6001"
        assert cfg.autostart is True

    def test_env_overrides_file(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"backend_url": "http://127.0.0.1:6001"}))
        cfg = settings.load_config(
            path, env={"FREE_TTS_BACKEND_URL": "http://127.0.0.1:7002"}
        )
        assert cfg.backend_url == "http://127.0.0.1:7002"

    def test_trailing_slash_stripped(self, tmp_path):
        cfg = settings.load_config(
            tmp_path / "x.json", env={"FREE_TTS_BACKEND_URL": "http://h:5000/"}
        )
        assert cfg.backend_url == "http://h:5000"

    def test_autostart_false_from_env(self, tmp_path):
        cfg = settings.load_config(
            tmp_path / "x.json", env={"FREE_TTS_AUTOSTART": "false"}
        )
        assert cfg.autostart is False

    def test_malformed_json_falls_back_to_defaults(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text("{not json")
        assert settings.load_config(path, env={}) == settings.DEFAULTS

    def test_invalid_int_falls_back_to_default(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"idle_timeout": "soon"}))
        assert settings.load_config(path, env={}).idle_timeout == 300

    def test_negative_int_clamped_to_zero(self, tmp_path):
        path = tmp_path / "config.json"
        path.write_text(json.dumps({"idle_timeout": -5}))
        assert settings.load_config(path, env={}).idle_timeout == 0

    def test_config_path_respects_xdg(self, monkeypatch, tmp_path):
        monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
        assert settings.config_path() == tmp_path / "free-tts" / "config.json"

    def test_config_path_falls_back_to_home(self, monkeypatch, tmp_path):
        monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
        monkeypatch.setenv("HOME", str(tmp_path))
        assert settings.config_path() == tmp_path / ".config" / "free-tts" / "config.json"


class TestMapRate:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            (0, "+0%"),
            (100, "+200%"),
            (50, "+100%"),
            (-100, "-50%"),
            (-50, "-25%"),
            (200, "+200%"),
            (-200, "-50%"),
        ],
    )
    def test_map_rate(self, raw, expected):
        assert settings.map_rate(raw) == expected

    def test_monotonic(self):
        values = [
            int(settings.map_rate(r).rstrip("%")) for r in range(-100, 101, 10)
        ]
        assert values == sorted(values)


class TestMapPitch:
    @pytest.mark.parametrize(
        "raw,expected",
        [(0, "+0Hz"), (100, "+50Hz"), (-100, "-50Hz"), (50, "+25Hz"), (300, "+50Hz")],
    )
    def test_map_pitch(self, raw, expected):
        assert settings.map_pitch(raw) == expected


class TestMapVolume:
    @pytest.mark.parametrize(
        "raw,expected", [(100, 1.0), (0, 0.5), (-100, 0.0), (-200, 0.0), (200, 1.0)]
    )
    def test_map_volume(self, raw, expected):
        assert settings.map_volume(raw) == pytest.approx(expected)
