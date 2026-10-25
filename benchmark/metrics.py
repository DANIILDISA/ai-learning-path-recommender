"""Turn raw `benchmark.runner.ProfileRun` records into comparable numbers.

Nothing here talks to a model or the disk -- it only aggregates what
`benchmark.runner.run_group` already collected, and picks the group
`benchmark/report.py` names in the "Сводка" sheet. The pick is computed from
whatever `GroupMetrics` it is handed, not written ahead of time -- see
`tests/test_benchmark_report.py::test_summary_verdict_is_computed_not_hardcoded`
for the proof that it moves when the numbers move.
"""
from __future__ import annotations

import statistics
from collections import defaultdict
from dataclasses import dataclass

from benchmark.runner import GroupResult, ProfileRun


@dataclass(frozen=True)
class GroupMetrics:
    """One row of the "Метрики" sheet."""

    group: str
    model: str | None
    status: str
    reason: str = ""
    calls: int = 0
    mean_latency: float | None = None
    median_latency: float | None = None
    total_prompt_tokens: int = 0
    total_completion_tokens: int = 0
    valid_ratio: float | None = None
    within_pool_ratio: float | None = None
    reproducibility: float | None = None


def reproducibility_of(runs: list[ProfileRun]) -> float | None:
    """Fraction of profiles whose recommendation order was identical across
    every repeat recorded for them.

    `None` when nothing is comparable -- fewer than two repeats recorded for
    any single profile.
    """
    by_user: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for run in runs:
        by_user[run.user_id].append(run.order)
    comparable = {uid: orders for uid, orders in by_user.items() if len(orders) >= 2}
    if not comparable:
        return None
    stable = sum(1 for orders in comparable.values() if len(set(orders)) == 1)
    return stable / len(comparable)


def summarize(result: GroupResult) -> GroupMetrics:
    """Turn one group's raw runs into the numbers a reader compares.

    A `not_measured` group (or one with no runs at all) gets a `GroupMetrics`
    whose numeric fields are all `None`/0 by dataclass default -- callers
    must check `.status`, not treat a `None` as "worse than zero".
    """
    if result.status != "measured" or not result.runs:
        return GroupMetrics(
            group=result.group, model=result.model, status=result.status, reason=result.reason,
        )
    latencies = [run.latency_seconds for run in result.runs]
    valid = [run.valid_json for run in result.runs]
    within = [run.within_pool for run in result.runs]
    return GroupMetrics(
        group=result.group,
        model=result.model,
        status=result.status,
        reason=result.reason,
        calls=len(result.runs),
        mean_latency=statistics.mean(latencies),
        median_latency=statistics.median(latencies),
        total_prompt_tokens=sum(run.prompt_tokens for run in result.runs),
        total_completion_tokens=sum(run.completion_tokens for run in result.runs),
        valid_ratio=sum(valid) / len(valid),
        within_pool_ratio=sum(within) / len(within),
        reproducibility=reproducibility_of(result.runs),
    )


def _last_orders(runs: list[ProfileRun]) -> dict[str, tuple[str, ...]]:
    """Each profile's most recently recorded order (highest `repeat`)."""
    latest: dict[str, tuple[int, tuple[str, ...]]] = {}
    for run in runs:
        seen = latest.get(run.user_id)
        if seen is None or run.repeat >= seen[0]:
            latest[run.user_id] = (run.repeat, run.order)
    return {uid: order for uid, (_repeat, order) in latest.items()}


def agreement_between(a: GroupResult, b: GroupResult) -> float | None:
    """Fraction of matching positions between two measured groups' final
    orders, over the profiles they both cover.

    `None` when either group was not measured, or they share no profile to
    compare -- never 0.0, which would misreport "measured and disagreed".
    """
    if a.status != "measured" or b.status != "measured":
        return None
    a_last = _last_orders(a.runs)
    b_last = _last_orders(b.runs)
    shared = set(a_last) & set(b_last)
    if not shared:
        return None
    matches = 0
    total = 0
    for uid in shared:
        oa, ob = a_last[uid], b_last[uid]
        for i in range(min(len(oa), len(ob))):
            total += 1
            if oa[i] == ob[i]:
                matches += 1
    return matches / total if total else None


def choose_group(metrics_by_group: dict[str, GroupMetrics]) -> tuple[str | None, str]:
    """Pick the best measured group and explain the pick in one sentence,
    using only the numbers already collected.

    Ranked by valid-JSON share first (a wrong answer is worse than a slow
    one), then by median latency, then by group name as a stable tie-break.
    Not-measured groups are never candidates.
    """
    measured = {
        name: metrics for name, metrics in metrics_by_group.items()
        if metrics.status == "measured" and metrics.valid_ratio is not None
    }
    if not measured:
        return None, "Ни одна группа моделей не была измерена; сравнение недоступно."

    def rank_key(item: tuple[str, GroupMetrics]) -> tuple[float, float, str]:
        name, metrics = item
        latency = metrics.median_latency if metrics.median_latency is not None else float("inf")
        return (-metrics.valid_ratio, latency, name)

    name, metrics = min(measured.items(), key=rank_key)

    latency_text = f"{metrics.median_latency:.2f} с" if metrics.median_latency is not None else "н/д"
    valid_pct = f"{metrics.valid_ratio * 100:.0f}%"
    repro_text = (
        f", воспроизводимость {metrics.reproducibility * 100:.0f}%"
        if metrics.reproducibility is not None else ""
    )
    text = (
        f"Выбрана группа «{name}» (модель {metrics.model or '—'}): "
        f"валидных ответов {valid_pct} ({metrics.calls} вызовов), "
        f"медианная задержка {latency_text}{repro_text}."
    )
    return name, text
