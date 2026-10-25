"""Направления интересов: явная разметка вместо нечёткого совпадения.

Шесть направлений, которые мастер показывает человеку, в данных заказчика не
существуют. Связь между ними и каталогом -- файл, который можно прочитать и
поправить (`interests.yaml`), а не эвристика по словам в названии, которая
молча ошибается. Тот же подход, что у `aliases.yaml`, и по той же причине:
однажды две «вероятные» догадки склеили три разных курса и приписали людям
чужое обучение.

Курс получает направления двумя путями: по подкатегории буклета и поимённо.
Второй путь существует ради 64 электронных курсов реестра, у которых нет ни
подкатегории, ни группы компетенций -- без него интересы не действовали бы на
40% каталога.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from core.models import Course


class InterestIndex:
    """Направление -> подкатегории и курсы, плюс обратный поиск."""

    def __init__(self, raw: dict[str, dict]) -> None:
        self._titles: dict[str, str] = {}
        self._by_subgroup: dict[str, set[str]] = {}
        self._by_course: dict[str, set[str]] = {}
        for key, value in raw.items():
            self._titles[key] = value["title"]
            for subgroup in value.get("subgroups") or []:
                self._by_subgroup.setdefault(subgroup, set()).add(key)
            for course_key in value.get("courses") or []:
                self._by_course.setdefault(course_key, set()).add(key)

    def keys(self) -> set[str]:
        return set(self._titles)

    def title(self, key: str) -> str:
        return self._titles[key]

    def titles(self) -> dict[str, str]:
        """Ключ -> подпись, для мастера и для чипов на карточке.

        Порядок -- как в файле: он повторяет порядок направлений в макете и
        осмысленнее любой сортировки, которую можно применить к подписям.
        """
        return dict(self._titles)

    def directions_for_subgroup(self, subgroup: str | None) -> set[str]:
        """Направления подкатегории. Отсутствие подкатегории -- пустой набор,
        а не ошибка: у 64 курсов реестра её нет вообще."""
        if subgroup is None:
            return set()
        return set(self._by_subgroup.get(subgroup, set()))

    def directions_for_course(self, course: Course) -> set[str]:
        """Направления курса: по подкатегории плюс поимённая разметка."""
        by_subgroup = self.directions_for_subgroup(course.competency_subgroup)
        return by_subgroup | set(self._by_course.get(course.course_key, set()))

    def all_course_keys(self) -> set[str]:
        """Все ключи, размеченные поимённо -- для проверки на опечатки."""
        return set(self._by_course)

    def all_subgroups(self) -> set[str]:
        """Все названные подкатегории -- для проверки на опечатки."""
        return set(self._by_subgroup)


def load_interests(path: str | Path) -> InterestIndex:
    """Прочитать файл направлений.

    Отсутствие файла -- ошибка, а не пустой индекс: тихо потерянные интересы
    выглядят как «мастер не работает», и искать причину пришлось бы долго.
    """
    with open(path, encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    return InterestIndex(raw)
