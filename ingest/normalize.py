"""Text normalisation for comparing customer-supplied strings.

Normalised forms exist only for matching -- never display them to a user.
"""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from core.paths import nfc

_NON_WORD = re.compile(r"[^\w\s]", flags=re.UNICODE)
_WHITESPACE = re.compile(r"\s+")
_UNDERSCORE = re.compile(r"_+")

# Latin letters that are visually identical to Cyrillic ones. The customer's
# registry really does contain them: "…теория и практикa" ends in a Latin "a"
# (U+0061), indistinguishable on screen from Cyrillic "а" (U+0430). Without
# folding, that course can never match its own history rows, so someone who
# completed it would be recommended it again. Folding runs after casefold(),
# so only lowercase forms need mapping.
# Only letters whose lowercase glyphs are genuinely indistinguishable are
# folded. Pairs like k/к or m/м render differently and folding them would risk
# merging distinct titles.
_HOMOGLYPHS = str.maketrans({
    "a": "а", "c": "с", "e": "е", "o": "о", "p": "р", "x": "х", "y": "у",
})


def is_blank(value: Any) -> bool:
    """True when the value carries no content."""
    return value is None or str(value).strip() == ""


def normalize_text(value: Any) -> str:
    """Fold a string to a comparable form.

    Composes Unicode, lowercases, collapses й/и and ё/е, strips punctuation to
    spaces and squeezes runs of whitespace. Folding й→и is deliberate: the
    customer's own files mix the composed and decomposed spellings, and folding
    removes that whole class of mismatch.
    """
    if is_blank(value):
        return ""
    text = nfc(str(value)).casefold()
    text = text.replace("ё", "е").replace("й", "и")
    text = text.translate(_HOMOGLYPHS)
    text = _NON_WORD.sub(" ", text)
    text = _UNDERSCORE.sub(" ", text)
    return _WHITESPACE.sub(" ", text).strip()


def load_aliases(path: str | Path) -> dict[str, str | None]:
    """Load the history-title -> catalogue-title dictionary.

    Both sides are stored normalised so lookups need no further work. A value
    of None marks a course that has no catalogue entry at all (retired from the
    programme line-up) -- it still gets a stable key so history stays countable.
    """
    raw = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    mapping: dict[str, str | None] = {}
    for source, target in (raw.get("aliases") or {}).items():
        mapping[normalize_text(source)] = normalize_text(target) if target else None
    return mapping


def course_key(title: Any, aliases: dict[str, str | None]) -> str:
    """Canonical identity of a course, independent of its delivery format.

    ППК and ЭК are two ways to deliver the same material, so the key is derived
    from the title alone. Feeding the delivery type into the key would let an
    already-passed course reappear under the other label -- the exact failure
    the acceptance criteria punish hardest.
    """
    normalized = normalize_text(title)
    if not normalized:
        return ""
    target = aliases.get(normalized)
    return target if target else normalized
