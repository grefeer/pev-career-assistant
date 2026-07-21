"""Live smoke test: Discovery Supervisor Agent x 4 public URLs.

Mirrors test_job_discovery_live_four_url_smoke.py but exercises
build_discovery_supervisor_agent() instead of run_web_navigation(),
so we validate the FULL pipeline:
  triage -> web_navigation -> (wechat_parse | OCR) -> JD extraction
  -> evidence verification -> candidate packaging

Usage:
  $env:FLAGS_use_onednn = '0'
  .\\.venv\\Scripts\\python.exe tests\\manual\\test_supervisor_four_url_smoke.py
"""

from __future__ import annotations

# -- MUST be set before any paddle/paddleocr import --
import os as _os
if "FLAGS_use_onednn" not in _os.environ:
    _os.environ["FLAGS_use_onednn"] = "0"

import hashlib
import json
import os
import sys
import time
import traceback
from dataclasses import asdict
from pathlib import Path
from typing import Any

# Ensure project root is on sys.path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain_core.messages import HumanMessage

from backend.app.config import Settings, _literal_tencent_dotenv_values
from backend.app.services.job_discovery.deepagents_runner import (
    build_discovery_supervisor_agent,
)
from backend.app.services.job_discovery.schemas import DiscoveryTaskInput
from backend.app.services.job_mappers import BUILTIN_SOURCES, extract_discovery_urls
from backend.app.services.tencent_smartsheet import TencentRecord, TencentSmartsheetGateway

# -- Constants ----------------------------------------------------------------

SOURCE_KEYS = ("tencent-27-referrals", "tencent-intern-referrals")
MAIN_PROJECT_DOTENV = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
MAX_PAGES = 5           # keep smoke runs fast
RECURSION_LIMIT = 100   # enough for SPA recruitment pages (was 50)


# -- Env setup ----------------------------------------------------------------

def _bootstrap_env() -> None:
    """Load required keys from .env into os.environ so agent tools find them."""
    if not MAIN_PROJECT_DOTENV.exists():
        return
    try:
        from dotenv import dotenv_values
        vals = dotenv_values(MAIN_PROJECT_DOTENV, interpolate=False)
        # Only set if not already in the process environment
        for key in ("READGZH_API_KEY",):
            if key not in os.environ and key in vals and vals[key]:
                os.environ[key] = vals[key]
                print(f"  [env] Loaded {key} from .env")
    except ImportError:
        pass  # python-dotenv not installed; keys must be set externally


_bootstrap_env()


# -- Helpers ------------------------------------------------------------------


def _live_tencent_token() -> str | None:
    values = _literal_tencent_dotenv_values(MAIN_PROJECT_DOTENV)
    return (
        os.environ.get("TEST_TENCENT_DOCS_TOKEN")
        or os.environ.get("TENCENT_DOCS_TOKEN")
        or values.get("test_tencent_docs_token")
        or values.get("tencent_docs_token")
    )


def _source_definition(source_key: str):
    for source in BUILTIN_SOURCES:
        if source.source_key == source_key:
            return source
    raise AssertionError(f"unknown source: {source_key}")


def _select_two_records_with_urls(
    gateway: TencentSmartsheetGateway,
    source_key: str,
) -> list[TencentRecord]:
    source = _source_definition(source_key)
    selected: list[TencentRecord] = []
    offset = 0
    while len(selected) < 2:
        page = gateway.list_records(
            source.file_id, source.sheet_id, offset=offset, limit=10
        )
        for record in page.records:
            if extract_discovery_urls(record, source_key):
                selected.append(record)
            if len(selected) == 2:
                break
        if len(selected) == 2 or not page.has_more:
            break
        offset = page.next_offset
    assert len(selected) == 2, f"{source_key} did not expose two URL records"
    return selected


def _field_text(record: TencentRecord, name: str) -> str:
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


def _field_values_for_task(record: TencentRecord) -> list[dict[str, Any]]:
    return record.field_values  # already list[dict]


def smoke_settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=300,
        job_discovery_max_pages_per_task=MAX_PAGES,
        job_discovery_ocr_enabled=True,
    )


# -- Result parsing -----------------------------------------------------------


def _parse_agent_result(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse deepagents output into structured fields.

    Tries: structured_response -> direct dict -> last message JSON.
    """
    status = "unknown"
    block_reason = None
    evidence: list[dict] = []
    candidates: list[dict] = []
    summary_text = ""

    # Strategy 1: structured_response from deepagents
    sr = raw.get("structured_response")
    if hasattr(sr, "model_dump"):
        sr = sr.model_dump()
    if isinstance(sr, dict) and "status" in sr:
        return sr  # trust the structured output

    # Strategy 2: direct dict
    if isinstance(raw, dict) and "status" in raw:
        return raw

    # Strategy 3: parse last message content as JSON
    messages = raw.get("messages", [])
    if messages:
        last = messages[-1]
        content = last.content if hasattr(last, "content") else str(last)
        if isinstance(content, str):
            try:
                parsed = json.loads(content)
                if isinstance(parsed, dict) and "status" in parsed:
                    return parsed
            except json.JSONDecodeError:
                pass

    # Strategy 4: scan ALL messages for JSON with status key (not just last)
    if messages:
        for msg in reversed(messages):
            content = msg.content if hasattr(msg, "content") else str(msg)
            if isinstance(content, str):
                try:
                    parsed = json.loads(content)
                    if isinstance(parsed, dict) and "status" in parsed:
                        return parsed
                except (json.JSONDecodeError, TypeError):
                    continue

    # Strategy 5: scan messages for tool output containing candidate/evidence data
    if messages:
        evidence_found = []
        candidates_found = []
        for msg in messages:
            content = msg.content if hasattr(msg, "content") else str(msg)
            if not isinstance(content, str):
                continue
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            if any(k in item for k in ("evidence_type", "content_hash")):
                                evidence_found.append(item)
                            if any(k in item for k in ("company_name", "title")) and "idempotency_key" not in item:
                                candidates_found.append(item)
            except (json.JSONDecodeError, TypeError):
                pass
        if candidates_found or evidence_found:
            return {
                "status": "succeeded" if candidates_found else "partial_success",
                "evidence": evidence_found,
                "candidates": candidates_found,
                "summary": f"Recovered from tool outputs: {len(evidence_found)} evidence, {len(candidates_found)} candidates",
            }

    # Fallback
    return {"status": "unknown", "evidence": [], "candidates": [], "summary": ""}


# -- Run One URL --------------------------------------------------------------


def run_supervisor_for_url(
    *,
    source_key: str,
    record: TencentRecord,
    url: str,
    settings: Settings,
    index: int,
) -> dict[str, Any]:
    url_hash = hashlib.sha256(url.encode()).hexdigest()[:16]
    task_input = DiscoveryTaskInput(
        source_id=source_key,
        raw_record_id=record.record_id,
        external_record_id=record.record_id,
        source_key=source_key,
        source_url=url,
        url_hash=url_hash,
        record_fields=_field_values_for_task(record),
    )

    agent = build_discovery_supervisor_agent(settings=settings)

    msg_content = json.dumps(asdict(task_input), ensure_ascii=False)
    agent_input = {"messages": [HumanMessage(content=msg_content)]}

    t0 = time.monotonic()
    print(f"\n{'='*70}")
    print(f"  [{index}/4] {source_key}")
    print(f"  URL: {url}")
    print(f"  URL type: {'WeChat' if 'mp.weixin.qq.com' in url else 'Career site'}")
    company = _field_text(record, "公司名称") or _field_text(record, "企业名称")
    title = _field_text(record, "招聘岗位")
    if company:
        print(f"  Record: {company} — {title}")
    print(f"{'='*70}")
    sys.stdout.flush()

    raw = agent.invoke(agent_input, config={"recursion_limit": RECURSION_LIMIT})
    elapsed = time.monotonic() - t0

    parsed = _parse_agent_result(raw)
    status = parsed.get("status", "unknown")
    block_reason = parsed.get("block_reason")
    evidence = parsed.get("evidence") or []
    candidates = parsed.get("candidates") or []
    summary_text = parsed.get("summary") or ""

    # -- Print results --
    print(f"\n  Elapsed: {elapsed:.0f}s")
    print(f"  Status:   {status}")
    if block_reason:
        print(f"  Block:    {block_reason}")
    print(f"  Evidence: {len(evidence)} pages")
    for i, ev in enumerate(evidence[:5]):
        et = ev.get("evidence_type", "?")
        ev_title = ev.get("title", "")[:80]
        ev_url = ev.get("url", "")[:80]
        print(f"      [{i}] type={et}  title={ev_title}")
        if ev_url and ev_url != url:
            print(f"          url={ev_url}")
    if len(evidence) > 5:
        print(f"      ... and {len(evidence) - 5} more")

    print(f"  Candidates: {len(candidates)}")
    for j, c in enumerate(candidates[:5]):
        c_company = c.get("company_name") or "?"
        c_title = c.get("title") or "?"
        locs = c.get("locations") or []
        conf = c.get("confidence", 0)
        print(f"      [{j}] {c_company} — {c_title}")
        if locs:
            print(f"          location: {', '.join(locs)}")
        print(f"          confidence: {conf:.2f}")
    if len(candidates) > 5:
        print(f"      ... and {len(candidates) - 5} more")

    if summary_text:
        print(f"  Summary:  {summary_text[:200]}")

    return {
        "index": index,
        "source_key": source_key,
        "record_id": record.record_id,
        "url": url,
        "url_type": "wechat" if "mp.weixin.qq.com" in url else "career_site",
        "elapsed_sec": round(elapsed, 1),
        "status": status,
        "block_reason": block_reason,
        "evidence_count": len(evidence),
        "candidate_count": len(candidates),
        "evidence_types": [
            e.get("evidence_type") for e in evidence[:10] if isinstance(e, dict)
        ],
        "candidate_titles": [
            f"{c.get('company_name', '?')} — {c.get('title', '?')}"
            for c in candidates[:10]
            if isinstance(c, dict)
        ],
        "summary": summary_text[:500] if summary_text else "",
        "error": raw.get("error") if isinstance(raw, dict) else None,
        "raw_status": status,
    }


def _error_summary(
    index: int, source_key: str, url: str, exc: Exception
) -> dict[str, Any]:
    return {
        "index": index,
        "source_key": source_key,
        "url": url,
        "url_type": "wechat" if "mp.weixin.qq.com" in url else "career_site",
        "elapsed_sec": 0,
        "status": "failed",
        "block_reason": None,
        "evidence_count": 0,
        "candidate_count": 0,
        "evidence_types": [],
        "candidate_titles": [],
        "summary": "",
        "error": str(exc),
    }


# -- Main ---------------------------------------------------------------------


def main() -> None:
    token = _live_tencent_token()
    if not token:
        print("No Tencent Docs token found. Set TENCENT_DOCS_TOKEN.")
        sys.exit(1)

    print(f"READGZH_API_KEY: {'set' if os.environ.get('READGZH_API_KEY') else 'MISSING'}")
    print(f"TENCENT_DOCS_TOKEN: {'set' if token else 'MISSING'}")

    gateway = TencentSmartsheetGateway(token=token)
    settings = smoke_settings()
    results: list[dict[str, Any]] = []

    idx = 0
    for source_key in SOURCE_KEYS:
        records = _select_two_records_with_urls(gateway, source_key)
        for record in records:
            urls = extract_discovery_urls(record, source_key)
            url = urls[0]
            idx += 1
            try:
                summary = run_supervisor_for_url(
                    source_key=source_key,
                    record=record,
                    url=url,
                    settings=settings,
                    index=idx,
                )
            except Exception as exc:
                print(f"\n  Agent crashed: {exc}")
                traceback.print_exc()
                summary = _error_summary(idx, source_key, url, exc)
            results.append(summary)

    # -- Final summary --
    print(f"\n\n{'='*70}")
    print(f"  FINAL SUMMARY — {len(results)} URLs")
    print(f"{'='*70}")
    print(f"\n  {'#':<3} {'Source':<28} {'Type':<12} {'Status':<20} {'Evidence':>8} {'Cand.':>6} {'Time':>6}")
    print(f"  {'-'*3} {'-'*28} {'-'*12} {'-'*20} {'-'*8} {'-'*6} {'-'*6}")
    for r in results:
        elapsed = r.get("elapsed_sec", 0)
        print(
            f"  {r['index']:<3} {r['source_key']:<28} {r.get('url_type', '?'):<12} "
            f"{r['status']:<20} {r['evidence_count']:>8} {r['candidate_count']:>6} "
            f"{elapsed:>5.0f}s"
        )

    # -- Detailed output --
    print(f"\n\n{'-'*70}")
    for r in results:
        print(f"\n  [{r['index']}] {r['url']}")
        print(f"      Status:       {r['status']}")
        print(f"      Evidence:     {r['evidence_count']} ({r.get('evidence_types', [])})")
        print(f"      Candidates:   {r['candidate_count']}")
        for ct in r.get("candidate_titles", []):
            print(f"        * {ct}")
        if r.get("block_reason"):
            print(f"      Block reason: {r['block_reason']}")
        if r.get("error"):
            print(f"      Error:        {r['error']}")

    # -- Assertions --
    wechat_results = [r for r in results if r.get("url_type") == "wechat"]
    career_results = [r for r in results if r.get("url_type") == "career_site"]

    print(f"\n\n  WeChat URLs:     {len(wechat_results)}")
    print(f"  Career site URLs: {len(career_results)}")

    wechat_ok = sum(1 for r in wechat_results if r["evidence_count"] > 0)
    print(f"  WeChat success:  {wechat_ok}/{len(wechat_results)} (evidence found)")

    career_ok = sum(1 for r in career_results if r["evidence_count"] > 0)
    print(f"  Career success:  {career_ok}/{len(career_results)} (evidence found)")

    total_candidates = sum(r["candidate_count"] for r in results)
    print(f"  Total candidates extracted: {total_candidates}")

    # Write detailed JSON
    out_path = Path(__file__).parent / "_supervisor_smoke_output.json"
    out_path.write_text(
        json.dumps(results, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"\n  Full results -> {out_path}")


if __name__ == "__main__":
    main()
