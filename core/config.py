"""Configuration loading.

Nothing about paths, thresholds or model choice belongs in Python source --
it all comes from config.yaml so the customer can retune without a developer.
"""
from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator

MODEL_GROUPS = {"russian", "foreign", "local"}


class LLMGroupConfig(BaseModel):
    """One entry under `llm.groups.<name>` in config.yaml.

    `client` names the adapter class to use ("gigachat" or "openai_compat"),
    never a code branch keyed on the group name itself -- foreign and local
    both resolve to "openai_compat".

    `extra="forbid"`: a misspelt key here (e.g. `verifiy_ssl`) must fail
    loudly rather than silently keeping the default and running with the
    wrong setting -- see D6 / phase-1 review finding #8.
    """

    model_config = ConfigDict(extra="forbid")

    client: str
    model: str
    api_key_env: str | None = None
    scope_env: str | None = None
    base_url: str | None = None
    verify_ssl: bool = True
    max_tokens: int = 1024
    # Только для GigaChat: запасной сервер и цепочка запасных моделей.
    # None -- значения по умолчанию клиента (старый сервер Сбера; Max -> Pro).
    fallback_base_urls: list[str] | None = None
    fallback_models: list[str] | None = None
    retries: int = 3
    retry_backoff_seconds: float = 1.0
    # Whether this group is available in the box-shipped product. The local
    # group is written but disabled by default (see phase-4 plan decisions).
    shipped: bool = True


class AppConfig(BaseModel):
    """The product's flat, validated configuration.

    `extra="forbid"` so a typo -- constructing `AppConfig` directly with an
    unknown keyword -- fails immediately instead of silently keeping a
    default (phase-1 review finding #8). `load_config` below additionally
    validates the *nested* YAML shape (`llm.model_gruop`, `track.weights.*`,
    ...) against `_RawConfig` before flattening it here, because those nested
    keys never reach this flat model directly.
    """

    model_config = ConfigDict(extra="forbid")

    history_prefix: str = "выгрузка"
    registry_prefix: str = "Реестр"
    history_sheet: str = "ГГС 2024-2025"
    aliases_file: str = "aliases.yaml"
    fuzzy_threshold: float = 0.95
    model_group: str = "russian"
    temperature: float = 0.0
    cache_enabled: bool = True
    llm_enabled: bool = True
    timeout_seconds: int = 60
    llm_groups: dict[str, LLMGroupConfig] = Field(default_factory=dict)
    max_recommendations: int = 10
    weight_navigator_track: float = 3.0
    weight_peer_completion: float = 2.0
    weight_same_iogv_bonus: float = 0.5
    weight_competency_gap: float = 1.5
    weight_unfinished_restart: float = 1.0
    weight_interest_match: float = 2.0
    weight_picked_course: float = 2.5
    weight_goal_match: float = 1.0
    weight_urgency_match: float = 1.0
    weight_feedback_want: float = 3.0
    weight_feedback_later: float = -2.0
    weight_topic_match: float = 1.0
    goal_competency_groups: dict[str, list[str]] = Field(default_factory=dict)
    quick_hours_max: int = 40
    max_per_competency_group: int = 3
    min_peer_cohort: int = 5
    min_peer_share: float = 0.05
    benchmark_sample_size: int = 10
    benchmark_sample_seed: int = 20260814
    benchmark_repeats: int = 2
    benchmark_groups: list[str] = Field(default_factory=lambda: ["russian", "foreign", "local"])

    @field_validator("model_group")
    @classmethod
    def _known_group(cls, value: str) -> str:
        if value not in MODEL_GROUPS:
            raise ValueError(f"Unknown model_group {value!r}, expected one of {sorted(MODEL_GROUPS)}")
        return value


class _DataSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    history_prefix: str = "выгрузка"
    registry_prefix: str = "Реестр"
    history_sheet: str = "ГГС 2024-2025"


class _CatalogSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    aliases_file: str = "aliases.yaml"
    # Порог нечёткой связи названий из истории с каталогом (0..1).
    fuzzy_threshold: float = 0.95


class _WeightsSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    navigator_track: float = 3.0
    peer_completion: float = 2.0
    same_iogv_bonus: float = 0.5
    competency_gap: float = 1.5
    unfinished_restart: float = 1.0
    interest_match: float = 2.0
    picked_course: float = 2.5
    goal_match: float = 1.0
    urgency_match: float = 1.0
    feedback_want: float = 3.0
    feedback_later: float = -2.0
    topic_match: float = 1.0


class _PreferencesSection(BaseModel):
    """`track.preferences:` -- как ответы мастера превращаются в веса.

    `goal_competency_groups` живёт в конфиге, а не в коде, потому что это
    наше допущение, а не данные заказчика: связи «карьерный рост -> такие-то
    группы компетенций» он нам не давал. Допущение, которое нельзя прочитать,
    невозможно оспорить.
    """

    model_config = ConfigDict(extra="forbid")

    goal_competency_groups: dict[str, list[str]] = Field(default_factory=dict)
    quick_hours_max: int = 40


class _TrackSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_recommendations: int = 10
    weights: _WeightsSection = Field(default_factory=_WeightsSection)
    preferences: _PreferencesSection = Field(default_factory=_PreferencesSection)
    max_per_competency_group: int = 3
    min_peer_cohort: int = 5
    min_peer_share: float = 0.05


class _LLMSection(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model_group: str = "russian"
    temperature: float = 0.0
    cache_enabled: bool = True
    enabled: bool = True
    timeout_seconds: int = 60
    groups: dict[str, LLMGroupConfig] = Field(default_factory=dict)

    @field_validator("model_group")
    @classmethod
    def _known_group(cls, value: str) -> str:
        if value not in MODEL_GROUPS:
            raise ValueError(f"Unknown model_group {value!r}, expected one of {sorted(MODEL_GROUPS)}")
        return value


class _BenchmarkSection(BaseModel):
    """`benchmark:` in config.yaml -- drives Фаза 6 / Задача 2's comparison
    run. Every knob the benchmark needs (how many profiles, which seed, how
    many repeats, which groups) lives here, not hardcoded in `benchmark/`,
    so the run is retunable without a code change and reproducible from the
    seed alone."""

    model_config = ConfigDict(extra="forbid")

    sample_size: int = 10
    sample_seed: int = 20260814
    repeats: int = 2
    groups: list[str] = Field(default_factory=lambda: ["russian", "foreign", "local"])

    @field_validator("groups")
    @classmethod
    def _known_groups(cls, value: list[str]) -> list[str]:
        unknown = sorted(set(value) - MODEL_GROUPS)
        if unknown:
            raise ValueError(
                f"Unknown benchmark group(s) {unknown}, expected one of {sorted(MODEL_GROUPS)}"
            )
        return value


class _RawConfig(BaseModel):
    """The shape of config.yaml itself, validated before it is flattened
    into `AppConfig`. `extra="forbid"` at every level is what turns a typo
    like `llm.model_gruop` or `track.weights.navigator_trak` into a loud
    failure naming the bad key, instead of a silently-ignored setting that
    leaves the product running with a default nobody chose."""

    model_config = ConfigDict(extra="forbid")

    data: _DataSection = Field(default_factory=_DataSection)
    catalog: _CatalogSection = Field(default_factory=_CatalogSection)
    llm: _LLMSection = Field(default_factory=_LLMSection)
    track: _TrackSection = Field(default_factory=_TrackSection)
    benchmark: _BenchmarkSection = Field(default_factory=_BenchmarkSection)


def load_config(path: str | Path) -> AppConfig:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    raw = _RawConfig(**data)
    return AppConfig(
        history_prefix=raw.data.history_prefix,
        registry_prefix=raw.data.registry_prefix,
        history_sheet=raw.data.history_sheet,
        aliases_file=raw.catalog.aliases_file,
        fuzzy_threshold=raw.catalog.fuzzy_threshold,
        model_group=raw.llm.model_group,
        temperature=raw.llm.temperature,
        cache_enabled=raw.llm.cache_enabled,
        llm_enabled=raw.llm.enabled,
        timeout_seconds=raw.llm.timeout_seconds,
        llm_groups=raw.llm.groups,
        max_recommendations=raw.track.max_recommendations,
        weight_navigator_track=raw.track.weights.navigator_track,
        weight_peer_completion=raw.track.weights.peer_completion,
        weight_same_iogv_bonus=raw.track.weights.same_iogv_bonus,
        weight_competency_gap=raw.track.weights.competency_gap,
        weight_unfinished_restart=raw.track.weights.unfinished_restart,
        weight_interest_match=raw.track.weights.interest_match,
        weight_picked_course=raw.track.weights.picked_course,
        weight_goal_match=raw.track.weights.goal_match,
        weight_urgency_match=raw.track.weights.urgency_match,
        weight_feedback_want=raw.track.weights.feedback_want,
        weight_feedback_later=raw.track.weights.feedback_later,
        weight_topic_match=raw.track.weights.topic_match,
        goal_competency_groups=raw.track.preferences.goal_competency_groups,
        quick_hours_max=raw.track.preferences.quick_hours_max,
        max_per_competency_group=raw.track.max_per_competency_group,
        min_peer_cohort=raw.track.min_peer_cohort,
        min_peer_share=raw.track.min_peer_share,
        benchmark_sample_size=raw.benchmark.sample_size,
        benchmark_sample_seed=raw.benchmark.sample_seed,
        benchmark_repeats=raw.benchmark.repeats,
        benchmark_groups=raw.benchmark.groups,
    )
