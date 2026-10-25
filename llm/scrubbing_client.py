from __future__ import annotations
import logging

from llm.base import LLMResult
from llm.pii_scrubber import scrub

_log = logging.getLogger(__name__)


class ScrubbingClient:
    """Wraps an LLMClient; masks PII-like patterns in system and user text before
    delegating. Outermost layer (around CachingClient) so the cache is keyed on
    the masked text and nothing raw is stored on disk either."""

    def __init__(self, inner) -> None:
        self._inner = inner

    @property
    def name(self) -> str:
        return self._inner.name

    def complete(self, system: str, user: str) -> LLMResult:
        masked_system, found_system = scrub(system)
        masked_user, found_user = scrub(user)
        found = found_system + found_user
        if found:
            _log.debug("PII redacted before LLM call: %s", found)
        return self._inner.complete(masked_system, masked_user)
