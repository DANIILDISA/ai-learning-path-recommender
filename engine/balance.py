"""Diversity cap: no more than N courses from the same competency group.

Runs after scoring and after the courses are already sorted best-first (see
`engine.scoring.rank_key`) -- capping does not reorder anything, it only
drops overflow so the final track is not dominated by one competency group.
Courses without a competency group (electronic courses outside the booklet)
are never capped against each other or against anything else.
"""
from __future__ import annotations

from core.models import CompetencyGroup
from engine.scoring import ScoredCourse


def apply_cap(
    scored_courses: list[ScoredCourse],
    max_per_competency_group: int,
    max_total: int,
) -> list[ScoredCourse]:
    """Keep `scored_courses` in order, dropping overflow per competency group
    and truncating to `max_total`.

    `scored_courses` is expected to already be sorted best-first; this
    function preserves that order among what it keeps.
    """
    group_counts: dict[CompetencyGroup, int] = {}
    selected: list[ScoredCourse] = []
    for scored in scored_courses:
        group = scored.course.competency_group
        if group is not None:
            if group_counts.get(group, 0) >= max_per_competency_group:
                continue
            group_counts[group] = group_counts.get(group, 0) + 1
        selected.append(scored)
        if len(selected) >= max_total:
            break
    return selected
