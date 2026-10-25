"""Data-quality reporting.

Surfaces the defects found in the customer's exports rather than silently
patching them. Every number here is evidence for the open questions in the
design spec.
"""
from __future__ import annotations

from collections import defaultdict

from pydantic import BaseModel, Field

from catalog.catalog import Catalog
from core.models import Delivery, HistoryEntry, Status
from ingest.normalize import normalize_text


class DataQualityReport(BaseModel):
    history_rows: int
    profiles: int
    catalogue_courses: int
    unmatched_keys: list[str] = Field(default_factory=list)
    dual_delivery_keys: list[str] = Field(default_factory=list)
    duplicated_outcomes: list[str] = Field(default_factory=list)
    duplicate_rows: int = 0
    contradictory_statuses: int = 0
    raw_positions: int = 0
    normalised_positions: int = 0
    fuzzy_links: int = 0
    unknown_positions: list[str] = Field(default_factory=list)
    unknown_position_people: int = 0
    default_grade: str = ""
    # {position, grade, how: keyword|default, matched, people}
    resolved_positions: list[dict] = Field(default_factory=list)
    # {name, iogvs, rows}: одинаковое ФИО в разных ИОГВ без табельного номера
    possible_homonyms: list[dict] = Field(default_factory=list)

    def render(self) -> str:
        """Human-readable summary for the CLI."""
        lines = [
            "Отчёт о качестве данных",
            "=" * 40,
            f"Строк истории обучения:      {self.history_rows}",
            f"Профилей пользователей:      {self.profiles}",
            f"Курсов в каталоге:           {self.catalogue_courses}",
            "",
            f"Курсов истории без каталога: {len(self.unmatched_keys)}",
            f"Курсов в двух форматах:      {len(self.dual_delivery_keys)}",
            f"Дублирующихся 'Результатов': {len(self.duplicated_outcomes)}",
            f"Точных дублей строк истории: {self.duplicate_rows}",
            f"Противоречивых статусов:     {self.contradictory_statuses}",
            f"Написаний должностей (сырых/после нормализации): {self.raw_positions}/{self.normalised_positions}",
            f"Связано с каталогом по сходству названий: {self.fuzzy_links}",
        ]
        lines.append(
            f"Должностей вне справочника (грейд по умолчанию «{self.default_grade or '-'}»): "
            f"{len(self.unknown_positions)} (у {self.unknown_position_people} чел.)"
        )
        if self.unknown_positions:
            lines += [f"  - {p}" for p in self.unknown_positions[:10]]
        if self.resolved_positions:
            lines += ["", f"Грейд определён автоматически ({len(self.resolved_positions)} должностей):"]
            for item in self.resolved_positions[:10]:
                how = f"по слову «{item['matched']}»" if item["how"] == "keyword" else "по умолчанию"
                lines.append(f"  - {item['position']} -> {item['grade']} ({how}; чел.: {item['people']})")
        if self.possible_homonyms:
            lines += ["", f"Возможные однофамильцы / переводы — проверьте ({len(self.possible_homonyms)}):"]
            for case in self.possible_homonyms[:10]:
                lines.append(f"  - {case['name']}: {', '.join(case['iogvs'])} (строк: {case['rows']})")
        if self.unmatched_keys:
            lines += ["", "Не связаны с каталогом (первые 10):"]
            lines += [f"  - {key}" for key in self.unmatched_keys[:10]]
        return "\n".join(lines)


def build_report(history: list[HistoryEntry], catalog: Catalog, fuzzy_links: int = 0) -> DataQualityReport:
    """Compare the history export against the catalogue and report the gaps.

    Takes the already-merged `Catalog` rather than a raw list of courses: once
    a course exists in both the electronic registry and the booklet, a raw
    list would carry it as two separate `Course` objects and any shared
    'outcomes' text would look like a duplicate that isn't one (phase 1 review
    finding #16). The catalogue has already merged those onto one record.
    """
    catalogue_keys = catalog.keys()

    deliveries_by_key: dict[str, set[Delivery]] = defaultdict(set)
    for entry in history:
        deliveries_by_key[entry.course_key].add(entry.delivery)

    outcomes_owners: dict[str, list[str]] = defaultdict(list)
    for course in catalog:
        if course.outcomes:
            outcomes_owners[course.outcomes].append(course.course_key)

    duplicated = sorted(
        key
        for owners in outcomes_owners.values()
        if len(owners) > 1
        for key in owners
    )

    # Exact duplicate rows: same person, position, ИОГВ, delivery, title and
    # status repeated verbatim. Left uncorrected these inflate a peer's course
    # count and, in turn, the peer signal phase 3 builds recommendations from.
    row_counts: dict[tuple, int] = defaultdict(int)
    for entry in history:
        row_key = (
            entry.user_id,
            entry.position,
            entry.iogv,
            entry.delivery,
            entry.title,
            entry.course_key,
            entry.status,
        )
        row_counts[row_key] += 1
    duplicate_rows = sum(count - 1 for count in row_counts.values() if count > 1)

    # Contradictory statuses: the same person marked both passed and not
    # passed for the same course. Whichever status "wins" is a data question
    # for the customer, not something to guess silently.
    statuses_by_person_course: dict[tuple[str, str], set[Status]] = defaultdict(set)
    for entry in history:
        statuses_by_person_course[(entry.user_id, entry.course_key)].add(entry.status)
    contradictory_statuses = sum(
        1
        for statuses in statuses_by_person_course.values()
        if Status.PASSED in statuses and Status.NOT_PASSED in statuses
    )

    # Position spelling variants: raw text vs normalised form. Phase 3 groups
    # peers by post, so "Ведущий специалист - юрисконсульт" and "Ведущий
    # специалист-юрисконсульт" must collapse onto one cohort.
    raw_positions = {entry.position for entry in history}
    normalised_positions = {normalize_text(entry.position) for entry in history}

    return DataQualityReport(
        history_rows=len(history),
        profiles=len({e.user_id for e in history}),
        catalogue_courses=len(catalogue_keys),
        unmatched_keys=sorted(set(deliveries_by_key) - catalogue_keys),
        dual_delivery_keys=sorted(
            k for k, v in deliveries_by_key.items() if len(v - {Delivery.UNKNOWN}) > 1
        ),
        duplicated_outcomes=duplicated,
        duplicate_rows=duplicate_rows,
        contradictory_statuses=contradictory_statuses,
        raw_positions=len(raw_positions),
        normalised_positions=len(normalised_positions),
        fuzzy_links=fuzzy_links,
    )
