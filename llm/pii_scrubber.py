from __future__ import annotations
import re

# Order matters: email first (avoids phone-regex false-triggering on digits inside
# an email local-part), then phone, then the FIO heuristic last (so an already-masked
# "[EMAIL]"/"[ТЕЛЕФОН]" placeholder can never itself look like a name).
# ASCII-only domain (won't catch Cyrillic/IDN domains like имя@почта.рф) — low real-world
# incidence since major Russian providers use Latin domains.
_EMAIL_RE = re.compile(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9.-]+")
# Prefix (+7/8/7) is optional so bare mobile numbers (e.g. "9161234567") are still caught;
# requiring the first digit to be 9 (all RU mobile operator codes start with 9) keeps this
# from degenerating into an overly broad "any 10 digits" matcher.
_PHONE_RE = re.compile(r"(?:(?:\+7|8|7)[\s\-]?)?\(?9\d{2}\)?[\s\-]?\d{3}[\s\-]?\d{2}[\s\-]?\d{2}")
# Best-effort: 2-3 consecutive Title-Case Cyrillic words (e.g. "Иванов Иван Иванович").
# Not a real NLP name detector — documented limitation, see design spec.
# Known false positives (accepted tradeoff): ordinary two-capitalized-word phrases like
# "Санкт Петербург" or "Старший Менеджер" also get masked. We accept this to avoid
# missing genuine names, which matters more for privacy than occasional over-masking.
_FIO_RE = re.compile(r"\b[А-ЯЁ][а-яё]+(?:[\s\-][А-ЯЁ][а-яё]+){1,2}\b")

_PATTERNS = (
    ("EMAIL", _EMAIL_RE),
    ("ТЕЛЕФОН", _PHONE_RE),
    ("ФИО", _FIO_RE),
)


def scrub(text: str) -> tuple[str, list[str]]:
    """Mask email/phone/FIO-like patterns in text; return (masked_text, found_labels).
    found may contain duplicate labels if a category matches more than once."""
    if not text:
        return text, []
    found: list[str] = []
    result = text
    for label, pattern in _PATTERNS:
        if label == "EMAIL" and "@" not in result:
            continue
        def _replace(m, label=label):
            found.append(label)
            return f"[{label}]"
        result = pattern.sub(_replace, result)
    return result, found
