"""Build the active-group LLM client from config.

The active group is a string in config.yaml (`llm.model_group`), not a code
branch: foreign and local both resolve to the same `openai_compat` adapter,
picked here by the `client` field of the group's config entry.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from dotenv import load_dotenv

from core.paths import PRODUCT_ROOT
from llm.base import LLMClient

_ENV_LOADED = False


def load_env_once() -> None:
    """Read PRODUCT_ROOT/.env so API keys reach os.environ.

    The path is anchored to the product folder rather than the working
    directory: the customer runs the .bat from wherever the archive landed, and
    a relative ".env" would silently resolve to nothing and look exactly like a
    missing key.

    Public (not `_`-prefixed) because `diagnostics/checks.py` also needs this
    guarantee: it calls this once at import time so that later test/CLI code
    monkeypatching os.environ (to simulate a missing key) is not silently
    undone by a second, later `.env` reload -- this function only ever reads
    the file once per process, no matter who calls it or how many times.
    """
    global _ENV_LOADED
    if not _ENV_LOADED:
        load_dotenv(PRODUCT_ROOT / ".env")
        _ENV_LOADED = True

if TYPE_CHECKING:
    from core.config import AppConfig

_CLIENT_BUILDERS = {}


def _build_gigachat(config: "AppConfig", group) -> LLMClient:
    from llm.gigachat_client import GigaChatClient

    return GigaChatClient(
        model=group.model,
        api_key_env=group.api_key_env,
        scope_env=group.scope_env,
        verify_ssl=group.verify_ssl,
        max_tokens=group.max_tokens,
        temperature=config.temperature,
        timeout=config.timeout_seconds,
        base_url=group.base_url,
        fallback_base_urls=group.fallback_base_urls,
        fallback_models=group.fallback_models,
        retries=group.retries,
        backoff_seconds=group.retry_backoff_seconds,
    )


def _build_openai_compat(config: "AppConfig", group) -> LLMClient:
    from llm.openai_compat import OpenAICompatClient

    return OpenAICompatClient(
        base_url=group.base_url,
        model=group.model,
        api_key_env=group.api_key_env,
        max_tokens=group.max_tokens,
        temperature=config.temperature,
        timeout=config.timeout_seconds,
    )


_CLIENT_BUILDERS = {
    "gigachat": _build_gigachat,
    "openai_compat": _build_openai_compat,
}


def build_client(config: "AppConfig") -> LLMClient | None:
    """Return the client for `config.model_group`, or None if LLM use is off.

    Returning `None` (rather than raising) is deliberate: callers must fall
    back to the deterministic Phase-3 track when there is no client, exactly
    as they do when a configured client raises `LLMUnavailable`.
    """
    if not config.llm_enabled:
        return None
    load_env_once()
    group = config.llm_groups.get(config.model_group)
    if group is None:
        return None
    builder = _CLIENT_BUILDERS.get(group.client)
    if builder is None:
        return None
    client = builder(config, group)

    # Wrapping here rather than at each call site is what makes the cache
    # actually take effect: config.cache_enabled was being honoured by
    # CachingClient and ignored by everyone who built a client, so identical
    # requests kept going to the network. The cache is what makes repeated
    # runs reproducible and keeps the token budget from being spent twice on
    # the same question.
    from llm.cache import CachingClient
    from core.paths import OUTPUT_DIR

    from llm.scrubbing_client import ScrubbingClient

    # Scrubbing is the outermost layer: every call site gets it for free, and
    # the cache key is built from the already-masked text.
    return ScrubbingClient(CachingClient(
        client,
        cache_dir=OUTPUT_DIR / "llm_cache",
        temperature=config.temperature,
        enabled=config.cache_enabled,
    ))
