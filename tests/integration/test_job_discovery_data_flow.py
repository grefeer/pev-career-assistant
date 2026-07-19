from __future__ import annotations

import json
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any
from unittest.mock import patch

from langchain_core.messages import HumanMessage
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from backend.app.db.base import Base
from backend.app.db.models import (
    DiscoveredJobCandidate,
    JobDiscoveryTask,
    JobDiscoveryTaskStatus,
    RawJobRecord,
    User,
    UserRole,
)
from backend.app.repositories.job_discovery import list_review_groups
from backend.app.services.job_discovery.deepagents_runner import (
    extract_jd_candidates,
    package_candidates,
    triage_link,
    verify_evidence,
)
from backend.app.services.job_discovery.schemas import PageEvidence
from backend.app.services.job_discovery.worker import JobDiscoveryWorker
from backend.app.services.job_sync import JobSyncService
from backend.app.services.tencent_smartsheet import (
    TencentField,
    TencentRecord,
    TencentRecordPage,
)
from tests.conftest import settings_override


NOW = datetime(2026, 7, 19, tzinfo=timezone.utc)


class FakeGateway:
    def __init__(self, fields: list[TencentField], records: list[TencentRecord]) -> None:
        self.fields = fields
        self.records = records
        self.calls: list[tuple[str, str, int]] = []

    def list_fields(self, _file_id: str, _sheet_id: str) -> list[TencentField]:
        return self.fields

    def list_records(
        self,
        file_id: str,
        sheet_id: str,
        *,
        offset: int,
        limit: int,
    ) -> TencentRecordPage:
        self.calls.append((file_id, sheet_id, offset))
        assert limit == 100
        assert offset == 0
        return TencentRecordPage(self.records, len(self.records), False, 0)


class DeterministicDiscoveryAgent:
    def __init__(self, jd_text_by_url: dict[str, str]) -> None:
        self.jd_text_by_url = jd_text_by_url
        self.outputs: list[dict[str, Any]] = []

    def invoke(self, payload: dict[str, list[HumanMessage]]) -> dict[str, Any]:
        task_input = json.loads(payload["messages"][0].content)
        source_url = task_input["source_url"]
        source_key = task_input["source_key"]
        triage = triage_link(source_url)
        assert triage["recommended_action"] == "run_web_navigation"

        jd_text = self.jd_text_by_url[source_url]
        content_hash = f"hash-{task_input['external_record_id']}"
        evidence = [
            {
                "evidence_type": "page_text",
                "url": source_url,
                "title": f"{task_input['external_record_id']} JD",
                "content_hash": content_hash,
                "text_excerpt": jd_text,
                "metadata": {"step": "web_navigation"},
            }
        ]

        candidates_json = extract_jd_candidates(jd_text, source_url)
        candidates = json.loads(candidates_json)
        for candidate in candidates:
            candidate["evidence_refs"] = [
                {
                    "type": "page_text",
                    "url": source_url,
                    "content_hash": content_hash,
                }
            ]

        verified_json = verify_evidence(
            json.dumps(candidates, ensure_ascii=False),
            json.dumps([asdict(PageEvidence(**evidence[0]))], ensure_ascii=False),
        )
        packaged_json = package_candidates(verified_json, content_hash, source_key)
        packaged = json.loads(packaged_json)
        for candidate in packaged:
            candidate["source_id"] = task_input["source_id"]
            candidate["raw_record_id"] = task_input["raw_record_id"]
            candidate["external_record_id"] = task_input["external_record_id"]

        result = {
            "status": "succeeded",
            "block_reason": None,
            "evidence": evidence,
            "candidates": packaged,
            "summary": f"Discovered {len(packaged)} candidates",
        }
        self.outputs.append(result)
        return {"structured_response": result}


def _url_field(title: str, link: str) -> dict[str, Any]:
    return {"field": title, "url_value": {"items": [{"link": link}]}}


def _text_field(title: str, text: str) -> dict[str, Any]:
    return {"field": title, "text_value": {"items": [{"text": text, "type": "text"}]}}


def _records_27() -> list[TencentRecord]:
    return [
        TencentRecord(
            "27-backend",
            [_text_field("企业名称", "星辰科技"), _url_field("内推链接", "https://fixture.test/27/backend")],
        ),
        TencentRecord(
            "27-frontend",
            [_text_field("企业名称", "星辰科技"), _url_field("内推链接", "https://fixture.test/27/frontend")],
        ),
    ]


def _records_intern() -> list[TencentRecord]:
    return [
        TencentRecord(
            "intern-backend",
            [
                _text_field("公司名称", "星辰科技"),
                _text_field("招聘岗位", "后端开发工程师"),
                _url_field("投递链接", "https://fixture.test/intern/backend"),
            ],
        ),
        TencentRecord(
            "intern-frontend",
            [
                _text_field("公司名称", "星辰科技"),
                _text_field("招聘岗位", "前端开发工程师"),
                _url_field("投递链接", "https://fixture.test/intern/frontend"),
            ],
        ),
    ]


def _jd_text(title: str, location: str, apply_url: str) -> str:
    return f"""
    岗位名称：{title}
    公司名称：星辰科技
    工作地点：{location}
    招聘类型：实习
    岗位职责：负责{title}相关研发工作，参与业务系统设计和交付。
    任职要求：计算机相关专业，熟悉工程实践，有项目经验。
    投递方式：{apply_url}
    截止日期：2026-08-31
    """


def test_section_11_data_flow_two_tencent_sources_extract_two_jobs_each() -> None:
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    SessionLocal = sessionmaker(bind=engine)
    settings = settings_override(
        job_discovery_enabled=True,
        job_discovery_agent_version="test-v1",
        job_discovery_task_timeout_seconds=60,
    )

    jd_by_url = {
        "https://fixture.test/27/backend": _jd_text("后端开发工程师", "北京", "https://apply.test/backend"),
        "https://fixture.test/27/frontend": _jd_text("前端开发工程师", "上海", "https://apply.test/frontend"),
        "https://fixture.test/intern/backend": _jd_text("后端开发工程师", "北京", "https://apply.test/backend"),
        "https://fixture.test/intern/frontend": _jd_text("前端开发工程师", "上海", "https://apply.test/frontend"),
    }
    agent = DeterministicDiscoveryAgent(jd_by_url)

    with Session(engine) as db:
        db.add(
            User(
                id="admin",
                account="admin",
                nickname="Admin",
                password_hash="unused",
                role=UserRole.ADMIN,
            )
        )
        db.commit()

        sync_27 = JobSyncService(
            FakeGateway(
                [TencentField("company", "企业名称", "text"), TencentField("url", "内推链接", "url")],
                _records_27(),
            ),
            now=lambda: NOW,
            correlation_id_factory=lambda: "sync-27",
            discovery_enabled=True,
            discovery_agent_version="test-v1",
        )
        sync_intern = JobSyncService(
            FakeGateway(
                [
                    TencentField("company", "公司名称", "text"),
                    TencentField("title", "招聘岗位", "text"),
                    TencentField("url", "投递链接", "url"),
                ],
                _records_intern(),
            ),
            now=lambda: NOW,
            correlation_id_factory=lambda: "sync-intern",
            discovery_enabled=True,
            discovery_agent_version="test-v1",
        )

        outcome_27 = sync_27.sync(db, source_key="tencent-27-referrals", actor_user_id="admin")
        outcome_intern = sync_intern.sync(db, source_key="tencent-intern-referrals", actor_user_id="admin")
        assert outcome_27.discovery_tasks_created == 2
        assert outcome_intern.discovery_tasks_created == 2
        assert len(db.scalars(select(RawJobRecord)).all()) == 4
        assert len(db.scalars(select(JobDiscoveryTask)).all()) == 4

    with patch(
        "backend.app.services.job_discovery.worker.build_discovery_supervisor_agent",
        return_value=agent,
    ):
        worker = JobDiscoveryWorker(SessionLocal, settings)
        processed = 0
        while worker.run_once():
            processed += 1

    with Session(engine) as db:
        assert processed == 4
        tasks = db.scalars(select(JobDiscoveryTask)).all()
        assert {task.status for task in tasks} == {JobDiscoveryTaskStatus.succeeded}

        candidates = db.scalars(select(DiscoveredJobCandidate)).all()
        assert len(candidates) == 4
        assert {candidate.title for candidate in candidates} == {
            "后端开发工程师",
            "前端开发工程师",
        }
        assert all(candidate.idempotency_key for candidate in candidates)
        assert all(candidate.evidence_refs_json for candidate in candidates)

        groups = list_review_groups(db)
        assert len(groups) == 2
        assert sorted(len(group["candidates"]) for group in groups) == [2, 2]
        assert len(agent.outputs) == 4
