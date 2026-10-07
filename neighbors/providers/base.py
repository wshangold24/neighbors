from __future__ import annotations

import logging
import os
from abc import ABC, abstractmethod
from pathlib import Path

from ..http import SafeSession
from ..models import Listing, Preferences

log = logging.getLogger("neighbors.providers")


class Provider(ABC):
    """Base class for listing sources."""

    name: str = "base"

    def __init__(self, session: SafeSession):
        self.session = session

    @abstractmethod
    def fetch(self, prefs: Preferences, max_pages: int = 2) -> list[Listing]:
        """Return validated listings matching a *loose* version of prefs.
        We deliberately widen filters (KNN narrows later)."""

    def _validated(self, listings: list[Listing]) -> list[Listing]:
        good = [l for l in listings if l.validate()]
        dropped = len(listings) - len(good)
        if dropped:
            log.info("%s: dropped %d invalid listings", self.name, dropped)
        return good


def resolve_secret(explicit: str | None, env_var: str, path: Path) -> str | None:
    """First non-blank of: the explicit value, $env_var, the contents of path."""
    for value in (explicit, os.environ.get(env_var)):
        if value and value.strip():
            return value.strip()
    if path.exists():
        return path.read_text().strip() or None
    return None
