"""Score one eligible course for one profile, with the facts behind the score.

Every weight lives in `config.yaml` (`track.weights`) -- nothing here is a
magic number. Reasons are built from real counts and shares so they read as
"82% коллег на должности «Главный специалист» прошли этот курс", never as a
placeholder. When a profile has no usable peer cohort (see `engine.peers`,
"Открытие 1" in the phase-3 plan -- 18 of 323 profiles have zero peers), the
peer-derived terms simply contribute nothing and scoring falls back to the
navigator-track match, the competency-balance term and, failing all of that,
a factual fallback reason -- it never raises and never leaves a course
reasonless.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from catalog.interests import InterestIndex
from core.config import AppConfig
from core.models import CompetencyGroup, Course, Delivery, Profile, Status
from core.preferences import Goal, Preferences, Urgency
from engine.audience import Audience, classify_audience
from engine.grades import Grade, TRACK_BY_GRADE
from engine.peers import PeerIndex


@dataclass
class ScoredCourse:
    """A course with the score and reasons computed for one profile."""

    course: Course
    score: float
    reasons: list[str] = field(default_factory=list)


def is_self_paced(course: Course) -> bool:
    """Курс, который человек проходит сам, без записи на программу.

    Признак -- отсутствие разметки буклета, а не формат подачи. Буклет
    «Линейки образовательных программ» задаёт трек «Навигатора» и группу
    компетенций; у 64 курсов реестра электронных курсов нет ни того, ни
    другого. Предикат по `deliveries` был бы неверен: два из этих 64 идут
    ещё и как ППК, и по формату отсеялись бы.
    """
    return not course.navigator_tracks and course.competency_group is None


def rank_key(scored: ScoredCourse) -> tuple[float, str]:
    """Sort key for a stable, deterministic order.

    Highest score first; equal scores break on `course_key` ascending so two
    runs over the same data produce byte-identical tracks.
    """
    return (-scored.score, scored.course.course_key)


GOAL_TITLES: dict[Goal, str] = {
    Goal.CAREER_GROWTH: "карьерный рост",
    Goal.CURRENT_ROLE: "развитие в текущей роли",
    Goal.NEW_FIELD: "новое направление",
    Goal.UNDECIDED: "пока не определились",
}


def _course_word(count: int) -> str:
    """Russian plural form of "курс" for `count`."""
    n_mod100 = count % 100
    if 11 <= n_mod100 <= 14:
        return "курсов"
    n_mod10 = count % 10
    if n_mod10 == 1:
        return "курс"
    if 2 <= n_mod10 <= 4:
        return "курса"
    return "курсов"


def _latest_status(profile: Profile, course_key: str) -> Status | None:
    """Status of `course_key` in the profile's history.

    Prefers "в процессе" over "не пройден" when both are present -- it is the
    more actionable fact to put in front of the person.
    """
    statuses = [entry.status for entry in profile.entries if entry.course_key == course_key]
    if not statuses:
        return None
    if Status.IN_PROGRESS in statuses:
        return Status.IN_PROGRESS
    return statuses[-1]


def _fallback_reason(course: Course) -> str:
    """A factual reason that always applies, so a course is never reasonless."""
    audience = classify_audience(course.audience or "")
    if audience is Audience.LEADERS_ONLY:
        return "Программа адресована категории «руководители» — соответствует вашей группе должностей"
    return "Программа открыта для всех государственных гражданских служащих"


def is_generic(scored: "ScoredCourse") -> bool:
    """True, если у курса нет ни одного персонального обоснования.

    Остаётся только формальная отписка «открыта для всех»: человека это ни о
    чём не говорит, и выдавать такой курс за рекомендацию нельзя.
    """
    return scored.reasons == [_fallback_reason(scored.course)]


def score_course(
    course: Course,
    profile: Profile,
    grade: Grade,
    peer_index: PeerIndex,
    competency_counts: dict[CompetencyGroup, int],
    config: AppConfig,
    preferences: Preferences | None = None,
    interests: InterestIndex | None = None,
    feedback: str | None = None,
) -> ScoredCourse:
    """Score `course` for `profile` and explain the score in Russian.

    `competency_counts` is the profile's count of passed courses per
    competency group (see `engine.track._competency_counts`); an empty dict
    means every group is tied at zero, which is itself a valid "everything is
    a gap" signal for someone new to the programme line-up.
    """
    score = 0.0
    reasons: list[str] = []

    track_name = TRACK_BY_GRADE[grade]
    if track_name in course.navigator_tracks:
        score += config.weight_navigator_track
        reasons.append(f"Программа входит в трек «{track_name}» линейки 2025 года")

    # A share still contributes to the score at any cohort size, but it is only
    # quoted back to the reader when the cohort is large enough for the number
    # to mean something. "100% коллег" computed over one person reads as strong
    # social proof and is not; the product exists to invite people to learn, and
    # an inflated claim spends exactly the trust it needs. Where the cohort does
    # justify a claim, the raw counts go with it -- "12 из 34" is both honest
    # and more persuasive than a bare percentage.
    peer_count = len(peer_index.peers_of(profile))
    peer_share = peer_index.completion_share(profile).get(course.course_key, 0.0)
    if peer_share > 0.0:
        score += config.weight_peer_completion * peer_share
        if peer_count >= config.min_peer_cohort and peer_share >= config.min_peer_share:
            reasons.append(
                f"{round(peer_share * 100)}% коллег на должности «{profile.position}» "
                f"прошли этот курс ({round(peer_share * peer_count)} из {peer_count})"
            )

    iogv_count = len(peer_index.peers_of(profile, same_iogv_only=True))
    iogv_share = peer_index.iogv_share(profile).get(course.course_key, 0.0)
    if iogv_share > 0.0:
        score += config.weight_same_iogv_bonus * iogv_share
        if iogv_count >= config.min_peer_cohort:
            reasons.append(
                f"{round(iogv_share * 100)}% коллег из вашего ИОГВ на этой должности "
                f"тоже прошли этот курс ({round(iogv_share * iogv_count)} из {iogv_count})"
            )

    if course.competency_group is not None:
        group_count = competency_counts.get(course.competency_group, 0)
        min_count = min((competency_counts.get(group, 0) for group in CompetencyGroup), default=0)
        if group_count == min_count:
            score += config.weight_competency_gap
            reasons.append(
                f"По направлению «{course.competency_group.value}» у вас пока "
                f"{group_count} {_course_word(group_count)}"
            )

    if course.course_key in profile.seen_keys():
        score += config.weight_unfinished_restart
        status = _latest_status(profile, course.course_key)
        if status is Status.IN_PROGRESS:
            reasons.append("Курс отмечен как «в процессе» — стоит завершить начатое")
        else:
            reasons.append("Курс отмечен как «не пройден» — можно пройти повторно")

    # Ответы мастера настройки. Мягкие веса ВНУТРИ уже допустимого пула:
    # отбор закончился в `engine.eligibility` задолго до этой строки, и ничто
    # здесь не может внести курс в выдачу или убрать его оттуда. Каждое
    # слагаемое добавляет свой факт и не трогает уже собранные -- обмен
    # проверяемого аргумента на приятный этот продукт уже проходил.
    if preferences is not None:
        score, reasons = _apply_preferences(
            course, preferences, interests, config, score, reasons
        )

    # Отметка, которую человек поставил на карточке. Работает так же мягко,
    # как ответы мастера: переставляет уже допущенное и ничего не вносит.
    # «Неактуально» сюда не доходит -- такая программа не попадает в пул
    # (`engine.track.build_track`), потому что «мне это не нужно» -- отказ, а
    # не пожелание переставить пониже.
    if feedback == "want":
        score += config.weight_feedback_want
        reasons.append("Вы отметили: хочу пройти")
    elif feedback == "later":
        # Обоснования отвечают на вопрос «почему рекомендовано», и строке
        # «вы это отложили» там не место: она объясняет не выбор продукта, а
        # выбор человека, который он и так помнит. Вес -- есть, фраза -- нет.
        score += config.weight_feedback_later

    if not reasons:
        reasons.append(_fallback_reason(course))

    return ScoredCourse(course=course, score=score, reasons=reasons)


def _apply_preferences(
    course: Course,
    preferences: Preferences,
    interests: InterestIndex | None,
    config: AppConfig,
    score: float,
    reasons: list[str],
) -> tuple[float, list[str]]:
    """Слагаемые, которые человек задал о себе сам."""
    if interests is not None and preferences.interests:
        matched = interests.directions_for_course(course) & set(preferences.interests)
        if matched:
            score += config.weight_interest_match
            names = ", ".join(sorted(interests.title(key) for key in matched))
            reasons.append(f"Ваш интерес: {names}")

    if course.course_key in preferences.picked_courses:
        score += config.weight_picked_course
        reasons.append("Вы отметили эту программу как интересную")

    if preferences.goal is not None and course.competency_group is not None:
        wanted = config.goal_competency_groups.get(preferences.goal.value, [])
        if course.competency_group.value in wanted:
            score += config.weight_goal_match
            reasons.append(f"Отвечает вашей цели: {GOAL_TITLES[preferences.goal]}")

    if preferences.urgency is Urgency.THREE_MONTHS:
        if course.hours is not None and course.hours <= config.quick_hours_max:
            score += config.weight_urgency_match
            form = (course.study_form or "").lower()
            if "дот" in form or "эо" in form:
                reasons.append(f"Можно пройти дистанционно за {course.hours} ч")
            else:
                reasons.append(f"Короткая программа — {course.hours} ч")
        elif course.hours is None and Delivery.EK in course.deliveries:
            # У 64 курсов реестра ЭК нет ни часов, ни формы обучения. Это не
            # мешает ответить на «нужно быстро»: электронный курс и есть тот
            # формат, который человек начинает сам и сегодня. Условие требует
            # именно отсутствия часов -- курс, у которого они есть и их много,
            # быстрым не становится от того, что он ещё и электронный.
            score += config.weight_urgency_match
            reasons.append("Электронный курс — проходится в своём темпе")

    elif preferences.urgency is Urgency.NOT_REQUIRED and Delivery.EK in course.deliveries:
        score += config.weight_urgency_match
        reasons.append("Электронный курс — без отрыва от работы")

    return score, reasons
