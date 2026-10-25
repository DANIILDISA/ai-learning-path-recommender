"""Application state: the corpus loaded once, at process start.

Loading the corpus (two xlsx exports, the YAML catalogue, and the peer index
built over all 323 profiles) takes seconds. Doing that per request is
unacceptable, so this module holds a single process-wide `AppState` built on
first access and reused by every request afterwards --
`test_corpus_is_loaded_once_not_per_request` in `tests/test_api.py` proves it.

Deterministic tracks are built once too, for every profile, at the same time
as the corpus (`main.build_all_tracks(..., client=None)`): the model is never
called at startup. Reranking all 323 profiles just to answer a request about
one of them is exactly the mistake that got an earlier run rate-limited (see
`main.track_one`). The model is only ever invoked for the one profile
actually requested, inside `AppState.track_for`.
"""
from __future__ import annotations

import sys
from dataclasses import dataclass, field
from pathlib import Path

from agents.reranker import rerank
from catalog.catalog import Catalog
from catalog.interests import InterestIndex, load_interests
from core.config import AppConfig, load_config
from core.models import Profile, Track
from core.paths import CATALOG_DIR, OUTPUT_DIR, PRODUCT_ROOT
from engine.grades import PositionIndex
from engine.peers import PeerIndex
from engine.reasons import ReasonContext, facts_for, fill_reasons
from engine.track import build_electronic, build_track
from ingest.normalize import load_aliases
from feedback.preferences_store import PreferencesStore
from feedback.store import FeedbackStore
from llm.base import LLMClient, LLMUnavailable
from llm.factory import build_client
import main as main_module
from main import build_all_tracks, load_everything
from quality.report import DataQualityReport


@dataclass
class AppState:
    profiles: dict[str, Profile]
    catalog: Catalog
    report: DataQualityReport
    config: AppConfig
    tracks: dict[str, Track]
    feedback: FeedbackStore
    client: LLMClient | None
    preferences: PreferencesStore
    interests: InterestIndex
    # Индексы, которыми собран корпус треков. Раньше они жили внутри
    # `build_all_tracks` и умирали вместе с вызовом; теперь нужны здесь, чтобы
    # пересобрать трек одного человека, ответившего на мастер, не трогая
    # остальные 322.
    peer_index: PeerIndex
    position_index: PositionIndex
    # Момент последней отметки, который уже учтён в собранном треке -- по
    # человеку. Пусто на старте: треки корпуса собираются без отметок, и
    # отметка, оставленная вчера, честно считается неучтённой, пока человек
    # не нажмёт «Обновить рекомендации».
    applied_feedback: dict[str, str | None] = field(default_factory=dict)
    # Паспорт данных: что и как прочитано из пакета выгрузок (экран «Данные»).
    passport: object | None = None

    def rebuild_for(self, user_id: str) -> None:
        """Пересобрать детерминированный трек одного человека.

        Вызывается после сохранения предпочтений и после «Обновить
        рекомендации». Пересобирается **ровно один** профиль: показ трека,
        который пересобирал корпус, однажды уже вызвал модель 323 раза за один
        запрос и выжег лимит ключа (фаза 4, дефект 2).

        Неизвестный `user_id` -- молча ничего: маршрут уже ответил 404, падать
        второй раз незачем.
        """
        profile = self.profiles.get(user_id)
        if profile is None:
            return
        self.tracks[user_id] = build_track(
            profile,
            self.catalog,
            self.peer_index,
            self.position_index,
            self.config,
            preferences=self.preferences.get(user_id),
            interests=self.interests,
            feedback=self.feedback.for_user(user_id),
        )
        self.applied_feedback[user_id] = self.feedback.latest_change(user_id)

    def feedback_pending(self, user_id: str) -> bool:
        """Есть ли отметки, которых собранная подборка ещё не видела.

        Ровно это включает кнопку «Обновить рекомендации». Сравниваются
        моменты, а не количество отметок: человек может переставить отметку с
        «хочу» на «неактуально», не меняя их числа, и подборка обязана
        считаться устаревшей.
        """
        latest = self.feedback.latest_change(user_id)
        return latest is not None and self.applied_feedback.get(user_id) != latest

    def reason_context(self, user_id: str) -> ReasonContext | None:
        """Всё, что нужно, чтобы сформулировать «почему рекомендовано» этому человеку."""
        profile = self.profiles.get(user_id)
        if profile is None:
            return None
        return ReasonContext.build(
            profile, self.catalog, self.peer_index, self.position_index, self.config,
            self.preferences.get(user_id), self.interests,
        )

    def recommendations_for(self, user_id: str) -> tuple[Track, ReasonContext] | None:
        """Подборка человека с готовыми предложениями-обоснованиями и их контекст.

        Отметки применяются здесь же, без отдельной кнопки: если после сборки
        подборки человек что-то отметил, она пересобирается перед ответом --
        отмеченная программа ушла, на её место встала следующая. Раньше
        пересборка ждала нажатия «Обновить рекомендации», и человек, отметивший
        все карточки, видел прежний экран.

        К модели уходит только этот один профиль (см. docstring модуля).
        Недоступная модель -- не ошибка: `agents.reranker.rerank` отдаёт
        детерминированный порядок. Предложение, которое модель не смогла
        обосновать фактами, заменяется шаблоном.
        """
        if self.feedback_pending(user_id):
            self.rebuild_for(user_id)
        track = self.tracks.get(user_id)
        ctx = self.reason_context(user_id)
        if track is None or ctx is None:
            return None
        facts = None
        if self.client is not None:
            facts = {}
            for rec in track.recommendations:
                course = self.catalog.get(rec.course_key)
                if course is not None:
                    facts[rec.course_key] = [f.text for f in facts_for(course, ctx)][:4]
        track = rerank(track, self.client, self.config, facts=facts)
        track = track.model_copy(
            update={"recommendations": fill_reasons(track.recommendations, ctx)}
        )
        return track, ctx

    def track_for(self, user_id: str) -> Track | None:
        """The reasoned track for one profile, reranked on demand."""
        found = self.recommendations_for(user_id)
        return found[0] if found else None

    def electronic_for(
        self, user_id: str, ctx: ReasonContext, exclude: set[str]
    ) -> list:
        """Очередь персональных электронных курсов с предложениями-обоснованиями."""
        profile = self.profiles.get(user_id)
        if profile is None:
            return []
        recs = build_electronic(
            profile, self.catalog, self.peer_index, self.position_index, self.config,
            preferences=ctx.preferences, interests=self.interests,
            exclude=exclude, feedback=self.feedback.for_user(user_id), ctx=ctx,
        )
        return fill_reasons(recs, ctx)


def _build_client(config: AppConfig) -> LLMClient | None:
    """Build the configured client, or None -- never raise into a request path."""
    if not config.llm_enabled:
        return None
    try:
        return build_client(config)
    except LLMUnavailable as exc:
        print(
            f"Модель недоступна ({exc}); API отдаёт треки без переранжирования.",
            file=sys.stderr,
        )
        return None


def _migrate_renamed_courses(catalog: Catalog, config: AppConfig, feedback: FeedbackStore, preferences: PreferencesStore) -> None:
    """Перенести сохранённые отметки и ответы мастера на новые ключи переименованных программ.

    Синоним в `aliases.yaml` («Интенсивпо ...» -> «Интенсив по ...») говорит, что программа раньше
    называлась иначе. Если такого ключа в каталоге уже нет, а новый есть, то всё, что человек успел
    отметить по старому ключу, переезжает на новый -- и не теряется, и не оживает как «чужая» отметка.
    """
    aliases = load_aliases(CATALOG_DIR / config.aliases_file)
    keys = catalog.keys()
    mapping = {
        source: target
        for source, target in aliases.items()
        if target and source != target and target in keys and source not in keys
    }
    if mapping:
        feedback.rekey_course_keys(mapping)
        preferences.rekey_course_keys(mapping)


_state: AppState | None = None


def _build_state(
    config: AppConfig | None,
    feedback_db: str | Path | None,
    preferences_db: str | Path | None = None,
) -> AppState:
    profiles, catalog, report = load_everything()
    passport = main_module.last_passport()
    cfg = config if config is not None else load_config(PRODUCT_ROOT / "config.yaml")

    preferences = PreferencesStore(
        preferences_db if preferences_db is not None else OUTPUT_DIR / "preferences.db"
    )
    interests = load_interests(CATALOG_DIR / "interests.yaml")
    position_index = PositionIndex.load(CATALOG_DIR / "positions.yaml")
    peer_index = PeerIndex.build(list(profiles.values()))
    feedback = FeedbackStore(feedback_db if feedback_db is not None else OUTPUT_DIR / "feedback.db")
    # Переименованные программы: сохранённые ключи переезжают ДО сборки треков,
    # иначе трек увидел бы ответы мастера под прежним ключом.
    _migrate_renamed_courses(catalog, cfg, feedback, preferences)

    # Ответы, данные вчера, действуют и сегодня: корпус собирается сразу с
    # сохранёнными предпочтениями, а не «по умолчанию, пока кто-нибудь не
    # откроет свой трек».
    tracks = build_all_tracks(
        profiles,
        catalog,
        cfg,
        client=None,
        preferences_by_user=preferences.all(),
        interests=interests,
    )
    client = _build_client(cfg)
    return AppState(
        profiles=profiles,
        catalog=catalog,
        report=report,
        config=cfg,
        tracks=tracks,
        feedback=feedback,
        client=client,
        preferences=preferences,
        interests=interests,
        peer_index=peer_index,
        position_index=position_index,
        passport=passport,
    )


def get_state(
    config: AppConfig | None = None,
    feedback_db: str | Path | None = None,
    preferences_db: str | Path | None = None,
) -> AppState:
    """Return the process-wide state, building it once on first call.

    `config`/`feedback_db` are only consulted the first time state is built
    in this process (or since the last `reset_state()`); once built, later
    calls -- including the ones routes make with no arguments -- return the
    very same object. Tests use the arguments to inject an LLM-disabled
    config and a throwaway feedback database before the app ever serves a
    request.
    """
    global _state
    if _state is None:
        _state = _build_state(config, feedback_db, preferences_db)
    return _state


def reset_state() -> None:
    """Test-only: forget the cached state so the next `get_state()` rebuilds."""
    global _state
    _state = None


def reload_state() -> AppState:
    """Re-read the data package in-process, keeping preferences, feedback and the model client.

    Used by the «Данные» screen after a new package is applied or reverted: no
    restart. Треки пересобираются целиком (детерминированно, без модели).
    """
    global _state
    if _state is None:
        return get_state()
    old = _state
    profiles, catalog, report = load_everything()
    passport = main_module.last_passport()
    position_index = PositionIndex.load(CATALOG_DIR / "positions.yaml")
    peer_index = PeerIndex.build(list(profiles.values()))
    _migrate_renamed_courses(catalog, old.config, old.feedback, old.preferences)
    tracks = build_all_tracks(
        profiles,
        catalog,
        old.config,
        client=None,
        preferences_by_user=old.preferences.all(),
        interests=old.interests,
    )
    _state = AppState(
        profiles=profiles,
        catalog=catalog,
        report=report,
        config=old.config,
        tracks=tracks,
        feedback=old.feedback,
        client=old.client,
        preferences=old.preferences,
        interests=old.interests,
        peer_index=peer_index,
        position_index=position_index,
        passport=passport,
    )
    return _state
