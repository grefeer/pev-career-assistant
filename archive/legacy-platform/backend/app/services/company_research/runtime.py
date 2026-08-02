"""Deterministic company-research runtime.

Reuses the job-discovery skill plumbing (the per-task cloned
``SkillArtifactStore`` and the allowlisted ``run_skill_script`` tool) but with
its own orchestrator: browse one URL -> parse openings from the rendered text
-> assemble a company profile.  No LLM, no pagination, no coverage gate.  A
blocked page surfaces as ``needs_manual_review`` (security gate #2) and is
never bypassed.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any
from uuid import uuid4

from backend.app.config import Settings
from backend.app.services.company_research.schemas import CompanyResearchResult
from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore
from backend.app.services.job_discovery.skill_runtime import (
    SkillToolPolicy,
    _detail_evidence_candidates,
    _public_json_candidates,
    _read_json,
    _script_tool,
)
from backend.app.services.job_discovery.skill_spec import get_skill_spec

#: Browse ``block_reason`` values (as written by ``scripts/browse.py``) mapped to
#: the domain ``CompanyResearchBlockReason`` vocabulary.
_BLOCK_STATUS_TO_REASON: dict[str, str] = {
    "anti_bot": "anti_bot",
    "login_required": "login_required",
    "captcha": "captcha",
}

_DESCRIPTION_EXCERPT_CHARS = 1000


class CompanyResearchRuntime:
    """Run one company-research task in an isolated bundled Skill clone.

    The only executable capability is the allowlisted ``browse`` script; all
    extraction is deterministic Python over the page text the browser wrote.
    """

    def __init__(
        self,
        settings: Settings,
        *,
        artifact_root: Path | None = None,
        object_store: Any | None = None,
    ) -> None:
        self.settings = settings
        self.spec = get_skill_spec("company-research")
        configured_root = getattr(
            settings, "company_research_artifact_root", "var/company-research-skill"
        )
        self.artifact_root = artifact_root or Path(configured_root)
        self.object_store = object_store

    def run(
        self,
        *,
        report_id: str,
        company_name: str,
        source_url: str,
    ) -> CompanyResearchResult:
        store = SkillArtifactStore(
            report_id,
            self.artifact_root,
            run_id=uuid4().hex,
            skill_name=self.spec.name,
            skill_source=self.spec.source_path,
        )
        skill_dir = store.prepare()
        try:
            self._invoke(source_url=source_url, skill_dir=skill_dir)
        except Exception as exc:
            # Agent-infrastructure failures are not research data.
            return CompanyResearchResult(
                status="failed",
                summary=f"company-research runtime failed: {type(exc).__name__}",
                last_error=str(exc)[:500],
            )
        result = self._result_from_artifacts(
            company_name=company_name, source_url=source_url, skill_dir=skill_dir,
        )
        if self.object_store is None:
            return result
        try:
            store.publish_evidence(self.object_store)
        except Exception:
            return CompanyResearchResult(
                status="needs_manual_review",
                block_reason="artifact_error",
                summary="company-research artifacts could not be retained in encrypted object storage",
                profile=result.profile,
                openings=result.openings,
                evidence_refs=result.evidence_refs,
            )
        return result

    def _invoke(self, *, source_url: str, skill_dir: Path) -> None:
        """Fetch one page through the allowlisted browse script."""
        tool = _script_tool(
            skill_dir,
            SkillToolPolicy(
                max_browse_calls=1,
                max_pages=1,
                script_timeout_seconds=60,
            ),
            allowed_scripts=self.spec.allowed_scripts,
        )
        tool.invoke(
            {
                "script": "browse",
                "cli_args": (
                    f"{source_url} --out output/evidence --wait 800 --max-pages 1"
                ),
            }
        )

    def _result_from_artifacts(
        self,
        *,
        company_name: str,
        source_url: str,
        skill_dir: Path,
    ) -> CompanyResearchResult:
        metadata = _read_json(skill_dir / "output" / "evidence" / "browse_metadata.json") or {}
        status = str(metadata.get("status") or "")
        pages = sorted(
            (skill_dir / "output" / "evidence" / "pages").glob("page_*.txt")
        )
        evidence_refs = _evidence_refs(pages)

        if status == "blocked":
            reason = _BLOCK_STATUS_TO_REASON.get(
                str(metadata.get("block_reason") or "anti_bot"), "anti_bot"
            )
            return CompanyResearchResult(
                status="needs_manual_review",
                block_reason=reason,
                summary="verification wall detected; needs manual review",
                evidence_refs=evidence_refs,
            )
        if status == "error":
            err = metadata.get("error")
            return CompanyResearchResult(
                status="failed",
                summary="company-research page fetch failed",
                last_error=str(err)[:500] if err else None,
                evidence_refs=evidence_refs,
            )
        if not pages:
            return CompanyResearchResult(
                status="needs_manual_review",
                block_reason="no_evidence",
                summary="page yielded no parseable evidence",
            )

        openings: list[dict[str, Any]] = []
        for page in pages:
            openings.extend(_public_json_candidates(page, company_name))
            openings.extend(_detail_evidence_candidates(page, company_name))

        page_text = pages[0].read_text(encoding="utf-8", errors="replace")
        locations = sorted(
            {
                loc
                for opening in openings
                for loc in (opening.get("locations") or [])
                if isinstance(loc, str)
            }
        )
        profile = {
            "company_name": company_name,
            "description": page_text[:_DESCRIPTION_EXCERPT_CHARS].strip() or None,
            "industries": [],
            "locations": locations,
            "opening_count": len(openings),
        }
        summary = f"researched {company_name}; found {len(openings)} opening(s)"
        return CompanyResearchResult(
            status="succeeded",
            profile=profile,
            openings=openings,
            evidence_refs=evidence_refs,
            summary=summary,
        )


def _evidence_refs(pages: list[Path]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for page in pages:
        refs.append(
            {
                "evidence_type": "page_text",
                "content_hash": hashlib.sha256(page.read_bytes()).hexdigest(),
                "relative_path": f"output/evidence/pages/{page.name}",
            }
        )
    return refs
