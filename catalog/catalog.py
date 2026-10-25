"""Unified course catalogue keyed on course identity."""
from __future__ import annotations

from collections.abc import Iterator

from core.models import Course


class Catalog:
    """Courses from every source, merged on `course_key`.

    Phase 1 feeds it the electronic-course registry; phase 2 adds the booklet
    programmes through the same `add`. Because ППК and ЭК are delivery formats
    of one course rather than separate courses, adding the same key twice
    merges the records instead of creating a duplicate.
    """

    def __init__(self) -> None:
        self._by_key: dict[str, Course] = {}

    def add(self, course: Course) -> None:
        """Add a course, merging into an existing entry with the same key."""
        if not course.course_key:
            raise ValueError(f"Refusing to add course with empty course_key: {course.title!r}")
        existing = self._by_key.get(course.course_key)
        if existing is None:
            self._by_key[course.course_key] = course.model_copy(deep=True)
            return
        existing.deliveries |= course.deliveries
        for field in ("annotation", "goal", "outcomes"):
            if getattr(existing, field) is None and getattr(course, field) is not None:
                setattr(existing, field, getattr(course, field))

    def get(self, key: str) -> Course | None:
        return self._by_key.get(key)

    def keys(self) -> set[str]:
        return set(self._by_key)

    def __len__(self) -> int:
        return len(self._by_key)

    def __iter__(self) -> Iterator[Course]:
        return iter(self._by_key.values())
