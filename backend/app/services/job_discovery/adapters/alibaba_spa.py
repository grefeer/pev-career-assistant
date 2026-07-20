"""Alibaba SPA Adapter -- direct XHR API extraction for campus recruitment pages."""
from __future__ import annotations

import fnmatch
import json

from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, DiscoveryTaskInput
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


class AlibabaSPAAdapter(DomainAdapter):
    """Fast-path adapter for Alibaba campus recruitment SPAs.

    Calls the internal JSON search API directly, bypassing browser navigation
    and LLM planning entirely (~8 seconds vs 3-5 minutes).
    """

    url_pattern: str = "campus*.alibaba.com/*"

    def execute(
        self,
        task: DiscoveryTaskInput,
        strategy: "StrategyRecord",
        trajectory: TrajectoryBuffer,
    ) -> DiscoveryRunResult:
        """Execute via direct API call. Delegates to the existing
        _fetch_alibaba_search_api and _generic_position_evidence_from_payload
        functions in deepagents_runner.

        In a follow-up, those functions should be extracted to a shared utility module.
        """
        from backend.app.services.job_discovery.deepagents_runner import (
            _fetch_alibaba_search_api,
            _generic_position_evidence_from_payload,
            verify_evidence,
            package_candidates,
        )

        trajectory.record_step("alibaba_api_fetch", "ok", {"url": task.source_url})

        try:
            search_data = _fetch_alibaba_search_api(task.source_url)
            evidence = _generic_position_evidence_from_payload(search_data, task.source_url)
            trajectory.record_step("alibaba_evidence_extract", "ok", {},
                                   {"evidence_count": len(evidence)})
        except Exception as exc:
            trajectory.record_step("alibaba_api_fetch", "failed", {"url": task.source_url},
                                   error=exc)
            return DiscoveryRunResult(
                status="failed",
                summary=f"Alibaba SPA adapter API call failed: {exc}",
            )

        if not evidence:
            return DiscoveryRunResult(
                status="failed",
                summary="No job evidence found in Alibaba search API response",
            )

        # Use deterministic tools for JD extraction / verification / packaging
        evidence_json = json.dumps(evidence, ensure_ascii=False)
        candidates_json = _run_extraction(task.source_url, evidence)
        verified_json = verify_evidence(candidates_json, evidence_json)
        evidence_hash = evidence[0].get("content_hash", task.url_hash) if evidence else task.url_hash
        packaged_json = package_candidates(verified_json, evidence_hash, task.source_key)
        candidates = json.loads(packaged_json)

        trajectory.record_step("extract_verify_package", "ok", {},
                               {"candidate_count": len(candidates)})

        return DiscoveryRunResult(
            status="succeeded",
            evidence=evidence,
            candidates=candidates,
            summary=f"Alibaba SPA adapter extracted {len(candidates)} candidate(s)",
        )

    def validate(self, url: str) -> bool:
        """Check if URL matches the Alibaba campus pattern.

        Tries matching the full URL and the scheme-stripped netloc+path,
        consistent with StrategyRouter._pattern_matches behaviour.
        """
        return fnmatch.fnmatch(url, self.url_pattern) or fnmatch.fnmatch(
            url.replace("https://", "").replace("http://", ""), self.url_pattern
        )


def _run_extraction(url: str, evidence: list) -> str:
    """Run JD extraction using tool functions."""
    from backend.app.services.job_discovery.deepagents_runner import extract_jd_candidates

    text_parts = []
    for ev in evidence:
        excerpt = ev.get("text_excerpt", "") if isinstance(ev, dict) else getattr(ev, "text_excerpt", "")
        if excerpt:
            text_parts.append(excerpt)
    combined_text = "\n\n".join(text_parts)
    return extract_jd_candidates(combined_text, url)
