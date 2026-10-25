"""Run a sample of profiles through one model group and record what happened.

`agents.reranker.rerank` is deliberately generous to the user: any model
failure at all -- no key, no network, malformed JSON, an invented course --
is swallowed and the deterministic track is shown instead. That is correct
for a civil servant looking at their track, and wrong for a benchmark, which
exists precisely to see those failures. `_Instrumented` sits between the
runner and the real client so every call is timed and inspected before
`rerank` ever gets a chance to hide it.

Nothing here calls a real network client directly: `run_group` takes a
`client_factory(group) -> LLMClient` so tests can hand it fakes, and
`build_benchmark_client` is the one production implementation of that
factory, used by `main.py`.
"""
from __future__ import annotations

import json
import random
import time
from dataclasses import dataclass, field
from typing import Callable

from agents.reranker import _parse, rerank
from catalog.catalog import Catalog
from core.config import AppConfig
from core.models import Profile
from engine.grades import PositionIndex
from engine.peers import PeerIndex
from engine.track import build_track
from llm.base import LLMClient, LLMResult, LLMUnavailable


def sample_user_ids(profiles: dict[str, Profile], size: int, seed: int) -> list[str]:
    """A fixed, seed-derived slice of user ids.

    Sorted before sampling so the draw depends only on `seed`, never on a
    plain dict's iteration order -- which itself follows insertion order,
    i.e. the order the export happened to be read in. Sorted again after,
    so two runs with the same seed produce not just the same set but the
    same displayed order.
    """
    pool = sorted(profiles)
    size = min(size, len(pool))
    return sorted(random.Random(seed).sample(pool, size))


@dataclass(frozen=True)
class ProfileRun:
    """What happened for one (profile, repeat) inside one group."""

    user_id: str
    repeat: int
    latency_seconds: float
    prompt_tokens: int
    completion_tokens: int
    valid_json: bool
    # Whether the model's own claimed order named only courses actually
    # offered in the pool. Independent of `valid_json`: a syntactically
    # valid answer can still invent a course; `rerank` would drop it before
    # the user ever sees it, but the benchmark records the attempt.
    within_pool: bool
    order: tuple[str, ...]  # final course_key order actually shown to the user


@dataclass
class GroupResult:
    """Everything measured for one model group, or why nothing was.

    `status="not_measured"` (never zero-filled runs) is how an unreachable
    group -- no server, no key, no budget -- is told apart from a group that
    really did answer in 0 seconds for 0 tokens, which never happens and
    would be a lie.
    """

    group: str
    model: str | None
    status: str  # "measured" | "not_measured"
    reason: str = ""
    runs: list[ProfileRun] = field(default_factory=list)

    @property
    def measured(self) -> bool:
        return self.status == "measured"


class _Instrumented:
    """Wraps a real `LLMClient` so the runner can see latency, token counts
    and answer validity directly -- see the module docstring for why this
    cannot be observed from outside `rerank`.
    """

    def __init__(self, inner: LLMClient) -> None:
        self._inner = inner
        self.name = inner.name
        self.called = False
        self.error: LLMUnavailable | None = None
        self.latency_seconds = 0.0
        self.prompt_tokens = 0
        self.completion_tokens = 0
        self.valid_json = False
        self.order: tuple[str, ...] = ()

    def complete(self, system: str, user: str) -> LLMResult:
        self.called = True
        self.error = None
        start = time.perf_counter()
        try:
            result = self._inner.complete(system, user)
        except LLMUnavailable as exc:
            self.latency_seconds = time.perf_counter() - start
            self.prompt_tokens = 0
            self.completion_tokens = 0
            self.valid_json = False
            self.order = ()
            self.error = exc
            raise
        self.latency_seconds = time.perf_counter() - start
        self.prompt_tokens = result.prompt_tokens
        self.completion_tokens = result.completion_tokens
        try:
            order, _reasons = _parse(result.text)
            self.valid_json = True
            self.order = tuple(order)
        except (ValueError, json.JSONDecodeError):
            self.valid_json = False
            self.order = ()
        return result


def build_benchmark_client(config: AppConfig, group: str) -> LLMClient | None:
    """The group's normal client, with the on-disk cache forced off.

    A benchmark's entire point is to measure latency and token spend; a
    second repeat served from `llm.cache.CachingClient`'s on-disk cache
    would report ~0s and 0 tokens, making a group look impossibly -- and
    dishonestly -- fast. Every other setting (model, temperature, timeout,
    ...) is inherited unchanged from `config`.
    """
    from llm.factory import build_client

    scoped = config.model_copy(update={"model_group": group, "cache_enabled": False})
    return build_client(scoped)


def run_group(
    group: str,
    sample: list[str],
    profiles: dict[str, Profile],
    catalog: Catalog,
    config: AppConfig,
    position_index: PositionIndex,
    peer_index: PeerIndex,
    client_factory: Callable[[str], LLMClient | None],
    repeats: int,
) -> GroupResult:
    """Run `sample` through `group`, `repeats` times each.

    Any `LLMUnavailable` -- missing key, refused connection, whatever --
    marks the whole group `not_measured` immediately and discards any runs
    already collected this call: a group we could not fully reach gets a
    reason, never a partial measurement dressed up as a complete one.
    """
    try:
        client = client_factory(group)
    except LLMUnavailable as exc:
        return GroupResult(group=group, model=None, status="not_measured", reason=str(exc))
    if client is None:
        return GroupResult(
            group=group, model=None, status="not_measured",
            reason=f"группа «{group}» не настроена или отключена",
        )

    wrapped = _Instrumented(client)
    runs: list[ProfileRun] = []
    for repeat in range(repeats):
        for user_id in sample:
            profile = profiles[user_id]
            track = build_track(profile, catalog, peer_index, position_index, config)
            pool_keys = {rec.course_key for rec in track.recommendations}

            wrapped.called = False
            reranked = rerank(track, wrapped, config)

            if wrapped.error is not None:
                return GroupResult(
                    group=group, model=client.name, status="not_measured",
                    reason=str(wrapped.error),
                )
            if not wrapped.called:
                continue  # empty track: rerank never calls the model

            raw_keys = set(wrapped.order) if wrapped.valid_json else set()
            runs.append(ProfileRun(
                user_id=user_id,
                repeat=repeat,
                latency_seconds=wrapped.latency_seconds,
                prompt_tokens=wrapped.prompt_tokens,
                completion_tokens=wrapped.completion_tokens,
                valid_json=wrapped.valid_json,
                within_pool=wrapped.valid_json and raw_keys <= pool_keys,
                order=tuple(rec.course_key for rec in reranked.recommendations),
            ))

    return GroupResult(group=group, model=client.name, status="measured", runs=runs)
