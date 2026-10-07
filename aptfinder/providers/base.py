from __future__ import annotations

import logging
from abc import ABC, abstractmethod

from ..http import SafeSession
from ..models import Listing, Preferences

log = logging.getLogger("aptfinder.providers")


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
