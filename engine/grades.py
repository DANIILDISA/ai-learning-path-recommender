"""Classify a raw position string into a ГГС grade, driving the Navigator track.

Human judgement -- which grade a given post title belongs to -- lives as data
in `catalog/positions.yaml`, not as a comment. Every entry carries an explicit
`confirmed` flag so a guess is visible in code (`PositionIndex.unconfirmed()`)
instead of hiding behind YAML prose that nothing reads.
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from pathlib import Path

import yaml

from ingest.normalize import normalize_text


class Grade(str, Enum):
    """ГГС grade group a post belongs to, per 79-ФЗ."""

    SPECIALIST = "specialist"
    MANAGER = "manager"
    SENIOR = "senior"


TRACK_BY_GRADE: dict[Grade, str] = {
    Grade.SPECIALIST: "Специалисты",
    Grade.MANAGER: "Руководители главной и ведущей групп должностей",
    Grade.SENIOR: "Руководители высшей группы должностей",
}


# Должность, которой нет в positions.yaml, но в названии есть признак руководителя.
# Сравнение идёт по целым словам нормализованного названия (ингредиенты склонения --
# по основе): «глава» -- только слово «глава/главы/главе/главу/главой», но НЕ «главный».
# Если слово «специалист» стоит раньше признака («специалист по работе с руководителями»,
# «главный специалист ...»), должность остаётся специалистом. Грейд -- manager: консервативно,
# высшую группу (senior) по догадке не присваиваем.
_LEADER_STEMS = ("начальник", "руководител", "заместител", "директор", "председател", "управляющ")
_LEADER_WORDS = {"глава", "главы", "главе", "главу", "главои", "главою"}
KEYWORD_GRADE = Grade.MANAGER


def _leader_keyword(norm: str) -> str | None:
    """Первый признак руководителя в нормализованной должности или None."""
    for word in norm.split():
        if word.startswith("специалист"):
            return None
        if word in _LEADER_WORDS or word.startswith(_LEADER_STEMS):
            return word
    return None


@dataclass(frozen=True)
class Resolution:
    """Как определён грейд: known (есть в справочнике) | keyword (по слову) | default."""

    grade: Grade
    how: str
    matched: str = ""


class PositionIndex:
    """Normalised-position -> grade lookup, loaded from `positions.yaml`."""

    def __init__(
        self,
        entries: dict[str, dict],
        default_grade: Grade,
    ) -> None:
        self._entries = entries
        self._default_grade = default_grade

    @classmethod
    def load(cls, path: str | Path) -> "PositionIndex":
        raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
        default_grade = Grade(raw["default_grade"])
        entries: dict[str, dict] = {}
        for position_norm, info in (raw.get("positions") or {}).items():
            entries[normalize_text(position_norm)] = {
                "grade": Grade(info["grade"]),
                "confirmed": bool(info.get("confirmed", False)),
            }
        return cls(entries, default_grade)

    def resolve(self, position: str) -> Resolution:
        """Грейд и способ: справочник -> ключевое слово руководителя -> грейд по умолчанию."""
        key = normalize_text(position)
        entry = self._entries.get(key)
        if entry is not None:
            return Resolution(entry["grade"], "known")
        word = _leader_keyword(key)
        if word is not None:
            return Resolution(KEYWORD_GRADE, "keyword", word)
        return Resolution(self._default_grade, "default")

    def grade_of(self, position: str) -> Grade:
        """Grade for a raw position string, normalising it first.

        Unknown positions are resolved by a leadership keyword rule, then fall
        back to `default_grade`; `resolve` / `is_known` tell a caller which path
        was actually used.
        """
        return self.resolve(position).grade

    def is_known(self, position: str) -> bool:
        """True when the position has an explicit entry in positions.yaml."""
        return normalize_text(position) in self._entries

    def unconfirmed(self) -> list[str]:
        """Normalised positions whose grade was guessed, not confirmed.

        Kept as data (not a YAML comment) so it can be surfaced in a report
        rather than silently trusted.
        """
        return sorted(
            key for key, info in self._entries.items() if not info["confirmed"]
        )
