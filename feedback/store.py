"""Feedback store: the "хочу / не сейчас / неактуально" a person leaves on
each card in the feed.

Phase-1 decision (see the design spec, section 4.2): this is collected from
day one so the API contract never has to change later to add it, but it does
**not** feed back into ranking in v1 -- that is a deliberate v2 decision.
`export()` exists now for the comparison report Phase 6 will build.

SQLite on disk (`output/feedback.db` by default, already gitignored) rather
than in memory, because the whole point is that a choice a person made
yesterday is still there when the server restarts today.
"""
from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta, timezone
from enum import Enum
from pathlib import Path

from core.paths import OUTPUT_DIR

# Недельный лимит отметок. Отметка -- осознанное решение о программе, а не
# реакция на каждую карточку подряд: ограничение просит подумать, прежде чем
# нажимать, и возвращает человека в продукт на следующей неделе. Таймер
# стартует с первой отметки окна, а не с понедельника: у каждого своя неделя.
MARKS_PER_WEEK = 5
MARKS_WINDOW = timedelta(days=7)


def _utcnow() -> datetime:
    """Текущий момент. Отдельная функция, чтобы тесты подменяли часы, а не спали."""
    return datetime.now(timezone.utc)


def format_wait(seconds: int) -> str:
    """«5 дн. 3 ч», «7 ч», «25 мин.» -- сколько ждать до новых отметок.

    Округление вверх: «через 0 ч» человек прочитал бы как «уже можно», а
    кнопки при этом ещё закрыты. Сокращения «дн.», «ч», «мин.» не склоняются,
    поэтому формулировка не ошибается ни на одном числе.
    """
    seconds = max(0, int(seconds))
    if seconds <= 3600:
        return f"{max(1, math.ceil(seconds / 60))} мин."
    days, hours = divmod(math.ceil(seconds / 3600), 24)
    if days and hours:
        return f"{days} дн. {hours} ч"
    if days:
        return f"{days} дн."
    return f"{hours} ч"


class MarkLimitReached(Exception):
    """Недельный лимит отметок исчерпан; `quota` -- то, что показать человеку."""

    def __init__(self, quota: dict) -> None:
        super().__init__(
            "Недельный лимит отметок исчерпан: новые отметки откроются через "
            f"{quota['resets_in_text']}."
        )
        self.quota = quota


class Action(str, Enum):
    """The three choices under every programme in the customer's booklet."""

    WANT = "want"          # актуально для меня
    LATER = "later"         # актуально для команды / не сейчас
    IRRELEVANT = "irrelevant"  # неактуально


class FeedbackStore:
    """Per-installation SQLite store of the latest choice per (user, course)."""

    def __init__(
        self,
        db_path: str | Path | None = None,
        marks_per_week: int = MARKS_PER_WEEK,
    ) -> None:
        self._path = Path(db_path) if db_path is not None else OUTPUT_DIR / "feedback.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._limit = marks_per_week
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback (
                    user_id TEXT NOT NULL,
                    course_key TEXT NOT NULL,
                    action TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (user_id, course_key)
                )
                """
            )
            # След снятых отметок. Отметку, которую человек снял, из таблицы
            # `feedback` вычёркивает, а подборка при этом устарела так же, как
            # от новой отметки: без следа «Обновить рекомендации» не включилась бы.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS feedback_touch (
                    user_id TEXT PRIMARY KEY,
                    updated_at TEXT NOT NULL
                )
                """
            )
            # Окно недельного лимита: когда человек поставил первую отметку
            # окна и сколько отметок в нём уже потрачено. Живёт на сервере, а
            # не в браузере: очистка кэша или другой компьютер не дают лишних
            # пяти отметок.
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS mark_window (
                    user_id TEXT PRIMARY KEY,
                    started_at TEXT NOT NULL,
                    used INTEGER NOT NULL
                )
                """
            )

    def clear(self, user_id: str, course_key: str) -> None:
        """Снять отметку (вернуть «неактуальное», передумать насчёт «хочу»)."""
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                "DELETE FROM feedback WHERE user_id = ? AND course_key = ?",
                (user_id, course_key),
            )
            conn.execute(
                """
                INSERT INTO feedback_touch (user_id, updated_at) VALUES (?, ?)
                ON CONFLICT(user_id) DO UPDATE SET updated_at = excluded.updated_at
                """,
                (user_id, updated_at),
            )

    # --- Недельный лимит отметок ------------------------------------------

    def _window(self, conn: sqlite3.Connection, user_id: str, now: datetime):
        """(начало, потрачено) действующего окна, или (None, 0), если его нет.

        Окно, которому исполнилось семь дней, считается закрытым: следующая
        отметка откроет новое, а до неё человек видит полный запас.
        """
        row = conn.execute(
            "SELECT started_at, used FROM mark_window WHERE user_id = ?", (user_id,)
        ).fetchone()
        if row is None:
            return None, 0
        started = datetime.fromisoformat(row[0])
        if now >= started + MARKS_WINDOW:
            return None, 0
        return started, row[1]

    def _quota_of(self, started: datetime | None, used: int, now: datetime) -> dict:
        resets_at = started + MARKS_WINDOW if started is not None else None
        seconds_left = (
            max(0, math.ceil((resets_at - now).total_seconds()))
            if resets_at is not None
            else None
        )
        left = max(0, self._limit - used)
        return {
            "limit": self._limit,
            "used": used,
            "left": left,
            "exhausted": left == 0,
            # Пока не поставлена первая отметка, таймера нет: говорить «откроется
            # через 7 дней» человеку, который ничего не тратил, было бы странно.
            "resets_at": resets_at.isoformat() if resets_at else None,
            "seconds_left": seconds_left,
            "resets_in_text": format_wait(seconds_left) if seconds_left is not None else None,
        }

    def quota(self, user_id: str, now: datetime | None = None) -> dict:
        """Сколько отметок осталось на этой неделе и когда откроются новые."""
        moment = now or _utcnow()
        with self._connect() as conn:
            started, used = self._window(conn, user_id, moment)
        return self._quota_of(started, used, moment)

    def mark(
        self, user_id: str, course_key: str, action: str, now: datetime | None = None
    ) -> dict:
        """Поставить отметку с учётом недельного лимита; вернуть остаток.

        Проверка и запись -- в одной транзакции: две отметки, пришедшие
        одновременно на последнюю единицу запаса, не должны обе пройти. При
        исчерпанном лимите ничего не пишется и поднимается `MarkLimitReached`.
        Снятие отметки (`clear`) лимит не тратит и не возвращает.
        """
        action_value = Action(action).value  # ValueError на неизвестное действие
        moment = now or _utcnow()
        stamp = moment.isoformat()
        conn = self._connect()
        try:
            conn.isolation_level = None  # транзакцией управляем сами
            conn.execute("BEGIN IMMEDIATE")
            try:
                started, used = self._window(conn, user_id, moment)
                if used >= self._limit:
                    conn.execute("ROLLBACK")
                    raise MarkLimitReached(self._quota_of(started, used, moment))
                if started is None:
                    started, used = moment, 0
                used += 1
                conn.execute(
                    """
                    INSERT INTO mark_window (user_id, started_at, used) VALUES (?, ?, ?)
                    ON CONFLICT(user_id)
                    DO UPDATE SET started_at = excluded.started_at, used = excluded.used
                    """,
                    (user_id, started.isoformat(), used),
                )
                conn.execute(
                    """
                    INSERT INTO feedback (user_id, course_key, action, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(user_id, course_key)
                    DO UPDATE SET action = excluded.action, updated_at = excluded.updated_at
                    """,
                    (user_id, course_key, action_value, stamp),
                )
                conn.execute("COMMIT")
            except MarkLimitReached:
                raise
            except Exception:
                conn.execute("ROLLBACK")
                raise
        finally:
            conn.close()
        return self._quota_of(started, used, moment)

    def reset_quota(self) -> int:
        """Сбросить таймер и счётчик у ВСЕХ профилей (служебная кнопка для тестов).

        Сами отметки на карточках остаются: сбрасывается только право ставить
        новые. Возвращает, у скольких людей окно было открыто.
        """
        with self._connect() as conn:
            return conn.execute("DELETE FROM mark_window").rowcount

    def rekey_course_keys(self, mapping: dict[str, str]) -> int:
        """Перенести отметки со старых ключей курсов на новые (переименованная программа).

        Если у человека уже есть отметка на новом ключе, она главнее: старая просто удаляется.
        Возвращает, сколько отметок перенесено.
        """
        moved = 0
        with self._connect() as conn:
            for old, new in mapping.items():
                moved += conn.execute(
                    "UPDATE OR IGNORE feedback SET course_key = ? WHERE course_key = ?", (new, old)
                ).rowcount
                conn.execute("DELETE FROM feedback WHERE course_key = ?", (old,))
        return moved

    def user_ids(self) -> set[str]:
        """Все, кто хоть раз что-то отметил -- для предупреждения при замене данных."""
        with self._connect() as conn:
            rows = conn.execute("SELECT DISTINCT user_id FROM feedback").fetchall()
        return {row[0] for row in rows}

    def record(self, user_id: str, course_key: str, action: str) -> None:
        """Store `action` as the current choice for (user_id, course_key).

        Only `want | later | irrelevant` are accepted -- anything else raises
        `ValueError` before touching the database. The latest call for a given
        (user, course) pair always wins; there is no history of past choices,
        only the current one.
        """
        action_value = Action(action).value  # raises ValueError on an unknown action
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO feedback (user_id, course_key, action, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, course_key)
                DO UPDATE SET action = excluded.action, updated_at = excluded.updated_at
                """,
                (user_id, course_key, action_value, updated_at),
            )

    def get(self, user_id: str, course_key: str) -> str | None:
        """The current action for (user_id, course_key), or None if unset."""
        with self._connect() as conn:
            row = conn.execute(
                "SELECT action FROM feedback WHERE user_id = ? AND course_key = ?",
                (user_id, course_key),
            ).fetchone()
        return row[0] if row else None

    def for_user(self, user_id: str) -> dict[str, str]:
        """Every course_key -> action choice this user has made."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT course_key, action FROM feedback WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {course_key: action for course_key, action in rows}

    def latest_change(self, user_id: str) -> str | None:
        """Момент последней отметки этого человека, или None если их нет.

        Нужен, чтобы отличить «отметки уже учтены в подборке» от «человек
        отметил что-то после того, как она собиралась»: на этом держится
        кнопка «Обновить рекомендации» -- см. `AppState.feedback_pending`.
        Время строкой ISO, как оно и лежит в таблице: сравнивается оно только
        на равенство с ранее увиденным значением, и разбирать его незачем.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT MAX(updated_at) FROM feedback WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            touch = conn.execute(
                "SELECT updated_at FROM feedback_touch WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        moments = [m for m in (row[0] if row else None, touch[0] if touch else None) if m]
        return max(moments) if moments else None

    def export(self) -> list[dict[str, str]]:
        """Every row, for the comparison report Phase 6 builds."""
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT user_id, course_key, action, updated_at FROM feedback "
                "ORDER BY user_id, course_key"
            ).fetchall()
        return [
            {"user_id": r[0], "course_key": r[1], "action": r[2], "updated_at": r[3]}
            for r in rows
        ]
