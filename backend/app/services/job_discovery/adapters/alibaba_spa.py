"""Alibaba SPA Adapter -- browser-based extraction for campus recruitment pages."""
from __future__ import annotations

import fnmatch
import hashlib
import json

from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class AlibabaSPAAdapter(DomainAdapter):
    """Fast-path adapter for Alibaba campus recruitment SPAs.

    Navigates the SPA with Playwright, captures XHR responses and rendered
    DOM text, builds evidence from whichever source yields data, then runs
    the deterministic JD-extraction pipeline (~15-30 s instead of 2-5 min
    via the full Supervisor).
    """

    url_pattern: str = "campus*.alibaba.com/*"

    def execute(
        self,
        task: DiscoveryTaskInput,
        strategy: "StrategyRecord",
        trajectory: TrajectoryBuffer,
    ) -> DiscoveryRunResult:
        """Execute via Playwright browser navigation + deterministic pipeline."""
        from backend.app.services.job_discovery.deepagents_runner import (
            _fetch_alibaba_search_api,
            _generic_position_evidence_from_payload,
            verify_evidence,
            package_candidates,
        )

        # Step 1: browser fetch
        trajectory.record_step("alibaba_browser_fetch", "ok", {"url": task.source_url})
        fetch_result = _fetch_alibaba_search_api(task.source_url)

        # Step 2: build evidence from XHR payloads or DOM text
        evidence: list[dict] = []
        payloads = fetch_result.get("payloads") or []
        for payload in payloads:
            evidence.extend(
                _generic_position_evidence_from_payload(payload, task.source_url)
            )

        xhr_evidence_count = len(evidence)

        # DOM-text fallback
        if not evidence:
            page_text = (fetch_result.get("page_text") or "").strip()
            page_title = fetch_result.get("page_title", "")
            if page_text and len(page_text) >= 100:
                content_hash = hashlib.sha256(page_text.encode("utf-8")).hexdigest()
                evidence.append({
                    "evidence_type": "rendered_page",
                    "url": task.source_url,
                    "title": page_title or "Alibaba Campus Position",
                    "content_hash": content_hash,
                    "text_excerpt": page_text[:5000],
                    "metadata": {
                        "source": "playwright_dom",
                        "page_title": page_title,
                        "xhr_payloads_captured": len(payloads),
                        "response_urls": fetch_result.get("response_urls", [])[:5],
                    },
                })

        trajectory.record_step(
            "alibaba_evidence_extract", "ok", {},
            {
                "evidence_count": len(evidence),
                "xhr_evidence_count": xhr_evidence_count,
                "dom_fallback": xhr_evidence_count == 0 and len(evidence) > 0,
                "payloads_captured": len(payloads),
            },
        )

        if not evidence:
            raise RuntimeError(
                "Alibaba SPA adapter: browser navigation succeeded but no "
                "job evidence found in XHR responses or DOM text. "
                f"XHR payloads captured: {len(payloads)}, "
                f"page text length: {len(fetch_result.get('page_text', ''))}"
            )

        # Step 3: extract, verify, package
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        candidates_json = _run_extraction(task.source_url, evidence)

        # Inject evidence_refs for verify_evidence
        candidates_data = json.loads(candidates_json)
        evidence_hashes = [ev.get("content_hash", "") for ev in evidence if ev.get("content_hash")]
        for c in candidates_data:
            if not c.get("evidence_refs"):
                c["evidence_refs"] = evidence_hashes[:5] if evidence_hashes else ["alibaba_spa_evidence"]
        candidates_json = json.dumps(candidates_data, ensure_ascii=False)

        verified_json = verify_evidence(candidates_json, evidence_json)
        evidence_hash = (
            evidence[0].get("content_hash", task.url_hash)
            if evidence else task.url_hash
        )
        packaged_json = package_candidates(verified_json, evidence_hash, task.source_key)
        candidates = json.loads(packaged_json)

        trajectory.record_step(
            "extract_verify_package", "ok", {},
            {"candidate_count": len(candidates)},
        )

        return DiscoveryRunResult(
            status="succeeded",
            evidence=evidence,
            candidates=candidates,
            summary=f"Alibaba SPA adapter extracted {len(candidates)} candidate(s)",
        )

    def validate(self, url: str) -> bool:
        return fnmatch.fnmatch(url, self.url_pattern) or fnmatch.fnmatch(
            url.replace("https://", "").replace("http://", ""), self.url_pattern
        )


def _run_extraction(url: str, evidence: list) -> str:
    """Run JD extraction per evidence item, then deduplicate."""
    from backend.app.services.job_discovery.deepagents_runner import extract_jd_candidates

    all_candidates: list[dict] = []
    seen_keys: set[str] = set()

    for ev in evidence:
        excerpt = ev.get("text_excerpt", "") if isinstance(ev, dict) else getattr(ev, "text_excerpt", "")
        if not excerpt:
            continue
        raw = extract_jd_candidates(excerpt, url)
        try:
            import json as _json
            parsed = _json.loads(raw)
        except Exception:
            continue
        if not isinstance(parsed, list):
            continue
        for c in parsed:
            if not isinstance(c, dict):
                continue
            title = (c.get("title") or c.get("职位名称") or "").strip()
            locs = c.get("locations") or []
            loc_key = "|".join(sorted(locs)) if locs else ""
            dedup_key = f"{title}||{loc_key}"
            if dedup_key in seen_keys:
                continue
            seen_keys.add(dedup_key)
            all_candidates.append(c)

    import json as _json
    return _json.dumps(all_candidates, ensure_ascii=False)
