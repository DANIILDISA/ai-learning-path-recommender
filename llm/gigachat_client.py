"""GigaChat adapter (russian group), over the official `gigachat` SDK.

Model id, scope, sampling and TLS verification all come from `config.yaml`
via the caller (`llm/factory.py`) -- nothing here is hardcoded. A missing
credential raises `LLMUnavailable` at construction time rather than letting
the SDK fail later, so the caller degrades to the deterministic track the
same way it would for any other unavailable client.

Resilience (final-stage requirements):

* **Two servers.** The main server is `https://api.giga.chat/v1` (the only one
  that serves `GigaChat-3-Ultra`); the old `gigachat.devices.sberbank.ru` is the
  automatic spare when the main one cannot be reached. The old server also
  tends to drop connections (WinError 10054), so a network drop is retried
  with backoff before the spare is tried.
* **Model chain.** `GigaChat-3-Ultra` -> `GigaChat-3-Pro` -> `GigaChat-2-Pro`
  (configurable): when a model is unavailable or keeps failing, the next one
  answers instead of the whole track degrading.
* **Honesty about the model.** Every answer carries `response.model`; if the
  server answers with a model that does not start with the requested name, a
  warning is logged -- we must not silently believe we talked to Ultra.

GigaChat requires `temperature` strictly > 0; a configured `0.0` (our default,
for reproducibility) is omitted so the API does not reject the request.

The transport can be injected (`transport=` / `transport_factory=`) so tests
never touch the network: pass a fake object exposing `.chat(chat_obj)`.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable, Sequence
from typing import Any

from llm.base import LLMResult, LLMUnavailable

logger = logging.getLogger("llm.gigachat")

DEFAULT_BASE_URL = "https://api.giga.chat/v1"
FALLBACK_BASE_URL = "https://gigachat.devices.sberbank.ru/api/v1"

# Модели, которые продукт знает по имени. Первая в каждой строке -- то, что
# показывается человеку; значения -- ровно то, что уходит в API.
KNOWN_MODELS: dict[str, str] = {
    "Lite": "GigaChat-2",
    "Pro": "GigaChat-2-Pro",
    "Max": "GigaChat-2-Max",
    "Ultra": "GigaChat-3-Ultra",
    "3-Pro": "GigaChat-3-Pro",
    "3-Lightning": "GigaChat-3-Lightning",
}
DEFAULT_MODEL = "GigaChat-3-Ultra"
DEFAULT_FALLBACK_MODELS: tuple[str, ...] = ("GigaChat-3-Pro", "GigaChat-2-Pro")

_MAX_BACKOFF = 30.0


class _Fatal(Exception):
    """Authentication-type failure: no other server or model will help."""


def _classify(exc: BaseException) -> str:
    """'retry' (network/429/5xx), 'fatal' (bad credentials) or 'skip' (this
    model/server cannot answer: 404, 400, 422, ...)."""
    status = getattr(exc, "status_code", None)
    if isinstance(status, int):
        if status in (401, 403):
            return "fatal"
        if status == 429 or status >= 500:
            return "retry"
        return "skip"
    try:
        import httpx

        if isinstance(exc, httpx.TransportError):
            return "retry"
    except ImportError:  # pragma: no cover
        pass
    if isinstance(exc, (ConnectionError, TimeoutError, OSError)):
        return "retry"
    name = type(exc).__name__.lower()
    if any(word in name for word in ("connect", "timeout", "network", "ssl")):
        return "retry"
    return "skip"


class GigaChatClient:
    def __init__(
        self,
        *,
        model: str,
        api_key_env: str | None,
        scope_env: str | None,
        verify_ssl: bool,
        max_tokens: int,
        temperature: float,
        timeout: float,
        transport=None,
        transport_factory: Callable[[str], Any] | None = None,
        base_url: str | None = None,
        fallback_base_urls: Sequence[str] | None = None,
        fallback_models: Sequence[str] | None = None,
        retries: int = 3,
        backoff_seconds: float = 1.0,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        api_key = os.environ.get(api_key_env, "") if api_key_env else ""
        if not api_key:
            raise LLMUnavailable(
                f"GigaChat: пустой ключ авторизации (проверьте {api_key_env} в .env)"
            )

        self._model = model
        self._max_tokens = max_tokens
        self._temperature = temperature
        self._timeout = timeout
        self._verify_ssl = verify_ssl
        self._api_key = api_key
        self._scope = (os.environ.get(scope_env, "") if scope_env else "") or "GIGACHAT_API_PERS"
        self._retries = max(0, int(retries))
        self._backoff = max(0.0, float(backoff_seconds))
        self._sleep = sleep
        self.name = f"gigachat:{model}"

        primary_url = base_url or DEFAULT_BASE_URL
        if fallback_base_urls is None:
            fallback_base_urls = [FALLBACK_BASE_URL]
        self._base_urls = _dedupe([primary_url, *fallback_base_urls])

        chain = list(DEFAULT_FALLBACK_MODELS if fallback_models is None else fallback_models)
        self._models = _dedupe([model, *chain])

        if transport is not None and transport_factory is None:
            transport_factory = lambda _url: transport  # noqa: E731
        self._factory = transport_factory
        self._sdk: dict[str, Any] = {}

        # Диагностика последнего вызова -- для журнала и интерфейса.
        self.last_model: str | None = None
        self.last_base_url: str | None = None
        self.warnings: list[str] = []

        if self._factory is None:
            try:
                import gigachat  # noqa: F401
            except ImportError as exc:  # pragma: no cover - environment-dependent
                raise LLMUnavailable(
                    "Не установлен пакет gigachat. Установите его: pip install gigachat"
                ) from exc

    # ------------------------------------------------------------------ SDK
    def _client_for(self, base_url: str):
        client = self._sdk.get(base_url)
        if client is not None:
            return client
        if self._factory is not None:
            client = self._factory(base_url)
        else:
            from gigachat import GigaChat

            client = GigaChat(
                credentials=self._api_key,
                scope=self._scope,
                base_url=base_url,
                verify_ssl_certs=self._verify_ssl,
                timeout=self._timeout,
                max_retries=0,  # повторы делаем сами: с запасным сервером и цепочкой моделей
            )
        self._sdk[base_url] = client
        return client

    def _drop_client(self, base_url: str) -> None:
        """Forget a (possibly broken) connection so the next try opens a new one."""
        client = self._sdk.pop(base_url, None)
        close = getattr(client, "close", None)
        if callable(close) and self._factory is None:
            try:
                close()
            except Exception:  # noqa: BLE001
                pass

    # ------------------------------------------------------------- one call
    def _chat_once(self, base_url: str, model: str, system: str, user: str):
        from gigachat.models import Chat, Messages, MessagesRole

        kwargs: dict = {
            "model": model,
            "messages": [
                Messages(role=MessagesRole.SYSTEM, content=system),
                Messages(role=MessagesRole.USER, content=user),
            ],
            "max_tokens": self._max_tokens,
        }
        if self._temperature and self._temperature > 0:
            kwargs["temperature"] = self._temperature
        return self._client_for(base_url).chat(Chat(**kwargs))

    def _delay(self, attempt: int, exc: BaseException) -> float:
        retry_after = getattr(exc, "retry_after", None)
        if isinstance(retry_after, (int, float)) and retry_after > 0:
            return min(float(retry_after), _MAX_BACKOFF)
        return min(self._backoff * (2 ** attempt), _MAX_BACKOFF)

    def complete(self, system: str, user: str) -> LLMResult:
        failures: list[str] = []
        for model in self._models:
            for base_url in self._base_urls:
                for attempt in range(self._retries + 1):
                    try:
                        response = self._chat_once(base_url, model, system, user)
                    except Exception as exc:  # noqa: BLE001 -- any transport failure degrades, never crashes
                        kind = _classify(exc)
                        failures.append(f"{model}@{base_url}: {type(exc).__name__}")
                        if kind == "fatal":
                            raise LLMUnavailable(f"{self.name}: {exc}") from exc
                        self._drop_client(base_url)
                        if kind == "retry" and attempt < self._retries:
                            logger.warning(
                                "GigaChat %s (%s): %s -- повтор %d/%d",
                                model, base_url, type(exc).__name__, attempt + 1, self._retries,
                            )
                            self._sleep(self._delay(attempt, exc))
                            continue
                        break  # следующий сервер / следующая модель
                    else:
                        return self._to_result(response, model, base_url, failures)
        raise LLMUnavailable(
            f"{self.name}: все модели и серверы недоступны ({'; '.join(failures[-6:])})"
        )

    # -------------------------------------------------------------- results
    def _to_result(self, response, requested: str, base_url: str, failures: list[str]) -> LLMResult:
        choice = response.choices[0]
        text = (choice.message.content or "").strip()

        usage = getattr(response, "usage", None)
        prompt_tokens = getattr(usage, "prompt_tokens", 0) or 0
        completion_tokens = getattr(usage, "completion_tokens", 0) or 0

        actual = getattr(response, "model", None)
        actual = actual if isinstance(actual, str) and actual else None
        self.last_model = actual or requested
        self.last_base_url = base_url
        if actual:
            logger.info("GigaChat: запрошена %s, ответила %s (%s)", requested, actual, base_url)
            if not actual.casefold().startswith(requested.casefold()):
                message = (
                    f"GigaChat: запрошена модель «{requested}», а сервер ответил моделью «{actual}»"
                )
                logger.warning(message)
                self.warnings.append(message)
        if requested != self._model:
            message = (
                f"GigaChat: основная модель «{self._model}» недоступна, ответила запасная «{requested}»"
            )
            logger.warning(message)
            self.warnings.append(message)
        if base_url != self._base_urls[0]:
            message = f"GigaChat: основной сервер недоступен, ответил запасной {base_url}"
            logger.warning(message)
            self.warnings.append(message)
        del self.warnings[:-20]

        return LLMResult(
            text=text,
            model=actual or requested,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
        )


def _dedupe(items: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for item in items:
        if item and item not in seen:
            seen.append(item)
    return seen
