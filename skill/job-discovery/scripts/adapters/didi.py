"""Didi public-JSON adapter: talent.didiglobal.com/recruit-portal-service/api/job/front/list.

Official unauthenticated listing endpoint.  Verified live 2026-08-08: bare
GET with page/pageSize/language, ``meta.code == 0``, ``data.items`` pages
(16 records/page, server ignores pageSize); the previous
``/api/jobList`` endpoint now returns 404 ("页面不存在") and has been
replaced.  ``jdId`` is the stable job id; records carry ``job_id`` =
``DD_<jdId>`` (C3 dedup key) and apply via the public detail page
``/position/<jdId>``.
"""
from __future__ import annotations

from typing import Any

from .base import BaseAdapter

_LIST_URL = (
    "https://talent.didiglobal.com/recruit-portal-service/api/job/front/list"
)
_PAGE_SIZE = 50  # server returns 16/page regardless; used for the 300 cap math only


class DidiAdapter(BaseAdapter):
    company = "didi"
    hosts = ("talent.didiglobal.com",)

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        data = self._request_json(
            method="GET",
            url=_LIST_URL,
            params={"page": page, "pageSize": _PAGE_SIZE, "language": "zh"},
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            return [], False
        raw_jobs = payload.get("items")
        if not isinstance(raw_jobs, list):
            return [], False
        records = [self.build_record(job) for job in raw_jobs if isinstance(job, dict)]
        total = payload.get("total", 0)
        has_more = bool(raw_jobs) and page * len(raw_jobs) < int(total or 0)
        return records, has_more

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        job_id = raw.get("jdId")
        description = _join_text(raw.get("jobDuty"), raw.get("jobQualification"))
        return {
            "job_id": f"DD_{job_id}",
            "title": raw.get("jobName", ""),
            "company": "滴滴出行",
            "location": raw.get("workArea", ""),
            "category": raw.get("recruitType", ""),
            "job_type": raw.get("jobTypeName", ""),
            "description": description,
            "apply_url": f"https://talent.didiglobal.com/position/{job_id}",
        }


def _join_text(*parts: Any) -> str:
    return "\n".join(str(p) for p in parts if p)
