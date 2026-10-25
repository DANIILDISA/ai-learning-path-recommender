"""Нечёткая связь названий из истории с каталогом.

Точное совпадение нормализованных названий -- основной путь. Но «Основы права»
в истории и «Основы права.» или «Основы права (обновл.)» в реестре -- один курс,
и без связи человек, прошедший его, получит его же в рекомендациях.

Правила осторожные, потому что ложная связь хуже отсутствующей (курс исчезнет
из рекомендаций как «уже пройденный»):

* сходство не ниже порога (по умолчанию 0.95);
* числа в названиях должны совпадать («Основы 1» не равно «Основы 2»);
* лучший кандидат должен заметно отрываться от второго;
* каждая связь попадает в отчёт качества и в паспорт данных.
"""
from __future__ import annotations

import re
from dataclasses import dataclass

try:
    from rapidfuzz import fuzz, process

    def _score(a: str, b: str) -> float:
        return max(fuzz.ratio(a, b), fuzz.token_sort_ratio(a, b)) / 100.0

except Exception:  # pragma: no cover - запасной вариант без rapidfuzz
    from difflib import SequenceMatcher

    def _score(a: str, b: str) -> float:
        return SequenceMatcher(None, a, b).ratio()

    process = None  # type: ignore[assignment]

DEFAULT_THRESHOLD = 0.95
MIN_MARGIN = 0.03
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True)
class FuzzyLink:
    source_key: str
    target_key: str
    score: float


def _numbers(text: str) -> tuple[str, ...]:
    return tuple(_DIGITS.findall(text))


def fuzzy_link(
    unmatched: set[str] | list[str],
    catalog_keys: set[str],
    threshold: float = DEFAULT_THRESHOLD,
) -> list[FuzzyLink]:
    """Link normalised history keys without an exact catalogue match to the closest key."""
    catalog = sorted(catalog_keys)
    links: list[FuzzyLink] = []
    for source in sorted(unmatched):
        if source in catalog_keys or not source:
            continue
        scored: list[tuple[float, str]] = []
        for target in catalog:
            # дешёвая отсечка по длине до дорогого сравнения
            if abs(len(target) - len(source)) > max(6, 0.15 * len(source)):
                continue
            score = _score(source, target)
            if score >= threshold and _numbers(source) == _numbers(target):
                scored.append((score, target))
        if not scored:
            continue
        scored.sort(reverse=True)
        best = scored[0]
        if len(scored) > 1 and best[0] - scored[1][0] < MIN_MARGIN:
            continue
        links.append(FuzzyLink(source, best[1], round(best[0], 3)))
    return links
