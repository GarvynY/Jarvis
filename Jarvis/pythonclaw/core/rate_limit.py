"""Provider-scoped external API limiting and retry helpers.

This module is intentionally small and dependency-free.  It gives current
single-process deployments a shared concurrency guard, while keeping the call
site shape compatible with a future Redis-backed distributed limiter.
"""

from __future__ import annotations

import asyncio
import email.utils
import logging
import random
import threading
import time
from collections.abc import Awaitable, Callable
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, TypeVar
from urllib.error import HTTPError, URLError

logger = logging.getLogger(__name__)

T = TypeVar("T")

_RETRYABLE_STATUS_CODES = {408, 429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403}


@dataclass(frozen=True)
class ProviderRateLimitConfig:
    """Runtime policy for one external API provider."""

    concurrency: int = 3
    max_retries: int = 3
    base_delay: float = 1.0
    max_delay: float = 30.0
    jitter: float = 0.25


_DEFAULT_LIMITS: dict[str, ProviderRateLimitConfig] = {
    "deepseek": ProviderRateLimitConfig(concurrency=3, max_retries=4, base_delay=1.0, max_delay=30.0),
    "openai": ProviderRateLimitConfig(concurrency=3, max_retries=4, base_delay=1.0, max_delay=30.0),
    "openai_compatible": ProviderRateLimitConfig(concurrency=3, max_retries=4, base_delay=1.0, max_delay=30.0),
    "anthropic": ProviderRateLimitConfig(concurrency=3, max_retries=3, base_delay=1.0, max_delay=30.0),
    "gemini": ProviderRateLimitConfig(concurrency=3, max_retries=3, base_delay=1.0, max_delay=30.0),
    "tavily": ProviderRateLimitConfig(concurrency=2, max_retries=3, base_delay=1.0, max_delay=20.0),
    "google_news": ProviderRateLimitConfig(concurrency=2, max_retries=2, base_delay=1.0, max_delay=20.0),
    "telegram": ProviderRateLimitConfig(concurrency=5, max_retries=3, base_delay=0.5, max_delay=10.0),
    "fx_data": ProviderRateLimitConfig(concurrency=4, max_retries=2, base_delay=0.5, max_delay=10.0),
    "image_fetch": ProviderRateLimitConfig(concurrency=4, max_retries=2, base_delay=0.5, max_delay=10.0),
    "generic": ProviderRateLimitConfig(concurrency=4, max_retries=2, base_delay=1.0, max_delay=20.0),
}

_sync_lock = threading.Lock()
_sync_semaphores: dict[tuple[str, int], threading.BoundedSemaphore] = {}
_async_semaphores: dict[tuple[int, str, int], asyncio.Semaphore] = {}


def get_rate_limit_config(provider: str) -> ProviderRateLimitConfig:
    """Return provider policy, allowing ``pythonclaw.json`` overrides."""

    key = _normalise_provider(provider)
    base = _DEFAULT_LIMITS.get(key, _DEFAULT_LIMITS["generic"])
    try:
        from pythonclaw import config

        raw = config.get("rateLimits", key, default={})
        if not isinstance(raw, dict):
            return base
        return ProviderRateLimitConfig(
            concurrency=_positive_int(raw.get("concurrency"), base.concurrency),
            max_retries=_non_negative_int(raw.get("maxRetries", raw.get("max_retries")), base.max_retries),
            base_delay=_positive_float(raw.get("baseDelay", raw.get("base_delay")), base.base_delay),
            max_delay=_positive_float(raw.get("maxDelay", raw.get("max_delay")), base.max_delay),
            jitter=_non_negative_float(raw.get("jitter"), base.jitter),
        )
    except Exception:  # noqa: BLE001 - config loading must not break API calls.
        return base


def call_with_backoff(
    provider: str,
    func: Callable[..., T],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run a blocking external call under provider concurrency and retry policy."""

    provider_key = _normalise_provider(provider)
    cfg = get_rate_limit_config(provider_key)
    last_exc: BaseException | None = None

    for attempt in range(cfg.max_retries + 1):
        try:
            with _sync_acquire(provider_key, cfg):
                return func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= cfg.max_retries or not is_retryable_error(exc):
                raise
            sleep_seconds = _retry_delay_seconds(exc, attempt, cfg)
            _log_retry(provider_key, attempt + 1, sleep_seconds, exc)
            time.sleep(sleep_seconds)

    assert last_exc is not None
    raise last_exc


@contextmanager
def rate_limit_context(provider: str):
    """Hold a provider concurrency slot for a long-lived operation.

    Streaming responses keep the HTTP request open while chunks are consumed, so
    the slot must remain held for the full stream lifetime.
    """

    provider_key = _normalise_provider(provider)
    cfg = get_rate_limit_config(provider_key)
    with _sync_acquire(provider_key, cfg):
        yield


async def async_call_with_backoff(
    provider: str,
    func: Callable[..., Awaitable[T]],
    *args: Any,
    **kwargs: Any,
) -> T:
    """Run an async external call under provider concurrency and retry policy."""

    provider_key = _normalise_provider(provider)
    cfg = get_rate_limit_config(provider_key)
    last_exc: BaseException | None = None

    for attempt in range(cfg.max_retries + 1):
        try:
            semaphore = _async_semaphore(provider_key, cfg)
            async with semaphore:
                return await func(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
            if attempt >= cfg.max_retries or not is_retryable_error(exc):
                raise
            sleep_seconds = _retry_delay_seconds(exc, attempt, cfg)
            _log_retry(provider_key, attempt + 1, sleep_seconds, exc)
            await asyncio.sleep(sleep_seconds)

    assert last_exc is not None
    raise last_exc


def is_retryable_error(exc: BaseException) -> bool:
    """Return True for transient HTTP/network failures worth retrying."""

    status = _status_code(exc)
    if status in _NON_RETRYABLE_STATUS_CODES:
        return False
    if status in _RETRYABLE_STATUS_CODES:
        return True
    if isinstance(exc, (TimeoutError, URLError)):
        return True
    name = exc.__class__.__name__.lower()
    return any(token in name for token in ("timeout", "rate", "connection", "server"))


def _sync_acquire(provider: str, cfg: ProviderRateLimitConfig) -> threading.BoundedSemaphore:
    with _sync_lock:
        key = (provider, cfg.concurrency)
        semaphore = _sync_semaphores.get(key)
        if semaphore is None:
            semaphore = threading.BoundedSemaphore(cfg.concurrency)
            _sync_semaphores[key] = semaphore
    return semaphore


def _async_semaphore(provider: str, cfg: ProviderRateLimitConfig) -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    key = (id(loop), provider, cfg.concurrency)
    semaphore = _async_semaphores.get(key)
    if semaphore is None:
        semaphore = asyncio.Semaphore(cfg.concurrency)
        _async_semaphores[key] = semaphore
    return semaphore


def _retry_delay_seconds(
    exc: BaseException,
    attempt: int,
    cfg: ProviderRateLimitConfig,
) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return min(cfg.max_delay, max(0.0, retry_after))
    exponential = min(cfg.max_delay, cfg.base_delay * (2 ** attempt))
    jitter = random.uniform(0.0, cfg.jitter) if cfg.jitter else 0.0
    return min(cfg.max_delay, exponential + jitter)


def _retry_after_seconds(exc: BaseException) -> float | None:
    headers = _headers(exc)
    if not headers:
        return None
    raw = None
    for key in ("retry-after", "Retry-After"):
        try:
            raw = headers.get(key)
        except Exception:  # noqa: BLE001
            raw = None
        if raw:
            break
    if not raw:
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        pass
    try:
        parsed = email.utils.parsedate_to_datetime(str(raw))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return (parsed - datetime.now(timezone.utc)).total_seconds()
    except Exception:  # noqa: BLE001
        return None


def _status_code(exc: BaseException) -> int | None:
    if isinstance(exc, HTTPError):
        return int(exc.code)
    for attr in ("status_code", "status", "code"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
    response = getattr(exc, "response", None)
    if response is not None:
        value = getattr(response, "status_code", getattr(response, "status", None))
        if isinstance(value, int):
            return value
    return None


def _headers(exc: BaseException) -> Any:
    headers = getattr(exc, "headers", None)
    if headers is not None:
        return headers
    response = getattr(exc, "response", None)
    if response is not None:
        return getattr(response, "headers", None)
    return None


def _normalise_provider(provider: str) -> str:
    return (provider or "generic").strip().lower().replace("-", "_")


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _non_negative_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _positive_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed > 0 else default
    except (TypeError, ValueError):
        return default


def _non_negative_float(value: Any, default: float) -> float:
    try:
        parsed = float(value)
        return parsed if parsed >= 0 else default
    except (TypeError, ValueError):
        return default


def _log_retry(
    provider: str,
    attempt: int,
    sleep_seconds: float,
    exc: BaseException,
) -> None:
    logger.warning(
        "External API retry: provider=%s attempt=%s sleep=%.2fs error=%s:%s",
        provider,
        attempt,
        sleep_seconds,
        exc.__class__.__name__,
        exc,
    )
