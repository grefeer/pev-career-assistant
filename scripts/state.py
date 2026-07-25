#!/usr/bin/env python3
"""state.py — Manage the incremental processing state for the job-discovery skill.

Reads/writes `output/state.json`, the master index that tracks which URLs have
been processed and whether they need re-extraction.

Usage:
  # Initialize state with source sheet config
  python scripts/state.py init

  # Check if a URL needs processing (returns exit code 0=skip, 1=needs_extraction, 2=error)
  python scripts/state.py check "<url>" "<update_time>"

  # Mark a URL as processed
  python scripts/state.py mark "<content_hash>" "<url>" "<update_time>" \
    --file-id "fGOTkFoVohnQ" --sheet-id "t00i2h" --company "小鹏集团" \
    --record-id "rec_abc" --candidates 9

  # List all processed entries
  python scripts/state.py list

  # Show URLs that need re-extraction (diff against state)
  python scripts/state.py diff <tasks.jsonl>
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

STATE_PATH = Path("output/state.json")
DEFAULT_SOURCE_SHEETS: dict[str, Any] = {
    "fGOTkFoVohnQ": {
        "title": "27届提前批秋招信息汇总（持续更新）",
        "sheets": ["t00i2h", "tbVCvT"],
    },
    "czGbCooFQHwb": {
        "title": "27届校招秋招实习内推合集（欢迎大家分享！）",
        "sheets": ["tZW9Ng", "BB08J2"],
    },
}


def _load_state() -> dict[str, Any]:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"source_sheets": {}, "processed": {}}


def _save_state(state: dict[str, Any]) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def cmd_init() -> None:
    """Initialize state.json with source sheet config."""
    state = _load_state()
    for file_id, config in DEFAULT_SOURCE_SHEETS.items():
        if file_id not in state["source_sheets"]:
            state["source_sheets"][file_id] = {**config, "last_scanned": None}
    _save_state(state)
    print(json.dumps({"status": "initialized", "sheets": list(state["source_sheets"].keys())}))


def cmd_check(url: str, update_time: str) -> int:
    """Check if a URL+update_time combo has been processed. Exit 0=skip, 1=extract."""
    state = _load_state()
    processed = state.get("processed", {})

    # Search for this URL in processed entries
    for content_hash, entry in processed.items():
        if entry.get("url") == url:
            if entry.get("last_update_time") == update_time:
                print(json.dumps({
                    "action": "skip",
                    "reason": "update_time unchanged",
                    "content_hash": content_hash,
                    "candidates_count": entry.get("candidates_count", 0),
                }))
                return 0
            else:
                print(json.dumps({
                    "action": "extract",
                    "reason": "update_time changed",
                    "old_update_time": entry.get("last_update_time"),
                    "new_update_time": update_time,
                }))
                return 1

    # URL not found at all
    print(json.dumps({"action": "extract", "reason": "new URL"}))
    return 1


def cmd_mark(
    content_hash: str,
    url: str,
    update_time: str,
    file_id: str,
    sheet_id: str,
    company: str = "",
    record_ids: str = "",
    candidates_count: int = 0,
) -> None:
    """Mark a URL as processed after successful extraction."""
    state = _load_state()
    now = datetime.now(timezone.utc).isoformat()

    # Update source sheet scan time
    if file_id in state.get("source_sheets", {}):
        state["source_sheets"][file_id]["last_scanned"] = now

    rec_ids = [r.strip() for r in record_ids.split(",") if r.strip()]

    # Merge record_ids if this content_hash already exists
    existing = state["processed"].get(content_hash, {})
    merged_rec_ids = list(set((existing.get("record_ids") or []) + rec_ids))

    state["processed"][content_hash] = {
        "url": url,
        "source_file_id": file_id,
        "source_sheet_id": sheet_id,
        "record_ids": merged_rec_ids,
        "last_update_time": update_time,
        "company": company,
        "extracted_at": now,
        "candidates_count": candidates_count or existing.get("candidates_count", 0),
    }
    _save_state(state)
    print(json.dumps({"status": "marked", "content_hash": content_hash}))


def cmd_list() -> None:
    """List all processed entries."""
    state = _load_state()
    processed = state.get("processed", {})
    entries = []
    for ch, entry in processed.items():
        entries.append({
            "content_hash": ch,
            "url": entry.get("url", ""),
            "company": entry.get("company", ""),
            "candidates": entry.get("candidates_count", 0),
            "extracted_at": entry.get("extracted_at", ""),
        })
    print(json.dumps({"total": len(entries), "entries": entries}, ensure_ascii=False))


def cmd_diff(tasks_file: str) -> None:
    """Compare tasks.jsonl against state to find URLs needing extraction."""
    state = _load_state()
    processed = state.get("processed", {})
    url_map: dict[str, dict] = {}
    for ch, entry in processed.items():
        url_map[entry.get("url", "")] = entry

    tasks_path = Path(tasks_file)
    if not tasks_path.exists():
        print(json.dumps({"error": f"File not found: {tasks_file}"}))
        sys.exit(2)

    to_skip: list[dict] = []
    to_extract: list[dict] = []

    for line in tasks_path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue

        url = rec.get("url") or rec.get("apply_url") or ""
        update_time = str(rec.get("update_time") or rec.get("更新时间") or "")
        company = rec.get("company") or rec.get("company_name") or rec.get("企业名称") or ""
        record_id = rec.get("record_id") or ""

        entry = url_map.get(url)
        if entry and entry.get("last_update_time") == update_time:
            to_skip.append({"url": url, "company": company, "content_hash": entry.get("content_hash", "")})
        else:
            reason = "update_time changed" if entry else "new URL"
            to_extract.append({"url": url, "company": company, "record_id": record_id, "reason": reason})

    print(json.dumps({
        "skip": len(to_skip),
        "extract": len(to_extract),
        "to_extract": to_extract,
    }, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser(description="Manage incremental processing state")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="Initialize state.json with source sheet config")

    p_check = sub.add_parser("check", help="Check if URL needs processing")
    p_check.add_argument("url", help="Career page URL")
    p_check.add_argument("update_time", help="Last update timestamp from smartsheet")

    p_mark = sub.add_parser("mark", help="Mark URL as processed")
    p_mark.add_argument("content_hash", help="SHA-256 content hash of page text")
    p_mark.add_argument("url", help="Career page URL")
    p_mark.add_argument("update_time", help="Last update timestamp")
    p_mark.add_argument("--file-id", required=True, help="Source smartsheet file_id")
    p_mark.add_argument("--sheet-id", required=True, help="Source smartsheet sheet_id")
    p_mark.add_argument("--company", default="", help="Company name")
    p_mark.add_argument("--record-id", default="", help="Comma-separated record_ids")
    p_mark.add_argument("--candidates", type=int, default=0, help="Number of candidates extracted")

    sub.add_parser("list", help="List all processed entries")

    p_diff = sub.add_parser("diff", help="Diff tasks.jsonl against state")
    p_diff.add_argument("tasks_file", help="Path to tasks.jsonl")

    args = parser.parse_args()

    if args.command == "init":
        cmd_init()
    elif args.command == "check":
        sys.exit(cmd_check(args.url, args.update_time))
    elif args.command == "mark":
        cmd_mark(
            args.content_hash, args.url, args.update_time,
            args.file_id, args.sheet_id,
            company=args.company,
            record_ids=args.record_id,
            candidates_count=args.candidates,
        )
    elif args.command == "list":
        cmd_list()
    elif args.command == "diff":
        cmd_diff(args.tasks_file)


if __name__ == "__main__":
    main()
