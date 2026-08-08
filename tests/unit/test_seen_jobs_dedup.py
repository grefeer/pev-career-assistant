"""C3 cross-run dedup ledger: record_seen / is_seen / filter_seen / prune."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import SeenJob
from backend.app.services.job_discovery.tools.seen_jobs import (
    filter_seen,
    is_seen,
    prune_expired,
    record_seen,
)


def _session() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    return Session(engine)


def _t(days: int = 0) -> datetime:
    return datetime(2026, 8, 8, tzinfo=timezone.utc) + timedelta(days=days)


def test_first_record_is_new_then_second_is_seen() -> None:
    with _session() as db:
        assert record_seen(db, job_id="didi-123", source="didi", content_hash="h1", now=_t()) is True
        assert record_seen(db, job_id="didi-123", source="didi", content_hash="h1", now=_t(1)) is False
        row = db.scalar(select(SeenJob).where(SeenJob.job_id == "didi-123"))
        assert row is not None
        assert row.seen_count == 2
        # SQLite round-trips timezone-aware datetimes as naive UTC
        assert row.first_seen.replace(tzinfo=timezone.utc) == _t()  # first-seen preserved
        assert row.last_seen.replace(tzinfo=timezone.utc) == _t(1)


def test_content_hash_refreshes_on_drift_at_same_identity() -> None:
    with _session() as db:
        record_seen(db, job_id="baidu-7", source="baidu", content_hash="old", now=_t())
        record_seen(db, job_id="baidu-7", source="baidu", content_hash="new", now=_t(1))
        row = db.scalar(select(SeenJob).where(SeenJob.job_id == "baidu-7"))
        assert row is not None and row.content_hash == "new"


def test_is_seen_and_filter_seen() -> None:
    with _session() as db:
        record_seen(db, job_id="netease-1", source="netease", content_hash="h1", now=_t())
        record_seen(db, job_id="netease-2", source="netease", content_hash="h2", now=_t())
        assert is_seen(db, job_id="netease-1") is True
        assert is_seen(db, job_id="netease-3") is False
        assert filter_seen(db, ["netease-1", "netease-2", "netease-3"]) == ["netease-1", "netease-2"]
        assert filter_seen(db, []) == []


def test_ledger_survives_commit_across_sessions() -> None:
    engine = create_engine("sqlite+pysqlite:///:memory:", poolclass=StaticPool)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        assert record_seen(db, job_id="web-42", source="web", content_hash="h", now=_t()) is True
        db.commit()
    with Session(engine) as db:
        assert is_seen(db, job_id="web-42") is True
        # second run reports the same job as already seen (cross-run dedup)
        assert record_seen(db, job_id="web-42", source="web", content_hash="h", now=_t(2)) is False


def test_prune_expired_removes_only_idle_rows() -> None:
    with _session() as db:
        record_seen(db, job_id="fresh", source="web", content_hash="h1", now=_t(0))
        record_seen(db, job_id="stale", source="web", content_hash="h2", now=_t(-31))
        assert prune_expired(db, ttl_days=30, now=_t(0)) == 1
        assert is_seen(db, job_id="fresh") is True
        assert is_seen(db, job_id="stale") is False
        # nothing left to prune
        assert prune_expired(db, ttl_days=30, now=_t(0)) == 0
