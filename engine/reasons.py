"""«Почему рекомендовано?» -- одно короткое предложение на карточку.

Раньше карточка раскрывала список из трёх строк, собранных из внутренних
обоснований движка: «Подобрано по данным: Программа входит в трек ...»,
«По направлению ... у вас пока 0 курсов». Строки повторяли друг друга, местами
наезжали на соседние и читались как служебный вывод, а не как довод в пользу
программы. Человек, которому предлагают учиться добровольно, должен прочитать
одну понятную фразу.

Правила этого модуля:

* **Ровно одно предложение, не больше 20 слов.** Длинное название пройденного
  курса сокращается (`short_title`), а не растягивает фразу.
* **Только факт, который есть в данных.** Каждая формулировка привязана к
  проверяемому признаку: пройденный курс того же направления, доля коллег,
  цель или интерес из мастера, незавершённый курс. Нет признака -- нет фразы;
  когда нет вообще ничего, остаётся честное «открыт для всех».
* **Холодный старт называется холодным стартом.** У человека без истории и без
  ответов мастера мы ничего не знаем о нём лично, и фраза говорит об этом
  прямо -- вместо выдуманной «персонализации».
* **Модель пишет формулировку, а не факты.** Если подключена языковая модель,
  её предложение принимается только после проверки (`validate_sentence`):
  одно предложение, до 20 слов, ни одного числа и названия в кавычках, которых
  нет среди переданных ей фактов. Иначе берётся шаблон.

Модуль чистый: ни сети, ни базы, один и тот же вход даёт один и тот же текст.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from catalog.catalog import Catalog
from catalog.interests import InterestIndex
from core.config import AppConfig
from core.models import Course, Delivery, Profile, Recommendation, Status
from core.preferences import Goal, Preferences, Urgency
from engine.audience import Audience, classify_audience
from engine.eligibility import eligible_courses
from engine.grades import Grade, PositionIndex, TRACK_BY_GRADE
from engine.peers import PeerIndex
from engine.scoring import is_self_paced

MAX_WORDS = 20

# Сколько знаков занимает название пройденного курса внутри фразы. Остальные
# слова фразы -- семь-девять, так что и с десятью словами названия она не
# выходит за 20.
_TITLE_CHARS = 80
_TITLE_WORDS = 10

# «Его прошли N из 10» -- не округляем в большую сторону: 7% коллег нельзя
# выдавать за «1 из 10». Ниже этой доли сигнал слишком слабый для фразы.
MIN_SHARE_FOR_PHRASE = 0.10

_WORD = re.compile(r"[^\W_]+(?:-[^\W_]+)*", re.UNICODE)

# Короткие служебные слова: слово «в» или «и» на конце обрезанного названия
# выглядит как обрыв фразы.
_DANGLING = {"и", "в", "во", "на", "по", "с", "со", "к", "о", "об", "от", "для",
             "при", "про", "за", "из", "у", "а", "но", "или", "как", "что", "не"}

# Окончания прилагательных. Короткие («-ий», «-ие», «-ей») с существительными
# совпадают («решений», «обеспечение»), поэтому берутся только однозначные
# формы единственного числа. Во множественном «-ых/-их/-ые» -- лишь у длинных
# слов: «государственных», «гражданских», но не «данных» и «служащих».
_ADJ_SINGULAR = re.compile(r"\w{4,}(?:ый|ой|ая|яя|ое|ее|ую|юю|ого|его|ому|ему|ым)$")
_ADJ_PLURAL = re.compile(r"\w{7,}(?:ые|ых|их|ыми)$")

_GROUP_NOUN = {
    Grade.SPECIALIST: "специалистов",
    Grade.MANAGER: "руководителей",
    Grade.SENIOR: "руководителей высшего звена",
}

# Приоритет видов обоснования: чем выше -- тем личнее и тем раньше выбирается.
KIND_ORDER = (
    "picked", "unfinished", "interest", "continues", "goal", "urgency",
    "popular", "cold_track", "peers_iogv", "peers", "peers_grade",
    "gap", "track", "open",
)

# Виды, которые не называют настоящей причины. Электронный курс, у которого
# нет ничего, кроме них, -- «доступен всем», а не «рекомендован».
GENERIC_KINDS = frozenset({"open"})


@dataclass(frozen=True)
class Fact:
    """Одна готовая фраза и вид признака, из которого она выросла.

    `alt` -- те же сведения другими словами. Когда одна и та же фраза уже
    стоит на соседней карточке, берётся вариант: три одинаковых строки подряд
    читаются как сбой, хотя каждая из них правдива.
    """

    kind: str
    text: str
    alt: tuple[str, ...] = ()


def count_words(text: str) -> int:
    return len(_WORD.findall(text))


def _dangling(word: str, plural: bool = False) -> bool:
    """Слово, на котором фразу обрывать нельзя: предлог, союз или прилагательное.

    «... в сфере» и «... на государственной» читаются как недописанная фраза;
    название честнее закончить на последнем существительном. `plural` --
    проверять и окончания множественного числа: нужно при обрезке по длине,
    где последнее слово наверняка не конец названия, но не при отсечении
    хвоста по запятой, где «... служащих» -- законченное название.
    """
    bare = word.strip(",;:").casefold()
    return (
        bare in _DANGLING
        or bool(_ADJ_SINGULAR.search(bare))
        or (plural and bool(_ADJ_PLURAL.search(bare)))
    )


# Места, где название можно оборвать без потери смысла: подзаголовок,
# уточнение в скобках, перечисление.
_CUT_POINTS = re.compile(r"(?::\s|\.\s| — | – | - | \(|;\s|,\s)")


def short_title(title: str, max_chars: int = _TITLE_CHARS, max_words: int = _TITLE_WORDS) -> str:
    """Название курса, пригодное для вставки в одну фразу.

    Названия реестра доходят до 220 знаков с подзаголовком, уточнением в
    скобках и перечислением. Сначала отбрасывается такой хвост -- но только
    если голова остаётся законченным названием: «... добровольческой
    (волонтёрской) деятельности» по скобке резать нельзя. Если и голова не
    помещается, название режется по границе слова и заканчивается
    многоточием. Обрыва посреди слова, на предлоге и на прилагательном не
    бывает.
    """
    text = " ".join(title.replace("«", "").replace("»", "").split())
    for match in _CUT_POINTS.finditer(text):
        head = text[: match.start()].rstrip(" ,;:-–—(")
        floor = 25 if match.group().startswith(",") else 12
        if len(head) >= floor and not _dangling(head.split()[-1]):
            text = head
            break
    words = text.split()
    if len(text) <= max_chars and len(words) <= max_words:
        return text
    kept: list[str] = []
    for word in words[:max_words]:
        if len(" ".join(kept + [word])) > max_chars:
            break
        kept.append(word)
    while len(kept) > 2 and _dangling(kept[-1], plural=True):
        kept.pop()
    return (" ".join(kept).rstrip(",;:-–—") + "…") if kept else text[:max_chars].rstrip() + "…"


def _with_title(title: str, template: str) -> str:
    """Шаблон с `{t}` на месте названия, не длиннее 20 слов.

    Название укорачивается по слову, пока вся фраза не уложится: правило
    «одно предложение до 20 слов» -- про фразу целиком, а не про название.
    """
    text = ""
    for words in range(_TITLE_WORDS, 2, -1):
        text = template.replace("{t}", short_title(title, max_words=words))
        if count_words(text) <= MAX_WORDS:
            break
    return text


def _stems(title: str) -> set[str]:
    """Основы значимых слов названия -- чтобы выбрать самый близкий курс."""
    return {w.casefold()[:6] for w in _WORD.findall(title) if len(w) >= 4}


def _lower_first(text: str) -> str:
    return text[:1].casefold() + text[1:] if text else text


def _in_tenths(share: float) -> int:
    """Доля -> «N из 10». Десять из десяти -- только если не пройдено лишь исключение."""
    tenths = round(share * 10)
    return tenths if share >= 0.995 else min(tenths, 9)


@dataclass(frozen=True)
class Relation:
    """Пройденный курс, с которым связан этот, и уровень связи."""

    course: Course
    level: str   # subgroup | direction | group
    label: str   # подкатегория, направление интереса или группа компетенций

    @property
    def strong(self) -> bool:
        """Связь по явной разметке (подкатегория или направление), а не по группе."""
        return self.level in ("subgroup", "direction")


@dataclass
class ReasonContext:
    """Всё, что нужно знать о человеке, чтобы сформулировать причину.

    Собирается один раз на запрос: доли коллег считаются проходом по когорте, и
    повторять его для каждой из двадцати карточек незачем.
    """

    profile: Profile
    catalog: Catalog
    config: AppConfig
    preferences: Preferences
    interests: InterestIndex
    grade: Grade
    passed: list[Course]
    cold_start: bool
    peer_count: int = 0
    peer_share: dict[str, float] = field(default_factory=dict)
    iogv_count: int = 0
    iogv_share: dict[str, float] = field(default_factory=dict)
    grade_size: int = 0
    grade_share: dict[str, float] = field(default_factory=dict)
    group_counts: dict[object, int] = field(default_factory=dict)
    popular_rank: dict[str, int] = field(default_factory=dict)
    seen: dict[str, Status] = field(default_factory=dict)
    # Направления интересов по каждому пройденному курсу -- явная разметка
    # `interests.yaml`, а не догадка по словам названия.
    passed_directions: list[tuple[Course, set[str]]] = field(default_factory=list)

    @classmethod
    def build(
        cls,
        profile: Profile,
        catalog: Catalog,
        peer_index: PeerIndex,
        position_index: PositionIndex,
        config: AppConfig,
        preferences: Preferences | None = None,
        interests: InterestIndex | None = None,
    ) -> "ReasonContext":
        preferences = preferences or Preferences.empty(profile.user_id)
        interests = interests or InterestIndex({})
        grade = position_index.grade_of(profile.position)

        passed_keys = profile.passed_keys()
        passed = [c for key in sorted(passed_keys) if (c := catalog.get(key)) is not None]
        seen_known = [e for e in profile.entries if catalog.get(e.course_key) is not None]
        cold = not passed and not seen_known and preferences.is_empty()

        status: dict[str, Status] = {}
        for entry in profile.entries:
            if entry.status is Status.PASSED:
                continue
            # «В процессе» весомее «не пройден»: это действие, а не итог.
            if status.get(entry.course_key) is not Status.IN_PROGRESS:
                status[entry.course_key] = entry.status

        peers = peer_index.peers_of(profile)
        iogv_peers = peer_index.peers_of(profile, same_iogv_only=True)
        grade_share, grade_size = peer_index.grade_completion_share(profile, position_index.grade_of)
        peer_share = peer_index.completion_share(profile)

        counts: dict[object, int] = {}
        for course in passed:
            if course.competency_group is not None:
                counts[course.competency_group] = counts.get(course.competency_group, 0) + 1

        ctx = cls(
            profile=profile, catalog=catalog, config=config, preferences=preferences,
            interests=interests, grade=grade, passed=passed, cold_start=cold,
            peer_count=len(peers), peer_share=peer_share,
            iogv_count=len(iogv_peers), iogv_share=peer_index.iogv_share(profile),
            grade_size=grade_size, grade_share=grade_share,
            group_counts=counts, seen=status,
            passed_directions=[(c, interests.directions_for_course(c)) for c in passed],
        )
        ctx.popular_rank = ctx._rank_popular(
            eligible_courses(catalog, passed_keys, profile.seen_keys(), grade,
                             attributes=set(preferences.attributes))
        )
        return ctx

    def _rank_popular(self, pool: list[Course]) -> dict[str, int]:
        """Место курса среди допустимых по доле коллег на той же должности."""
        if self.peer_count < self.config.min_peer_cohort:
            return {}
        ranked = sorted(
            (c for c in pool if self.peer_share.get(c.course_key, 0.0) >= self.config.min_peer_share),
            key=lambda c: (-self.peer_share[c.course_key], c.course_key),
        )
        return {c.course_key: place for place, c in enumerate(ranked, start=1)}

    # --- Связь с пройденным ---------------------------------------------

    def related_passed(self, course: Course) -> "Relation | None":
        """Пройденный курс того же направления и то, чем именно они связаны.

        Три уровня, от узкого к широкому: общая подкатегория буклета
        (`subgroup`: «логично продолжит»), общее направление интереса из
        `interests.yaml` (`direction`: явная разметка, а не угадывание по
        словам), одна и та же группа компетенций (`group`: говорим только
        «из того же направления»). Из нескольких подходящих берётся самый
        близкий по словам названия, при равенстве -- короткое название и ключ:
        выбор воспроизводим.
        """
        own_directions = self.interests.directions_for_course(course)
        by_level: dict[str, list[tuple[Course, str]]] = {"subgroup": [], "direction": [], "group": []}
        for other, directions in self.passed_directions:
            if other.course_key == course.course_key:
                continue
            shared = sorted(own_directions & directions)
            if (
                course.competency_subgroup is not None
                and other.competency_subgroup == course.competency_subgroup
            ):
                by_level["subgroup"].append((other, course.competency_subgroup))
            elif shared:
                by_level["direction"].append((other, _lower_first(self.interests.title(shared[0]))))
            elif (
                course.competency_group is not None
                and other.competency_group == course.competency_group
            ):
                by_level["group"].append((other, course.competency_group.value))
        stems = _stems(course.title)

        def closeness(pair):
            other = pair[0]
            return (-len(stems & _stems(other.title)), len(other.title), other.course_key)

        for level in ("subgroup", "direction", "group"):
            if by_level[level]:
                other, label = min(by_level[level], key=closeness)
                return Relation(other, level, label)
        return None


def facts_for(course: Course, ctx: ReasonContext) -> list[Fact]:
    """Все фразы, которые про этот курс можно сказать правду, от личных к общим."""
    prefs = ctx.preferences
    config = ctx.config
    self_paced = is_self_paced(course)
    facts: list[Fact] = []

    if course.course_key in prefs.picked_courses:
        facts.append(Fact("picked", "Вы сами отметили эту программу как интересную"))

    status = ctx.seen.get(course.course_key)
    if status is Status.IN_PROGRESS:
        facts.append(Fact("unfinished", "Вы уже начали этот курс — самое время его завершить"))
    elif status is Status.NOT_PASSED:
        facts.append(Fact("unfinished", "Вы уже начинали этот курс — можно вернуться к нему"))

    matched = ctx.interests.directions_for_course(course) & set(prefs.interests)
    if matched:
        names = ", ".join(_lower_first(ctx.interests.title(k)) for k in sorted(matched)[:2])
        facts.append(Fact(
            "interest",
            f"Совпадает с вашим интересом: {names}",
            (f"Связан с вашим интересом: {names}", f"Отвечает вашему интересу: {names}"),
        ))

    related = ctx.related_passed(course)
    if related is not None:
        title = related.course.title
        if related.level == "subgroup":
            facts.append(Fact(
                "continues",
                _with_title(title, "Вы прошли «{t}» — этот курс логично его продолжит"),
                (
                    _with_title(title, "Следующий шаг после курса «{t}», который вы уже прошли"),
                    _with_title(title, "Продолжает тему курса «{t}», пройденного вами"),
                ),
            ))
        else:
            label = related.label
            facts.append(Fact(
                "continues",
                _with_title(title, f"Вы прошли «{{t}}» — этот курс из того же направления: {label}"),
                (
                    _with_title(title, f"Тоже из направления «{label}», как пройденный вами курс «{{t}}»"),
                    _with_title(title, f"Направление «{label}» вам уже знакомо по курсу «{{t}}»"),
                ),
            ))

    if prefs.goal is not None and course.competency_group is not None:
        wanted = config.goal_competency_groups.get(prefs.goal.value, [])
        if course.competency_group.value in wanted:
            goal = _GOAL_PHRASE[prefs.goal]
            facts.append(Fact(
                "goal",
                f"Совпадает с вашей целью: {goal}",
                (f"Работает на вашу цель: {goal}", f"Поддерживает вашу цель: {goal}"),
            ))

    if prefs.urgency is Urgency.THREE_MONTHS:
        if course.hours is not None and course.hours <= config.quick_hours_max:
            form = (course.study_form or "").lower()
            how = (
                f"можно пройти дистанционно за {course.hours} ч"
                if "дот" in form or "эо" in form
                else f"короткая программа, {course.hours} ч"
            )
            facts.append(Fact("urgency", f"Подходит под ваши сроки: {how}"))
        elif course.hours is None and Delivery.EK in course.deliveries:
            facts.append(Fact("urgency", "Подходит под ваши сроки: проходится в своём темпе"))
    elif prefs.urgency is Urgency.NOT_REQUIRED and Delivery.EK in course.deliveries:
        facts.append(Fact("urgency", "Электронный курс — можно пройти без отрыва от работы"))

    own_track = TRACK_BY_GRADE[ctx.grade] in course.navigator_tracks

    if ctx.cold_start:
        rank = ctx.popular_rank.get(course.course_key)
        if rank == 1:
            facts.append(Fact("popular", "Самое востребованное у коллег на вашей должности"))
        elif rank is not None and rank <= 3:
            facts.append(Fact("popular", "Одно из самых востребованных у коллег на вашей должности"))
        if own_track:
            facts.append(Fact(
                "cold_track",
                "Мы пока с вами не знакомы, но этот курс чаще всего полезен на вашей должности",
                (
                    "Пока мы вас не знаем, но этот курс чаще всего полезен на вашей должности",
                    "Мы только знакомимся с вами, а этот курс чаще всего полезен на вашей должности",
                ),
            ))

    config_min = config.min_peer_cohort
    iogv = ctx.iogv_share.get(course.course_key, 0.0)
    if ctx.iogv_count >= config_min and iogv >= MIN_SHARE_FOR_PHRASE:
        n = _in_tenths(iogv)
        facts.append(Fact(
            "peers_iogv",
            f"Его прошли {n} из 10 ваших коллег из того же органа власти",
            (
                f"В вашем органе власти этот курс прошли {n} из 10 коллег на такой же должности",
                f"Из 10 ваших коллег из того же органа власти его прошли {n}",
            ),
        ))
    share = ctx.peer_share.get(course.course_key, 0.0)
    if ctx.peer_count >= config_min and share >= MIN_SHARE_FOR_PHRASE:
        n = _in_tenths(share)
        facts.append(Fact(
            "peers",
            f"Его прошли {n} из 10 ваших коллег на такой же должности",
            (
                f"На вашей должности этот курс прошли {n} из 10 коллег",
                f"Среди коллег на вашей должности его прошли {n} из 10",
            ),
        ))
    grade_share = ctx.grade_share.get(course.course_key, 0.0)
    if ctx.grade_size >= config_min and grade_share >= MIN_SHARE_FOR_PHRASE:
        noun = _GROUP_NOUN.get(ctx.grade, "коллег")
        n = _in_tenths(grade_share)
        facts.append(Fact(
            "peers_grade",
            f"Его прошли {n} из 10 ваших коллег-{noun}",
            (
                f"Среди {noun} этот курс прошли {n} из 10",
                f"Из 10 ваших коллег-{noun} его прошли {n}",
            ),
        ))

    if course.competency_group is not None:
        group = ctx.group_counts.get(course.competency_group, 0)
        fewest = min((ctx.group_counts.get(g, 0) for g in type(course.competency_group)), default=0)
        if group == fewest:
            name = course.competency_group.value
            # Формулировки -- приглашение, а не упрёк: «у вас пока нет ...» читалось
            # как выговор. Факт тот же -- в этом направлении у человека пройдено
            # меньше всего (или ничего).
            if group == 0:
                facts.append(Fact(
                    "gap",
                    f"Новое для вас направление — «{name}»",
                    (
                        f"Откройте для себя направление «{name}»",
                        f"Здесь можно начать знакомство с направлением «{name}»",
                    ),
                ))
            else:
                facts.append(Fact(
                    "gap",
                    f"Направление «{name}» — ваша точка роста: здесь пройдено меньше всего программ",
                    (
                        f"Расширьте направление «{name}», где у вас пока меньше всего программ",
                        f"Развейте направление «{name}» — здесь у вас пройдено меньше всего",
                    ),
                ))

    if own_track:
        facts.append(Fact(
            "track",
            "Входит в линейку 2025 года для вашей группы должностей",
            ("Включён в программу обучения для вашей группы должностей",
             "Составлен для вашей группы должностей в линейке 2025 года"),
        ))

    open_text, *open_alt = _open_sentences(course, self_paced)
    facts.append(Fact("open", open_text, tuple(open_alt)))
    return facts


_GOAL_PHRASE = {
    Goal.CAREER_GROWTH: "карьерный рост",
    Goal.CURRENT_ROLE: "развитие в текущей роли",
    Goal.NEW_FIELD: "новое направление",
    Goal.UNDECIDED: "пока не определились",
}


def _open_sentences(course: Course, self_paced: bool) -> tuple[str, ...]:
    """Последний, самый общий довод -- и он тоже правдив."""
    if classify_audience(course.audience or "") is Audience.LEADERS_ONLY:
        return (
            "Адресована руководителям — это ваша группа должностей",
            "Программа для руководителей, а вы относитесь к этой группе должностей",
        )
    if self_paced:
        return (
            "Электронный курс открыт всем служащим — можно начать в любой момент",
            "Электронный курс без записи — доступен вам прямо сейчас",
        )
    return (
        "Программа открыта для всех служащих и доступна вашей должности",
        "Эта программа доступна всем служащим, в том числе на вашей должности",
        "Для этой программы ограничений по должности нет",
    )


def is_personal(facts: list[Fact]) -> bool:
    """Есть ли среди фраз хоть одна не «открыт для всех»."""
    return any(f.kind not in GENERIC_KINDS for f in facts)


def choose(facts: list[Fact], avoid: str | None = None) -> Fact:
    """Самая личная фраза; вид `avoid` (у соседней карточки) -- только если иного нет.

    Три карточки подряд с одним зачином «Вы прошли ...» читаются как шаблон.
    Предпочитаем другой вид, если он есть, но не ценой правды: фразы вида
    «другой» берутся только из настоящих признаков этой же карточки.
    """
    ordered = sorted(facts, key=lambda f: KIND_ORDER.index(f.kind))
    best = ordered[0]
    if best.kind != avoid:
        return best
    for fact in ordered[1:]:
        # Ради разнообразия не опускаемся до «открыт для всех»: повторить
        # настоящий довод лучше, чем заменить его пустым.
        if fact.kind in GENERIC_KINDS:
            break
        if fact.kind != avoid:
            return fact
    return best


def fill_reasons(recommendations: list[Recommendation], ctx: ReasonContext) -> list[Recommendation]:
    """Проставить `reason` каждой карточке по порядку выдачи.

    Предложение, которое уже написала и подтвердила модель, не трогаем.
    Решение о соседстве принимается по порядку, в котором карточки идут на
    экране, поэтому вид соседней карточки учитывается и у «написанной моделью».
    """
    result: list[Recommendation] = []
    previous: str | None = None
    used: set[str] = set()
    for rec in recommendations:
        if rec.reason:
            result.append(rec)
            previous = "ai"
            continue
        course = ctx.catalog.get(rec.course_key)
        if course is None:
            result.append(rec)
            previous = None
            continue
        fact = choose(facts_for(course, ctx), avoid=previous)
        previous = fact.kind
        text = next((t for t in (fact.text, *fact.alt) if t not in used), fact.text)
        used.add(text)
        result.append(rec.model_copy(update={"reason": text}))
    return result


# --- Проверка предложения языковой модели --------------------------------

_FORBIDDEN = ("обязан", "необходимо пройти", "в соответствии с", "**", "\n")

# Обещания и оценки, которых нет в данных: что человек «научится», «освоит»,
# «получит», что курс «критичен» или «ключевой». Фактов об этом у нас нет --
# только о том, почему курс подобран, -- и модель, которой дали название курса,
# любит дописывать пользу. Такая фраза отбрасывается целиком.
_PROMISES = re.compile(
    r"научит|освои|изуч|получит|разовь|развит|повыс|помож|позвол|поймет|узнает|разбер|"
    r"закро|даст\b|дает\b|укреп|расшир|пригодит|критичн|ключев|необходим|важн|"
    r"эффективн|качеств|уверенн",
    re.IGNORECASE,
)

_NUMBER = re.compile(r"\d+")
_QUOTED = re.compile(r"«([^»]+)»")
_SENTENCE_BREAK = re.compile(r"[.!?…]\s+\S")

# Слова без собственного содержания: местоимения, связки, общие слова про курс.
_FILLER = frozenset({
    "этот", "этого", "этой", "этому", "курс", "курса", "курсу", "курсы", "программа",
    "программы", "программу", "который", "которые", "которого", "можно", "чтобы",
    "также", "именно", "очень", "ваших", "вашей", "вашим", "вашу", "ваши", "вашего",
    "вашем", "вашими", "такой", "таких", "такая", "всего", "чаще", "более", "более",
    "прошли", "прошел", "прошла", "прошёл", "пока", "вами", "знакомы", "потому",
    "поэтому", "когда", "тоже", "только", "ещё", "уже",
})
MIN_GROUNDED = 0.6


def _content_stems(text: str) -> set[str]:
    stems = set()
    for word in _WORD.findall(text.casefold().replace("ё", "е")):
        if len(word) >= 5 and word not in _FILLER:
            stems.add(word[:5])
    return stems


def validate_sentence(text: str, fact_texts: list[str], extra: list[str] | None = None) -> str | None:
    """Предложение модели, если оно годится, иначе None (и будет шаблон).

    Годится: ровно одно предложение, не больше 20 слов; ни одного числа и
    названия в кавычках, которых нет в фактах; ни канцелярских оборотов, ни
    обещаний результата («научитесь», «освоите», «критично»); и не меньше 60%
    значимых слов взяты из фактов (`extra` -- ещё и название курса). Это не
    доказательство, что модель ничего не придумала, но заслон от самых вредных
    вымыслов: цифр, названий, обещаний и фраз, не связанных с фактами.
    """
    cleaned = " ".join((text or "").split()).strip(' "\'')
    if not cleaned or any(token in cleaned for token in _FORBIDDEN):
        return None
    if _SENTENCE_BREAK.search(cleaned):
        return None
    if count_words(cleaned) > MAX_WORDS or count_words(cleaned) < 3:
        return None
    known = " ".join(fact_texts)
    # Обещание -- это слово, которого нет среди фактов: «развитие» из фразы
    # про цель «развитие в текущей роли» обещанием не считается.
    known_words = set(_WORD.findall(known.casefold()))
    invented = [w for w in _WORD.findall(cleaned.casefold()) if w not in known_words]
    if _PROMISES.search(" ".join(invented)):
        return None
    known_numbers = set(_NUMBER.findall(known))
    if any(number not in known_numbers for number in _NUMBER.findall(cleaned)):
        return None
    if any(quoted not in known for quoted in _QUOTED.findall(cleaned)):
        return None
    mine = _content_stems(cleaned)
    if mine:
        allowed = _content_stems(" ".join([known, *(extra or [])]))
        if len(mine & allowed) / len(mine) < MIN_GROUNDED:
            return None
    return cleaned
