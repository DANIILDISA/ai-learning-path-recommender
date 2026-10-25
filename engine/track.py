"""Assemble one profile's learning track: pool -> score -> balance -> order.

Each stage is a pure function tested on its own (`engine.eligibility`,
`engine.scoring`, `engine.balance`); this module only wires them together in
the order the phase-3 plan specifies. No stage here knows about the others'
internals -- `build_track` just threads data through.
"""
from __future__ import annotations

from catalog.catalog import Catalog
from catalog.interests import InterestIndex
from core.config import AppConfig
from core.models import CompetencyGroup, Course, Profile, Recommendation, Track
from core.preferences import Preferences
from engine.balance import apply_cap
from engine.eligibility import eligible_courses
from engine.grades import PositionIndex
from engine.peers import PeerIndex
from engine.reasons import ReasonContext, facts_for, is_personal, short_title
from engine.scoring import is_generic, is_self_paced, rank_key, score_course


def _competency_counts(profile: Profile, catalog: Catalog) -> dict[CompetencyGroup, int]:
    """How many passed courses the profile has in each competency group.

    Courses without a competency group (electronic courses outside the
    booklet) do not count toward any group -- there is nothing to balance
    them against.
    """
    counts: dict[CompetencyGroup, int] = {}
    for key in profile.passed_keys():
        course = catalog.get(key)
        if course is None or course.competency_group is None:
            continue
        counts[course.competency_group] = counts.get(course.competency_group, 0) + 1
    return counts


def build_track(
    profile: Profile,
    catalog: Catalog,
    peer_index: PeerIndex,
    position_index: PositionIndex,
    config: AppConfig,
    preferences: Preferences | None = None,
    interests: InterestIndex | None = None,
    feedback: dict[str, str] | None = None,
) -> Track:
    """Build the deterministic, reasoned track for one profile.

    Degrades gracefully when the profile has no usable peer cohort (see
    `engine.peers`): the peer-derived score terms simply contribute nothing,
    and the track still fills from the navigator-track match and the
    competency-balance term, so it is never empty as long as at least one
    course is eligible.
    """
    grade = position_index.grade_of(profile.position)
    # Признаки, заявленные в мастере, -- единственное, чем предпочтения влияют
    # на СОСТАВ пула, и влияют строго в одну сторону: открывают программу,
    # которая требует именно этого признака. Всё остальное из предпочтений
    # работает ниже, весами, и состав пула не трогает.
    declared = set(preferences.attributes) if preferences is not None else set()
    marks = feedback or {}
    pool = [
        course
        for course in eligible_courses(
            catalog, profile.passed_keys(), profile.seen_keys(), grade,
            attributes=declared,
        )
        # Любая отметка -- «хочу пройти», «не сейчас», «неактуально» -- уводит
        # программу из подборки: человек уже принял по ней решение, и она
        # живёт теперь в «Моём обучении». На её место встаёт следующая по
        # рейтингу (потолок в `max_recommendations` считается уже после
        # исключения). Внести программу в выдачу отметка не может --
        # инвариант 1 приёмки сильнее любого пожелания.
        if course.course_key not in marks
    ]
    competency_counts = _competency_counts(profile, catalog)

    scored = [
        score_course(
            course, profile, grade, peer_index, competency_counts, config,
            preferences=preferences, interests=interests,
        )
        for course in pool
    ]
    scored.sort(key=rank_key)

    capped = apply_cap(scored, config.max_per_competency_group, config.max_recommendations)

    recommendations = [
        Recommendation(
            course_key=item.course.course_key,
            title=item.course.title,
            score=item.score,
            reasons=item.reasons,
        )
        for item in capped
    ]

    return Track(
        user_id=profile.user_id,
        position=profile.position,
        iogv=profile.iogv,
        grade=grade.value,
        recommendations=recommendations,
    )


def build_quick_start(
    profile: Profile,
    catalog: Catalog,
    peer_index: PeerIndex,
    position_index: PositionIndex,
    config: AppConfig,
    preferences: Preferences | None = None,
    interests: InterestIndex | None = None,
    exclude: set[str] | None = None,
    limit: int = 4,
    feedback: dict[str, str] | None = None,
) -> list[Recommendation]:
    """Курсы, которые можно начать сразу, ранжированные среди своих.

    Зачем отдельный блок. Скоринг вознаграждает полноту разметки: трек
    «Навигатора» даёт 3.0, пробел по группе компетенций -- 1.5, и программа
    буклета получает 4.5 балла просто за то, что она из буклета. У курсов
    реестра этих полей нет вообще, поэтому в общем списке они недостижимы:
    измерено на всех 323 профилях -- 0 позиций из 3230. Наказывать курс за
    дефект выгрузки -- та же ошибка, что наказывать человека без когорты
    коллег за отсутствие коллег.

    Почему не подмешиваем их в трек. Это разные вещи в данных самого
    заказчика: на программу повышения квалификации (16-520 часов) человека
    записывают, а электронный курс он открывает и проходит сам. Они не
    конкурируют за одно место в его жизни, и общая линейка сравнивала бы
    несравнимое. Квота была бы ещё хуже: она поставила бы курс с баллом 0.68
    выше программы с 4.5, то есть сказала бы неправду о том, что важнее.

    Ранжирование внутри блока осмысленно и это проверено: у 60 профилей на
    первом месте оказываются 19 разных курсов, полная ничья в топ-5 -- лишь
    у 4 из 60, а обоснования настоящие (доля коллег, незавершённый курс).
    """
    scored = _score_self_paced(
        profile, catalog, peer_index, position_index, config,
        preferences, interests, exclude, feedback,
    )
    return [_as_recommendation(item) for item in scored[:limit]]


def _as_recommendation(item) -> Recommendation:
    return Recommendation(
        course_key=item.course.course_key,
        title=item.course.title,
        score=item.score,
        reasons=item.reasons,
    )


def build_electronic(
    profile: Profile,
    catalog: Catalog,
    peer_index: PeerIndex,
    position_index: PositionIndex,
    config: AppConfig,
    preferences: Preferences | None = None,
    interests: InterestIndex | None = None,
    exclude: set[str] | None = None,
    limit: int = 8,
    feedback: dict[str, str] | None = None,
    ctx: ReasonContext | None = None,
) -> list[Recommendation]:
    """Электронные курсы, персональные для этого человека, лучшие первыми.

    В очередь попадает курс, про который можно сказать хотя бы один настоящий
    довод (`engine.reasons.is_personal`): коллеги на той же должности или в
    том же ИОГВ его проходили, он близок по теме к пройденному, совпадает с
    интересом из мастера, остался незавершённым. Курс, у которого нет ничего,
    кроме «открыт всем», не рекомендуется -- он есть в каталоге, и выдавать
    его за персональную подборку нельзя: у двух разных людей получался один и
    тот же алфавитный список.

    Очередь, а не три карточки: человек отмечает показанное, и на место
    отмеченного встаёт следующий. Сколько показывать крупно, решает экран.
    """
    ctx = ctx or ReasonContext.build(
        profile, catalog, peer_index, position_index, config, preferences, interests
    )
    scored = _score_self_paced(
        profile, catalog, peer_index, position_index, config,
        preferences, interests, exclude, feedback, ctx=ctx,
    )
    personal = [item for item in scored if is_personal(facts_for(item.course, ctx))]
    return [_as_recommendation(item) for item in personal[:limit]]


def _score_self_paced(
    profile: Profile,
    catalog: Catalog,
    peer_index: PeerIndex,
    position_index: PositionIndex,
    config: AppConfig,
    preferences: Preferences | None,
    interests: InterestIndex | None,
    exclude: set[str] | None,
    feedback: dict[str, str] | None,
    ctx: ReasonContext | None = None,
) -> list:
    ctx = ctx or ReasonContext.build(
        profile, catalog, peer_index, position_index, config, preferences, interests
    )
    grade = position_index.grade_of(profile.position)
    declared = set(preferences.attributes) if preferences is not None else set()
    skip = exclude or set()
    # Электронные курсы подчиняются отметкам ровно как основной трек: человек
    # отмечает карточку, а не то, в каком ряду она нарисована.
    marks = feedback or {}

    pool = [
        course
        for course in eligible_courses(
            catalog, profile.passed_keys(), profile.seen_keys(), grade,
            attributes=declared,
        )
        if is_self_paced(course)
        and course.course_key not in skip
        and course.course_key not in marks
    ]
    competency_counts = _competency_counts(profile, catalog)

    # Курсам реестра нечем быть персональными: ни трека, ни группы компетенций.
    # Остаётся то, что делали коллеги. Должность -- самая узкая когорта, но у
    # неё часто нет ни одного человека, поэтому добавлена вторая, шире:
    # вся группа должностей (все руководители или все специалисты).
    grade_share, grade_size = peer_index.grade_completion_share(
        profile, position_index.grade_of
    )
    group_word = {"specialist": "специалистов", "manager": "руководителей",
                  "senior": "руководителей высшего звена"}.get(grade.value, "коллег")

    scored = []
    for course in pool:
        item = score_course(
            course, profile, grade, peer_index, competency_counts, config,
            preferences=preferences, interests=interests,
        )
        # Близость к пройденному: курс того же направления интересов, что и
        # уже пройденный (явная разметка `interests.yaml`), -- единственный
        # тематический сигнал, который у электронного курса вообще есть.
        related = ctx.related_passed(course)
        if related is not None and related.strong:
            if is_generic(item):
                item.reasons = []
            item.score += config.weight_topic_match
            item.reasons.append(
                f"Тема близка к пройденному курсу «{short_title(related.course.title)}»"
            )
        share = grade_share.get(course.course_key, 0.0)
        if share > 0.0:
            item.score += config.weight_same_iogv_bonus * share
            if grade_size >= config.min_peer_cohort and share >= config.min_peer_share:
                if is_generic(item):
                    item.reasons = []
                item.reasons.append(
                    f"{round(share * 100)}% {group_word} прошли этот курс "
                    f"({round(share * grade_size)} из {grade_size})"
                )
        scored.append(item)
    scored.sort(key=rank_key)
    return scored
