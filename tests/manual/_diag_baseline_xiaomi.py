"""Diagnostic: call extract_rendered_job_evidence directly on xiaomi.

Isolates whether the deterministic baseline (which paginates the career-site
list pages and captures job_detail_json XHR evidence) works on its own, or
whether it returns nothing / errors (which would explain run_web_navigation's
422-char failure result inside the supervisor).

Standalone (no LLM, no pytest). Run:
  python tests/manual/_diag_baseline_xiaomi.py
"""
# ruff: noqa: E402
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.services.job_discovery.deepagents_runner import (
    extract_rendered_job_evidence,
    _extract_and_verify_candidates_from_evidence,
)

URL = "https://xiaomi.jobs.f.mioffce.cn/s/kJVnd58xtWY"


def main() -> None:
    print(f"\nextract_rendered_job_evidence({URL!r}) ...", flush=True)
    t0 = time.monotonic()
    try:
        raw = extract_rendered_job_evidence(URL)
    except Exception as exc:  # noqa: BLE001
        print(f"  RAISED {type(exc).__name__}: {exc}", flush=True)
        return
    elapsed = time.monotonic() - t0
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(parsed, dict):
        print(f"  unexpected type: {type(parsed)}", flush=True)
        return
    pages = parsed.get("evidence_pages") or []
    by_type: dict[str, int] = {}
    for p in pages:
        if isinstance(p, dict):
            t = p.get("evidence_type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
    print(f"  elapsed={elapsed:.1f}s evidence_pages={len(pages)} by_type={by_type}", flush=True)
    print(f"  error={parsed.get('error')!r} page_count={parsed.get('page_count')}", flush=True)

    print("\n_extract_and_verify_candidates_from_evidence(...) ...", flush=True)
    t1 = time.monotonic()
    try:
        cands, ev_hash = _extract_and_verify_candidates_from_evidence(pages, URL)
    except Exception as exc:  # noqa: BLE001
        print(f"  RAISED {type(exc).__name__}: {exc}", flush=True)
        return
    print(f"  elapsed={time.monotonic()-t1:.1f}s candidates={len(cands)} ev_hash={ev_hash[:12]}",
          flush=True)
    titles = []
    for c in cands[:15]:
        titles.append(c.get("title") if isinstance(c, dict) else getattr(c, "title", None))
    print(f"  first 15 titles: {titles}", flush=True)


if __name__ == "__main__":
    main()
