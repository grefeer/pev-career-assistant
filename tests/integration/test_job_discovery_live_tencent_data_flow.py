from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
import json
import os
from typing import Any

import pytest
from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.config import Settings, _literal_tencent_dotenv_values
from backend.app.db.models import (
    Base,
    DiscoveredJobCandidate,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    RawJobRecord,
)
from backend.app.repositories.job_discovery import list_review_groups
from backend.app.services.job_discovery.deepagents_runner import (
    package_candidates,
    run_web_navigation,
    standardize_from_record_fields,
    verify_evidence,
)
from backend.app.services.job_discovery.worker import JobDiscoveryWorker
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.job_sync import JobSyncService
from backend.app.services.tencent_smartsheet import (
    PAGE_SIZE,
    TencentField,
    TencentRecord,
    TencentRecordPage,
    TencentSmartsheetGateway,
)


SOURCE_KEYS = ("tencent-27-referrals", "tencent-intern-referrals")
PROJECT_ROOT = Path(__file__).resolve().parents[2]
MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")


def _live_tencent_token() -> str | None:
    return (
        os.environ.get("TEST_TENCENT_DOCS_TOKEN")
        or os.environ.get("TENCENT_DOCS_TOKEN")
        or _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV).get("test_tencent_docs_token")
        or _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV).get("tencent_docs_token")
    )


pytestmark = pytest.mark.skipif(
    not os.environ.get("RUN_LIVE_TENCENT_DISCOVERY"),
    reason="set RUN_LIVE_TENCENT_DISCOVERY=1 to read real Tencent docs and public URLs",
)


class _TwoRecordLiveGateway:
    def __init__(self, live_gateway: TencentSmartsheetGateway) -> None:
        self.live_gateway = live_gateway
        self._selected: dict[tuple[str, str], list[TencentRecord]] = {}

    def list_fields(self, file_id: str, sheet_id: str) -> list[TencentField]:
        return self.live_gateway.list_fields(file_id, sheet_id)

    def list_records(
        self,
        file_id: str,
        sheet_id: str,
        *,
        offset: int,
        limit: int = PAGE_SIZE,
    ) -> TencentRecordPage:
        key = (file_id, sheet_id)
        if key not in self._selected:
            source_key = _source_key_for(file_id, sheet_id)
            self._selected[key] = _select_two_live_records_with_urls(
                self.live_gateway,
                file_id=file_id,
                sheet_id=sheet_id,
                source_key=source_key,
            )
        records = self._selected[key]
        if offset > 0:
            return TencentRecordPage([], len(records), False, offset)
        return TencentRecordPage(records, len(records), False, len(records))


def _source_key_for(file_id: str, sheet_id: str) -> str:
    for source in BUILTIN_SOURCES:
        if source.file_id == file_id and source.sheet_id == sheet_id:
            return source.source_key
    raise AssertionError(f"unknown live Tencent source: {file_id}/{sheet_id}")


def _select_two_live_records_with_urls(
    gateway: TencentSmartsheetGateway,
    *,
    file_id: str,
    sheet_id: str,
    source_key: str,
) -> list[TencentRecord]:
    selected: list[TencentRecord] = []
    offset = 0
    while len(selected) < 2:
        page = gateway.list_records(file_id, sheet_id, offset=offset, limit=10)
        for record in page.records:
            if extract_discovery_urls(record, source_key):
                selected.append(record)
            if len(selected) == 2:
                break
        if len(selected) == 2:
            break
        if not page.has_more:
            break
        offset = page.next_offset
    assert len(selected) == 2, f"{source_key} did not expose two URL records"
    return selected


class _RealToolDiscoveryAgent:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.outputs: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, list[HumanMessage]]) -> dict[str, Any]:
        task_input = json.loads(payload["messages"][0].content)
        navigation = run_web_navigation(task_input["source_url"], settings=self.settings)
        evidence = navigation.get("evidence_pages") or []
        if not evidence:
            result = {
                "status": "needs_manual_review",
                "block_reason": "unknown",
                "evidence": [],
                "candidates": [],
                "summary": navigation.get("error") or "No evidence pages found",
            }
            self.outputs.append(result)
            return {"structured_response": result}

        candidates_json = standardize_from_record_fields(
            json.dumps(task_input["record_fields"], ensure_ascii=False),
            json.dumps(evidence, ensure_ascii=False),
            task_input["source_url"],
        )
        verified_json = verify_evidence(
            candidates_json,
            json.dumps(evidence, ensure_ascii=False),
        )
        evidence_hash = evidence[0].get("content_hash") or task_input["url_hash"]
        packaged = json.loads(
            package_candidates(
                verified_json,
                evidence_hash,
                task_input["source_key"],
            )
        )
        for candidate in packaged:
            candidate["source_id"] = task_input["source_id"]
            candidate["raw_record_id"] = task_input["raw_record_id"]
            candidate["external_record_id"] = task_input["external_record_id"]

        result = {
            "status": "succeeded" if packaged else "needs_manual_review",
            "block_reason": None if packaged else "unknown",
            "evidence": evidence,
            "candidates": packaged,
            "summary": f"Real Tencent discovery produced {len(packaged)} candidate(s)",
        }
        self.outputs.append(result)
        return {"structured_response": result}


@pytest.fixture
def db_factory() -> Iterator[sessionmaker[Session]]:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    try:
        yield factory
    finally:
        engine.dispose()


def test_real_tencent_two_sources_two_jobs_each_data_flow(
    db_factory: sessionmaker[Session],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token = _live_tencent_token()
    assert token
    settings = Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_agent_version="live-test-v1",
        job_discovery_task_timeout_seconds=60,
        job_discovery_max_pages_per_task=5,
    )
    live_gateway = TencentSmartsheetGateway(token=token)
    limited_gateway = _TwoRecordLiveGateway(live_gateway)
    agent = _RealToolDiscoveryAgent(settings)

    with db_factory() as db:
        service = JobSyncService(
            limited_gateway,
            discovery_enabled=True,
            discovery_agent_version="live-test-v1",
        )
        outcomes = [
            service.sync(db, source_key=source_key, actor_user_id="admin")
            for source_key in SOURCE_KEYS
        ]
        assert [outcome.discovery_tasks_created for outcome in outcomes] == [2, 2]
        assert db.scalar(select(func.count()).select_from(RawJobRecord)) == 4
        assert db.scalar(select(func.count()).select_from(JobDiscoveryTask)) == 4

    monkeypatch.setattr(
        "backend.app.services.job_discovery.worker.build_discovery_supervisor_agent",
        lambda settings: agent,
    )
    worker = JobDiscoveryWorker(db_factory, settings)
    processed = 0
    while worker.run_once():
        processed += 1
    assert processed == 4

    with db_factory() as db:
        tasks = db.scalars(select(JobDiscoveryTask)).all()
        assert {task.status for task in tasks} == {JobDiscoveryTaskStatus.succeeded}
        candidates = db.scalars(select(DiscoveredJobCandidate)).all()
        assert len(candidates) == 4
        candidate_tasks = {
            db.get(JobDiscoveryTask, candidate.task_id).source_key
            for candidate in candidates
        }
        assert candidate_tasks == set(SOURCE_KEYS)
        assert all(candidate.title for candidate in candidates)
        assert all(candidate.company_name for candidate in candidates)
        assert all(candidate.apply_url for candidate in candidates)
        assert all(candidate.evidence_refs_json for candidate in candidates)
        groups = list_review_groups(db)
        assert sum(len(group["candidates"]) for group in groups) == 4
    assert len(agent.outputs) == 4
