"""10-URL parity gate: job-discovery workflow subgraph vs B-mode baseline.

Gate (spec §7): the workflow tool's per-URL success count and extracted
candidate count must not be WORSE than the recorded B-mode baseline
(``tests/manual/_skill_ten_url_*.json``, produced by
``run_skill_ten_url_eval.py``).  Exits 0 on parity, 1 on regression.

Requires: real stack (docker compose up, LLM key) + RUN_DEEPAGENTS_PARITY=1.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# ruff: noqa: E402  (sys.path bootstrap must precede project imports)
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.services.career_skills.job_discovery import (  # noqa: E402
    enable_playwright_fallback,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs import (  # noqa: E402
    build_job_discovery_tool,
)

_BASELINE_DIR = Path(__file__).resolve().parent
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


def main() -> int:
    import os

    if os.environ.get("RUN_DEEPAGENTS_PARITY") != "1":
        print(SKIP_MSG)
        return 0
    # the workflow's default fetch is the requests fast-path; the Playwright
    # render fallback is OFF unless the live caller toggles it (same pattern
    # as eval_runner.py + main.py runtime assembly).  Brief iteration (a):
    # wire the fallback so SPA shells actually render.
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
