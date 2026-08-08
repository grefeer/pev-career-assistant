"""Cross-run job dedup ledger store (C3, plan §7).

docs/findjobs-optimization-plan.zh-CN.md §7 C3.  The ``seen_jobs`` table
records every normalized job identity ever captured; this module owns all
access to it:

  - ``record_seen`` — insert-or-bump; returns ``True`` when the identity is
    new, ``False`` when it was already on the ledger (drift: ``content_hash``
    is refreshed to the latest value so a caller can detect content change
    at the same identity);
  - ``is_seen`` / ``filter_seen`` — cheap dedup checks for pre-fetch gating
    and post-fetch filtering;
  - ``prune_expired`` — TTL sweep so the ledger does not grow unboundedly.

MySQL stays the authority for business state (security gate #5); this table
is deliberately FK-free so task deletion never cascades into the ledger.
All entry points accept ``now`` for deterministic tests.
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Sequence

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from backend.app.db.models import SeenJob
from backend.app.db.base import utc_now


def record_seen(
    session: Session,
    *,
    job_id: str,
    source: str,
    content_hash: str,
    now: datetime | None = None,
) -> bool:
    """Ledger an identity; ``True`` when it was not seen before.

    Re-seen identities bump ``last_seen`` / ``seen_count`` and refresh
    ``content_hash`` to the latest value (drift detection), keeping
    ``first_seen`` untouched.
    """
    stamp = now or utc_now()
    existing = session.scalar(select(SeenJob).where(SeenJob.job_id == job_id))
    if existing is None:
        session.add(
            SeenJob(
                job_id=job_id,
                source=source,
                content_hash=content_hash,
                first_seen=stamp,
                last_seen=stamp,
                seen_count=1,
            )
        )
        return True
    existing.last_seen = stamp
    existing.seen_count += 1
    existing.content_hash = content_hash
    return False


def is_seen(session: Session, *, job_id: str) -> bool:
    """Whether the identity already exists on the ledger."""
    return (
        session.scalar(select(SeenJob.job_id).where(SeenJob.job_id == job_id)) is not None
    )


def filter_seen(session: Session, job_ids: Sequence[str]) -> list[str]:
    """Return the subset of ``job_ids`` already on the ledger (dedup targets).

    Callers compute the fetch set as ``[i for i in ids if i not in seen]``.
    """
    if not job_ids:
        return []
    rows = session.execute(
        select(SeenJob.job_id).where(SeenJob.job_id.in_(list(job_ids)))
    ).scalars()
    return sorted(set(rows))


def prune_expired(
    session: Session,
    *,
    ttl_days: int = 30,
    now: datetime | None = None,
) -> int:
    """Delete ledger rows idle past ``ttl_days``; returns the deleted count."""
    stamp = now or utc_now()
    cutoff = stamp - timedelta(days=ttl_days)
    result = session.execute(delete(SeenJob).where(SeenJob.last_seen < cutoff))
    return result.rowcount or 0
