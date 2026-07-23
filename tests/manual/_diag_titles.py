"""Diagnostic: dump all tool-extracted candidate titles for a career URL.

Runs the deterministic baseline (extract_rendered_job_evidence) + candidate
extraction directly (no supervisor LLM) and prints every extracted title,
so we can see which titles are real jobs vs false positives (banners, category
headers) that the title-only fallback over-extracts.

Usage: python tests/manual/_diag_titles.py <url>
  python tests/manual/_diag_titles.py "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x"
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


def main() -> None:
    url = sys.argv[1] if len(sys.argv) > 1 else \
        "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x"
    print(f"\nextract_rendered_job_evidence({url!r}) ...", flush=True)
    t0 = time.monotonic()
    raw = extract_rendered_job_evidence(url)
    elapsed = time.monotonic() - t0
    parsed = json.loads(raw) if isinstance(raw, str) else raw
    pages = parsed.get("evidence_pages") or [] if isinstance(parsed, dict) else []
    by_type: dict[str, int] = {}
    for p in pages:
        if isinstance(p, dict):
            t = p.get("evidence_type") or "unknown"
            by_type[t] = by_type.get(t, 0) + 1
    print(f"  elapsed={elapsed:.1f}s evidence_pages={len(pages)} by_type={by_type}", flush=True)
    print(f"  error={parsed.get('error')!r}", flush=True)

    cands, _ = _extract_and_verify_candidates_from_evidence(pages, url)
    print(f"\n  candidates={len(cands)}", flush=True)
    for i, c in enumerate(cands, 1):
        title = c.get("title") if isinstance(c, dict) else getattr(c, "title", None)
        resp = (c.get("responsibilities") if isinstance(c, dict) else getattr(c, "responsibilities", "")) or ""
        loc = (c.get("locations") if isinstance(c, dict) else getattr(c, "locations", [])) or []
        flag = " [TITLE-ONLY]" if not resp else ""
        print(f"  {i:3d}. {title!r:50s} loc={loc}{flag}", flush=True)

    # Dump page_text evidence so we can see the raw structure (which lines are
    # real job titles vs section/category headers like "管培生" / "区域业务管培生").
    print(f"\n--- page_text evidence excerpts ---", flush=True)
    for pi, p in enumerate(pages, 1):
        if not isinstance(p, dict) or p.get("evidence_type") != "page_text":
            continue
        txt = (p.get("text_excerpt") or "")[:2000]
        print(f"\n[page {pi}] url={p.get('url')}\n{txt}", flush=True)


if __name__ == "__main__":
    main()
