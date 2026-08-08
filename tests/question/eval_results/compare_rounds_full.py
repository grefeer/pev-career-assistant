"""Compare two eval rounds across ALL ids (Q/C/R), including chain links.

``compare_rounds.py`` only globs ``Q*.json``; this version reads every
``*.json`` under each round dir and aggregates:

- singles: per-doc result.status
- chains: per-chain result.status AND per-link result.status (links carry the
  actionable signal: a chain is waiting_user when any link is)

Usage::

    python -m tests.question.eval_results.compare_rounds_full \\
        --base tests/question/eval_results/redesign_full_20260808_v2 \\
        --new tests/question/eval_results/phase_e_round_1

Exit code 0; prints a per-question diff table plus totals. If any file that
exists in one round is missing in the other it is listed as MISSING and does
not enter the totals (that would be a broken comparison, surfaced loudly).
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
from collections import Counter

sys.stdout.reconfigure(encoding="utf-8")


def _rows(round_dir: pathlib.Path) -> dict[str, dict]:
    """id -> {"doc_status": str, "link_statuses": list[str]}."""
    out: dict[str, dict] = {}
    for path in sorted(round_dir.glob("*.json")):
        if path.stem == "manifest":
            continue
        doc = json.loads(path.read_text(encoding="utf-8"))
        links = doc.get("links") or []
        out[path.stem] = {
            "doc_status": doc["result"]["status"],
            "link_statuses": [link["result"]["status"] for link in links],
        }
    return out


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", required=True)
    parser.add_argument("--new", required=True)
    args = parser.parse_args()

    base = _rows(pathlib.Path(args.base))
    new = _rows(pathlib.Path(args.new))

    ids = sorted(set(base) | set(new))
    missing = [(i, "base" if i not in base else "new") for i in ids if i not in base or i not in new]
    if missing:
        print(f"WARNING: {len(missing)} id(s) present in only one round (excluded):")
        for qid, side in missing:
            print(f"  - {qid} (missing in {side})")

    common = [i for i in ids if i in base and i in new]

    def doc_status(rows: dict[str, dict], qid: str) -> str:
        return rows[qid]["doc_status"]

    def link_statuses(rows: dict[str, dict], qid: str) -> list[str]:
        return rows[qid]["link_statuses"]

    def agg(rows: dict[str, dict]) -> tuple[Counter, Counter]:
        single_docs = [qid for qid in common if not link_statuses(rows, qid)]
        chain_links = [s for qid in common for s in link_statuses(rows, qid)]
        return (
            Counter(doc_status(rows, qid) for qid in single_docs),
            Counter(chain_links),
        )

    base_singles, base_links = agg(base)
    new_singles, new_links = agg(new)
    base_chains = Counter(doc_status(base, qid) for qid in common if link_statuses(base, qid))
    new_chains = Counter(doc_status(new, qid) for qid in common if link_statuses(new, qid))

    print("== totals ==")
    print(f"base  singles: {dict(base_singles)}   chains: {dict(base_chains)}   links: {dict(base_links)}")
    print(f"new   singles: {dict(new_singles)}   chains: {dict(new_chains)}   links: {dict(new_links)}")
    print(f"delta singles succeeded: {new_singles['succeeded'] - base_singles['succeeded']:+d}  "
          f"links succeeded: {new_links['succeeded'] - base_links['succeeded']:+d}")

    print("\n== per-question diff (doc status) ==")
    for qid in common:
        old_s, new_s = doc_status(base, qid), doc_status(new, qid)
        if old_s != new_s:
            marker = "**" if new_s == "failed" else "!!" if old_s == "succeeded" and new_s != "succeeded" else "++"
            print(f"{marker} {qid}: {old_s} -> {new_s}")

    print("\n== per-link diff (chain links) ==")
    for qid in common:
        old_links, new_links_l = link_statuses(base, qid), link_statuses(new, qid)
        if old_links != new_links_l:
            print(f"{qid}:")
            for idx, (o, n) in enumerate(zip(old_links, new_links_l)):
                if o != n:
                    print(f"    link[{idx}] {o} -> {n}")
            if len(old_links) != len(new_links_l):
                print(f"    link count {len(old_links)} -> {len(new_links_l)}")

    print("\n== summary ==")
    improved = [qid for qid in common if doc_status(base, qid) != doc_status(new, qid)
                and (doc_status(new, qid) == "succeeded" or (doc_status(base, qid) == "waiting_user" and doc_status(new, qid) != "failed"))]
    worsened = [qid for qid in common if doc_status(base, qid) != doc_status(new, qid)
                and doc_status(base, qid) == "succeeded" and doc_status(new, qid) != "succeeded"]
    print(f"improved docs: {len(improved)} {improved}")
    print(f"worsened docs: {len(worsened)} {worsened}")


if __name__ == "__main__":
    main()
