"""Deterministic diagnostic (NO LLM): how many job titles are actually in the
captured rendered text for a career-site list page, and how many does the
title-only extractor recover?

Isolates the under-extraction root cause:
  - if the rendered text only has ~5 titles  -> capture problem (lazy-load/scroll)
  - if the text has all 21 but the filter keeps ~5 -> filter problem (suffix list)

Usage:
  PYTHONUTF8=1 PYTHONIOENCODING=utf-8 ./.venv/Scripts/python.exe \
      tests/manual/_diag_deeproute_titles.py
"""
from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _extract_title_only_candidates,
    extract_rendered_job_evidence,
)
from backend.app.services.job_discovery.normalization.jd_normalizer import (  # noqa: E402
    normalize_title,
)

# Usage: argv[1]=url [argv[2]=slug]
_DEFAULT_URL = "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home"
URL = sys.argv[1] if len(sys.argv) > 1 else _DEFAULT_URL
SLUG = sys.argv[2] if len(sys.argv) > 2 else "default"

print(f"=== baseline extract_rendered_job_evidence({URL}) ===", flush=True)
raw = extract_rendered_job_evidence(URL)
data = json.loads(raw)
pages = data.get("evidence_pages", [])
print(f"\nevidence_pages: {len(pages)}", flush=True)
for i, p in enumerate(pages):
    et = p.get("evidence_type")
    src = (p.get("metadata") or {}).get("source")
    txt = p.get("text_excerpt", "")
    print(f"  [{i}] type={et} source={src} url={p.get('url','')[:70]} text_len={len(txt)}", flush=True)

print("\n=== per page_text evidence: title-only extraction ===", flush=True)
all_titles: list[str] = []
for i, p in enumerate(pages):
    if p.get("evidence_type") != "page_text":
        continue
    txt = p.get("text_excerpt", "")
    ref = {"url": p.get("url", ""), "content_hash": p.get("content_hash", "")}
    cands = _extract_title_only_candidates(txt, p.get("url", ""), ref)
    titles = [c.title for c in cands]
    all_titles.extend(titles)
    print(f"\n  page[{i}] source={(p.get('metadata') or {}).get('source')} "
          f"-> {len(titles)} titles:", flush=True)
    for t in titles:
        print(f"      {t!r}", flush=True)
    # show first 60 non-empty lines of the rendered text so we can eyeball
    # how many real job titles are present
    print(f"  --- first 40 non-empty lines of rendered text (page[{i}]) ---", flush=True)
    lines = [ln.strip() for ln in txt.splitlines() if ln.strip()]
    for ln in lines[:40]:
        print(f"      {ln[:60]!r}", flush=True)

print(f"\n=== ALL extracted titles (with dups): {len(all_titles)} ===", flush=True)
normed = [normalize_title(t) for t in all_titles]
unique = set(normed)
print(f"unique normalized titles: {len(unique)}", flush=True)
for t in sorted(unique):
    print(f"    {t!r}", flush=True)

# Persist a compact JSON summary for cross-URL comparison.
_out = Path(__file__).resolve().parent / f"_diag_titles_{SLUG}.json"
_out.write_text(json.dumps({
    "url": URL, "evidence_pages": len(pages),
    "all_titles": all_titles, "unique_count": len(unique),
    "unique_titles": sorted(unique),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nwrote {_out}", flush=True)
