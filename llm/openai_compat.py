"""OpenAI-compatible adapter shared by the foreign and local groups.

OpenRouter (foreign) and Ollama/LM Studio (local) both expose the same
`/v1/chat/completions` protocol, so this single class serves both -- only
`base_url`, `model` and whether a real key is needed differ, and that
difference lives entirely in `config.yaml`, not in a second class.

The transport can be injected (`transport=`) so tests never touch the
network: pass a fake object exposing `.chat.completions.create(...)`.
"""
from __future__ import annotations

import os

from openai import OpenAI

from llm.base import LLMResult, LLMUnavailable

# Ollama and LM Studio ignore the API key but the OpenAI SDK still requires a
# non-empty string to construct the client.
_NO_KEY_NEEDED = "not-needed"


class OpenAICompatClient:
    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        api_key_env: str | None,
        max_tokens: int,
        temperature: float,
        timeout: float,
        transport=None,
    ) -> None:
        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self.name = f"openai_compat:{model}"

        api_key = (os.environ.get(api_key_env, "") if api_key_env else "") or _NO_KEY_NEEDED
        if transport is not None:
            self._client = transport
        else:
            self._client = OpenAI(api_key=api_key, base_url=base_url, timeout=timeout)

    def complete(self, system: str, user: str) -> LLMResult:
        try:
            response = self._client.chat.completions.create(
                model=self._model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                max_tokens=self._max_tokens,
                temperature=self._temperature,
            )
        except Exception as exc:  # noqa: BLE001 -- any transport failure degrades, never crashes
            raise LLMUnavailable(f"{self.name}: {exc}") from exc

        choice = response.choices[0]
        text = (choice.message.content or "").strip()

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0

        return LLMResult(
            text=text,
            model=self._model,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )
