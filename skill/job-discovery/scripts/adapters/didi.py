"""Didi public-JSON adapter: talent.didiglobal.com/api/jobList.

Official unauthenticated listing endpoint (FindJobs job_crawler_v2.py
DidiCrawler L734).  GET with page/pageSize/language; ``data.list`` items map
to records with stable ``job_id`` = ``DD_<id>`` (C3 dedup key).
"""
from __future__ import annotations

from typing import Any

from .base import BaseAdapter

_LIST_URL = "https://talent.didiglobal.com/api/jobList"
_PAGE_SIZE = 50


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
        raw_jobs = payload.get("list")
        if not isinstance(raw_jobs, list):
            return [], False
        records = [self.build_record(job) for job in raw_jobs if isinstance(job, dict)]
        total = payload.get("total", 0)
        has_more = bool(raw_jobs) and page * _PAGE_SIZE < int(total or 0)
        return records, has_more

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        job_id = raw.get("id")
        description = _join_text(raw.get("description"), raw.get("requirement"))
        return {
            "job_id": f"DD_{job_id}",
            "title": raw.get("name", ""),
            "company": "滴滴出行",
            "location": raw.get("city", ""),
            "category": raw.get("recruitType", ""),
            "job_type": raw.get("category", ""),
            "description": description,
            "apply_url": f"https://talent.didiglobal.com/position/{job_id}",
        }


def _join_text(*parts: Any) -> str:
    return "\n".join(str(p) for p in parts if p)
