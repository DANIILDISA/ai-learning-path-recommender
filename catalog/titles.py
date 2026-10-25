"""Слитно написанные слова в названиях программ: «Интенсивпо языковой грамотности».

Буклет в PDF набран так, что пробел перед предлогом иногда пропадает при
извлечении текста («Интенсивпо»). В выгрузке истории то же название написано
правильно -- «Интенсив по языковой грамотности», и человек видел на экране две
разные программы с одним смыслом.

Правило здесь то же, что у `aliases.yaml`: **исправлять только подтверждённое.**
Название чинится, лишь если раздельное написание уже встречается где-то ещё --
в выгрузке истории, в реестре электронных курсов или в файле
`title_spellings.yaml` -- и отличается от слитного только пробелами. Догадка
«давайте отрежем предлог» испортила бы настоящие слова («Отношения»,
«Предпринимательство»), а сверка с написанием, которое заказчик сам видит у
себя, ошибиться не может.
"""
from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import yaml

from ingest.normalize import normalize_text


def flat_key(title: str) -> str:
    """Название без пробелов и пунктуации: по нему слитное и раздельное написание совпадают."""
    return normalize_text(title).replace(" ", "")


def load_title_spellings(path: str | Path) -> list[str]:
    """Написания, подтверждённые выгрузками заказчика (`catalog/title_spellings.yaml`)."""
    file = Path(path)
    if not file.is_file():
        return []
    raw = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    return [str(title).strip() for title in raw.get("spellings") or [] if str(title).strip()]


def known_spellings(titles: Iterable[str]) -> dict[str, str]:
    """Плоский ключ -> написание, в котором слов больше всего.

    Из нескольких написаний одного названия берётся то, где больше пробелов:
    слитное написание -- дефект извлечения текста, а не выбор автора.
    """
    best: dict[str, str] = {}
    for title in titles:
        text = str(title or "").strip()
        if not text:
            continue
        key = flat_key(text)
        current = best.get(key)
        if current is None or _words(text) > _words(current):
            best[key] = text
    return best


def _words(text: str) -> int:
    return len(normalize_text(text).split())


def repair_glued(title: str, known: dict[str, str]) -> str:
    """Название с восстановленными пробелами или оно же, если подтверждения нет."""
    spelled = known.get(flat_key(title))
    if spelled is None:
        return title
    if normalize_text(spelled) == normalize_text(title):
        return title  # то же самое слово в слово: чинить нечего
    if _words(spelled) <= _words(title):
        return title  # раздельное написание обязано быть именно раздельным
    return spelled
