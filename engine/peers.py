"""Peer signal for the recommendation engine, keyed on normalised position.

Measured on the real export (see the phase-3 plan, "Открытие 1"): grouping by
position + ИОГВ produces cohorts with a median size of 1, and not one of the
323 profiles ever reaches three peers -- there is no statistic to compute on
a cohort that small. Grouping by normalised position alone gives 293 of 323
profiles a cohort of three or more. So `PeerIndex` groups on `position_norm`
and treats ИОГВ overlap as a separate, weaker signal (`iogv_share`), not the
grouping key.
"""
from __future__ import annotations

from collections import defaultdict

from core.models import Profile


class PeerIndex:
    """Position-based peer lookup and course-completion share."""

    def __init__(self, by_position: dict[str, list[Profile]]) -> None:
        self._by_position = by_position
        # Счётчики по группе должностей считаются один раз на группу: это
        # проход по всем профилям, а спрашивают их на каждый показ главной.
        self._grade_counts: dict[object, tuple[dict[str, int], int]] = {}

    @classmethod
    def build(cls, profiles: list[Profile]) -> "PeerIndex":
        by_position: dict[str, list[Profile]] = defaultdict(list)
        for profile in profiles:
            by_position[profile.position_norm].append(profile)
        return cls(dict(by_position))

    def peers_of(self, profile: Profile, same_iogv_only: bool = False) -> list[Profile]:
        """Everyone in the same normalised-position cohort, excluding this profile.

        `same_iogv_only` narrows further to peers who also share the profile's
        ИОГВ -- a much smaller, weaker-signal group (see the phase-3 plan,
        "Открытие 1").
        """
        cohort = self._by_position.get(profile.position_norm, [])
        peers = [p for p in cohort if p.user_id != profile.user_id]
        if same_iogv_only:
            peers = [p for p in peers if p.iogv == profile.iogv]
        return peers

    def completion_share(self, profile: Profile) -> dict[str, float]:
        """Fraction of position-peers (this profile excluded) who passed each course."""
        return self._share(self.peers_of(profile))

    def iogv_share(self, profile: Profile) -> dict[str, float]:
        """Fraction of same-ИОГВ position-peers who passed each course.

        A separate, weaker lookup than `completion_share` -- see the module
        docstring for why ИОГВ cannot be the primary grouping key.
        """
        return self._share(self.peers_of(profile, same_iogv_only=True))

    def grade_completion_share(self, profile: Profile, grade_of) -> tuple[dict[str, float], int]:
        """Доля коллег **той же группы должностей** (руководители / специалисты),
        прошедших каждый курс, и размер этой когорты без самого человека.

        Сигнал шире и слабее `completion_share`: там -- люди на той же должности,
        здесь -- все, кто в той же группе. Нужен для курсов без разметки буклета,
        у которых иначе нет ни одного персонального признака. `grade_of` --
        функция «должность -> группа», чтобы модуль не зависел от справочника.
        """
        grade = grade_of(profile.position)
        cached = self._grade_counts.get(grade)
        if cached is None:
            counts: dict[str, int] = defaultdict(int)
            size = 0
            for cohort in self._by_position.values():
                for member in cohort:
                    if grade_of(member.position) != grade:
                        continue
                    size += 1
                    for key in member.passed_keys():
                        counts[key] += 1
            cached = (dict(counts), size)
            self._grade_counts[grade] = cached
        counts, size = cached
        own = profile.passed_keys()
        cohort = size - 1 if size > 0 else 0
        if cohort <= 0:
            return {}, 0
        shares = {
            key: (count - (1 if key in own else 0)) / cohort for key, count in counts.items()
        }
        return {k: v for k, v in shares.items() if v > 0}, cohort

    @staticmethod
    def _share(peers: list[Profile]) -> dict[str, float]:
        if not peers:
            return {}
        counts: dict[str, int] = defaultdict(int)
        for peer in peers:
            for key in peer.passed_keys():
                counts[key] += 1
        denominator = len(peers)
        return {key: count / denominator for key, count in counts.items()}
