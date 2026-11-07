"""Per-platform collection context: bounded HTTP, retry logging, raw provenance."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Callable

from cs2archive.shorts.social.config import PlatformConfig
from cs2archive.shorts.social.models import CollectedPost, FetchResult
from cs2archive.shorts.social.safety import (
    MAX_ATTEMPTS, BudgetExceeded, RateLimiter, TerminalError, iso, retry_call,
)
from cs2archive.shorts.social.store import RunCounters, SocialStore


def _error_detail(payload, response) -> str:
    """Prefer a structured API error message; fall back to the raw body."""
    if isinstance(payload, dict):
        error = payload.get("error")
        if isinstance(error, dict) and error.get("message"):
            extra = error.get("error_user_title") or error.get("type") or ""
            return f"{error['message']}" + (f" ({extra})" if extra else "")
        if isinstance(error, str) and error:
            return error
    return getattr(response, "text", "") or ""


class HttpError(RuntimeError):
    def __init__(self, status: int, url: str, body: str = ""):
        detail = " ".join(str(body or "").split())[:300]
        super().__init__(f"HTTP {status} for {url[:160]}" + (f": {detail}" if detail else ""))
        self.status = status
        self.body = body[:500]


@dataclass
class Env:
    """The credentials a collector is allowed to use (never persisted raw)."""

    youtube_api_key: str = ""
    meta_system_user_token: str = ""
    meta_page_access_token: str = ""
    meta_ig_user_id: str = ""
    root: str = "."

    @property
    def meta_token(self) -> str:
        return self.meta_system_user_token or self.meta_page_access_token

    def secrets(self) -> tuple[str, ...]:
        return tuple(s for s in (
            self.youtube_api_key, self.meta_system_user_token,
            self.meta_page_access_token) if s)


@dataclass
class PlatformContext:
    store: SocialStore
    run_id: str
    platform: str
    config: PlatformConfig
    env: Env
    rate: RateLimiter
    counters: RunCounters = field(default_factory=RunCounters)
    client: object | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    max_attempts: int = MAX_ATTEMPTS
    raw_root: object | None = None
    window_start: str | None = None
    window_end: str | None = None
    sleep: Callable[[float], None] = time.sleep
    quota_units: float = 0.0
    quota_limit: float = 5000.0
    foundation_dir: object | None = None

    def store_clock(self) -> str:
        """Observation timestamp for this run (injectable in tests)."""
        return iso(self.clock())

    def budget_left(self) -> float:
        return float(self.quota_limit) - float(self.quota_units)

    # ── raw provenance ────────────────────────────────────────────────────
    def _write_raw(self, result: FetchResult) -> FetchResult:
        ref = self.store.write_raw(
            platform=self.platform, run_id=self.run_id, name=result.name,
            payload=result.payload, endpoint=result.endpoint,
            http_status=result.http_status, fetched_at=result.fetched_at,
            params_note=result.params_note, raw_root=self.raw_root)
        result.content_path = ref.content_path
        result.sha256 = ref.sha256
        result.byte_size = ref.byte_size
        result.raw_id = ref.raw_id
        return result

    # ── HTTP with rate limit + capped retries ─────────────────────────────
    def fetch(self, url: str, *, name: str, params: dict | None = None,
              headers: dict | None = None, endpoint: str | None = None,
              allow_status: tuple[int, ...] = (200,), quota_units: float = 0.0,
              stage: str = "fetch", parse: bool = True,
              follow_redirects: bool = True,
              transform: Callable[[object], object] | None = None) -> FetchResult:
        def once() -> FetchResult:
            self.rate.take()
            self.quota_units += quota_units
            response = self.client.get(url, params=params, headers=headers, timeout=30,
                                       follow_redirects=follow_redirects)
            fetched_at = iso(self.clock())
            payload = None
            if transform is not None:
                payload = transform(response)
            elif parse:
                try:
                    payload = response.json()
                except ValueError:
                    payload = None
            if response.status_code not in allow_status:
                raise HttpError(response.status_code, url, _error_detail(payload, response))
            result = FetchResult(
                endpoint=endpoint or url, name=name, payload=payload,
                http_status=response.status_code, fetched_at=fetched_at,
                params_note=self.store.scrub_params(params))
            return self._write_raw(result)

        def on_attempt(attempt: int, exc: Exception) -> None:
            self.store.log_error(
                run_id=self.run_id, platform=self.platform, stage=stage,
                code="http_error" if isinstance(exc, HttpError) else "request_error",
                message=str(exc), attempt=attempt, max_attempts=self.max_attempts,
                http_status=getattr(exc, "status", None),
                request_note=self.store.scrub_params(params), counters=self.counters)

        return retry_call(
            once, code="collect_failed", attempts=self.max_attempts,
            retry_on=lambda exc: (not isinstance(exc, HttpError)) or _retryable(exc.status),
            on_attempt=on_attempt, sleep=self.sleep)

    def try_fetch(self, *args, **kwargs) -> FetchResult | None:
        """A budget hit ends collection *with* the posts already fetched."""
        try:
            return self.fetch(*args, **kwargs)
        except BudgetExceeded:
            self.counters.notes["budget_exhausted"] = True
            return None

    def terminal(self, *, stage: str, exc: Exception, code: str = "collect_failed") -> None:
        self.store.log_error(
            run_id=self.run_id, platform=self.platform, stage=stage, code=code,
            message=str(exc), attempt=self.max_attempts, max_attempts=self.max_attempts,
            http_status=getattr(exc, "status", None), terminal=True, counters=self.counters)


def _retryable(status: int) -> bool:
    return status == 429 or 500 <= int(status) < 600


def digest(value, secrets: tuple[str, ...]) -> object:
    from cs2archive.shorts.social.safety import redact_payload
    return redact_payload(value, secrets)


__all__ = [
    "BudgetExceeded", "CollectedPost", "Env", "HttpError", "PlatformContext",
    "TerminalError", "digest",
]
