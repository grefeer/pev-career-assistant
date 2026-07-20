"""StrategyRouter -- URL pattern matching against the strategy library.

Matches incoming task URLs against registered strategy patterns, returning
the best-matching active strategy or None for Supervisor fallback.
"""
from __future__ import annotations

import fnmatch
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.strategy_store import get_active_strategies


class StrategyRouter:
    """Matches URLs against the strategy library and returns the best strategy.

    Usage::

        router = StrategyRouter(db)
        strategy = router.match(task.source_url)
        if strategy is not None:
            ...  # execute via adapter or SnapshotExecutor
    """

    def __init__(self, db: Session) -> None:
        self._strategies = get_active_strategies(db)

    def match(self, url: str) -> JobDiscoveryStrategy | None:
        """Return the best-matching strategy for *url*, or None.

        Matching rules:
        1. Normalize URL (strip query string, standardize scheme)
        2. For each active strategy, fnmatch the host+path against url_pattern
        3. Return the match with highest priority; ties broken by success_count
        """
        normalized = self._normalize_url(url)
        best: tuple[int, int, JobDiscoveryStrategy] | None = None  # (priority, success_count, strategy)

        for s in self._strategies:
            if self._pattern_matches(normalized, s.url_pattern):
                score = (s.priority or 0, s.success_count or 0)
                if best is None or score > (best[0], best[1]):
                    best = (score[0], score[1], s)

        return best[2] if best else None

    # -- internal -----------------------------------------------------------

    @staticmethod
    def _normalize_url(url: str) -> str:
        """Strip query string and fragment, force https scheme."""
        parts = urlsplit(url)
        return urlunsplit(("https", parts.netloc, parts.path, "", ""))

    @staticmethod
    def _pattern_matches(normalized_url: str, pattern: str) -> bool:
        """Check if normalized_url matches the given glob pattern."""
        return fnmatch.fnmatch(normalized_url, pattern) or fnmatch.fnmatch(
            normalized_url.replace("https://", ""), pattern
        )
