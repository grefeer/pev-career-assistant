"""Confirm Option-B pagination fired and measure the deterministic tool count.

No LLM, no supervisor: calls extract_rendered_job_evidence directly, then the
deterministic _extract_and_verify_candidates_from_evidence. Prints per-URL:
  - list_pages metadata (did pagination advance to page 2+?)
  - evidence breakdown by source (rendered_dom / list_page / detail_page / xhr)
  - deterministic tool candidate count + sample titles

This isolates the pagination mechanism from supervisor-convergence variance.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _extract_and_verify_candidates_from_evidence,
    extract_rendered_job_evidence,
)

URLS = [
    ("小米", "https://xiaomi.jobs.f.mioffice.cn/s/m5DjWDrhl_g"),
    ("拼多多", "https://careers.pddglobalhr.com/campus/grad?t=N5ch0DXEtA"),
]


def classify(ev: dict) -> str:
    src = (ev.get("metadata") or {}).get("source")
    if src:
        return src
    return "xhr_or_payload"


for name, url in URLS:
    print(f"\n{'=' * 70}\n{name}: {url}\n{'=' * 70}")
    raw = extract_rendered_job_evidence(url)
    try:
        data = json.loads(raw)
    except Exception:
        print("NON-JSON result:", raw[:500])
        continue
    if data.get("error"):
        print("ERROR:", data["error"])
    ev_pages = data.get("evidence_pages", [])
    meta = data.get("metadata") or {}
    print(f"list_pages = {meta.get('list_pages')}  | total evidence = {len(ev_pages)}")
    by_src: dict[str, int] = {}
    for ev in ev_pages:
        by_src[classify(ev)] = by_src.get(classify(ev), 0) + 1
    print("evidence by source:", by_src)
    # Deterministic extraction (no LLM)
    cands, ev_hash = _extract_and_verify_candidates_from_evidence(ev_pages, url)
    print(f"deterministic tool candidates = {len(cands)}  (evidence_hash={ev_hash[:12]})")
    for c in cands[:8]:
        d = c if isinstance(c, dict) else dict(c)
        title = d.get("title") or ""
        loc = (d.get("locations") or ["?"])[0] if d.get("locations") else "?"
        print(f"   - {title[:50]!r}  [{loc}]")
    if len(cands) > 8:
        print(f"   ... +{len(cands) - 8} more")

print("\nDONE.")
