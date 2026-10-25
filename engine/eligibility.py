"""Hard eligibility filters guarding the top acceptance criterion.

An already-completed course must never reappear in a track -- getting this
wrong drops "Качество рекомендаций" from 3 points to 1 (see the phase-3
plan, Задача 3). Everything downstream (scoring, balance, order) only ever
sees what survives `eligible_courses`.
"""
from __future__ import annotations

from catalog.catalog import Catalog
from core.models import Course
from core.preferences import Attribute
from engine.audience import Audience, classify_audience, required_attribute
from engine.grades import Grade


def eligible_courses(
    catalog: Catalog,
    passed_keys: set[str],
    seen_keys: set[str],
    grade: Grade,
    attributes: set[Attribute] | None = None,
) -> list[Course]:
    """Courses a profile of the given grade may be offered.

    Order of exclusion: completed courses first, then RESTRICTED programmes
    (unverifiable eligibility -- see `engine.audience`), then LEADERS_ONLY
    programmes for `Grade.SPECIALIST`. `seen_keys` is accepted for callers
    that need it but does not exclude anything here: a "не пройден" course is
    still worth offering again.

    `attributes` are the things a person declared about themselves in the
    wizard (age/reserve, supervision duties, ...). A declaration lifts the
    restriction on exactly the programme that asks for it, and only for that
    person. It cannot do anything else: the completed-course filter runs first
    and the grade filter runs after, so no declaration returns a passed course
    or turns a specialist into a manager. Declaring nothing -- the default --
    leaves behaviour exactly as it was before the wizard existed.
    """
    declared = attributes or set()
    result: list[Course] = []
    for course in catalog:
        if course.course_key in passed_keys:
            continue
        audience = classify_audience(course.audience or "")
        if audience is Audience.RESTRICTED:
            needed = required_attribute(course.audience or "")
            if needed is None or needed not in declared:
                continue
        if audience is Audience.LEADERS_ONLY and grade is Grade.SPECIALIST:
            continue
        result.append(course)
    return result
