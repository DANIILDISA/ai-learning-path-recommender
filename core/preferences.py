"""Что человек сообщил о себе сам: цель, сроки, интересы, признаки допуска.

Всё здесь -- добровольный ввод через мастер настройки, а не данные из выгрузки
заказчика. Отсюда два свойства, на которые опирается остальной код:

1. **Пустые предпочтения -- валидное состояние.** Мастер можно пропустить
   целиком или на любом шаге, и продукт обязан работать ровно так же, как до
   его появления (инвариант 4 приёмки: продукт работает без модели; здесь то
   же самое про предпочтения).
2. **Признак допуска -- заявление, а не подтверждённый факт.** В выгрузке нет
   ни возраста, ни членства в кадровом резерве, ни служебных обязанностей, и
   взять их неоткуда. Поэтому спрашиваем у человека, а не угадываем по
   должности -- догадка здесь означала бы выдать программу тому, кому она не
   положена.
"""
from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class Goal(str, Enum):
    """Как человек видит своё развитие в ближайшие 2-3 года (шаг 1 мастера)."""

    CAREER_GROWTH = "career_growth"
    CURRENT_ROLE = "current_role"
    NEW_FIELD = "new_field"
    UNDECIDED = "undecided"


class Urgency(str, Enum):
    """Когда человек планирует учиться (шаг 2 мастера).

    Заменяет собой сигнал «свежести», которого нет в данных: дат прохождения
    заказчик не передал (открытый вопрос 1), поэтому срочность спрашиваем.
    """

    THREE_MONTHS = "three_months"
    THIS_YEAR = "this_year"
    NOT_REQUIRED = "not_required"
    UNKNOWN = "unknown"


class Attribute(str, Enum):
    """Признак допуска, который человек заявил о себе (шаг 5 мастера).

    Каждый открывает ровно одну программу каталога и только тому, кто его
    заявил. Пятая закрытая программа («богатый опыт и значимые результаты»)
    признака не имеет намеренно: это самооценка, и вопрос о ней был бы
    приглашением приписать себе заслуги.
    """

    YOUTH_RESERVE = "youth_reserve"
    PROF_DEVELOPMENT = "prof_development"
    SUPERVISION = "supervision"
    FINANCIAL_LITERACY = "financial_literacy"


class Preferences(BaseModel):
    """Ответы одного человека на мастер настройки. Любое поле может быть пустым."""

    model_config = ConfigDict(extra="forbid")

    user_id: str = Field(min_length=1)
    goal: Goal | None = None
    urgency: Urgency | None = None
    # Ограничение «до трёх» -- из макета (шаг 3, «Выберите до трёх вариантов»).
    # Проверяется здесь, а не во фронте: фронт заменяем, правило остаётся.
    interests: list[str] = Field(default_factory=list, max_length=3)
    picked_courses: list[str] = Field(default_factory=list)
    attributes: list[Attribute] = Field(default_factory=list)

    @classmethod
    def empty(cls, user_id: str) -> "Preferences":
        """Предпочтения человека, который мастер не проходил."""
        return cls(user_id=user_id)

    def is_empty(self) -> bool:
        """Мастер не пройден ни в какой части."""
        return not (
            self.goal
            or self.urgency
            or self.interests
            or self.picked_courses
            or self.attributes
        )
