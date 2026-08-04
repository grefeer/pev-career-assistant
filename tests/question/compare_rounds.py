"""Compare two eval-round summaries side by side (status + key metrics).

Usage:
    python -m tests.question.compare_rounds tests/question/eval_results/round_1_summary.json tests/question/eval_results/round_2_summary.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


def load_summary(path: Path) -> dict[str, object]:
    with path.open(encoding="utf-8") as fh:
        data = json.load(fh)
    by_id = {item["id"]: item for item in data["questions"]}
    return {"status_counts": data["status_counts"], "by_id": by_id}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("round1", type=Path, help="round 1 summary JSON")
    parser.add_argument("round2", type=Path, help="round 2 summary JSON")
    args = parser.parse_args()
    r1 = load_summary(args.round1)
    r2 = load_summary(args.round2)

    print("== status distribution ==")
    for status in ("succeeded", "waiting_user", "failed"):
        print(f"  {status:12s} r1={r1['status_counts'].get(status, 0)}  r2={r2['status_counts'].get(status, 0)}")

    print("\n== per-question status/error ==")
    print(f"  {'id':6s} {'r1':13s} {'r2':13s}  {'r1_error':<28s} {'r2_error':<28s}  turns r1->r2")
    for qid in sorted(set(r1["by_id"]) | set(r2["by_id"])):
        a = r1["by_id"].get(qid)
        b = r2["by_id"].get(qid)
        if a is None or b is None:
            print(f"  {qid:6s} missing in one round")
            continue
        turns = f"{a['turns']}->{b['turns']}"
        print(
            f"  {qid:6s} {a['status']:13s} {b['status']:13s}  "
            f"{str(a.get('error_code') or ''):<28s} {str(b.get('error_code') or ''):<28s}  {turns}"
        )

    improved = [
        qid for qid in set(r1["by_id"]) & set(r2["by_id"])
        if _rank(r2["by_id"][qid]["status"]) > _rank(r1["by_id"][qid]["status"])
    ]
    regressed = [
        qid for qid in set(r1["by_id"]) & set(r2["by_id"])
        if _rank(r2["by_id"][qid]["status"]) < _rank(r1["by_id"][qid]["status"])
    ]
    print(f"\nimproved: {sorted(improved) or '-'}")
    print(f"regressed: {sorted(regressed) or '-'}")


def _rank(status: str) -> int:
    return {"succeeded": 2, "waiting_user": 1, "failed": 0}.get(status, 0)


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
