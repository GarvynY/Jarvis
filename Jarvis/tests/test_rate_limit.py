#!/usr/bin/env python3
"""Standalone tests for provider-scoped external call limiting."""

from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

spec = importlib.util.spec_from_file_location(
    "rate_limit_under_test",
    ROOT / "pythonclaw" / "core" / "rate_limit.py",
)
assert spec and spec.loader
rate_limit = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = rate_limit
spec.loader.exec_module(rate_limit)


class HttpLikeError(Exception):
    def __init__(self, status_code: int, headers: dict | None = None) -> None:
        super().__init__(f"HTTP {status_code}")
        self.status_code = status_code
        self.headers = headers or {}


def test_sync_retries_429_then_succeeds() -> None:
    sleeps: list[float] = []
    original_sleep = rate_limit.time.sleep
    original_config = rate_limit.get_rate_limit_config
    calls = {"count": 0}

    def flaky() -> str:
        calls["count"] += 1
        if calls["count"] < 3:
            raise HttpLikeError(429)
        return "ok"

    try:
        rate_limit.time.sleep = sleeps.append  # type: ignore[assignment]
        rate_limit.get_rate_limit_config = lambda provider: rate_limit.ProviderRateLimitConfig(
            concurrency=1,
            max_retries=3,
            base_delay=0.1,
            max_delay=1,
            jitter=0,
        )
        assert rate_limit.call_with_backoff("deepseek", flaky) == "ok"
        assert calls["count"] == 3
        assert sleeps == [0.1, 0.2]
    finally:
        rate_limit.time.sleep = original_sleep
        rate_limit.get_rate_limit_config = original_config


def test_sync_does_not_retry_auth_errors() -> None:
    original_sleep = rate_limit.time.sleep
    original_config = rate_limit.get_rate_limit_config
    calls = {"count": 0}

    def unauthorized() -> str:
        calls["count"] += 1
        raise HttpLikeError(401)

    try:
        rate_limit.time.sleep = lambda seconds: None  # type: ignore[assignment]
        rate_limit.get_rate_limit_config = lambda provider: rate_limit.ProviderRateLimitConfig(
            concurrency=1,
            max_retries=3,
            base_delay=0.1,
            max_delay=1,
            jitter=0,
        )
        try:
            rate_limit.call_with_backoff("deepseek", unauthorized)
            raise AssertionError("Expected auth error")
        except HttpLikeError as exc:
            assert exc.status_code == 401
        assert calls["count"] == 1
    finally:
        rate_limit.time.sleep = original_sleep
        rate_limit.get_rate_limit_config = original_config


def test_retry_after_header_is_respected() -> None:
    original_sleep = rate_limit.time.sleep
    original_config = rate_limit.get_rate_limit_config
    sleeps: list[float] = []
    calls = {"count": 0}

    def retry_after() -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise HttpLikeError(429, {"Retry-After": "0.7"})
        return "ok"

    try:
        rate_limit.time.sleep = sleeps.append  # type: ignore[assignment]
        rate_limit.get_rate_limit_config = lambda provider: rate_limit.ProviderRateLimitConfig(
            concurrency=1,
            max_retries=1,
            base_delay=0.1,
            max_delay=1,
            jitter=0,
        )
        assert rate_limit.call_with_backoff("tavily", retry_after) == "ok"
        assert sleeps == [0.7]
    finally:
        rate_limit.time.sleep = original_sleep
        rate_limit.get_rate_limit_config = original_config


def test_async_retries_503_then_succeeds() -> None:
    original_sleep = rate_limit.asyncio.sleep
    original_config = rate_limit.get_rate_limit_config
    calls = {"count": 0}
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    async def flaky() -> str:
        calls["count"] += 1
        if calls["count"] == 1:
            raise HttpLikeError(503)
        return "ok"

    async def run() -> None:
        assert await rate_limit.async_call_with_backoff("google_news", flaky) == "ok"

    try:
        rate_limit.asyncio.sleep = fake_sleep  # type: ignore[assignment]
        rate_limit.get_rate_limit_config = lambda provider: rate_limit.ProviderRateLimitConfig(
            concurrency=1,
            max_retries=2,
            base_delay=0.1,
            max_delay=1,
            jitter=0,
        )
        asyncio.run(run())
        assert calls["count"] == 2
        assert sleeps == [0.1]
    finally:
        rate_limit.asyncio.sleep = original_sleep
        rate_limit.get_rate_limit_config = original_config


if __name__ == "__main__":
    tests = [
        test_sync_retries_429_then_succeeds,
        test_sync_does_not_retry_auth_errors,
        test_retry_after_header_is_respected,
        test_async_retries_503_then_succeeds,
    ]
    for test in tests:
        test()
        print(f"PASS {test.__name__}")
    print(f"\nAll {len(tests)} rate_limit tests passed.")
