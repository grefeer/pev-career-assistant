"""Stage-count diagnostic for xiaomi: how many candidates survive each stage
(extract -> verify -> dedup -> subsumption), split by full-JD vs title-only.
"""
from __future__ import annotations
import json
import sys
from pathlib import Path
_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))
from backend.app.services.job_discovery.deepagents_runner import (  # noqa: E402
    _extract_jd_candidates,
    _extract_title_only_candidates,
    _verify_evidence,
    extract_rendered_job_evidence,
    normalize_title,
    deduplicate_candidates,
)
from backend.app.services.job_discovery.schemas import PageEvidence  # noqa: E402

URL = "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY"
raw = extract_rendered_job_evidence(URL)
data = json.loads(raw)
pages = data.get("evidence_pages", [])

_EVI_FIELDS = {f.name for f in PageEvidence.__dataclass_fields__.values()}
evidence_objs = []
for page in pages:
    fields = {k: v for k, v in (page or {}).items() if k in _EVI_FIELDS}
    fields.setdefault("evidence_type", "page_text")
    evidence_objs.append(PageEvidence(**fields))

# Stage 1: extract per page
all_cands = []
xhr_full = 0
xhr_title_only = 0
page_text_full = 0
page_text_title_only = 0
for page, ev_obj in zip(pages, evidence_objs):
    text = (page or {}).get("text_excerpt") or ""
    if not text.strip():
        continue
    is_pt = ((page or {}).get("evidence_type") == "page_text") or str(ev_obj.evidence_type) == "page_text"
    extracted = _extract_jd_candidates(text, (page or {}).get("url") or URL)
    ref = {"url": ev_obj.url, "content_hash": ev_obj.content_hash, "evidence_type": ev_obj.evidence_type}
    has_detail = any((getattr(c, "responsibilities", "") or getattr(c, "requirements", "")) for c in extracted)
    keep_strict = (not is_pt) or has_detail
    if keep_strict:
        for cand in extracted:
            _t = cand.title or ""
            for _ch in "​‌‍﻿\t":
                _t = _t.replace(_ch, "")
            _t = _t.strip()
            if not _t:
                continue
            cand.title = _t
            cand.evidence_refs = [ref]
            all_cands.append(cand)
            if is_pt:
                page_text_full += 1
            else:
                xhr_full += 1
    if is_pt and not has_detail:
        t0 = len(all_cands)
        all_cands.extend(_extract_title_only_candidates(text, (page or {}).get("url") or URL, ref))
        page_text_title_only += len(all_cands) - t0

print(f"Stage1 extract: total={len(all_cands)}  xhr_full={xhr_full} page_text_full={page_text_full} page_text_title_only={page_text_title_only}", flush=True)

# Stage 2: verify
verified = _verify_evidence(all_cands, evidence_objs)
vf_full = sum(1 for c in verified if (getattr(c,"responsibilities","") or "").strip() or (getattr(c,"requirements","") or "").strip())
vf_to = len(verified) - vf_full
print(f"Stage2 verify: total={len(verified)}  full_jd={vf_full} title_only={vf_to}", flush=True)

# Stage 3: dedup
deduped = deduplicate_candidates(verified)
dd_full = sum(1 for c in deduped if (getattr(c,"responsibilities","") or "").strip() or (getattr(c,"requirements","") or "").strip())
dd_to = len(deduped) - dd_full
# how many full-JD merged? compare core_hash uniqueness
from backend.app.services.job_discovery.normalization.jd_normalizer import core_hash, normalize_company
hashes = {}
for c in deduped:
    if (getattr(c,"responsibilities","") or "").strip() or (getattr(c,"requirements","") or "").strip():
        h = core_hash(c.responsibilities, c.requirements)
        hashes.setdefault(h, 0)
        hashes[h] += 1
merged_groups = {h:n for h,n in hashes.items() if n > 1}
print(f"Stage3 dedup: total={len(deduped)}  full_jd={dd_full} title_only={dd_to}", flush=True)
print(f"  full-JD core_hash groups: {len(hashes)} unique hashes for {dd_full} full-JD candidates", flush=True)
print(f"  groups with >1 member (merged): {len(merged_groups)} (total extra collapsed: {sum(n-1 for n in merged_groups.values())})", flush=True)

# Find the PRE-dedup groups: which 151 full-JD candidates share a core_hash?
from collections import defaultdict
pre_groups = defaultdict(list)
for c in verified:
    if (getattr(c,"responsibilities","") or "").strip() or (getattr(c,"requirements","") or "").strip():
        h = core_hash(c.responsibilities, c.requirements)
        pre_groups[h].append(c)
print(f"\n=== PRE-dedup full-JD core_hash groups (size>1 = merged) ===", flush=True)
ng = 0
for h, members in pre_groups.items():
    if len(members) > 1:
        ng += 1
        titles = [normalize_title(getattr(m,"title","") or "") for m in members]
        print(f"  group({len(members)}): {titles}", flush=True)
        if ng >= 25:
            print("  ... (truncated)", flush=True)
            break
print(f"total merged groups: {sum(1 for m in pre_groups.values() if len(m)>1)}, jobs collapsed: {sum(len(m)-1 for m in pre_groups.values() if len(m)>1)}", flush=True)

