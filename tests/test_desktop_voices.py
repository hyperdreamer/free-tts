"""Voice catalog construction and resolution rules."""

from desktop.voices import Voice, VoiceCatalog

PAYLOAD = {
    "default_voice": "en-US-AvaMultilingualNeural",
    "voices": [
        {
            "ShortName": "en-US-AvaMultilingualNeural",
            "Gender": "Female",
            "Locale": "en-US",
        },
        {"ShortName": "en-US-AndrewNeural", "Gender": "Male", "Locale": "en-US"},
        {"ShortName": "en-GB-SoniaNeural", "Gender": "Female", "Locale": "en-GB"},
        {"ShortName": "fr-FR-DeniseNeural", "Gender": "Female", "Locale": "fr-FR"},
        {"ShortName": "fr-FR-HenriNeural", "Gender": "Male", "Locale": "fr-FR"},
    ],
}


def _catalog():
    return VoiceCatalog.from_payload(PAYLOAD)


class TestFromPayload:
    def test_all_voices_loaded(self):
        assert len(_catalog()) == 5

    def test_default_voice_recorded(self):
        assert _catalog().default_voice == "en-US-AvaMultilingualNeural"

    def test_malformed_payload_is_empty(self):
        assert len(VoiceCatalog.from_payload(["nope"])) == 0

    def test_entries_missing_name_skipped(self):
        catalog = VoiceCatalog.from_payload({"voices": [{"Locale": "en-US"}]})
        assert len(catalog) == 0


class TestProtocolRows:
    def test_rows_use_exact_names_and_variant_none(self):
        rows = _catalog().protocol_rows()
        assert ("en-US-AvaMultilingualNeural", "en-US", "none") in rows
        assert all(variant == "none" for _name, _lang, variant in rows)

    def test_every_voice_is_listed(self):
        assert len(_catalog().protocol_rows()) == 5


class TestResolve:
    def test_exact_synthesis_voice_wins(self):
        voice = _catalog().resolve(
            synthesis_voice="fr-FR-HenriNeural", language="en-US"
        )
        assert voice == Voice("fr-FR-HenriNeural", "fr-FR", "Male")

    def test_null_synthesis_voice_ignored(self):
        voice = _catalog().resolve(synthesis_voice="NULL", language="fr-FR")
        assert voice is not None and voice.locale == "fr-FR"

    def test_unknown_synthesis_voice_falls_back_to_language(self):
        voice = _catalog().resolve(synthesis_voice="nope", language="en-GB")
        assert voice == Voice("en-GB-SoniaNeural", "en-GB", "Female")

    def test_language_underscore_form_matches(self):
        voice = _catalog().resolve(language="fr_FR")
        assert voice is not None and voice.locale == "fr-FR"

    def test_language_prefix_matches_region_variant(self):
        voice = _catalog().resolve(language="fr")
        assert voice is not None and voice.locale == "fr-FR"

    def test_female_voice_type_prefers_female(self):
        voice = _catalog().resolve(language="fr-FR", voice_type="female1")
        assert voice == Voice("fr-FR-DeniseNeural", "fr-FR", "Female")

    def test_male_voice_type_prefers_male(self):
        voice = _catalog().resolve(language="fr-FR", voice_type="male1")
        assert voice == Voice("fr-FR-HenriNeural", "fr-FR", "Male")

    def test_male2_falls_back_when_only_one_male(self):
        voice = _catalog().resolve(language="fr-FR", voice_type="male2")
        assert voice == Voice("fr-FR-HenriNeural", "fr-FR", "Male")

    def test_child_voice_type_still_resolves(self):
        voice = _catalog().resolve(language="en-US", voice_type="child_female")
        assert voice is not None and voice.gender == "Female"

    def test_unknown_language_uses_default_voice(self):
        voice = _catalog().resolve(language="ja-JP")
        assert voice is not None
        assert voice.name == "en-US-AvaMultilingualNeural"

    def test_first_voice_used_when_no_default(self):
        catalog = VoiceCatalog.from_payload({"voices": PAYLOAD["voices"]})
        voice = catalog.resolve(language="ja-JP")
        assert voice is not None
        assert voice.name == "en-US-AvaMultilingualNeural"

    def test_empty_catalog_resolves_to_none(self):
        assert VoiceCatalog.from_payload({}).resolve(language="en-US") is None
