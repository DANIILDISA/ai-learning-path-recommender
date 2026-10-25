"""On-disk cache wrapping any LLMClient.

Needed three times over: reproducibility (an acceptance criterion -- reruns
must return the same recommendations), budget (OpenRouter <=100 rub, GigaChat
Lite <=300k tokens) and speed across 323 profiles. Keyed on disk, not in
memory, so a second process (a rerun tomorrow) still hits the cache.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from llm.base import LLMClient, LLMResult


class CachingClient:
    """Wraps an LLMClient; caches responses on disk keyed by request content."""

    def __init__(
        self,
        inner: LLMClient,
        cache_dir: str | Path,
        temperature: float = 0.0,
        enabled: bool = True,
    ) -> None:
        self._inner = inner
        self._dir = Path(cache_dir)
        self._temperature = temperature
        self._enabled = enabled
        self.name = inner.name
        if self._enabled:
            self._dir.mkdir(parents=True, exist_ok=True)

    def _key_path(self, system: str, user: str) -> Path:
        raw = f"{self._inner.name}\x00{system}\x00{user}\x00{self._temperature}"
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
        return self._dir / f"{digest}.json"

    def complete(self, system: str, user: str) -> LLMResult:
        if not self._enabled:
            return self._inner.complete(system, user)

        path = self._key_path(system, user)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            return LLMResult(
                text=data["text"],
                model=data["model"],
                prompt_tokens=data.get("prompt_tokens", 0),
                completion_tokens=data.get("completion_tokens", 0),
                cached=True,
            )

        result = self._inner.complete(system, user)
        path.write_text(
            json.dumps(
                {
                    "text": result.text,
                    "model": result.model,
                    "prompt_tokens": result.prompt_tokens,
                    "completion_tokens": result.completion_tokens,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return result
