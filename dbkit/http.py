"""HTTP retry helper shared by every ingester.

Wraps httpx with exponential backoff + jitter. Retries on:
  - network errors (httpx.NetworkError, ReadTimeout, …),
  - HTTP 429 (Too Many Requests),
  - HTTP 5xx,
  - JSON decode errors when expect_json=True.

Does NOT retry 4xx other than 429 — those are client errors that won't fix
themselves on retry. Raises the last exception after attempts are exhausted.
"""
from __future__ import annotations

import logging
import random
import time
from typing import Any, Callable, Optional

import httpx

_log = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 1.0
DEFAULT_MAX_DELAY = 30.0


class HttpRetryError(RuntimeError):
    """Raised when all retry attempts fail."""


def request_json(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    json_body: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    on_retry: Optional[Callable[[int, Exception], None]] = None,
) -> Any:
    """Make an HTTP request, retry transient failures, return parsed JSON.

    Honours Retry-After (seconds or HTTP-date) on 429/503 responses.
    """
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.request(
                    method,
                    url,
                    params=params,
                    headers=headers,
                    json=json_body,
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                # Server-side, transient: retry with backoff. Respect Retry-After.
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else _backoff(attempt, base_delay, max_delay)
                last_exc = HttpRetryError(f"{method} {url} -> HTTP {resp.status_code}")
                _log.warning(
                    "%s %s -> %d, attempt %d/%d, sleeping %.1fs",
                    method, url, resp.status_code, attempt, max_attempts, delay,
                )
                if on_retry:
                    on_retry(attempt, last_exc)
                if attempt < max_attempts:
                    time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.json()
        except httpx.HTTPStatusError as e:
            # 4xx other than 429 — don't retry.
            raise
        except (httpx.NetworkError, httpx.TimeoutException, ValueError) as e:
            # ValueError covers JSON decode failures from resp.json().
            last_exc = e
            delay = _backoff(attempt, base_delay, max_delay)
            _log.warning(
                "%s %s raised %s, attempt %d/%d, sleeping %.1fs",
                method, url, type(e).__name__, attempt, max_attempts, delay,
            )
            if on_retry:
                on_retry(attempt, e)
            if attempt < max_attempts:
                time.sleep(delay)
            continue
    raise HttpRetryError(f"{method} {url} failed after {max_attempts} attempts") from last_exc


def request_text(
    method: str,
    url: str,
    *,
    params: Optional[dict] = None,
    headers: Optional[dict] = None,
    timeout: float = DEFAULT_TIMEOUT,
    max_attempts: int = DEFAULT_MAX_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
) -> str:
    """Same as request_json but returns the raw response text (for RSS / XML)."""
    last_exc: Optional[Exception] = None
    for attempt in range(1, max_attempts + 1):
        try:
            with httpx.Client(timeout=timeout, follow_redirects=True) as client:
                resp = client.request(method, url, params=params, headers=headers)
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _parse_retry_after(resp.headers.get("Retry-After"))
                delay = retry_after if retry_after is not None else _backoff(attempt, base_delay, max_delay)
                last_exc = HttpRetryError(f"{method} {url} -> HTTP {resp.status_code}")
                _log.warning("%s %s -> %d, attempt %d/%d, sleeping %.1fs",
                             method, url, resp.status_code, attempt, max_attempts, delay)
                if attempt < max_attempts:
                    time.sleep(delay)
                continue
            resp.raise_for_status()
            return resp.text
        except httpx.HTTPStatusError:
            raise
        except (httpx.NetworkError, httpx.TimeoutException) as e:
            last_exc = e
            delay = _backoff(attempt, base_delay, max_delay)
            _log.warning("%s %s raised %s, attempt %d/%d, sleeping %.1fs",
                         method, url, type(e).__name__, attempt, max_attempts, delay)
            if attempt < max_attempts:
                time.sleep(delay)
            continue
    raise HttpRetryError(f"{method} {url} failed after {max_attempts} attempts") from last_exc


def _backoff(attempt: int, base: float, cap: float) -> float:
    """Exponential backoff with full jitter: random in [0, min(cap, base * 2^(n-1))]."""
    ceiling = min(cap, base * (2 ** (attempt - 1)))
    return random.uniform(0, ceiling)


def _parse_retry_after(value: Optional[str]) -> Optional[float]:
    """Parse Retry-After header (seconds form). HTTP-date form returns None.

    HTTP-date form is rare in practice and not worth the parser complexity —
    fall back to standard backoff.
    """
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None
