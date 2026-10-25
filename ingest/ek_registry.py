"""Reader for the customer's electronic-course registry.

Like the history reader, built on `ingest.smart_table`: sheet and columns are
found by content. A missing or unreadable registry is a warning in the data
passport, never a crash.
"""
from __future__ import annotations

from pathlib import Path

from core.models import Course, Delivery
from ingest.normalize import course_key
from ingest.passport import Passport
from ingest.smart_table import (
    DataProfile,
    TableAnalysis,
    analyze_table,
    cell_text,
    iter_data_rows,
    read_file,
)

SHEET_NAME = "Отчет"   # подсказка; лист ищется по содержимому


def _text(row, idx) -> str | None:
    if idx is None or idx >= len(row):
        return None
    value = cell_text(row[idx])
    return value or None


def parse_registry(
    analysis: TableAnalysis,
    aliases: dict[str, str | None],
    passport: Passport | None = None,
) -> list[Course]:
    passport = passport if passport is not None else Passport()
    c_title = analysis.column("title")
    c_ann, c_goal, c_out = analysis.column("annotation"), analysis.column("goal"), analysis.column("outcomes")
    courses: list[Course] = []
    for _number, row in iter_data_rows(analysis):
        title = _text(row, c_title)
        if not title:
            continue
        key = course_key(title, aliases)
        if not key:
            continue
        courses.append(
            Course(
                course_key=key,
                title=title,
                annotation=_text(row, c_ann),
                goal=_text(row, c_goal),
                outcomes=_text(row, c_out),
                deliveries={Delivery.EK},
            )
        )
    passport.counters["registry_rows"] = len(courses)
    return courses


def read_ek_registry(
    path: str | Path,
    aliases: dict[str, str | None],
    *,
    passport: Passport | None = None,
    profile: DataProfile | None = None,
    llm=None,
) -> list[Course]:
    """Read the registry out of one file. Raises `ValueError` if it holds none."""
    tables = read_file(path)
    analyses = [analyze_table(t, profile=profile, llm=llm, prefer_kind="registry") for t in tables]
    candidates = [a for a in analyses if a.kind == "registry"]
    if not candidates:
        raise ValueError(
            f"В файле {Path(path).name} не найден реестр курсов "
            "(колонки «Название курса», «Аннотация», «Цель», «Результаты»)."
        )
    candidates.sort(key=lambda a: (a.data_rows, a.score), reverse=True)
    return parse_registry(candidates[0], aliases, passport)
