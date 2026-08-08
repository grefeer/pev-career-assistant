"""10-URL parity gate: job-discovery workflow subgraph vs B-mode baseline.

Gate (spec §7): the workflow tool's per-URL success count and extracted
candidate count must not be WORSE than the recorded B-mode baseline
(``tests/manual/_skill_ten_url_*.json``, produced by
``run_skill_ten_url_eval.py``).  Exits 0 on parity, 1 on regression.

Requires: real stack (docker compose up, LLM key) + RUN_DEEPAGENTS_PARITY=1.

Report mode (``--report [--log-dir <dir>]``): renders the parity table
from the rows of ``parity_run*.log`` files — never hardcoded (Task 6
review minor a).  Each row is classified from that log's own content
(PASSED / FAILED / SKIP / ERROR (crashed) / INCOMPLETE), so a crashed run
shows as a crash rather than a fabricated gate result.

The heavy runtime imports (``enable_playwright_fallback`` /
``build_job_discovery_tool``) live ONLY inside ``main()``'s env-guarded
branch: importing this module must not load them (Task 6 review minor b
— the SKIP path's claim "no imports of live-only modules" holds).
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

# ruff: noqa: E402  (sys.path bootstrap must precede project imports)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

_BASELINE_DIR = Path(__file__).resolve().parent
_LOG_DIR = Path(__file__).resolve().parent
_URLS = [
    "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home",
    "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x",
    "https://xiaopeng.jobs.feishu.cn/campus/position/list",
    "https://recruit.inovance.com/#/jobs",
    "https://job.xiaohongshu.com/campus/position",
    "https://talent.didiglobal.com/campus/",
    "https://hr.163.com/campus.html",
    "https://talent.baidu.com/jobs/campus/list",
    "https://jobs.bytedance.com/campus/position",
    "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY",
]

SKIP_MSG = "RUN_DEEPAGENTS_PARITY=1 required (live LLM + Playwright)"

_SUMMARY_RE = re.compile(
    r"baseline success=(\d+) candidates=(\d+) \| ours success=(\d+) candidates=(\d+)"
)
_COVERAGE_RE = re.compile(r"coverage=(\{[^\n]*\})")


def _unwrap(payload: str) -> dict:
    """Decode the ToolObservation JSON the workflow tool returns.

    The tool (``skill_graphs/build_job_discovery_tool``) returns a
    ``ToolObservation`` serialized as JSON with the workflow results NESTED
    under ``output`` (``{"tool_name": ..., "status": ..., "output":
    {"per_url_results": [...], "candidates": [...], "coverage": {...}}}``);
    a missing ``output`` means the workflow itself failed (folded into the
    observation, never raised), which the gate treats as a failed run.
    """
    result = json.loads(payload)
    return result.get("output") or {}


def _parse_log_row(text: str) -> dict:
    """Derive one report row from a log file's content (never hardcoded).

    Verdict classes: ``PASSED`` (PARITY PASSED line), ``FAILED`` (PARITY
    FAILED line + reason), ``SKIP`` (SKIP_MSG, no gate numbers), ``ERROR
    (crashed)`` (a traceback — run 1 of Task 6 was exactly this, while the
    old report claimed gate numbers for it), ``INCOMPLETE (no verdict)``
    (a log whose run never reached a verdict, e.g. killed mid-run).
    """
    if "Traceback (most recent call last):" in text:
        verdict = "ERROR (crashed)"
        tail = [ln for ln in text.strip().splitlines() if ln.strip()]
        note = tail[-1] if tail else ""
    elif "PARITY PASSED" in text:
        verdict, note = "PASSED", ""
    elif "PARITY FAILED" in text:
        verdict = "FAILED"
        note = next(
            (ln for ln in text.splitlines() if "PARITY FAILED" in ln), ""
        )
    elif SKIP_MSG in text:
        verdict, note = "SKIP", ""
    else:
        verdict, note = "INCOMPLETE (no verdict)", ""
    match = _SUMMARY_RE.search(text)
    numbers = tuple(map(int, match.groups())) if match else None
    coverage = _parse_coverage(text)
    return {
        "verdict": verdict,
        "baseline": numbers[:2] if numbers else None,
        "ours": numbers[2:] if numbers else None,
        "verified": (coverage or {}).get("verified"),
        "note": note,
    }


def _parse_coverage(text: str) -> dict | None:
    """Extract the coverage dict from the log line (Python-repr form)."""
    match = _COVERAGE_RE.search(text)
    if not match:
        return None
    try:
        value = ast.literal_eval(match.group(1))
    except (SyntaxError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def render_parity_report(log_dir: Path) -> str:
    """Render the parity table from ``parity_run*.log`` rows (log-driven).

    Every row is parsed from the log file's own content — no hardcoded
    numbers, no fabricated gate results (Task 6 review minor a).
    """
    lines = ["# DeepAgents parity report (log-driven)", ""]
    paths = sorted(log_dir.glob("parity_run*.log"))
    if not paths:
        lines.append(f"(no parity_run*.log files in {log_dir})")
        return "\n".join(lines)
    lines.append(
        "| run | verdict | baseline succ/cand | ours succ/cand | "
        "coverage verified | note |"
    )
    lines.append("|-----|---------|--------------------|-----------------|-------------------|------|")
    for path in paths:
        row = _parse_log_row(path.read_text(encoding="utf-8", errors="replace"))
        baseline = (
            "{0}/{1}".format(*row["baseline"]) if row["baseline"] else "–"
        )
        ours = "{0}/{1}".format(*row["ours"]) if row["ours"] else "–"
        verified = str(row["verified"]) if row["verified"] is not None else "–"
        note = row["note"].replace("|", "/").strip()[:80]
        lines.append(
            "| {0} | {1} | {2} | {3} | {4} | {5} |".format(
                path.name, row["verdict"], baseline, ours, verified, note
            )
        )
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--report",
        action="store_true",
        help="render the parity table from parity_run*.log rows",
    )
    parser.add_argument(
        "--log-dir",
        default=str(_LOG_DIR),
        help="log directory for --report (default: this script's directory)",
    )
    args = parser.parse_args(argv)

    if args.report:
        print(render_parity_report(Path(args.log_dir)))
        return 0

    import os

    if os.environ.get("RUN_DEEPAGENTS_PARITY") != "1":
        print(SKIP_MSG)
        return 0
    # heavy imports only on the live path — the SKIP path must never load
    # the runtime (the workflow's default fetch is the requests fast-path;
    # the Playwright render fallback is OFF unless the live caller toggles
    # it, same pattern as eval_runner.py + main.py runtime assembly)
    from backend.app.services.career_skills.job_discovery import (
        enable_playwright_fallback,
    )
    from backend.app.services.deepagents_runtime.tools.skill_graphs import (
        build_job_discovery_tool,
    )

    enable_playwright_fallback(True)
    tool = build_job_discovery_tool()
    output = _unwrap(tool.invoke({"payload": json.dumps(_URLS)}))
    per_url = output.get("per_url_results", [])
    candidates = output.get("candidates", [])
    coverage = output.get("coverage") or {}

    # the glob also matches _merged (JSON lists) and _summary ({"rows": [...]})
    # artifacts; only the per-URL record dicts carry status/unique_listing_count
    baseline_records: list[dict] = []
    for path in sorted(_BASELINE_DIR.glob("_skill_ten_url_*.json")):
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict):
            baseline_records.append(record)
    baseline_success = sum(
        1 for record in baseline_records if record.get("status") == "succeeded"
    )
    baseline_candidates = sum(
        record.get("unique_listing_count", 0) for record in baseline_records
    )
    our_success = sum(
        1 for entry in per_url if entry.get("status") == "succeeded"
    )
    print(
        f"baseline success={baseline_success} candidates={baseline_candidates} | "
        f"ours success={our_success} candidates={len(candidates)} coverage={coverage}"
    )
    if our_success < baseline_success or len(candidates) < baseline_candidates:
        print("PARITY FAILED: regression vs B-mode baseline")
        return 1
    if not coverage.get("verified", False):
        print("PARITY FAILED: coverage gate not verified")
        return 1
    print("PARITY PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
