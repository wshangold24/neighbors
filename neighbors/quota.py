"""Monthly API quota ledger (Milestone 4).

Per-run budgets (SafeSession) protect a single run; this ledger protects the
MONTH. It counts billable requests per provider per calendar month so that
iterating several times a week can never blow through a free tier.

Only providers listed in MONTHLY_LIMITS are constrained — the NYC open-data
sources have no monthly quota (they're throttled per-run by SafeSession).
"""

from __future__ import annotations

import time
from pathlib import Path

from .store import open_db

# MONTHLY_LIMITS is the provider's REAL tier ceiling (what you pay for /
# what the dashboard shows). SOFT_STOP is where WE stop fetching — below the
# ceiling so a miscount on our side can never overshoot the real limit.
# RentCast free tier = 50 requests/month.
MONTHLY_LIMITS = {"rentcast": 50}
SOFT_STOP = {"rentcast": 45}
WARN_AT = {"rentcast": 35}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS quota (provider TEXT, month TEXT, n INTEGER,
                                  PRIMARY KEY (provider, month));
"""


def current_month() -> str:
    return time.strftime("%Y-%m")


class QuotaLedger:
    """Persistent {provider: {month: count}} counter, stored in the shared
    SQLite database (`store.db`, table `quota`).

    save() applies record() calls as increments to the stored count rather than
    overwriting it, so concurrent runs (CLI and web UI) both get counted;
    set_used() is an explicit absolute override."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._counts: dict[str, dict[str, int]] = {}
        self._added: dict[tuple[str, str], int] = {}   # unsaved increments
        self._overridden: set[tuple[str, str]] = set()  # unsaved set_used()
        with open_db(self.path, _SCHEMA) as conn:
            for provider, month, n in conn.execute("SELECT provider, month, n FROM quota"):
                self._counts.setdefault(provider, {})[month] = n

    def save(self) -> None:
        with open_db(self.path, _SCHEMA) as conn:
            conn.executemany(
                "INSERT OR REPLACE INTO quota (provider, month, n) VALUES (?, ?, ?)",
                [(p, m, self._counts[p][m]) for p, m in self._overridden])
            conn.executemany(
                "INSERT INTO quota (provider, month, n) VALUES (?, ?, ?) "
                "ON CONFLICT(provider, month) DO UPDATE SET n = n + excluded.n",
                [(p, m, n) for (p, m), n in self._added.items()
                 if (p, m) not in self._overridden])
            for p, m in set(self._added) | self._overridden:
                row = conn.execute("SELECT n FROM quota WHERE provider = ? AND month = ?",
                                   (p, m)).fetchone()
                self._counts[p][m] = row[0]
        self._added.clear()
        self._overridden.clear()

    def spent(self, provider: str, month: str | None = None) -> int:
        return self._counts.get(provider, {}).get(month or current_month(), 0)

    def record(self, provider: str, n: int) -> None:
        if n <= 0:
            return
        month = current_month()
        months = self._counts.setdefault(provider, {})
        months[month] = months.get(month, 0) + n
        self._added[(provider, month)] = self._added.get((provider, month), 0) + n

    def set_used(self, provider: str, n: int) -> None:
        """Overwrite this month's count — for syncing with the provider's own
        dashboard (this ledger only sees calls made through this app)."""
        month = current_month()
        self._counts.setdefault(provider, {})[month] = max(0, n)
        self._overridden.add((provider, month))

    def allow(self, provider: str) -> tuple[bool, str]:
        """(allowed, reason). Providers without a monthly limit always pass."""
        limit = MONTHLY_LIMITS.get(provider)
        if limit is None:
            return True, ""
        used = self.spent(provider)
        stop = SOFT_STOP.get(provider, limit)
        if used >= stop:
            return False, (f"monthly quota auto-stop reached ({used}/{limit} used, "
                           f"stop at {stop}) — resets next month, or fix the count "
                           "with: quota --set-used")
        if used >= WARN_AT.get(provider, stop):
            return True, f"quota warning: {used}/{limit} used this month"
        return True, f"{used}/{limit} used this month"

    def summary(self) -> dict[str, dict]:
        """Current-month view for the `quota` command."""
        month = current_month()
        return {
            p: {"used": self.spent(p, month), "limit": MONTHLY_LIMITS.get(p),
                "stop_at": SOFT_STOP.get(p)}
            for p in sorted(set(self._counts) | set(MONTHLY_LIMITS))
        }
