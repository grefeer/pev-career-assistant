"""Discover non-Alibaba career site URLs and WeChat URLs from Tencent Smartsheet.

Usage:
  .\.venv\Scripts\python.exe tests\manual\_discover_urls.py
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.config import _literal_tencent_dotenv_values
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentSmartsheetGateway

MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")


def _get_token() -> str | None:
    values = _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV)
    return (
        os.environ.get("TEST_TENCENT_DOCS_TOKEN")
        or os.environ.get("TENCENT_DOCS_TOKEN")
        or values.get("test_tencent_docs_token")
        or values.get("tencent_docs_token")
    )


def _field_text(record, name: str) -> str:
    for field in record.field_values:
        if field.get("field") != name:
            continue
        parts: list[str] = []
        for key in ("text_value", "option_value", "url_value"):
            block = field.get(key) or {}
            for item in block.get("items", []) or []:
                text = item.get("text") or item.get("link")
                if text:
                    parts.append(text)
        return "、".join(parts)
    return ""


def main() -> None:
    token = _get_token()
    if not token:
        print("No Tencent Docs token found.")
        sys.exit(1)

    gateway = TencentSmartsheetGateway(token=token)

    all_entries: list[dict] = []

    for source in BUILTIN_SOURCES:
        print(f"\n{'='*80}")
        print(f"Source: {source.source_key} — {source.name}")
        print(f"{'='*80}")

        offset = 0
        found = 0
        while True:
            page = gateway.list_records(source.file_id, source.sheet_id, offset=offset, limit=50)
            for record in page.records:
                urls = extract_discovery_urls(record, source.source_key)
                if not urls:
                    continue

                company = _field_text(record, "公司名称") or _field_text(record, "企业名称")
                title = _field_text(record, "招聘岗位")
                dept = _field_text(record, "部门")

                for url in urls:
                    if "alibaba.com" in url or "talent.alibaba" in url:
                        continue  # skip Alibaba

                    url_type = "wechat" if "mp.weixin.qq.com" in url else "career_site"
                    domain = url.split("/")[2] if "//" in url else "unknown"

                    entry = {
                        "source_key": source.source_key,
                        "record_id": record.record_id,
                        "company": company,
                        "title": title,
                        "dept": dept,
                        "url": url,
                        "url_type": url_type,
                        "domain": domain,
                    }
                    all_entries.append(entry)
                    found += 1
                    print(f"\n  [{found}] {url_type.upper():12s} | {company or '???'} | {title or '???'}")
                    print(f"       URL: {url[:120]}")
                    print(f"       Domain: {domain}")

            if not page.has_more:
                break
            offset = page.next_offset

    # Summary
    wechat_urls = [e for e in all_entries if e["url_type"] == "wechat"]
    career_urls = [e for e in all_entries if e["url_type"] == "career_site"]

    print(f"\n\n{'='*80}")
    print(f"SUMMARY — {len(all_entries)} non-Alibaba URLs found")
    print(f"  WeChat:       {len(wechat_urls)}")
    print(f"  Career sites: {len(career_urls)}")

    # Unique domains for career sites
    domains: dict[str, int] = {}
    for e in career_urls:
        domains[e["domain"]] = domains.get(e["domain"], 0) + 1
    print(f"\n  Career site domains:")
    for d, c in sorted(domains.items(), key=lambda x: -x[1]):
        print(f"    {d}: {c} URLs")

    # Print all entries for easy reference
    print(f"\n{'='*80}")
    print("ALL URLS (for copy-paste):")
    print(f"{'='*80}")
    for i, e in enumerate(all_entries):
        print(f"\n[{i+1}] [{e['url_type']}] {e['company']} — {e['title']}")
        print(f"    URL: {e['url']}")

    # Print recommended test set
    print(f"\n\n{'='*80}")
    print("RECOMMENDED TEST SET (4 career + 2 wechat):")
    print(f"{'='*80}")

    # Pick first 2 wechats
    for i, w in enumerate(wechat_urls[:2]):
        print(f"\nWeChat {i+1}: {w['company']} — {w['title']}")
        print(f"  URL: {w['url']}")

    # Pick 4 diverse career sites (different domains preferred)
    seen_domains: set[str] = set()
    career_picks: list[dict] = []
    for c in career_urls:
        if c["domain"] not in seen_domains or len(career_picks) < 4:
            if len(career_picks) >= 4:
                break
            career_picks.append(c)
            seen_domains.add(c["domain"])

    for i, c in enumerate(career_picks[:4]):
        print(f"\nCareer {i+1}: {c['company']} — {c['title']}")
        print(f"  URL: {c['url']}")
        print(f"  Domain: {c['domain']}")


if __name__ == "__main__":
    main()
