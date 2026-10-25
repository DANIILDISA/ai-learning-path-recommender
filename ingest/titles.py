"""«Одежда» названий курсов в сырой выгрузке и её снятие.

Система обучения выгружает названия учебных групп вместе со служебной
обвязкой::

    2024_01_27-2024_02_01_Критическое мышление (ГЗ)(ОДО)
    05.12.2024-10.12.2024 Основы права (ДОТ)
    2024_01-27-2024_02_01_Название (120)(ГЗ)(анкетирование)

Нам нужно само название программы. Правила:

* любые даты в начале (одна или диапазон, разделители ``_ - . /``, в том числе
  кривые вроде ``2024_01-27`` и заглушка ``9999_99_99``) снимаются;
* в конце снимаются скобочные теги: известные слова (ГЗ, ОДО, ДОТ, очно,
  анкетирование ...) -- всегда, а любые короткие ЗАГЛАВНЫЕ сокращения и числа
  вроде ``(120)`` -- только если у названия была дата в начале (иначе это может
  быть настоящая часть названия, например «(ИКТ)»);
* содержательные скобки («Основы права (для руководителей)») не трогаются;
* несбалансированный хвост ``(ГЗ(ОДО)`` разбирается так же.

Признак «это учебная группа» (а значит ППК, а не электронный курс) выводится
из даты в начале или из групповых тегов.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from core.models import Delivery

_SEP = r"[._\-/]"
_MONTHS = (
    "января|февраля|марта|апреля|мая|июня|июля|августа|сентября|октября|ноября|декабря"
)
_DATE = (
    rf"(?:\d{{4}}{_SEP}\d{{1,2}}{_SEP}\d{{1,2}}"            # 2024_01_27, 2024-1-5, 9999_99_99
    rf"|\d{{1,2}}\.\d{{1,2}}\.(?:\d{{4}}|\d{{2}})"           # 05.12.2024, 5.12.24
    rf"|\d{{1,2}}[/\-_]\d{{1,2}}[/\-_]\d{{4}}"               # 05-12-2024
    rf"|\d{{1,2}}\s+(?:{_MONTHS})(?:\s+\d{{4}})?)"           # 5 декабря 2024
)
_DATE_PREFIX = re.compile(
    rf"^\s*(?:с\s+)?{_DATE}(?:\s*(?:[-–—_]|по)\s*{_DATE})?(?:\s*г(?:ода|\.)?)?[\s_\-–—:.,;]*",
    re.IGNORECASE,
)
_TRAILING_GROUP = re.compile(r"\(\s*([^()]*?)\s*\)\s*$")
_TRAILING_OPEN = re.compile(r"\(\s*([^()]*?)\s*$")        # несбалансированный «(ГЗ»
_TRAILING_CLOSE = re.compile(r"\s*\)+\s*$")               # одинокие «)» после снятия

# Теги, которые однозначно служебные.
_GROUP_TAGS = {
    "гз", "одо", "дот", "до", "анкетирование", "очно", "очное", "очная", "заочно", "заочное",
    "заочная", "очно-заочное", "очно-заочно", "дистанционно", "дистанционное", "онлайн",
    "вебинар", "группа", "ппк", "пк", "лекция", "семинар", "практика",
}
_EK_TAGS = {"эк", "эо", "электронный курс", "электронное обучение", "самостоятельно"}
_GENERIC_TAG = re.compile(r"^(?:[A-ZА-ЯЁ0-9]{2,6}|\d{1,4}\s*(?:ч|час|часа|часов|ак\.?\s*ч\.?)?)$")


@dataclass
class TitleParts:
    title: str
    had_date: bool = False
    group_tags: list[str] = field(default_factory=list)
    ek_tags: list[str] = field(default_factory=list)
    other_tags: list[str] = field(default_factory=list)

    @property
    def dressed(self) -> bool:
        return self.had_date or bool(self.group_tags or self.ek_tags or self.other_tags)

    @property
    def delivery_hint(self) -> Delivery | None:
        """PPK when the title looks like a study-group; EK when it says so; else None."""
        if self.ek_tags and not self.group_tags and not self.had_date:
            return Delivery.EK
        if self.had_date or self.group_tags:
            return Delivery.PPK
        return None


def _tag_kind(content: str, had_date: bool) -> str | None:
    text = content.strip()
    low = text.casefold().replace("ё", "е")
    if not low:
        return "empty"
    if low in _GROUP_TAGS:
        return "group"
    if low in _EK_TAGS:
        return "ek"
    if had_date and _GENERIC_TAG.match(text):
        return "other"
    return None


def parse_title(raw: object) -> TitleParts:
    """Split a raw history title into the programme name and its dressing."""
    text = " ".join(str(raw).replace("\xa0", " ").split()) if raw is not None else ""
    original = text
    parts = TitleParts(title=text)
    match = _DATE_PREFIX.match(text)
    if match and match.end() < len(text):
        parts.had_date = True
        text = text[match.end():]
    # Заглушка вроде «9999_99_99-9999-99_99_» тоже дата по форме -- отдельного случая не нужно.

    while True:
        stripped = text.rstrip()
        m = _TRAILING_GROUP.search(stripped)
        if m is None:
            m = _TRAILING_OPEN.search(stripped)
        if m is None:
            close = _TRAILING_CLOSE.search(stripped)
            # лишние «)» остаются от «(ГЗ(ОДО)» после снятия внутреннего тега
            if close and stripped.count(")") > stripped.count("(") and (parts.group_tags or parts.other_tags or parts.ek_tags):
                text = stripped[: close.start()]
                continue
            break
        kind = _tag_kind(m.group(1), parts.had_date)
        if kind is None:
            break
        if kind == "group":
            parts.group_tags.append(m.group(1).strip())
        elif kind == "ek":
            parts.ek_tags.append(m.group(1).strip())
        elif kind == "other":
            parts.other_tags.append(m.group(1).strip())
        text = stripped[: m.start()]
    # Висящие разделители после снятия тегов: «Название _», «Название -».
    text = text.strip().rstrip("_-–—:,;").strip()
    parts.title = text or original
    return parts


def clean_title(raw: object) -> str:
    return parse_title(raw).title
