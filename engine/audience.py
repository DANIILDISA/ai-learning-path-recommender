"""Classify a programme's audience wording into an eligibility bucket.

Measured on the 99-programme catalogue (see the phase-3 plan, "Открытие 2"):
78 programmes are open to any ГГС employee, 15 are restricted to the
"руководители" category, and 6 name an attribute the export cannot verify
(age, reserve membership, job duties, ...). That third bucket -- RESTRICTED --
is deliberately excluded from the automatic track: we have no data to check
eligibility, and showing someone a course they cannot actually take is a
direct hit on the engagement the product exists to build.
"""
from __future__ import annotations

from enum import Enum

from core.preferences import Attribute
from ingest.normalize import normalize_text

# Order matters: an audience string naming an unverifiable attribute takes
# priority over "руководители" wording even if both appear together.
#
# Every marker here belongs to a clause that *narrows* the audience -- a
# participle naming who, among civil servants, may attend. "подведомственных"
# used to sit in this list and did the opposite: in the one programme carrying
# it ("Применение бережливых технологий") the clause reads "государственные
# гражданские служащие; сотрудники ИОГВ ... и подведомственных им учреждений",
# which *adds* audiences. The programme is open to every civil servant, and the
# marker hid it from all 323 profiles in the corpus.
_RESTRICTED_MARKERS = (
    "не достигшие возраста",
    "кадровыи резерв",
    "ответственные в органе власти",
    "богатыи опыт",
    "финансовои культуры",
    "участвующие в организации",
)

_LEADERS_ONLY_MARKER = "руководител"

# Маркер -> признак, которым человек может открыть себе программу (шаг 5
# мастера). "богатыи опыт" здесь отсутствует намеренно: подтвердить «значимые
# результаты и достижения» нечем, и вопрос об этом был бы предложением
# приписать себе заслуги ради доступа к программе.
_ATTRIBUTE_BY_MARKER: dict[str, Attribute] = {
    "не достигшие возраста": Attribute.YOUTH_RESERVE,
    "кадровыи резерв": Attribute.YOUTH_RESERVE,
    "ответственные в органе власти": Attribute.PROF_DEVELOPMENT,
    "участвующие в организации": Attribute.SUPERVISION,
    "финансовои культуры": Attribute.FINANCIAL_LITERACY,
}


class Audience(str, Enum):
    """Who a catalogue programme may be recommended to."""

    OPEN = "open"
    LEADERS_ONLY = "leaders_only"
    RESTRICTED = "restricted"


def classify_audience(text: str) -> Audience:
    """Bucket a programme's raw `audience` wording.

    Works on the normalised text so PDF-export quirks (missing spaces,
    й/ё spelling variants, punctuation) do not create false negatives.
    """
    normalized = normalize_text(text)
    if any(marker in normalized for marker in _RESTRICTED_MARKERS):
        return Audience.RESTRICTED
    if _LEADERS_ONLY_MARKER in normalized:
        return Audience.LEADERS_ONLY
    return Audience.OPEN


def required_attribute(text: str) -> Attribute | None:
    """Признак, которым человек может открыть себе эту программу.

    `None` означает «открыть нечем» и возвращается в двух разных случаях:
    программа и так доступна (тогда спрашивать не о чем), либо её ограничение
    непроверяемо в принципе. Вызывающий сначала смотрит `classify_audience` и
    только для RESTRICTED спрашивает про признак -- поэтому различать эти два
    случая здесь не требуется.
    """
    if classify_audience(text) is not Audience.RESTRICTED:
        return None
    normalized = normalize_text(text)
    for marker, attribute in _ATTRIBUTE_BY_MARKER.items():
        if marker in normalized:
            return attribute
    return None
