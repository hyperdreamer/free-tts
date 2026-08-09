"""Editing the user's speechd.conf inside a single managed block."""

from desktop import speechd_config as sc


def _apply(text):
    return sc.apply_managed_block(text, "sd_free-tts", "free-tts.conf")


class TestApply:
    def test_adds_block_to_empty_config(self):
        result = _apply("")
        assert sc.BEGIN_MARKER in result
        assert sc.END_MARKER in result
        assert 'AddModule "free-tts" "sd_free-tts" "free-tts.conf"' in result
        assert "DefaultModule free-tts" in result

    def test_preserves_existing_content(self):
        result = _apply("LogLevel 3\n")
        assert result.startswith("LogLevel 3\n")

    def test_is_idempotent(self):
        once = _apply("LogLevel 3\n")
        twice = sc.apply_managed_block(once, "sd_free-tts", "free-tts.conf")
        assert once == twice
        assert twice.count(sc.BEGIN_MARKER) == 1

    def test_replaces_a_stale_block(self):
        stale = f"{sc.BEGIN_MARKER}\nAddModule \"old\" \"sd_old\" \"old.conf\"\n{sc.END_MARKER}\n"
        result = _apply(stale)
        assert "sd_old" not in result
        assert result.count(sc.BEGIN_MARKER) == 1

    def test_comments_out_competing_default_module(self):
        result = _apply("DefaultModule espeak-ng\n")
        assert f"{sc.DISABLED_PREFIX}DefaultModule espeak-ng" in result
        assert "\nDefaultModule espeak-ng" not in result

    def test_leaves_already_commented_default_module_alone(self):
        result = _apply("# DefaultModule espeak-ng\n")
        assert "# DefaultModule espeak-ng" in result
        assert sc.DISABLED_PREFIX not in result

    def test_keeps_other_addmodule_lines(self):
        result = _apply('AddModule "espeak-ng" "sd_espeak-ng" "espeak-ng.conf"\n')
        assert 'AddModule "espeak-ng"' in result

    def test_no_horizontal_rules_or_trailing_blank_growth(self):
        result = _apply("LogLevel 3\n")
        assert result.endswith("\n")
        assert not result.endswith("\n\n\n")


class TestRemove:
    def test_removes_the_block(self):
        result = sc.remove_managed_block(_apply("LogLevel 3\n"))
        assert sc.BEGIN_MARKER not in result
        assert "DefaultModule free-tts" not in result
        assert "LogLevel 3" in result

    def test_restores_a_disabled_default_module(self):
        result = sc.remove_managed_block(_apply("DefaultModule espeak-ng\n"))
        assert "DefaultModule espeak-ng" in result
        assert sc.DISABLED_PREFIX not in result

    def test_is_idempotent(self):
        once = sc.remove_managed_block(_apply("LogLevel 3\n"))
        assert sc.remove_managed_block(once) == once

    def test_untouched_config_is_unchanged(self):
        original = "LogLevel 3\nDefaultVolume 100\n"
        assert sc.remove_managed_block(original) == original

    def test_round_trip_returns_original(self):
        original = "LogLevel 3\nDefaultModule espeak-ng\n"
        assert sc.remove_managed_block(_apply(original)) == original
