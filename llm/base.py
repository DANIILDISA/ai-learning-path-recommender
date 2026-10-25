"""Shared LLM client protocol.

Every group (russian / foreign / local) speaks this same interface, so the
rest of the product never branches on which group is active.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


class LLMUnavailable(RuntimeError):
    """Raised when a model cannot be reached or refuses to answer.

    Never fatal: the caller falls back to the deterministic track, because a
    missing API key must not stop a civil servant from seeing recommendations.
    """


@dataclass(frozen=True)
class LLMResult:
    text: str
    model: str
    prompt_tokens: int = 0
    completion_tokens: int = 0
    cached: bool = False


class LLMClient(Protocol):
    name: str

    def complete(self, system: str, user: str) -> LLMResult: ...
