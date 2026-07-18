from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.app.db.base import Base
from backend.app.db.models import (
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    JobSource,
    JobSourceProvider,
    RawJobRecord,
)
from backend.app.services.job_discovery.tasks import (
    JobDiscoveryTaskFactory,
    _idempotency_key,
    _url_hash,
)
from backend.app.services.job_mappers import extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord


# ---------------------------------------------------------------------------
# URL extraction helpers
# ---------------------------------------------------------------------------

def _fv_url(title: str, link: str) -> dict[str, Any]:
    return {
        "field": title,
        "url_value": {"items": [{"link": link}]},
    }


def _fv_text(title: str, text: str) -> dict[str, Any]:
    return {
        "field": title,
        "text_value": {"items": [{"text": text}]},
    }


def _fv_string(title: str, value: str) -> dict[str, Any]:
    return {
        "field": title,
        "string_value": value,
    }


def _record(field_values: list[dict[str, Any]]) -> TencentRecord:
    return TencentRecord(record_id="r1", field_values=field_values)


class TestExtractDiscoveryUrls:
    """3.1 URL extraction from smart sheet records."""

    def test_27_referrals_extracts_referral_link(self) -> None:
        record = _record([
            _fv_url("内推链接", "https://example.com/refer"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://example.com/refer"]

    def test_27_referrals_extracts_official_site(self) -> None:
        record = _record([
            _fv_text("官网", "https://company.com/careers"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://company.com/careers"]

    def test_27_referrals_extracts_article_link(self) -> None:
        record = _record([
            _fv_url("文章链接", "https://mp.weixin.qq.com/s/test"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://mp.weixin.qq.com/s/test"]

    def test_27_referrals_extracts_delivery_link(self) -> None:
        record = _record([
            _fv_url("投递链接", "https://example.com/apply"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://example.com/apply"]

    def test_27_referrals_matches_any_link_field(self) -> None:
        record = _record([
            _fv_url("内部推荐链接", "https://example.com/inner"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://example.com/inner"]

    def test_27_referrals_matches_url_in_title(self) -> None:
        record = _record([
            _fv_text("岗位URL", "https://example.com/job/1"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://example.com/job/1"]

    def test_27_referrals_skips_non_matching_fields(self) -> None:
        record = _record([
            _fv_text("企业名称", "腾讯"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == []

    def test_intern_referrals_extracts_delivery_link(self) -> None:
        record = _record([
            _fv_url("投递链接", "https://example.com/intern/apply"),
        ])
        urls = extract_discovery_urls(record, "tencent-intern-referrals")
        assert urls == ["https://example.com/intern/apply"]

    def test_intern_referrals_extracts_apply_link(self) -> None:
        record = _record([
            _fv_text("申请链接", "https://example.com/apply"),
        ])
        urls = extract_discovery_urls(record, "tencent-intern-referrals")
        assert urls == ["https://example.com/apply"]

    def test_intern_referrals_extracts_delivery_field(self) -> None:
        record = _record([
            _fv_url("交付链接", "https://example.com/deliver"),
        ])
        urls = extract_discovery_urls(record, "tencent-intern-referrals")
        assert urls == ["https://example.com/deliver"]

    def test_intern_referrals_skips_non_matching(self) -> None:
        record = _record([
            _fv_text("公司名称", "测试公司"),
        ])
        urls = extract_discovery_urls(record, "tencent-intern-referrals")
        assert urls == []

    def test_extracts_from_string_value(self) -> None:
        record = _record([
            _fv_string("投递链接", "https://example.com/raw"),
        ])
        urls = extract_discovery_urls(record, "tencent-intern-referrals")
        assert urls == ["https://example.com/raw"]

    def test_deduplicates_urls(self) -> None:
        record = _record([
            _fv_url("内推链接", "https://example.com/job"),
            _fv_text("官网", "https://example.com/job"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == ["https://example.com/job"]

    def test_filters_non_http_urls(self) -> None:
        record = _record([
            _fv_text("官网", "ftp://example.com/file"),
            _fv_text("链接", "javascript:void(0)"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert urls == []

    def test_unknown_source_key_returns_empty(self) -> None:
        record = _record([
            _fv_text("企业名称", "https://example.com/job"),
        ])
        urls = extract_discovery_urls(record, "unknown-source")
        assert urls == []

    def test_mixed_url_and_text_values(self) -> None:
        record = _record([
            _fv_url("内推链接", "https://example.com/ref"),
            _fv_text("官网", "https://example.com/site"),
            _fv_text("企业名称", "某公司"),
        ])
        urls = extract_discovery_urls(record, "tencent-27-referrals")
        assert sorted(urls) == sorted([
            "https://example.com/ref",
            "https://example.com/site",
        ])


# ---------------------------------------------------------------------------
# Factory tests (require DB)
# ---------------------------------------------------------------------------

@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session


def _create_source(db: Session, **overrides: Any) -> JobSource:
    values = {
        "source_key": "test-source",
        "provider": JobSourceProvider.TENCENT_SMARTSHEET,
        "name": "Test Source",
        "file_id": "f1",
        "sheet_id": "s1",
        "mapper_version": "v1",
        **overrides,
    }
    source = JobSource(**values)
    db.add(source)
    db.flush()
    return source


def _create_raw_record(db: Session, source_id: str, **overrides: Any) -> RawJobRecord:
    values = {
        "source_id": source_id,
        "external_record_id": "ext-1",
        "payload_hash": "a" * 64,
        "raw_fields": [],
        **overrides,
    }
    record = RawJobRecord(**values)
    db.add(record)
    db.flush()
    return record


class TestJobDiscoveryTaskFactory:
    """3.2 JobDiscoveryTaskFactory behavior."""

    def test_disabled_factory_returns_zeros(self, db: Session) -> None:
        factory = JobDiscoveryTaskFactory(enabled=False, agent_version="1.0.0")
        source = _create_source(db)
        record_obj = _record([_fv_url("内推链接", "https://example.com/job")])

        counts = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id="rr-1",
            external_record_id="ext-1",
            source_key="tencent-27-referrals",
            payload_hash="a" * 64,
            record=record_obj,
        )

        assert counts == {"created": 0, "existing": 0, "skipped": 0}

    def test_enabled_factory_creates_tasks(
        self, db: Session
    ) -> None:
        factory = JobDiscoveryTaskFactory(enabled=True, agent_version="1.0.0")
        source = _create_source(db)
        raw = _create_raw_record(db, source.id)
        record_obj = _record([_fv_url("内推链接", "https://example.com/job")])

        counts = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="ext-1",
            source_key="tencent-27-referrals",
            payload_hash="b" * 64,
            record=record_obj,
        )

        assert counts == {"created": 1, "existing": 0, "skipped": 0}
        task = db.query(JobDiscoveryTask).one()
        assert task.source_url == "https://example.com/job"
        assert task.status is JobDiscoveryTaskStatus.queued
        assert task.agent_version == "1.0.0"
        assert task.source_key == "tencent-27-referrals"

    def test_enabled_factory_creates_multiple_urls(
        self, db: Session
    ) -> None:
        factory = JobDiscoveryTaskFactory(enabled=True, agent_version="1.0.0")
        source = _create_source(db)
        raw = _create_raw_record(db, source.id)
        record_obj = _record([
            _fv_url("内推链接", "https://example.com/ref"),
            _fv_text("官网", "https://example.com/site"),
        ])

        counts = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="ext-1",
            source_key="tencent-27-referrals",
            payload_hash="c" * 64,
            record=record_obj,
        )

        assert counts == {"created": 2, "existing": 0, "skipped": 0}
        assert db.query(JobDiscoveryTask).count() == 2

    def test_idempotent_repeated_call_returns_existing(
        self, db: Session
    ) -> None:
        factory = JobDiscoveryTaskFactory(enabled=True, agent_version="1.0.0")
        source = _create_source(db)
        raw = _create_raw_record(db, source.id)
        record_obj = _record([_fv_url("内推链接", "https://example.com/job")])

        first = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="ext-1",
            source_key="tencent-27-referrals",
            payload_hash="d" * 64,
            record=record_obj,
        )
        second = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="ext-1",
            source_key="tencent-27-referrals",
            payload_hash="d" * 64,
            record=record_obj,
        )

        assert first == {"created": 1, "existing": 0, "skipped": 0}
        assert second == {"created": 0, "existing": 1, "skipped": 0}
        assert db.query(JobDiscoveryTask).count() == 1

    def test_no_urls_returns_zeros(self, db: Session) -> None:
        factory = JobDiscoveryTaskFactory(enabled=True, agent_version="1.0.0")
        source = _create_source(db)
        raw = _create_raw_record(db, source.id)
        record_obj = _record([_fv_text("企业名称", "某公司")])

        counts = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="ext-1",
            source_key="tencent-27-referrals",
            payload_hash="e" * 64,
            record=record_obj,
        )

        assert counts == {"created": 0, "existing": 0, "skipped": 0}
        assert db.query(JobDiscoveryTask).count() == 0

    def test_creates_tasks_for_intern_source(self, db: Session) -> None:
        factory = JobDiscoveryTaskFactory(enabled=True, agent_version="1.0.0")
        source = _create_source(db, source_key="tencent-intern-referrals")
        raw = _create_raw_record(db, source.id)
        record_obj = _record([_fv_url("投递链接", "https://example.com/intern")])

        counts = factory.create_tasks(
            db,
            source_id=source.id,
            raw_record_id=raw.id,
            external_record_id="ext-1",
            source_key="tencent-intern-referrals",
            payload_hash="f" * 64,
            record=record_obj,
        )

        assert counts == {"created": 1, "existing": 0, "skipped": 0}
        task = db.query(JobDiscoveryTask).one()
        assert task.source_key == "tencent-intern-referrals"

    def test_hashes_are_deterministic(self) -> None:
        source_id = "src-1"
        ext_id = "ext-1"
        url = "https://example.com/job"
        url_h = _url_hash(url)
        payload_h = "a" * 64
        agent = "1.0.0"

        key1 = _idempotency_key(source_id, ext_id, url_h, payload_h, agent)
        key2 = _idempotency_key(source_id, ext_id, url_h, payload_h, agent)
        key3 = _idempotency_key(source_id, "ext-2", url_h, payload_h, agent)

        assert key1 == key2
        assert key1 != key3
        assert len(key1) == 64

    def test_url_hash_is_consistent(self) -> None:
        h1 = _url_hash("https://example.com/job")
        h2 = _url_hash("https://example.com/job")
        h3 = _url_hash("https://example.com/other")
        assert h1 == h2
        assert h1 != h3
        assert len(h1) == 64
