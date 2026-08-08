"""Baidu public-JSON adapter: talent.baidu.com/httservice/getPostListNew.

Official unauthenticated listing endpoint (FindJobs job_crawler_v2.py
BaiduCrawler L431).  POST recruitType (SOCIAL/CAMPUS) x page; ``data.list``
items map to records with stable ``job_id`` = ``BD_<postId>``.

Paging spans two recruit types; each type pages independently until its list
is exhausted, then the adapter moves to the next type.  A fresh instance is
created per fetch (browse_fetch loads one adapter per URL), so the
``_type_*`` state below is per-execute and never shared.
"""
from __future__ import annotations

from typing import Any

from .base import BaseAdapter

_LIST_URL = "https://talent.baidu.com/httservice/getPostListNew"
_PAGE_SIZE = 50
_RECRUIT_TYPES = ("SOCIAL", "CAMPUS")


class BaiduAdapter(BaseAdapter):
    company = "baidu"
    hosts = ("talent.baidu.com",)

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._type_index = 0
        self._type_page = 0

    def fetch_page(self, page: int) -> tuple[list[dict[str, Any]], bool]:
        """One page of records; has_more until both recruit types are drained.

        ``has_more`` means "keep calling fetch_page" — it stays true when the
        current recruit type is exhausted but another type still awaits, so
        the outer execute loop does not stop at the SOCIAL/CAMPUS boundary.
        """
        while True:
            if self._type_index >= len(_RECRUIT_TYPES):
                return [], False
            recruit_type = _RECRUIT_TYPES[self._type_index]
            self._type_page += 1
            records, type_more = self._fetch_type_page(recruit_type, self._type_page)
            if records:
                another_type = self._type_index + 1 < len(_RECRUIT_TYPES)
                return records, type_more or another_type
            # Type yielded no records this page: move to the next type.
            self._type_index += 1
            self._type_page = 0

    def _fetch_type_page(
        self, recruit_type: str, type_page: int
    ) -> tuple[list[dict[str, Any]], bool]:
        data = self._request_json(
            method="POST",
            url=_LIST_URL,
            payload={"recruitType": recruit_type, "pageSize": _PAGE_SIZE, "curPage": type_page},
            headers={"Content-Type": "application/json"},
        )
        payload = data.get("data")
        if not isinstance(payload, dict):
            return [], False
        raw_posts = payload.get("list")
        if not isinstance(raw_posts, list) or not raw_posts:
            return [], False
        records = [self.build_record(post) for post in raw_posts if isinstance(post, dict)]
        total = payload.get("total", 0)
        has_more = type_page * _PAGE_SIZE < int(total or 0)
        return records, has_more

    def build_record(self, raw: dict[str, Any]) -> dict[str, Any]:
        post_id = raw.get("postId")
        description = _join_text(raw.get("workContent"), raw.get("serviceCondition"))
        return {
            "job_id": f"BD_{post_id}",
            "title": raw.get("name", ""),
            "company": "百度",
            "location": raw.get("workPlace", ""),
            "category": raw.get("recruitType", ""),
            "job_type": raw.get("serviceType", ""),
            "description": description,
            "apply_url": f"https://talent.baidu.com/jobs/detail/{post_id}",
        }


def _join_text(*parts: Any) -> str:
    return "\n".join(str(p) for p in parts if p)
