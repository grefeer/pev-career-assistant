"""Summarize a redesigned-eval results directory (independent + chains).

Usage:
    python -m tests.question.redesign.summarize tests/question/eval_results/retry_6
"""

from __future__ import annotations

import json
import pathlib
import sys

from collections import Counter


def summarize_dir(out_dir: pathlib.Path) -> None:
    rows: list[tuple[str, str, str, float, int, str]] = []
    chain_links: dict[str, int] = {}
    for path in sorted(out_dir.glob("*.json")):
        doc = json.loads(path.read_text(encoding="utf-8"))
        qid = doc["id"]
        if "links" in doc:
            chain_links[qid] = sum(
                1 for link in doc["links"] if link["result"]["status"] == "succeeded"
            )
            result = doc["result"]
            rows.append(
                (
                    qid,
                    result["status"],
                    result["error_code"] or "",
                    doc["wall_seconds"],
                    sum(len(link["turns"]) for link in doc["links"]),
                    (result.get("summary") or "")[:80].replace("\n", " "),
                )
            )
        else:
            result = doc["result"]
            rows.append(
                (
                    qid,
                    result["status"],
                    result["error_code"] or "",
                    doc["wall_seconds"],
                    len(doc["turns"]),
                    (result.get("summary") or "")[:80].replace("\n", " "),
                )
            )
    counts = Counter(status for _, status, *_ in rows)
    print("== status ==")
    for status in ("succeeded", "waiting_user", "failed"):
        print(f"  {status:12s} {counts.get(status, 0)}")
    print(f"  {'total':12s} {len(rows)}")
    if chain_links:
        print("== chains ==")
        for qid in sorted(chain_links):
            print(f"  {qid}: {chain_links[qid]} links succeeded")
    print("== detail ==")
    for qid, status, error, wall, turns, summary in rows:
        print(f"  {qid:8s} {status:<12s} err={error:<22s} wall={wall:6.0f}s turns={turns:>3}  {summary}")


if __name__ == "__main__":
    summarize_dir(pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "tests/question/eval_results/retry_6"))
