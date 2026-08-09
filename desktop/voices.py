"""Voice catalog built from GET /voices, plus Speech Dispatcher resolution.

The Speech Dispatcher variant field is always "none": Qt folds variant into the
locale, so anything else corrupts the locale it reports. Gender is kept here
only to serve symbolic voice types for non-Qt clients.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

_VOICE_TYPE = re.compile(r"^(child_)?(male|female)(\d*)$")


@dataclass(frozen=True)
class Voice:
    """One synthesis voice as exposed to Speech Dispatcher."""

    name: str
    locale: str
    gender: str


def _normalise(tag: str) -> str:
    return tag.replace("_", "-").strip().lower()


class VoiceCatalog:
    """Immutable snapshot of the backend's voice list."""

    def __init__(self, voices: list[Voice], default_voice: str | None) -> None:
        self._voices = voices
        self.default_voice = default_voice

    @classmethod
    def from_payload(cls, payload: object) -> VoiceCatalog:
        """Build a catalog from a parsed /voices response."""
        if not isinstance(payload, dict):
            return cls([], None)
        raw = payload.get("voices")
        voices: list[Voice] = []
        if isinstance(raw, list):
            for entry in raw:
                if not isinstance(entry, dict):
                    continue
                name = str(entry.get("ShortName", "")).strip()
                if not name:
                    continue
                voices.append(
                    Voice(
                        name=name,
                        locale=str(entry.get("Locale", "")).strip(),
                        gender=str(entry.get("Gender", "")).strip(),
                    )
                )
        default = payload.get("default_voice")
        return cls(voices, str(default) if isinstance(default, str) else None)

    def __len__(self) -> int:
        return len(self._voices)

    def protocol_rows(self) -> list[tuple[str, str, str]]:
        """Return (name, language, variant) rows for LIST VOICES."""
        return [(voice.name, voice.locale or "none", "none") for voice in self._voices]

    def _by_name(self, name: str) -> Voice | None:
        lowered = name.strip().lower()
        for voice in self._voices:
            if voice.name.lower() == lowered:
                return voice
        return None

    def _for_language(self, language: str) -> list[Voice]:
        wanted = _normalise(language)
        if not wanted:
            return []
        exact = [v for v in self._voices if _normalise(v.locale) == wanted]
        if exact:
            return exact
        prefix = wanted.split("-", 1)[0]
        return [v for v in self._voices if _normalise(v.locale).split("-", 1)[0] == prefix]

    @staticmethod
    def _pick_by_type(candidates: list[Voice], voice_type: str) -> Voice | None:
        match = _VOICE_TYPE.match(voice_type.strip().lower())
        if not match:
            return None
        wanted_gender = match.group(2)
        index = int(match.group(3)) - 1 if match.group(3) else 0
        gendered = [
            v for v in candidates if v.gender.strip().lower() == wanted_gender
        ]
        if not gendered:
            return None
        return gendered[index] if 0 <= index < len(gendered) else gendered[0]

    def resolve(
        self,
        synthesis_voice: str | None = None,
        language: str | None = None,
        voice_type: str | None = None,
    ) -> Voice | None:
        """Pick a voice: exact name, then locale, then default, then first."""
        if synthesis_voice and synthesis_voice != "NULL":
            exact = self._by_name(synthesis_voice)
            if exact is not None:
                return exact
        if language and language != "NULL":
            candidates = self._for_language(language)
            if candidates:
                if voice_type and voice_type != "NULL":
                    chosen = self._pick_by_type(candidates, voice_type)
                    if chosen is not None:
                        return chosen
                return candidates[0]
        if self.default_voice:
            fallback = self._by_name(self.default_voice)
            if fallback is not None:
                return fallback
        return self._voices[0] if self._voices else None
