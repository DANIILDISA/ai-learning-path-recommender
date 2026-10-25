"""REST endpoints over the engine -- the contract from the design spec
(section 6.1) plus `/api/health`. This is the *only* way the frontend touches
the backend: no endpoint here reaches into a Python module the frontend could
not also reach over HTTP.

`/api/export` (spec section 6.1) and the LLM-comparison export are Phase 6
work and are deliberately not implemented here.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from api.state import get_state
from api.views import (
    ATTRIBUTE_OPTIONS,
    GOAL_OPTIONS,
    URGENCY_OPTIONS,
    catalogue_view,
    course_view,
    delivery_title,
    track_cards,
    track_view,
)
from core.preferences import Preferences
from diagnostics.checks import Status, run_checks
from feedback.store import Action, MarkLimitReached

router = APIRouter()


class FeedbackIn(BaseModel):
    """Body of `POST /api/feedback`. `action` is restricted to the three
    choices under every programme in the customer's booklet -- FastAPI turns
    anything else into a 422 before this ever reaches the store."""

    user_id: str
    course_key: str
    action: Action


def _profile_or_404(user_id: str):
    state = get_state()
    profile = state.profiles.get(user_id)
    if profile is None:
        raise HTTPException(status_code=404, detail=f"Профиль «{user_id}» не найден.")
    return state, profile


@router.get("/health")
def health():
    """Data/catalogue/positions readiness -- no model group is implied here,
    so no probe is made; that stays `check_llm_group`'s job for `--doctor`."""
    state = get_state()
    checks = run_checks(group=None)
    ok = all(result.status is not Status.FAIL for result in checks)
    return {
        "status": "ok" if ok else "fail",
        "profiles": len(state.profiles),
        "courses": len(state.catalog),
        "checks": [
            {"name": r.name, "status": r.status.value, "detail": r.detail}
            for r in checks
        ],
    }


@router.get("/profiles")
def list_profiles():
    """Lightweight list for the identification screen -- full history lives
    behind `GET /api/profile/{id}`."""
    state = get_state()
    return [
        {"user_id": p.user_id, "position": p.position, "iogv": p.iogv}
        for p in state.profiles.values()
    ]


@router.get("/profile/{user_id}")
def get_profile(user_id: str):
    """The profile plus, per history entry, whether the catalogue knows the course.

    25 courses appear in people's history but not in the 2025 catalogue --
    retired, or renamed in a way the customer's methodologist has yet to
    confirm. Saying so here lets the client skip asking for details it will
    never get, instead of firing requests that 404. A browser logs a failed
    response no matter how gracefully the script handles it, so the only way to
    keep a customer's console clean is to not make the request.
    """
    state, profile = _profile_or_404(user_id)
    payload = profile.model_dump()
    for entry in payload.get("entries", []):
        entry["in_catalogue"] = state.catalog.get(entry["course_key"]) is not None
    return payload


@router.get("/track/{user_id}")
def get_track(user_id: str):
    """Экран «Моё обучение» и главная: маршрут, электронные курсы, прогресс, профиль.

    Маршрут приходит разделённым на «ближайшие шаги» и остаток. Делит бэкенд,
    а не фронт: сколько программ показывать крупно -- решение о содержании, и
    оно должно пережить замену интерфейса.

    Переранжируется моделью только этот один профиль -- см. `AppState.track_for`.
    """
    return _track_body(user_id)


def _track_body(user_id: str) -> dict:
    """Тело ответа экрана «Моё обучение» -- общее для показа и для пересборки.

    Одно тело на оба маршрута намеренно: «обновить» обязано вернуть ровно то,
    что вернул бы следующий показ экрана, иначе состояние после нажатия и
    состояние после перезагрузки разойдутся.
    """
    state, profile = _profile_or_404(user_id)
    found = state.recommendations_for(user_id)
    track, ctx = found
    marks = state.feedback.for_user(user_id)
    # Очередь электронных курсов: персональные, ранжированные по долям коллег
    # той же должности и ИОГВ, близости к пройденному и интересам из мастера.
    # Курсы, которые уже стоят в маршруте, сюда не попадают.
    electronic = state.electronic_for(
        user_id, ctx, exclude={rec.course_key for rec in track.recommendations}
    )
    return track_view(
        track,
        profile,
        state.catalog,
        marks,
        state.preferences.get(user_id),
        electronic=electronic,
        interests=state.interests,
        quota=state.feedback.quota(user_id),
        cold_start=ctx.cold_start,
    )


@router.get("/wizard/pool/{user_id}")
def wizard_pool(user_id: str, limit: int = Query(6, ge=1, le=12)):
    """Программы для шага 4 мастера.

    Берутся из того же трека, что и всё остальное. Собственного отбора здесь
    нет и заводить его не следует: шаг 4 показывает человеку конкретные
    программы, а значит подчиняется тем же инвариантам приёмки. Второй путь,
    по которому программа попадает человеку на глаза, означал бы второй набор
    инвариантов, который надо защищать отдельно, -- и когда-нибудь один из них
    отстанет от другого.
    """
    state, _profile = _profile_or_404(user_id)
    track = state.track_for(user_id)
    marks = state.feedback.for_user(user_id)
    return {
        "user_id": user_id,
        "items": track_cards(track, state.catalog, marks)[:limit],
    }


@router.get("/recommendations/{user_id}")
def get_recommendations(
    user_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(1, ge=1, le=50),
):
    """Карточки ленты — страница того же маршрута.

    Позиция карточки считается от начала МАРШРУТА, а не от начала страницы:
    иначе «Рекомендуем начать отсюда» появлялось бы на первой карточке каждой
    открытой страницы, и бейдж перестал бы что-либо значить.
    """
    state, _profile = _profile_or_404(user_id)
    track = state.track_for(user_id)
    cards = track_cards(track, state.catalog, state.feedback.for_user(user_id))
    return {
        "user_id": user_id,
        "total": len(track.recommendations),
        "offset": offset,
        "limit": limit,
        "items": cards[offset : offset + limit],
    }


@router.get("/catalogue")
def catalogue(
    user_id: str | None = None,
    query: str | None = None,
    delivery: str | None = None,
    interest: str | None = None,
    hours: str | None = None,
    offset: int = Query(0, ge=0),
    limit: int = Query(24, ge=1, le=200),
):
    """Весь каталог с поиском, фильтрами и состоянием для конкретного человека.

    `hours` принимает `short` (<= 40 часов), `long` (> 40) и `unspecified`.
    Последнее обязательно и не является технической мелочью: у 64 курсов
    реестра трудоёмкости нет вообще, и фильтр без этой позиции спрятал бы от
    человека 40% каталога -- ровно ту его часть, которую можно пройти сразу
    и самостоятельно.

    Порядок -- по названию, всегда один и тот же: список, который
    переставляется между открытиями, невозможно просматривать.
    """
    state = get_state()
    profile = state.profiles.get(user_id) if user_id else None
    grade = state.position_index.grade_of(profile.position) if profile else None
    declared = (
        set(state.preferences.get(user_id).attributes) if profile else set()
    )

    # Поиск -- по названию и по описанию: слово из аннотации человек помнит
    # чаще, чем точное название программы.
    needle = query.casefold() if query else ""
    items = [
        catalogue_view(course, profile, grade, declared, state.interests)
        for course in state.catalog
        if not needle
        or needle in course.title.casefold()
        or needle in (course.annotation or "").casefold()
    ]
    if delivery:
        items = [i for i in items if i["delivery"] == delivery]
    if interest:
        items = [i for i in items if interest in i["interests"]]
    if hours == "unspecified":
        items = [i for i in items if i["hours"] is None]
    elif hours == "short":
        items = [i for i in items if i["hours"] is not None and i["hours"] <= 40]
    elif hours == "long":
        items = [i for i in items if i["hours"] is not None and i["hours"] > 40]

    items.sort(key=lambda i: i["title"])
    return {
        "total": len(items),
        "offset": offset,
        "limit": limit,
        "items": items[offset : offset + limit],
    }


@router.get("/course/{course_key}")
def get_course(course_key: str, user_id: str | None = None):
    """Карточка курса. С `user_id` добавляет то, что известно про этого человека:
    его отметку и доступность программы с причиной."""
    state = get_state()
    course = state.catalog.get(course_key)
    if course is None:
        raise HTTPException(status_code=404, detail=f"Курс «{course_key}» не найден в каталоге.")
    body = course_view(course)
    profile = state.profiles.get(user_id) if user_id else None
    if profile is not None:
        grade = state.position_index.grade_of(profile.position)
        declared = set(state.preferences.get(user_id).attributes)
        view = catalogue_view(course, profile, grade, declared, state.interests)
        body["state"] = view["state"]
        body["state_note"] = view["state_note"]
        body["feedback"] = state.feedback.get(user_id, course_key)
        body["quota"] = state.feedback.quota(user_id)
    return body


@router.get("/my-learning/{user_id}")
def my_learning(user_id: str):
    """История обучения плюс то, что человек отметил в ленте.

    До появления этого экрана три действия были чёрной дырой: фидбэк копился
    в базе и нигде не показывался, человек нажимал «хочу» и не видел никаких
    последствий. Теперь у кнопок есть адрес.

    «Неактуально» не смешивается с планами: это отказ, а не отложенный план.
    Оно лежит отдельным блоком «Скрытые мной» -- человек, передумавший, иначе
    не нашёл бы программу, которую сам убрал, и не смог бы её вернуть.
    """
    state, profile = _profile_or_404(user_id)
    marks = state.feedback.for_user(user_id)

    def entry_view(entry) -> dict:
        return {
            "course_key": entry.course_key,
            "title": entry.title,
            "delivery": entry.delivery.value,
            "status": entry.status.value,
            # 25 названий истории каталогу 2025 года неизвестны. Говорим об
            # этом здесь, чтобы интерфейс не запрашивал карточку, которой нет:
            # браузер запишет 404 в консоль заказчика, как бы аккуратно скрипт
            # ни обработал ответ.
            "in_catalogue": state.catalog.get(entry.course_key) is not None,
        }

    def marked(action: str) -> list[dict]:
        items = []
        for course_key, value in sorted(marks.items()):
            if value != action:
                continue
            course = state.catalog.get(course_key)
            if course is None:
                continue
            items.append({
                "course_key": course_key,
                "title": course.title,
                "delivery": delivery_title(course),
                "hours": course.hours,
            })
        return items

    def unique(entries) -> list[dict]:
        """Одна строка на курс. В выгрузке одна программа бывает записана несколько раз
        (повторное прохождение, дубль записи): «Русский язык на государственной гражданской
        службе» стоял в «Пройдено» трижды. `count` -- сколько записей схлопнуто.
        """
        merged: dict[str, dict] = {}
        for entry in entries:
            view = entry_view(entry)
            known = merged.get(view["course_key"])
            if known is None:
                view["count"] = 1
                merged[view["course_key"]] = view
                continue
            known["count"] += 1
            if view["status"] == "in_progress":
                known["status"] = "in_progress"  # начатое весомее брошенного
        return list(merged.values())

    passed_keys = profile.passed_keys()
    return {
        "user_id": user_id,
        "passed": unique(e for e in profile.entries if e.status.value == "passed"),
        # Курс, который уже пройден, «не завершённым» не числится, сколько бы
        # раз до этого ни был записан как «не пройден».
        "unfinished": [
            item
            for item in unique(e for e in profile.entries if e.status.value != "passed")
            if item["course_key"] not in passed_keys
        ],
        "want": marked("want"),
        "later": marked("later"),
        # Скрытые человеком программы -- чтобы он мог передумать и вернуть.
        "hidden": marked("irrelevant"),
    }


# Объявлен ДО `/preferences/{user_id}`: иначе FastAPI сопоставит слово
# «options» с параметром пути и ответит 404 на существующий маршрут.
@router.get("/preferences/options")
def preferences_options():
    """Варианты для мастера настройки — всё, что он рисует на пяти шагах."""
    state = get_state()
    return {
        "goals": [
            {"key": key, "title": title, "description": description}
            for key, title, description in GOAL_OPTIONS
        ],
        "urgency": [
            {"key": key, "title": title, "description": description}
            for key, title, description in URGENCY_OPTIONS
        ],
        # Порядок -- как в interests.yaml, а не по алфавиту и не по ключу.
        # Он осмысленный и повторяет макет; сортировка по ключу давала
        # «Аналитика, Коммуникации, Цифровые технологии, Управление, Работа с
        # людьми», что читается как перемешанный список.
        "interests": [
            {"key": key, "title": title}
            for key, title in state.interests.titles().items()
        ],
        "attributes": [
            {"key": key, "title": title} for key, title in ATTRIBUTE_OPTIONS
        ],
        "max_interests": 3,
    }


@router.get("/preferences/{user_id}")
def get_preferences(user_id: str):
    """Ответы человека, или пустая форма, если мастер не пройден."""
    state, _profile = _profile_or_404(user_id)
    return state.preferences.get(user_id).model_dump()


@router.put("/preferences/{user_id}")
def put_preferences(user_id: str, payload: Preferences):
    """Сохранить ответы мастера и пересобрать трек этого человека.

    Расхождение `user_id` в пути и в теле -- ошибка, а не мелочь: молча приняв
    его, мы записали бы ответы одного человека под именем другого.
    """
    state, _profile = _profile_or_404(user_id)
    if payload.user_id != user_id:
        raise HTTPException(
            status_code=400,
            detail="Идентификатор пользователя в адресе и в теле запроса не совпадают.",
        )
    state.preferences.save(payload)
    state.rebuild_for(user_id)
    return payload.model_dump()


@router.post("/track/{user_id}/refresh")
def refresh_track(user_id: str):
    """Пересобрать подборку принудительно и вернуть тело экрана.

    Отметки применяются сами: любой показ подборки сначала пересобирает её,
    если после сборки появилась отметка (`AppState.recommendations_for`).
    Эта ручка осталась для тех, кому нужна явная пересборка (служебные
    сценарии, тесты) -- результат у неё тот же, что у обычного показа.
    """
    state, _profile = _profile_or_404(user_id)
    state.rebuild_for(user_id)
    return _track_body(user_id)


@router.post("/feedback", status_code=201)
def post_feedback(payload: FeedbackIn):
    """Поставить отметку. Не больше пяти в неделю на человека.

    При исчерпанном лимите -- 429 с понятным текстом и остатком (`quota`), по
    которому экран рисует «новые отметки откроются через ...».
    """
    state = get_state()
    try:
        quota = state.feedback.mark(payload.user_id, payload.course_key, payload.action.value)
    except MarkLimitReached as exc:
        return JSONResponse(
            status_code=429, content={"detail": str(exc), "quota": exc.quota}
        )
    return {"status": "ok", "quota": quota}


@router.get("/marks/{user_id}/quota")
def marks_quota(user_id: str):
    """Сколько отметок осталось на этой неделе и когда откроются новые."""
    state, _profile = _profile_or_404(user_id)
    return state.feedback.quota(user_id)


@router.post("/marks/reset")
def marks_reset():
    """Служебная кнопка для тестов: сбросить недельный лимит у ВСЕХ профилей.

    Отметки на карточках остаются; сбрасывается только счётчик и таймер.
    Кнопка спрятана в самом низу служебного экрана «Данные».
    """
    state = get_state()
    return {"status": "ok", "reset": state.feedback.reset_quota()}


@router.delete("/feedback/{user_id}/{course_key}")
def delete_feedback(user_id: str, course_key: str):
    """Снять отметку: «Вернуть» скрытую программу или передумать насчёт «хочу»."""
    state, _profile = _profile_or_404(user_id)
    state.feedback.clear(user_id, course_key)
    return {"status": "ok"}
