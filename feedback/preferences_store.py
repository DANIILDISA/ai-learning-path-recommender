"""Хранилище ответов мастера настройки. Рядом с фидбэком и по тем же правилам.

Одна строка на человека, а не история ответов: мастер отвечает на вопрос
«что человек думает сейчас». Предыдущие ответы никому не нужны, а хранить их
значит отвечать на вопрос, который никто не задавал.

SQLite на диске (`output/preferences.db` по умолчанию, уже в gitignore), потому
что мастер проходят один раз, а треки собирают много раз и после перезапусков.

Ответы кладём одним JSON-полем, а не колонкой на каждый шаг: набор шагов мастера
ещё меняется, и миграция схемы на каждую правку макета обошлась бы дороже, чем
то, что мы теряем -- запросы по отдельным полям, которых никто не делает.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from core.paths import OUTPUT_DIR
from core.preferences import Preferences


class PreferencesStore:
    """Per-installation SQLite store of the latest wizard answers per user."""

    def __init__(self, db_path: str | Path | None = None) -> None:
        self._path = Path(db_path) if db_path is not None else OUTPUT_DIR / "preferences.db"
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @property
    def path(self) -> Path:
        """Файл, в котором лежат ответы. Нужен диагностике и тестам."""
        return self._path

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self._path)

    def _init_schema(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS preferences (
                    user_id TEXT PRIMARY KEY,
                    payload TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )

    def save(self, preferences: Preferences) -> None:
        """Сделать `preferences` текущими ответами этого человека.

        Последний вызов всегда побеждает: `user_id` -- первичный ключ, так что
        повторное прохождение мастера заменяет строку, а не добавляет вторую.
        """
        payload = preferences.model_dump_json()
        updated_at = datetime.now(timezone.utc).isoformat()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO preferences (user_id, payload, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(user_id)
                DO UPDATE SET payload = excluded.payload, updated_at = excluded.updated_at
                """,
                (preferences.user_id, payload, updated_at),
            )

    def get(self, user_id: str) -> Preferences:
        """Ответы человека, или пустые предпочтения если мастер не пройден.

        Пустые предпочтения, а не None: «мастер не проходил» -- обычное
        состояние, и вызывающий код не должен различать его отдельной веткой.
        """
        with self._connect() as conn:
            row = conn.execute(
                "SELECT payload FROM preferences WHERE user_id = ?", (user_id,)
            ).fetchone()
        if row is None:
            return Preferences.empty(user_id)
        return self._parse(user_id, row[0])

    def rekey_course_keys(self, mapping: dict[str, str]) -> int:
        """Заменить в «отмеченных программах» старые ключи курсов на новые.

        Программу переименовали (например, исправили слитное написание в названии), а ответы мастера
        хранят прежний ключ: без переноса отметка «эта программа мне интересна» молча пропала бы.
        Возвращает, у скольких людей что-то изменилось.
        """
        changed = 0
        for user_id, preferences in self.all().items():
            picked = list(dict.fromkeys(mapping.get(key, key) for key in preferences.picked_courses))
            if picked != preferences.picked_courses:
                self.save(preferences.model_copy(update={"picked_courses": picked}))
                changed += 1
        return changed

    def all(self) -> dict[str, Preferences]:
        """Все сохранённые предпочтения -- для прогона треков по корпусу."""
        with self._connect() as conn:
            rows = conn.execute("SELECT user_id, payload FROM preferences").fetchall()
        return {
            user_id: self._parse(user_id, payload) for user_id, payload in rows
        }

    @staticmethod
    def _parse(user_id: str, payload: str) -> Preferences:
        """Разобрать строку базы, не роняя запрос из-за неё.

        Ответы лежат одним JSON-полем, а `Preferences` объявлена с
        `extra="forbid"`: стоит удалить или переименовать шаг мастера, и
        строки, записанные прежней версией, перестанут проходить валидацию.
        Уронить на этом показ рекомендаций нельзя -- человек увидел бы ошибку
        на главном экране из-за того, что когда-то ответил на вопрос, который
        мы потом убрали.

        Поэтому нечитаемая строка означает «мастер не пройден»: трек
        собирается детерминированно, как у всех остальных, а причина уходит
        в stderr -- ровно как с недоступной моделью. Молчаливый откат этот
        продукт уже проходил, и час на поиск причины стоил дороже строки в лог.
        """
        try:
            return Preferences.model_validate(json.loads(payload))
        except (ValueError, TypeError) as exc:
            print(
                f"Сохранённые предпочтения «{user_id}» не читаются ({exc}); "
                f"трек строится без них.",
                file=sys.stderr,
            )
            return Preferences.empty(user_id)
