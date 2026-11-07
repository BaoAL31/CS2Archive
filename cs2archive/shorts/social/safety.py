"""Credential scrubbing, hashing, rate limiting and the capped retry loop.

Retry policy matches AGENTS.md: **three logged attempts maximum**, then a
terminal failure. Nothing here retries forever.
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

MAX_ATTEMPTS = 3
SECRET_QUERY_KEYS = frozenset({
    "access_token", "key", "api_key", "apikey", "token", "sig", "signature",
    "client_secret", "secret", "password", "sessionid", "auth",
})
_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(?:key|access_token|api_key|apikey|token|sig|signature|client_secret)\b\s*[=:]\s*[\"']?[A-Za-z0-9_\-\.]{8,}"
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(value: datetime | None = None) -> str:
    return (value or utcnow()).isoformat().replace("+00:00", "Z")


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sanitize_text(text: str, secrets: tuple[str, ...] = ()) -> str:
    """Remove credential values from a string that will be persisted or logged.

    A value is only redacted when it is a known configured secret (so short
    non-secret words are not mangled) or it matches an obvious key=value shape.
    """
    out = str(text or "")
    for secret in secrets:
        if secret and len(secret) >= 8:
            out = out.replace(secret, "[REDACTED]")
    return _SECRET_VALUE_RE.sub("[REDACTED]", out)


def sanitize_url_params(params: dict | None, secrets: tuple[str, ...] = ()) -> str:
    """Describe query parameters by name only; secret values are never written."""
    if not params:
        return ""
    parts = []
    for name in sorted(params):
        if str(name).lower() in SECRET_QUERY_KEYS:
            parts.append(f"{name}=[REDACTED]")
        else:
            value = sanitize_text(str(params[name]), secrets)
            parts.append(f"{name}={value}")
    return "&".join(parts)


def redact_payload(value, secrets: tuple[str, ...] = ()):
    """Recursively replace configured secret strings in a decoded payload."""
    if isinstance(value, dict):
        return {str(k): redact_payload(v, secrets) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_payload(v, secrets) for v in value]
    if isinstance(value, str):
        return sanitize_text(value, secrets)
    return value


class BudgetExceeded(RuntimeError):
    """Raised when a run hits its configured request/page budget."""


@dataclass
class TerminalError(RuntimeError):
    code: str
    message: str
    attempts: int

    def __str__(self) -> str:  # pragma: no cover - trivial
        return f"{self.code}: {self.message} (attempts={self.attempts})"


class RateLimiter:
    """Minimum interval between requests, plus a hard per-run request ceiling."""

    def __init__(self, min_interval_seconds: float = 0.0, max_requests: int | None = None):
        self.min_interval = max(0.0, float(min_interval_seconds))
        self.max_requests = max_requests
        self.requests = 0
        self._last = 0.0
        self._sleep = time.sleep
        self._clock = time.monotonic

    def take(self) -> None:
        if self.max_requests is not None and self.requests >= self.max_requests:
            raise BudgetExceeded(
                f"request budget exhausted ({self.max_requests} requests)")
        if self.min_interval and self.requests:
            wait = self.min_interval - (self._clock() - self._last)
            if wait > 0:
                self._sleep(wait)
        self.requests += 1
        self._last = self._clock()


def retry_call(fn, *, code: str, attempts: int = MAX_ATTEMPTS, retry_on=None,
               on_attempt=None, sleep=None, backoff_seconds: float = 1.0):
    """Run ``fn`` at most ``attempts`` times, logging each failed attempt.

    ``on_attempt(attempt, error)`` records every failure. Exhaustion raises
    :class:`TerminalError` — callers must persist a terminal error row and the
    run must finish non-ok, never silently continue.
    """
    attempts = max(1, min(int(attempts), MAX_ATTEMPTS))
    sleep = sleep or time.sleep
    last: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except BudgetExceeded:
            raise
        except Exception as exc:  # noqa: BLE001 - classified below
            last = exc
            if retry_on is not None and not retry_on(exc):
                raise
            if on_attempt is not None:
                on_attempt(attempt, exc)
            if attempt < attempts:
                sleep(backoff_seconds * attempt)
    raise TerminalError(code=code, message=str(last), attempts=attempts)


def is_retryable_http(status: int | None) -> bool:
    return status is None or status == 429 or 500 <= int(status) < 600


def parse_int(value) -> int | None:
    """Platform counts arrive as strings; blank/hidden stays NULL, never 0."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value == value else None
    text = str(value).strip().replace(",", "")
    if not text or text.lower() in {"none", "null", "unavailable", "hidden", "-"}:
        return None
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def parse_iso_duration(value: str | None) -> float | None:
    """ISO-8601 duration (``PT24S``, ``PT1M2S``) to seconds."""
    if not value:
        return None
    m = re.fullmatch(
        r"P(?:(\d+)D)?T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?", str(value).strip())
    if not m:
        return None
    days, hours, minutes, seconds = (float(g) if g else 0.0 for g in m.groups())
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def parse_timestamp(value) -> str | None:
    """Normalise a platform timestamp to UTC ISO-8601, or None if unparseable."""
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        try:
            return iso(datetime.fromtimestamp(float(value), tz=timezone.utc))
        except (OverflowError, OSError, ValueError):
            return None
    text = str(value).strip()
    if text.isdigit() and len(text) >= 9:
        return parse_timestamp(int(text))
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S.%f%z", "%Y-%m-%dT%H:%M:%SZ",
        "%Y-%m-%d %H:%M:%S", "%Y-%m-%d",
    ):
        try:
            stamp = datetime.strptime(text.replace("+0000", "+00:00"), fmt)
        except ValueError:
            continue
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        return iso(stamp)
    try:
        stamp = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return iso(stamp)


def dumps(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)
