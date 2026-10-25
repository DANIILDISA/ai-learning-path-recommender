"""Domain models for the learning-track agent."""
from __future__ import annotations

import re
from enum import Enum

from pydantic import BaseModel, Field


def _fold(raw: object) -> str:
    """Casefold, ё->е, NBSP and punctuation -> single spaces: for vocabulary lookups."""
    text = str(raw if raw is not None else "").replace("\xa0", " ").casefold().replace("ё", "е")
    text = re.sub(r"[^\w\s]", " ", text.replace("_", " "))
    return " ".join(text.split())


_STATUS_NEGATIVE = [
    re.compile(p)
    for p in (
        r"\bне\s?(?:пройден|прошел|прошла|завершен|выполнен|сдан|освоен|освоил|освоено|аттестован|зачтен|успешн|допущен|окончен)",
        r"\bнеполн|\bнезавершен|\bнепройден|\bнесдан|\bнеуспешн|\bнеудовл|\bнезачет",
        r"отчисл|провал|прерван|отказ|исключен|не явил|неявк|не приступ|не начат|не начинал|академ",
    )
]
_STATUS_PROGRESS = [
    re.compile(
        r"в процесс|в работе|обучается|обучение идет|идет обучение|проходит|изучает|начат|назначен|"
        r"записан|зачислен|ожида|в ходе|\bидет\b|текущ|не закончен"
    )
]
_STATUS_POSITIVE = [
    re.compile(
        r"пройден|прошел|прошла|заверш|выполнен|сдан|освоен|освоил|успешн|зачет|зачтен|окончен|"
        r"аттестован|сертифиц|выдан|закончен|готово|\bok\b|\bdone\b|\bpassed\b|\bcomplete"
    )
]


class Status(str, Enum):
    """Outcome of a learning record.

    UNKNOWN is what an unrecognised or empty status becomes: the row is kept
    (the person did meet the course), never counted as passed, and the data
    passport counts such values so a human can see them.
    """

    PASSED = "passed"
    NOT_PASSED = "not_passed"
    IN_PROGRESS = "in_progress"
    UNKNOWN = "unknown"

    @classmethod
    def parse(cls, raw: object) -> "Status":
        """Map the customer's wording onto the enum -- never raises.

        Exact words first, then keyword rules; negations are checked before
        positives so «не завершён» is not read as «завершён».
        """
        text = _fold(raw)
        if not text:
            return cls.UNKNOWN
        exact = _STATUS_EXACT.get(text)
        if exact is not None:
            return exact
        for pattern in _STATUS_NEGATIVE:
            if pattern.search(text):
                return cls.NOT_PASSED
        for pattern in _STATUS_PROGRESS:
            if pattern.search(text):
                return cls.IN_PROGRESS
        for pattern in _STATUS_POSITIVE:
            if pattern.search(text):
                return cls.PASSED
        return cls.UNKNOWN


_STATUS_EXACT = {
    "пройден": Status.PASSED,
    "не пройден": Status.NOT_PASSED,
    "в процессе": Status.IN_PROGRESS,
}


class Delivery(str, Enum):
    """How a course is delivered. Not part of course identity -- see D6."""

    PPK = "ППК"
    EK = "ЭК"
    # Формат не указан в выгрузке и не определился ни по названию, ни по
    # реестру, ни по каталогу. Раньше такие записи молча считались
    # электронными курсами, и методист получал искажённую картину.
    UNKNOWN = "не указан"

    @classmethod
    def parse(cls, raw: object) -> "Delivery | None":
        """Delivery type from the customer's wording; None when not recognised.

        None (not an exception) lets the caller fall back to the title dressing
        or the electronic registry instead of failing the whole file.
        """
        text = _fold(raw)
        if not text:
            return None
        if text in _DELIVERY_EXACT:
            return _DELIVERY_EXACT[text]
        for pattern, value in _DELIVERY_PATTERNS:
            if pattern.search(text):
                return value
        return None


_DELIVERY_EXACT = {
    "ппк": Delivery.PPK,
    "эк": Delivery.EK,
    "эо": Delivery.EK,
    "пк": Delivery.PPK,
}
_DELIVERY_PATTERNS = [
    (re.compile(r"электронн|\bэо\b|\bэк\b|самостоятельн"), Delivery.EK),
    (re.compile(r"повышени\w* квалификаци|\bппк\b|\bпк\b|\bочн|групп|вебинар|семинар|\bдот\b"), Delivery.PPK),
]


class HistoryEntry(BaseModel):
    """One row of the customer's learning-history export."""

    user_id: str = Field(min_length=1)
    position: str = Field(min_length=1)
    iogv: str = Field(min_length=1)
    delivery: Delivery
    title: str = Field(min_length=1)
    course_key: str = Field(min_length=1)
    status: Status


class CompetencyGroup(str, Enum):
    """Top-level competency grouping used by the 2025 programme booklet."""

    PERSONAL = "Личностные компетенции"
    MANAGEMENT_TOOLS = "Инструменты управления"
    HARD_SKILLS = "Профильные hard-skills"
    PROFESSIONAL = "Профессиональные компетенции"
    BASIC = "Базовые компетенции"

    @classmethod
    def parse(cls, raw: str) -> "CompetencyGroup":
        text = str(raw).strip()
        for member in cls:
            if member.value.casefold() == text.casefold():
                return member
        raise ValueError(f"Unknown competency group: {raw!r}")


class Course(BaseModel):
    """A course in the catalogue, identified independently of delivery format."""

    course_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    annotation: str | None = None
    goal: str | None = None
    outcomes: str | None = None
    deliveries: set[Delivery] = Field(default_factory=set)
    hours: int | None = Field(default=None, gt=0)
    study_form: str | None = None
    audience: str | None = None
    competency_group: CompetencyGroup | None = None
    competency_subgroup: str | None = None
    navigator_tracks: list[str] = Field(default_factory=list)


class Profile(BaseModel):
    """A civil servant and everything the export says about their learning."""

    user_id: str = Field(min_length=1)
    position: str = Field(min_length=1)
    iogv: str = Field(min_length=1)
    position_norm: str = ""
    entries: list[HistoryEntry] = Field(default_factory=list)

    def passed_keys(self) -> set[str]:
        """Course keys this person has completed -- never recommend these."""
        return {e.course_key for e in self.entries if e.status is Status.PASSED}

    def seen_keys(self) -> set[str]:
        """Every course key this person has encountered, whatever the outcome."""
        return {e.course_key for e in self.entries}


class Recommendation(BaseModel):
    """One position in a learning track, with the reasons that put it there."""

    course_key: str = Field(min_length=1)
    title: str = Field(min_length=1)
    score: float
    reasons: list[str] = Field(default_factory=list)
    # "ai" -- обоснование написала модель; "facts" -- модель промолчала, текст
    # собран кодом из фактов; "" -- ИИ не участвовал.
    explanation_source: str = ""
    # Одно предложение «почему рекомендовано», которое видит человек
    # (`engine.reasons`). Пусто, пока его не проставили: движок и отчёты
    # живут и без него, а интерфейс получает его на границе API.
    reason: str = ""


class Track(BaseModel):
    """A person's full learning track: ordered, reasoned recommendations."""

    user_id: str = Field(min_length=1)
    position: str
    iogv: str
    grade: str
    recommendations: list[Recommendation] = Field(default_factory=list)
    # Диагностика переранжирования моделью: сколько названий вне пула она
    # выдумала (и они отброшены), сколько восстановлено нечётким сравнением,
    # сколько карточек получили обоснование от кода, а не от модели.
    llm_invented: int = 0
    llm_invented_names: list[str] = Field(default_factory=list)
    llm_fuzzy_matched: int = 0
    llm_backfilled: int = 0
    llm_fact_explanations: int = 0
