"""Loader for the booklet's generated programme catalogue.

Reads `catalog/programs.yaml` -- produced ahead of time by the build-time
tool in `docs-internal/продукт-3/tools/extract_booklet.py` -- into `Course`
records. No PDF library is imported here: the product only ever reads the
committed YAML.
"""
from __future__ import annotations

from pathlib import Path

import yaml

from catalog.titles import repair_glued
from core.models import Course, Delivery
from ingest.normalize import course_key


def load_programmes(
    path: str | Path,
    aliases: dict[str, str | None],
    known_spellings: dict[str, str] | None = None,
) -> list[Course]:
    """Read the programme catalogue into `Course` records marked as ППК.

    `known_spellings` (`catalog.titles.known_spellings`) -- написания, уже
    встречавшиеся в выгрузках: слитное название из старого каталога
    («Интенсивпо ...») восстанавливается по ним до того, как из названия
    получен ключ, так что ключ совпадает с тем, что стоит в истории.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    navigator: dict[str, list[str]] = raw.get("navigator") or {}

    tracks_by_title: dict[str, list[str]] = {}
    for track, titles in navigator.items():
        for title in titles:
            tracks_by_title.setdefault(title, []).append(track)

    courses: list[Course] = []
    for programme in raw.get("programmes") or []:
        raw_title = str(programme["title"]).strip()
        title = repair_glued(raw_title, known_spellings) if known_spellings else raw_title
        courses.append(
            Course(
                course_key=course_key(title, aliases),
                title=title,
                annotation=programme.get("annotation"),
                outcomes=None,
                deliveries={Delivery.PPK},
                hours=programme.get("hours"),
                study_form=programme.get("study_form"),
                audience=programme.get("audience"),
                competency_group=programme.get("competency_group"),
                competency_subgroup=programme.get("competency_subgroup"),
                navigator_tracks=tracks_by_title.get(raw_title, []),
            )
        )
    return courses
