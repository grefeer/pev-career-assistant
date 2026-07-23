"""Deterministic count (NO LLM): run extract_rendered_job_evidence then the
real _extract_and_verify_candidates_from_evidence, and report unique candidate
count. This is the deterministic baseline the supervisor merges the Web Nav
Agent into - a close proxy for the supervisor count without LLM cost.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _extract_and_verify_candidates_from_evidence,
    extract_rendered_job_evidence,
)
from backend.app.services.job_discovery.normalization.jd_normalizer import (  # noqa: E402
    normalize_title,
)

URLS = {
    "deeproute": ("https://app.mokahr.com/campus-recruitment/deeproute/145894#/home", 21),
    "xiaomi": ("https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", 151),
    "pdd": ("https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x", 22),
}

slug = sys.argv[1] if len(sys.argv) > 1 else "deeproute"
if slug not in URLS:
    print(f"unknown slug {slug!r}; choose from {list(URLS)}"); sys.exit(1)
url, real = URLS[slug]

print(f"=== {slug}: {url} (real={real}) ===", flush=True)
raw = extract_rendered_job_evidence(url)
data = json.loads(raw)
if data.get("error"):
    print("ERROR:", data["error"], flush=True); sys.exit(1)
pages = data.get("evidence_pages", [])
# breakdown by evidence_type + source
from collections import Counter
bt = Counter(p.get("evidence_type") for p in pages)
bs = Counter((p.get("metadata") or {}).get("source") for p in pages)
meta = data.get("metadata") or {}
print(f"evidence_pages={len(pages)} by_type={dict(bt)} by_source={dict(bs)}", flush=True)
print(f"metadata={meta}", flush=True)

# sample evidence to identify noise source
print("\n--- first 5 job_detail_json evidence (title + text_excerpt head) ---", flush=True)
n = 0
for p in pages:
    if p.get("evidence_type") != "job_detail_json":
        continue
    print(f"  title={p.get('title')!r}", flush=True)
    print(f"    text_head={(p.get('text_excerpt') or '')[:120]!r}", flush=True)
    n += 1
    if n >= 5:
        break
print("\n--- first 3 page_text evidence first 8 non-empty lines ---", flush=True)
n = 0
for p in pages:
    if p.get("evidence_type") != "page_text":
        continue
    src = (p.get("metadata") or {}).get("source")
    print(f"  [src={src}] url={p.get('url','')[:60]}", flush=True)
    lines = [ln.strip() for ln in (p.get("text_excerpt") or "").splitlines() if ln.strip()]
    for ln in lines[:8]:
        print(f"      {ln[:70]!r}", flush=True)
    n += 1
    if n >= 3:
        break


candidates, ev_hash = _extract_and_verify_candidates_from_evidence(pages, url)
print(f"\nraw candidates: {len(candidates)}", flush=True)

def _title(c):
    if isinstance(c, dict):
        return c.get("title") or ""
    return getattr(c, "title", "") or ""

titles = [_title(c) for c in candidates]
normed = [normalize_title(t) for t in titles]
unique = set(normed)
print(f"unique normalized titles: {len(unique)} (real={real})", flush=True)
dupes = [t for t in normed if normed.count(t) > 1]
print(f"duplicate titles (by normalized): {len(set(dupes))} distinct duped", flush=True)

out = Path(__file__).resolve().parent / f"_diag_count_{slug}.json"
out.write_text(json.dumps({
    "url": url, "real": real,
    "evidence_pages": len(pages),
    "by_type": dict(bt), "by_source": dict(bs), "metadata": meta,
    "raw_candidates": len(candidates),
    "unique_count": len(unique),
    "unique_titles": sorted(unique),
}, ensure_ascii=False, indent=2), encoding="utf-8")
print(f"\nwrote {out}", flush=True)
print("--- all unique titles ---", flush=True)
for t in sorted(unique):
    print(f"  {t!r}", flush=True)
