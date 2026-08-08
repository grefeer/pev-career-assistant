"""Netease public-JSON adapter: hr.163.com/api/hr163/position/queryPage.

Official unauthenticated listing endpoint (FindJobs job_crawler_v2.py
NeteaseCrawler L564).  POST with currentPage/pageSize + Origin header;
``data.list`` items map to records with stable ``job_id`` = ``NE_<id>``.
"""
from __future__ import annotations

from typing import Any

from .base import BaseAdapter

_LIST_URL = "https://hr.163.com/api/hr163/position/queryPage"
_PAGE_SIZE = 100


class NeteaseAdapter(BaseAdapter):
    company = "netease"
    hosts = ("hr.163.com",)

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        data = self._request_json(
            method="POST",
            url=_LIST_URL,
            payload={"currentPage": page, "pageSize": _PAGE_SIZE},
            headers={"Origin": "https://hr.163.com", "Content-Type": "application/json"},
        )
        if data.get("code") != 200:
            return [], False
        payload = data.get("data")
        if not isinstance(payload, dict):
            return [], False
        raw_positions = payload.get("list")
        if not isinstance(raw_positions, list):
            return [], False
        records = [self.build_record(pos) for pos in raw_positions if isinstance(pos, dict)]
        pages = payload.get("pages", 0)
        has_more = bool(raw_positions) and page < int(pages or 0)
        return records, has_more

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        position_id = raw.get("id")
        return {
            "job_id": f"NE_{position_id}",
            "title": raw.get("name", ""),
            "company": "网易",
            "location": raw.get("workPlaceName", ""),
            "category": raw.get("recruitTypeName", ""),
            "job_type": raw.get("firstPostTypeName", ""),
            "special_program": raw.get("deptName", ""),
            "description": raw.get("requirement", ""),
            "apply_url": f"https://hr.163.com/position/detail.html?id={position_id}",
        }
