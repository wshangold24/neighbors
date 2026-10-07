"""Rate-limited, cached, budgeted HTTP client.

Every provider MUST fetch through SafeSession. Guarantees:
  * Per-domain token bucket (default: 1 request / 4 seconds)
  * Hard global budget per run (default: 25 network requests)
  * Disk cache with TTL (default: 12h) — cache hits cost nothing
  * Exponential backoff on 429/5xx, honors Retry-After
  * Realistic browser headers, timeouts on everything
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import random
import time
from pathlib import Path
from typing import Optional

import requests

log = logging.getLogger("neighbors.http")

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}


class BudgetExceeded(Exception):
    """Raised when the per-run network request budget is exhausted."""


class DomainRateLimiter:
    """Token-bucket per domain with jitter."""

    def __init__(self, min_interval: float = 4.0, jitter: float = 1.5):
        self.min_interval = min_interval
        self.jitter = jitter
        self._last: dict[str, float] = {}

    def wait(self, domain: str) -> None:
        now = time.monotonic()
        last = self._last.get(domain)
        if last is not None:
            delay = self.min_interval + random.uniform(0, self.jitter)
            remaining = last + delay - now
            if remaining > 0:
                log.debug("rate limit: sleeping %.1fs for %s", remaining, domain)
                time.sleep(remaining)
        self._last[domain] = time.monotonic()


class DiskCache:
    """JSON-file cache keyed by URL hash, with TTL."""

    def __init__(self, cache_dir: Path, ttl_seconds: int = 12 * 3600):
        self.dir = Path(cache_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.ttl = ttl_seconds

    def _path(self, key: str) -> Path:
        return self.dir / (hashlib.sha256(key.encode()).hexdigest()[:24] + ".json")

    def get(self, key: str) -> Optional[str]:
        p = self._path(key)
        try:
            entry = json.loads(p.read_text())
            fresh = time.time() - entry["ts"] <= self.ttl
            body = entry["body"]
        except FileNotFoundError:
            return None
        except (OSError, ValueError, KeyError, TypeError):
            fresh, body = False, None    # unreadable/corrupt entry: drop it
        if not fresh:
            p.unlink(missing_ok=True)
            return None
        return body

    def put(self, key: str, body: str) -> None:
        # write-then-rename so a concurrent reader never sees a partial file
        p = self._path(key)
        tmp = p.with_name(f"{p.name}.{os.getpid()}.tmp")
        try:
            tmp.write_text(json.dumps({"ts": time.time(), "body": body}))
            os.replace(tmp, p)
        except OSError as e:
            tmp.unlink(missing_ok=True)
            log.warning("cache write failed: %s", e)


class SafeSession:
    def __init__(
        self,
        cache_dir: Path,
        budget: int = 25,
        min_interval: float = 4.0,
        cache_ttl: int = 12 * 3600,
        timeout: float = 15.0,
        max_retries: int = 3,
    ):
        self.session = requests.Session()
        self.session.headers.update(DEFAULT_HEADERS)
        self.limiter = DomainRateLimiter(min_interval=min_interval)
        self.cache = DiskCache(cache_dir, ttl_seconds=cache_ttl)
        self.budget = budget
        self.spent = 0
        self.timeout = timeout
        self.max_retries = max_retries
        # Chronological record of every fetch outcome (for --debug reports).
        self.history: list[dict] = []

    @property
    def remaining_budget(self) -> int:
        return max(0, self.budget - self.spent)

    def get(self, url: str, params: Optional[dict] = None, use_cache: bool = True,
            headers: Optional[dict] = None) -> Optional[str]:
        """Fetch URL text. Returns None on unrecoverable failure (never raises
        for HTTP errors — raises BudgetExceeded only when out of budget).

        `headers` are merged per-request (e.g. API keys) and deliberately
        excluded from the cache key — never put anything in headers that
        changes the response body for the same URL+params."""
        key = url + "?" + json.dumps(params, sort_keys=True) if params else url

        if use_cache:
            cached = self.cache.get(key)
            if cached is not None:
                log.debug("cache hit: %s", url)
                self.history.append({"url": url, "params": params,
                                     "event": "cache_hit"})
                return cached

        domain = url.split("/")[2]
        backoff, wait = 2.0, 0.0
        for attempt in range(1, self.max_retries + 1):
            # checked per attempt: retries spend budget too
            if self.spent >= self.budget:
                self.history.append({"url": url, "params": params,
                                     "event": "budget_exhausted"})
                raise BudgetExceeded(
                    f"Request budget ({self.budget}) exhausted. "
                    "Try again later (cached responses are free)."
                )
            time.sleep(wait)
            self.limiter.wait(domain)
            self.spent += 1
            if self.spent >= self.budget:
                log.warning("network budget fully spent (%d/%d)", self.spent, self.budget)
            t0 = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=self.timeout,
                                        headers=headers)
            except requests.RequestException as e:
                log.warning("request error (%s attempt %d): %s", domain, attempt, e)
                self.history.append({"url": url, "params": params, "event": "exception",
                                     "error": str(e), "attempt": attempt})
                wait, backoff = backoff, backoff * 2
                continue

            self.history.append({
                "url": url, "params": params, "event": "response",
                "status": resp.status_code, "attempt": attempt,
                "elapsed_ms": round((time.monotonic() - t0) * 1000),
                "bytes": len(resp.content),
            })

            if resp.status_code == 200:
                self.cache.put(key, resp.text)
                return resp.text
            if resp.status_code in (401, 403):
                log.warning("%s blocked request (%d) — likely bot protection; not retrying",
                            domain, resp.status_code)
                return None
            if resp.status_code == 429 or resp.status_code >= 500:
                retry_after = _retry_after(resp.headers.get("Retry-After"))
                wait = min(backoff if retry_after is None else retry_after, 60.0)
                backoff *= 2
                if attempt < self.max_retries:
                    log.warning("%s returned %d; backing off %.0fs",
                                domain, resp.status_code, wait)
                continue
            log.warning("%s returned %d; giving up", domain, resp.status_code)
            return None
        log.warning("%s: giving up after %d attempts", domain, self.max_retries)
        return None


def _retry_after(value: Optional[str]) -> Optional[float]:
    """Seconds from a Retry-After header (delta-seconds form only; the
    HTTP-date form falls back to our own backoff)."""
    try:
        return max(0.0, float(value)) if value else None
    except ValueError:
        return None
