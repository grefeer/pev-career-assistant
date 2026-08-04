"""Merge per-question eval JSONs into a round summary (status table + aggregates).

Usage::

    python -m tests.question.merge_round --in-dir tests/question/eval_results/round_1 --out tests/question/eval_results/round_1_summary.json
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys

sys.stdout.reconfigure(encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--in-dir", required=True)
    parser.add_argument("--out", required=True)
    args = parser.parse_args()

    in_dir = pathlib.Path(args.in_dir)
    records = []
    for path in sorted(in_dir.glob("Q*.json")):
        records.append(json.loads(path.read_text(encoding="utf-8")))
    records.sort(key=lambda rec: rec["id"])

    summary = {
        "round": in_dir.parent.name if in_dir.parent.name.startswith("round") else in_dir.name,
        "total": len(records),
        "status_counts": {},
        "questions": [],
    }
    for rec in records:
        status = rec["result"]["status"]
        summary["status_counts"][status] = summary["status_counts"].get(status, 0) + 1
        summary["questions"].append(
            {
                "id": rec["id"],
                "status": status,
                "error_code": rec["result"]["error_code"],
                "seed_note": rec["seed_note"],
                "seeded": bool(rec["seeded_urls"]),
                "steps": [
                    {
                        "sequence": s["sequence"],
                        "skills": s["allowed_skills"],
                        "status": s["status"],
                    }
                    for s in rec["plan"]["steps"]
                ],
                "revisions": rec["plan"]["revisions"],
                "artifacts": [a["artifact_type"] for a in rec["artifacts"]],
                "tool_calls": {t["tool_name"]: t["succeeded"] for t in rec["tool_calls"]},
                "tool_failures": {
                    t["tool_name"]: {"failed": t["failed"], "error_codes": t["error_codes"]}
                    for t in rec["tool_calls"]
                    if t["failed"]
                },
                "wall_seconds": rec["wall_seconds"],
                "turns": len(rec["turns"]),
                "summary": (rec["result"]["summary"] or "")[:200],
            }
        )

    summary_path = pathlib.Path(args.out)
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    print(f"total={summary['total']} status={summary['status_counts']}")
    for q in summary["questions"]:
        tools = ",".join(q["tool_calls"])
        print(
            f"{q['id']}: {q['status']:<12} wall={q['wall_seconds']:5.1f}s "
            f"turns={q['turns']:>2} skills={[s['skills'] for s in q['steps']]} "
            f"artifacts={len(q['artifacts'])} tools=[{tools}]"
        )


if __name__ == "__main__":
    main()
