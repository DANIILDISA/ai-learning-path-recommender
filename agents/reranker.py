"""Let a language model reorder and phrase a track -- and nothing more.

The deterministic engine has already decided *what* a person is allowed to be
offered: nothing they completed, nothing their post makes them ineligible for.
That guarantee is the single highest-stakes acceptance criterion of the whole
product, so it is not delegated to a model. What a model is genuinely good at
is judging a sensible order and writing an invitation a human wants to accept.

So the contract here is narrow and enforced rather than requested: whatever the
model answers, the result is rebuilt from the pool it was given. Keys it
invented are dropped, keys it repeated collapse to one, keys it ignored are
appended in the engine's own order, and any failure at all -- no key, no
network, malformed JSON, an apology instead of an answer -- returns the
deterministic track untouched. A missing API key must never stop a civil
servant from seeing their recommendations.
"""
from __future__ import annotations

import json
import re
import sys
from difflib import SequenceMatcher

from core.config import AppConfig
from core.models import Recommendation, Track
from core.paths import PRODUCT_ROOT
from engine.reasons import validate_sentence
from ingest.normalize import normalize_text
from llm.base import LLMClient, LLMUnavailable

_PROMPT_PATH = PRODUCT_ROOT / "llm" / "prompts" / "rerank.md"

# Models habitually wrap JSON in ``` fences however plainly they are asked not to.
_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.DOTALL)


# Near-exact names the model mangled (case, punctuation, a dropped letter) are
# recovered; anything less similar than this is treated as invented.
FUZZY_THRESHOLD = 0.92


class _Resolver:
    """Map a name the model returned to a pool key: exact, normalised, fuzzy."""

    def __init__(self, track: Track) -> None:
        self._exact = {rec.course_key: rec.course_key for rec in track.recommendations}
        self._norm: dict[str, str] = {}
        for rec in track.recommendations:
            for text in (rec.course_key, rec.title):
                self._norm.setdefault(normalize_text(text), rec.course_key)

    def __call__(self, name: str) -> tuple[str | None, int]:
        if name in self._exact:
            return name, 0
        norm = normalize_text(name)
        if not norm:
            return None, 0
        if norm in self._norm:
            return self._norm[norm], 1
        best, best_ratio = None, 0.0
        for candidate, key in self._norm.items():
            ratio = SequenceMatcher(None, norm, candidate).ratio()
            if ratio > best_ratio:
                best, best_ratio = key, ratio
        if best is not None and best_ratio >= FUZZY_THRESHOLD:
            return best, 1
        return None, 0


def fact_explanation(rec: Recommendation) -> str:
    """A plain explanation built from the engine's own facts (no model)."""
    if rec.reasons:
        return f"Подобрано по данным: {rec.reasons[0].rstrip('.')}."
    return "Подобрано по вашей должности и истории обучения."


def load_prompt() -> str:
    """The system prompt, read from disk so it can be tuned without a release."""
    return _PROMPT_PATH.read_text(encoding="utf-8")


def _candidates_payload(track: Track, facts: dict[str, list[str]] | None = None) -> str:
    """Describe the pool to the model in the exact keys it must answer with.

    With `facts` (the ready-made, verifiable sentences of `engine.reasons`) the
    model is told what it may rely on and nothing else: engine-internal reason
    strings stay out of the payload, they are what made earlier explanations
    read like a log.
    """
    if facts is not None:
        return json.dumps(
            {
                "профиль": {
                    "должность": track.position,
                    "ИОГВ": track.iogv,
                    "группа_должностей": track.grade,
                },
                "курсы": [
                    {
                        "course_key": rec.course_key,
                        "название": rec.title,
                        "факты": facts.get(rec.course_key, []),
                    }
                    for rec in track.recommendations
                ],
            },
            ensure_ascii=False,
            indent=2,
        )
    return json.dumps(
        {
            "профиль": {
                "должность": track.position,
                "ИОГВ": track.iogv,
                "группа_должностей": track.grade,
            },
            "курсы": [
                {
                    "course_key": rec.course_key,
                    "название": rec.title,
                    "почему_подобран": rec.reasons,
                }
                for rec in track.recommendations
            ],
        },
        ensure_ascii=False,
        indent=2,
    )


def _parse(text: str) -> tuple[list[str], dict[str, str]]:
    """Pull (order, reasons) out of a model answer. Raises ValueError if absent."""
    stripped = text.strip()
    fenced = _FENCE.match(stripped)
    if fenced:
        stripped = fenced.group(1)
    data = json.loads(stripped)
    if not isinstance(data, dict):
        raise ValueError("model returned JSON that is not an object")
    order = data.get("order") or []
    reasons = data.get("reasons") or {}
    if not isinstance(order, list) or not isinstance(reasons, dict):
        raise ValueError("model returned 'order' or 'reasons' of the wrong shape")
    return [str(key) for key in order], {str(k): str(v) for k, v in reasons.items()}


def rerank(
    track: Track,
    client: LLMClient | None,
    config: AppConfig,
    facts: dict[str, list[str]] | None = None,
) -> Track:
    """Return `track` reordered and re-worded by the model, or unchanged.

    Never raises on account of the model: every failure path yields the
    deterministic track the engine produced.

    `facts` -- course_key -> the verifiable sentences the model may rely on
    (`engine.reasons.facts_for`). When given, the model's wording is accepted
    only if it passes `engine.reasons.validate_sentence`: one sentence, at most
    20 words, no number or quoted title that is not in the facts. A sentence
    that fails is dropped and the card gets the deterministic template; the
    order the model chose is kept either way.
    """
    if client is None or not track.recommendations:
        return track

    by_key = {rec.course_key: rec for rec in track.recommendations}

    prompt, payload = load_prompt(), _candidates_payload(track, facts)
    try:
        try:
            order, reasons = _parse(client.complete(prompt, payload).text)
        except (ValueError, json.JSONDecodeError):
            # Measured on the benchmark: GigaChat returns an empty body on
            # roughly a third of calls, which is not truncation -- the answers
            # that do arrive sit well under the token cap. One retry costs a
            # second and recovers most of them; a transport failure is not
            # retried, since that is a real outage rather than a hiccup.
            order, reasons = _parse(client.complete(prompt, payload).text)
    except (LLMUnavailable, ValueError, json.JSONDecodeError) as exc:
        # Falling back is correct, but doing it silently hides a dead API key
        # behind a track that looks fine. Say why, once, on stderr.
        print(
            f"{track.user_id}: модель не переупорядочила трек "
            f"({type(exc).__name__}: {exc}); показан детерминированный порядок.",
            file=sys.stderr,
        )
        return track

    resolve = _Resolver(track)
    invented: list[str] = []
    fuzzy = 0
    ordered: list[Recommendation] = []
    used: set[str] = set()
    reason_by_key: dict[str, str] = {}
    for raw_key, text in reasons.items():
        key, _ = resolve(raw_key)
        if key is not None and text.strip():
            reason_by_key.setdefault(key, text.strip())
    for raw in order:
        key, was_fuzzy = resolve(raw)
        if key is None:
            # invented -- the pool is the authority, not the answer
            invented.append(raw)
            continue
        if key in used:
            continue
        used.add(key)
        fuzzy += was_fuzzy
        recommendation = by_key[key]
        reason = reason_by_key.get(key, "")
        if reason and facts is not None:
            reason = validate_sentence(reason, facts.get(key, []), [recommendation.title]) or ""
        # The model's sentence goes first -- it is the invitation a person
        # actually reads. The computed reasons stay underneath it: "15% коллег
        # на должности «Начальник отдела» прошли этот курс (9 из 60)" is the
        # verifiable part, and the customer grades recommendations on having a
        # concrete justification. Replacing facts with prose would trade the
        # strongest evidence for the prettiest wording.
        ordered.append(
            recommendation.model_copy(
                update={
                    "reasons": [reason, *recommendation.reasons],
                    "explanation_source": "ai",
                    # Только при проверке по фактам: без неё предложение
                    # модели остаётся в `reasons`, как и прежде (CLI, отчёты).
                    "reason": reason if facts is not None else "",
                }
            )
            if reason
            else recommendation
        )

    # Anything the model stayed silent about keeps the engine's own order
    # (backfill from the deterministic ranking).
    leftovers = [rec for rec in track.recommendations if rec.course_key not in used]
    ordered.extend(leftovers)

    # Every card gets an explanation: where the model wrote none, build one
    # from facts and mark it so.
    final: list[Recommendation] = []
    fact_count = 0
    for rec in ordered:
        if rec.explanation_source != "ai":
            rec = rec.model_copy(
                update={
                    "reasons": [fact_explanation(rec), *rec.reasons],
                    "explanation_source": "facts",
                }
            )
            fact_count += 1
        final.append(rec)

    if invented:
        print(
            f"{track.user_id}: модель назвала {len(invented)} курс(ов) вне пула, "
            f"они отброшены: {invented}; добавлено из детерминированного порядка: {len(leftovers)}.",
            file=sys.stderr,
        )

    return track.model_copy(
        update={
            "recommendations": final,
            "llm_invented": len(invented),
            "llm_invented_names": invented[:10],
            "llm_fuzzy_matched": fuzzy,
            "llm_backfilled": len(leftovers),
            "llm_fact_explanations": fact_count,
        }
    )
