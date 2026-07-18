from __future__ import annotations

import hashlib

from sqlalchemy.orm import Session

from backend.app.repositories.job_discovery import create_or_get_task
from backend.app.services.job_mappers import extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord


def _url_hash(url: str) -> str:
    return hashlib.sha256(url.encode("utf-8")).hexdigest()


def _idempotency_key(
    source_id: str,
    external_record_id: str,
    url_hash: str,
    payload_hash: str,
    agent_version: str,
) -> str:
    raw = (
        f"{source_id}::{external_record_id}::{url_hash}"
        f"::{payload_hash}::{agent_version}"
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class JobDiscoveryTaskFactory:
    """Creates discovery tasks from smart sheet records after sync."""

    def __init__(self, *, enabled: bool, agent_version: str) -> None:
        self.enabled = enabled
        self.agent_version = agent_version

    def create_tasks(
        self,
        db: Session,
        *,
        source_id: str,
        raw_record_id: str,
        external_record_id: str,
        source_key: str,
        payload_hash: str,
        record: TencentRecord,
    ) -> dict[str, int]:
        """Create discovery tasks for URLs found in a synced record.

        Returns {"created": N, "existing": N, "skipped": N}.
        """
        if not self.enabled:
            return {"created": 0, "existing": 0, "skipped": 0}

        urls = extract_discovery_urls(record, source_key)
        if not urls:
            return {"created": 0, "existing": 0, "skipped": 0}

        created = 0
        existing = 0

        for url in urls:
            url_h = _url_hash(url)
            idem_key = _idempotency_key(
                source_id,
                external_record_id,
                url_h,
                payload_hash,
                self.agent_version,
            )
            _task, is_new = create_or_get_task(
                db,
                source_id=source_id,
                raw_record_id=raw_record_id,
                external_record_id=external_record_id,
                source_key=source_key,
                source_url=url,
                url_hash=url_h,
                payload_hash=payload_hash,
                idempotency_key=idem_key,
                agent_version=self.agent_version,
            )
            if is_new:
                created += 1
            else:
                existing += 1

        return {"created": created, "existing": existing, "skipped": 0}
