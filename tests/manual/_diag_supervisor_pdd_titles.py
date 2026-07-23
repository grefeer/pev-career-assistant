"""Diagnostic: run the PATH C supervisor on pdd and dump ALL candidate titles.

The supervisor summary says "22 positions" but the candidate list has 36.
This dumps every candidate title + the evidence page text excerpts so we can
see whether the 14 extras are location variants, false positives, or
genuinely distinct postings.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

os.environ.setdefault("PYTHONUTF8", "1")
os.environ.setdefault("RUN_SUPERVISOR_BASELINE", "1")

# Reuse the integration test's supervisor runner so the diagnostic exercises
# the exact same PATH C code path (build -> invoke -> parse -> enforce).
from tests.integration.job_discovery.test_supervisor_baseline_real_urls import (  # noqa: E402
    _build_job_discovery_llm,
    _run_supervisor,
    _settings,
    _setup_db,
)
from backend.app.services.job_discovery.normalization.jd_normalizer import (  # noqa: E402
    normalize_title,
)

URL = "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x"
COMPANY = "拼多多"


def main() -> None:
    settings = _settings()
    db = _setup_db()
    llm = _build_job_discovery_llm(settings=settings)
    result = _run_supervisor(COMPANY, URL, settings, db, llm)

    cands = result.candidates or []
    titles = [getattr(c, "title", None) for c in cands]
    norm_titles = sorted({normalize_title(t or "") for t in titles})

    print(f"\nstatus={result.status} count={len(cands)} unique_norm={len(norm_titles)}")
    print(f"\n--- all {len(cands)} candidate titles (raw) ---")
    for i, t in enumerate(titles, 1):
        print(f"  {i:2d}. {t}")
    print(f"\n--- {len(norm_titles)} unique normalized titles ---")
    for i, t in enumerate(norm_titles, 1):
        print(f"  {i:2d}. {t}")

    print(f"\nsummary: {(result.summary or '')[:500]}")

    out = {
        "count": len(cands),
        "unique_norm": len(norm_titles),
        "titles": titles,
        "unique_normalized": norm_titles,
        "summary": result.summary or "",
    }
    out_path = Path(__file__).parent / "_diag_supervisor_pdd_titles.json"
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path}")


if __name__ == "__main__":
    main()
