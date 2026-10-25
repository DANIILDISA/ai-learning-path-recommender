"""Форма ответов API.

Модели движка (`core.models`) об интерфейсе не знают и знать не должны: их
читают отчёты `report/` и сравнительный харнесс `benchmark/`. Всё, что нужно
карточке из макета, собирается здесь, на границе, -- так новый интерфейс
получает свои поля, а `Track` остаётся тем же, каким его ждут отчёты.

Главное правило этого модуля: **показывать меньше, но не выдумывать.** У 64
курсов реестра ЭК нет ни трудоёмкости, ни формы обучения, ни группы
компетенций -- это 40% каталога. Макет рисует на карточке «Электронный курс ·
18 часов», и соблазн подставить ноль или прочерк велик. Ноль часов -- неправда
о программе заказчика; отсутствующее поле уезжает как `null`, а решение, что с
ним делать на экране, принимает интерфейс.
"""
from __future__ import annotations

import re

from catalog.catalog import Catalog
from catalog.interests import InterestIndex
from core.models import Course, Delivery, Profile, Recommendation, Status, Track
from core.preferences import Attribute, Preferences
from engine.audience import Audience, classify_audience, required_attribute
from engine.badges import badges_for
from engine.grades import Grade
from engine.scoring import is_self_paced

# Сколько программ маршрута показывается крупными карточками со стрелками.
# Ровно столько рисует макет. Остальные программы маршрута экран траектории не
# рисует (длинный список «на год вперёд» демотивирует), но API их отдаёт: это
# очередь главной -- на место отмеченной карточки встаёт следующая.
NEAR_TERM_SIZE = 4

_DELIVERY_TITLES = {Delivery.PPK: "ППК", Delivery.EK: "Электронный курс"}

# Граница предложения: точка, «?» или «!», за которыми пробел и заглавная буква.
_SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+(?=[А-ЯЁA-Z])")

# Сокращения, у которых точка предложение НЕ заканчивает. Без этого списка
# «планирование, контроль и т.д. Далее...» обрывается на «и т.д.», и человек
# читает огрызок фразы. Список короткий намеренно: сюда попадает только то,
# что реально встречается в аннотациях каталога.
_ABBREVIATIONS = ("т.д.", "т.п.", "т.е.", "т.ч.", "др.", "пр.", "гг.", "г.", "ст.")


# Подписи вариантов мастера. Общий словарь интерфейса, а не внутренняя деталь:
# их читает и /api/preferences/options, и сводка профиля на экране траектории.
# Живут на бэкенде по той же причине, что и вся остальная логика. Живут на бэкенде, а не во фронте, по той же
# причине, по какой там живёт вся остальная логика: интерфейс -- заменяемый
# слой, а заказчик может встроить бэкенд модулем в свой портал. Списки,
# зашитые в JavaScript, пришлось бы переписывать вместе с ним.
GOAL_OPTIONS = (
    ("career_growth", "Карьерный рост",
     "Хочу перейти на более высокую должность"),
    ("current_role", "Развитие в текущей роли",
     "Хочу углубить знания и стать сильнее в своей специализации"),
    ("new_field", "Новое направление",
     "Рассматриваю переход в другую профессиональную область"),
    ("undecided", "Пока не определился",
     "Хочу изучить возможные направления развития"),
)

URGENCY_OPTIONS = (
    ("three_months", "В течение 3 месяцев",
     "Обучение нужно в ближайшее время"),
    ("this_year", "В течение года",
     "Планирую пройти обучение в этом году"),
    ("not_required", "Не требуется",
     "Обязательного обучения сейчас нет"),
    ("unknown", "Не знаю",
     "Пока не располагаю такой информацией"),
)

# Пятый шаг. Формулировки от первого лица и без юридических терминов: человек
# отвечает о себе, а не заполняет анкету отдела кадров. Каждый признак
# открывает ровно одну программу каталога -- см. `engine.audience`.
ATTRIBUTE_OPTIONS = (
    ("youth_reserve",
     "Мне ещё не исполнилось 36 лет или я в Молодёжном кадровом резерве"),
    ("prof_development",
     "Я отвечаю в своём органе за профессиональное развитие сотрудников"),
    ("supervision",
     "Моя работа связана с контролем и надзором"),
    ("financial_literacy",
     "Моя работа связана с повышением финансовой грамотности граждан"),
)


def first_sentence(text: str | None) -> str | None:
    """Первое предложение аннотации целиком, или None если аннотации нет.

    Возвращает именно None, а не пустую строку: «аннотации нет» и «аннотация
    пустая» -- одно и то же состояние, и двух способов его выразить быть не
    должно, иначе интерфейс начнёт проверять оба.
    """
    if not text or not text.strip():
        return None
    cleaned = text.strip()
    for match in _SENTENCE_BREAK.finditer(cleaned):
        head = cleaned[: match.start()]
        if head.endswith(_ABBREVIATIONS):
            continue
        return head
    return cleaned


def delivery_title(course: Course) -> str | None:
    """«ППК» или «Электронный курс».

    Девять курсов каталога идут в обоих форматах -- у них один ключ и одна
    идентичность (инвариант 1 приёмки). Показываем ППК: программа повышения
    квалификации весомее для человека, который выбирает, чем заняться.
    """
    if Delivery.PPK in course.deliveries:
        return _DELIVERY_TITLES[Delivery.PPK]
    if Delivery.EK in course.deliveries:
        return _DELIVERY_TITLES[Delivery.EK]
    return None


def recommendation_view(
    rec: Recommendation,
    course: Course | None,
    position: int,
    feedback: str | None,
    electronic: bool = False,
) -> dict:
    """Одна карточка: всё, что она показывает, и ничего сверх того.

    `course` может быть None: 25 названий из истории обучения каталогу 2025
    года неизвестны. Карточка в этом случае показывает то, что знает трек, и
    не падает.

    `score` наружу не отдаётся. Число, сложенное из весов конфига, человеку
    ничего не говорит и приглашает сравнивать себя с другими по величине,
    которая для этого не предназначена. Вместо него -- бейджи и одно
    предложение `reason`: факт, а не оценка.

    Внутренние строки обоснований движка (`rec.reasons`) наружу не уходят:
    списком из трёх таких строк карточка читалась как служебный журнал.
    `electronic` -- карточка из очереди электронных курсов: она подписана
    «Электронный курс», даже если курс числится и как ППК (таких два).
    """
    badges = badges_for(rec.reasons, position=position)
    delivery = delivery_title(course) if course else None
    if electronic and course is not None and Delivery.EK in course.deliveries:
        delivery = _DELIVERY_TITLES[Delivery.EK]
    return {
        "course_key": rec.course_key,
        "title": rec.title,
        "delivery": delivery,
        "self_paced": bool(course is not None and is_self_paced(course)),
        "hours": course.hours if course else None,
        "study_form": course.study_form if course else None,
        "competency_group": (
            course.competency_group.value
            if course and course.competency_group
            else None
        ),
        "competency_subgroup": course.competency_subgroup if course else None,
        "annotation_short": first_sentence(course.annotation) if course else None,
        "reason": rec.reason or None,
        "explanation_source": rec.explanation_source,
        "badges": [{"kind": b.kind, "text": b.text} for b in badges],
        "position": position,
        "feedback": feedback,
    }


def competency_progress(
    profile: Profile, catalog: Catalog, route_keys: set[str] | None = None
) -> list[dict]:
    """Пройдено из доступного по каждой группе компетенций.

    Считается по программам буклета, у которых указана группа компетенций: у 64 курсов реестра ЭК группы нет
    вообще, и приписать их какой-либо группе значило бы выдумать данные.
    Поэтому знаменатель здесь -- не весь каталог, и подпись на экране обязана
    говорить, что панель про программы буклета.

    Порядок групп -- по названию, чтобы панель не переставлялась между
    запросами: воспроизводимость выдачи распространяется на всё, что видит
    человек, а не только на список курсов.
    """
    totals: dict[str, int] = {}
    passed: dict[str, int] = {}
    planned: dict[str, int] = {}
    passed_keys = profile.passed_keys()
    in_route = route_keys or set()
    for course in catalog:
        if course.competency_group is None:
            continue
        name = course.competency_group.value
        totals[name] = totals.get(name, 0) + 1
        if course.course_key in passed_keys:
            passed[name] = passed.get(name, 0) + 1
        elif course.course_key in in_route:
            planned[name] = planned.get(name, 0) + 1
    # `planned` -- сколько программ направления стоит в маршруте человека.
    # «1 из 55» читалось как «осталось пройти 54», хотя человеку предлагают
    # три; экран теперь показывает пройденное и запланированное, а размер
    # линейки -- справкой.
    return [
        {
            "group": name,
            "passed": passed.get(name, 0),
            "planned": planned.get(name, 0),
            "total": total,
        }
        for name, total in sorted(totals.items())
    ]



def _title_of(options, key):
    """Человеческая подпись ключа предпочтения."""
    for option in options:
        if option[0] == key:
            return option[1]
    return key


def profile_summary(
    profile: Profile,
    track: Track,
    preferences: Preferences,
    interests: InterestIndex,
) -> dict:
    """Левая колонка экрана «Моя траектория».

    Счётчики и подписи считает бэкенд. Во фронте это означало бы, что он знает
    внутренние ключи предпочтений и их русские названия -- то есть половина
    словаря продукта переехала бы в JavaScript, который мы как раз заменяем.

    Счётчик называется «не завершено», а не «в процессе», как в макете:
    статуса «В процессе» в выгрузке заказчика нет вообще, и такой счётчик
    всегда показывал бы ноль. «Не пройден» в данных есть -- 145 записей, --
    и это осмысленное число: человек видит, к чему можно вернуться.
    """
    # По курсам, а не по записям выгрузки: одна программа, записанная трижды,
    # -- один пройденный курс. Незавершённым курс, уже пройденный, не считается.
    passed_keys = profile.passed_keys()
    unfinished = {
        entry.course_key
        for entry in profile.entries
        if entry.status is not Status.PASSED and entry.course_key not in passed_keys
    }
    return {
        "user_id": profile.user_id,
        "position": profile.position,
        "iogv": profile.iogv,
        "grade": track.grade,
        "passed": len(passed_keys),
        "unfinished": len(unfinished),
        "goal": _title_of(GOAL_OPTIONS, preferences.goal.value) if preferences.goal else None,
        "urgency": (
            _title_of(URGENCY_OPTIONS, preferences.urgency.value)
            if preferences.urgency
            else None
        ),
        "interests": [
            interests.title(key)
            for key in preferences.interests
            if key in interests.keys()
        ],
        "attributes": [
            _title_of(ATTRIBUTE_OPTIONS, attribute.value)
            for attribute in preferences.attributes
        ],
    }


def track_cards(
    track: Track,
    catalog: Catalog,
    feedback: dict[str, str],
) -> list[dict]:
    """Весь маршрут карточками, пронумерованный от начала.

    Позиция считается от начала МАРШРУТА, а не от начала страницы: иначе
    «Рекомендуем начать отсюда» появлялось бы на первой карточке каждой
    открытой страницы ленты.
    """
    return [
        recommendation_view(
            rec,
            catalog.get(rec.course_key),
            position=index + 1,
            feedback=feedback.get(rec.course_key),
        )
        for index, rec in enumerate(track.recommendations)
    ]


def ai_note(track: Track) -> str | None:
    """Короткая пометка для прозрачности: что ИИ предложил вне каталога."""
    if track.llm_invented:
        return f"ИИ предложил {track.llm_invented} курсов вне каталога — они отброшены."
    return None


def track_view(
    track: Track,
    profile: Profile,
    catalog: Catalog,
    feedback: dict[str, str],
    preferences: Preferences,
    electronic: list[Recommendation] | None = None,
    interests: InterestIndex | None = None,
    quota: dict | None = None,
    cold_start: bool = False,
) -> dict:
    """Полный ответ экрана «Моё обучение» и очереди главной.

    `near_term` и `later` -- маршрут без отмеченного (отмеченное живёт в
    разделах «Моего обучения»); `electronic` -- очередь персональных
    электронных курсов. Главная показывает по три первых карточки каждой
    очереди и подставляет следующую, когда человек отмечает показанную.
    """
    cards = track_cards(track, catalog, feedback)
    marked = sum(1 for key, action in feedback.items() if catalog.get(key) is not None)
    return {
        "user_id": track.user_id,
        "position": track.position,
        "iogv": track.iogv,
        "grade": track.grade,
        "ai_note": ai_note(track),
        "near_term": cards[:NEAR_TERM_SIZE],
        "later": cards[NEAR_TERM_SIZE:],
        "electronic": [
            recommendation_view(
                rec, catalog.get(rec.course_key), position=0,
                feedback=feedback.get(rec.course_key), electronic=True,
            )
            for rec in (electronic or [])
        ],
        # «В плане» -- подборка и то, что человек сам отметил «хочу пройти»:
        # отмеченное уходит из подборки, но из плана не исчезает.
        "progress": competency_progress(
            profile,
            catalog,
            {rec.course_key for rec in track.recommendations}
            | {key for key, action in feedback.items() if action == "want"},
        ),
        "preferences_applied": not preferences.is_empty(),
        "cold_start": cold_start,
        "enough_data": enough_data(track, profile, catalog, marked=marked),
        "marked_total": marked,
        # Что действительно участвовало в подборке -- для блока «Почему именно
        # такая последовательность». Раньше экран выводил это из текстов
        # обоснований на карточках; теперь на карточках одно предложение.
        "used_signals": {
            "peers": any(
                "коллег" in reason for rec in track.recommendations for reason in rec.reasons
            ),
            "marks": bool(feedback),
        },
        "quota": quota,
        "profile_summary": profile_summary(profile, track, preferences, interests),
    }


def catalogue_view(
    course: Course,
    profile: Profile | None,
    grade: Grade | None,
    declared: set[Attribute],
    interests: InterestIndex,
) -> dict:
    """Карточка каталога с честным состоянием и причиной, если оно закрыто.

    Недоступная программа не прячется, а объясняется. Спрятанная выглядит для
    человека как несуществующая, и он никогда не узнает, что она есть и что
    её можно открыть, заявив о себе на пятом шаге мастера. Объяснённая
    выглядит как честная система, которая знает правила и говорит их вслух.

    `profile` может быть None: каталог открывается и до выбора профиля, и
    тогда состояние программы просто неизвестно.
    """
    state_name, note = "available", None
    if profile is not None and course.course_key in profile.passed_keys():
        state_name, note = "passed", "Вы уже прошли эту программу"
    elif profile is not None:
        audience = classify_audience(course.audience or "")
        if audience is Audience.RESTRICTED:
            needed = required_attribute(course.audience or "")
            if needed is None or needed not in declared:
                state_name = "restricted"
                note = (
                    "Программа для отдельной категории слушателей: "
                    f"{course.audience}"
                )
        elif audience is Audience.LEADERS_ONLY and grade is Grade.SPECIALIST:
            state_name = "restricted"
            note = "Программа адресована категории «руководители»"

    return {
        "course_key": course.course_key,
        "title": course.title,
        "delivery": delivery_title(course),
        "hours": course.hours,
        "study_form": course.study_form,
        "competency_group": (
            course.competency_group.value if course.competency_group else None
        ),
        "competency_subgroup": course.competency_subgroup,
        "annotation_short": first_sentence(course.annotation),
        "interests": sorted(interests.directions_for_course(course)),
        "state": state_name,
        "state_note": note,
    }


# course_key курса, у которого поле «Результаты» дословно повторяет описание
# другого курса реестра (дефект D1). Проверено на живых данных: тот же абзац
# про 59-ФЗ стоит у «Порядка рассмотрения обращений граждан».
#
# Чинить молча нельзя -- мы не знаем, каковы настоящие результаты «Русского
# языка», и придумать их значило бы соврать человеку о том, чему его научат.
# Поэтому поле не показывается, а факт удержания уезжает наружу отдельным
# признаком: интерфейс должен сказать, что данных нет, а не сделать вид, что
# у программы просто нет результатов.
BORROWED_OUTCOMES = frozenset({"русскии язык на государственнои гражданскои службе"})

# Разделители пунктов в поле «Результаты» реестра.
_OUTCOME_SEPARATORS = re.compile(r"[;\n]+")


def outcome_points(text: str | None) -> list[str]:
    """«Знать / уметь / владеть» пунктами.

    Пробел вокруг разделителя в реестре стоит не всегда: без чистки
    «основы;уметь» склеилось бы на экране в одно слово. Слипшиеся слова в
    описаниях этот продукт уже показывал -- нашлись живым проходом по
    интерфейсу, а не тестом.
    """
    if not text or not text.strip():
        return []
    parts = _OUTCOME_SEPARATORS.split(text)
    return [part.strip() for part in parts if part.strip()]


def course_view(course: Course) -> dict:
    """Полная карточка курса для панели, которая выезжает справа."""
    withheld = course.course_key in BORROWED_OUTCOMES
    return {
        "course_key": course.course_key,
        "title": course.title,
        "delivery": delivery_title(course),
        "hours": course.hours,
        "study_form": course.study_form,
        "audience": course.audience,
        "annotation": course.annotation,
        "goal": course.goal,
        "outcome_points": [] if withheld else outcome_points(course.outcomes),
        "outcomes_withheld": withheld,
        "competency_group": (
            course.competency_group.value if course.competency_group else None
        ),
        "competency_subgroup": course.competency_subgroup,
        "navigator_tracks": course.navigator_tracks,
    }


# Ниже этого числа рекомендаций интерфейс показывает «Пока недостаточно
# данных» вместо ленты.
#
# Замер на всех 323 профилях выгрузки: не срабатывает ни разу, каждый получает
# по 10 рекомендаций (docs-internal/продукт-3/tools/measure_empty.py). Это
# защитное состояние на случай другой выгрузки, а не рабочий режим, и в
# документации администратора так и написано. Экран, который никогда не врёт,
# полезнее отсутствующего.
ENOUGH_DATA_MIN = 3


def enough_data(
    track: Track, profile: Profile, catalog: Catalog, marked: int = 0
) -> bool:
    """Хватает ли программ, чтобы показывать подборку подборкой.

    Условие одно: пул после жёстких фильтров не почти пуст. Отмеченные
    человеком программы в пул не входят, но в счёт идут: человек, который
    разобрал почти всё, видит «вы разобрали подборку», а не «данных мало».

    Раньше было и второе условие: хотя бы одна запись истории связана с
    каталогом. Человеку без истории показывался экран «недостаточно данных».
    Теперь он получает подборку по должности с честной подписью «мы пока с
    вами не знакомы» (`engine.reasons`): отказ показать что-либо хуже, чем
    показать и прямо сказать, на чём это основано. `profile` и `catalog`
    оставлены в сигнатуре ради вызывающих.
    """
    return len(track.recommendations) + marked >= ENOUGH_DATA_MIN
