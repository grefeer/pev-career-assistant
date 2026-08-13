"""Reviewed business-Skill metadata used to constrain PEV planning/execution."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from backend.app.services.agent_runtime.error_policy import (
    ErrorPolicy,
    default_error_policy,
    merge_error_policies,
)
from backend.app.services.agent_runtime.skill_definition import (
    ArtifactPort,
    CompletionContract,
    SkillDefinition,
    SkillRegistry,
    VerificationPolicy,
)
from backend.app.services.agent_runtime.skill_package import discover_skill_packages
from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.agent_runtime.tool_registry import ToolRegistry

@dataclass(frozen=True)
class CareerSkillManifest:
    """A product-facing Skill boundary, distinct from low-level tool names."""

    name: str
    description: str
    requires_evidence: bool
    supports_user_data: bool


CAREER_SKILL_MANIFESTS: dict[str, CareerSkillManifest] = {
    "job-discovery": CareerSkillManifest(
        name="job-discovery",
        description="Collect and normalize evidence-backed public job descriptions.",
        requires_evidence=True,
        supports_user_data=False,
    ),
    "job-matching": CareerSkillManifest(
        name="job-matching",
        description="Rank jobs against confirmed profile facts and stated preferences.",
        requires_evidence=True,
        supports_user_data=True,
    ),
    "resume-tailoring": CareerSkillManifest(
        name="resume-tailoring",
        description="Produce fact-grounded resume diff operations for one job.",
        requires_evidence=True,
        supports_user_data=True,
    ),
    "career-planning": CareerSkillManifest(
        name="career-planning",
        description="Create JD-grounded search, preparation and interview plans.",
        requires_evidence=True,
        supports_user_data=True,
    ),
}


def get_career_skill_manifest(name: str) -> CareerSkillManifest | None:
    """Return only an explicitly reviewed business Skill."""
    return CAREER_SKILL_MANIFESTS.get(name)


_DISCOVERY_EVIDENCE_TOOLS = frozenset(
    {"fetch-public-job-pages", "fetch-public-job-page", "fetch-wechat-article"}
)
_DISCOVERY_VERIFIED_TOOLS = frozenset(
    {
        "search-public-job-pages",
        "query-career-sheet-records",
        "extract-observed-job-details",
        "extract-observed-job-details-batch",
    }
)
_DISCOVERY_DETAIL_TOOLS = frozenset(
    {"extract-observed-job-details", "extract-observed-job-details-batch"}
)


def _discovery_observation_is_valid(observation: ToolObservation) -> bool:
    """Require a page-backed JD/detail, not a search index, for completion."""
    if observation.tool_name in {"search-public-job-pages", "query-career-sheet-records"}:
        return False
    if observation.tool_name in _DISCOVERY_DETAIL_TOOLS:
        output = observation.output or {}
        candidates = output.get("candidates")
        return (
            isinstance(output.get("source_url"), str)
            and bool(output["source_url"])
            and isinstance(output.get("content_hash"), str)
            and bool(output["content_hash"])
            and isinstance(candidates, list)
            and any(isinstance(candidate, dict) for candidate in candidates)
            and output.get("source_quality") in {None, "jd_complete"}
        )
    if observation.tool_name not in _DISCOVERY_EVIDENCE_TOOLS:
        return False
    output = observation.output or {}
    raw_pages = output.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else [output]
    return any(
        isinstance(page, dict)
        and all(
            isinstance(page.get(key), str) and page[key]
            for key in ("source_url", "content_hash", "visible_text")
        )
        and page.get("quality") in {None, "jd_complete"}
        for page in pages
    )


def career_error_policy() -> ErrorPolicy:
    """Return job-source error rules owned by the career skill adapter."""
    return merge_error_policies(
        default_error_policy(),
        ErrorPolicy(
            blocked_codes=frozenset(
                {
                    "domain_temporarily_blocked",
                    "needs_manual_review",
                    "wechat_ocr_disabled",
                    "wechat_ocr_failed",
                    "adapter:url_not_allowlisted",
                    "adapter:empty_result",
                    "adapter:malformed_payload",
                    "adapter:adapter_error",
                    "adapter:adapter_unknown",
                    "adapter:adapter_invalid",
                    "adapter:allowlist_missing",
                }
            ),
            blocked_prefixes=("adapter:",),
            blocked_http_statuses=frozenset(
                str(code) for code in range(400, 500) if code not in {408, 429}
            ),
            transient_codes=frozenset(
                {"adapter:timeout", "adapter:dns_error", "adapter:transport_error"}
            ),
        ),
    )


def build_career_skill_registry(
    tools: ToolRegistry | None = None,
    *,
    package_root: Path | None = None,
) -> SkillRegistry:
    """Translate career adapters and canonical ``skill/`` packages to runtime metadata."""
    tool_registry = tools or _build_tools()
    package_root = package_root or Path(__file__).resolve().parents[4] / "skill"
    packages = {package.name: package for package in discover_skill_packages(package_root)}
    missing_packages = set(CAREER_SKILL_MANIFESTS) - set(packages)
    if missing_packages:
        missing = ", ".join(sorted(missing_packages))
        raise ValueError(
            f"canonical Skill package(s) missing from {package_root}: {missing}"
        )
    definitions: list[SkillDefinition] = []
    for name, manifest in CAREER_SKILL_MANIFESTS.items():
        if name == "job-discovery":
            # Search/sheet are discovery routes, not completion deliverables.
            # Only a captured public page or a source-bound extraction can
            # close a job-discovery step.
            deliverable_tools = _DISCOVERY_EVIDENCE_TOOLS | _DISCOVERY_DETAIL_TOOLS
            checker = _discovery_observation_is_valid
        elif name == "job-matching":
            deliverable_tools = frozenset({"match-observed-jobs"})
            checker = None
        elif name == "resume-tailoring":
            deliverable_tools = frozenset({"build-resume-tailoring-brief"})
            checker = None
        else:
            deliverable_tools = frozenset({"build-preparation-plan"})
            checker = None
        package = packages.get(name)
        definitions.append(
            SkillDefinition(
                name=name,
                description=manifest.description,
                completion_contract=CompletionContract(
                    deliverable_tools=deliverable_tools,
                    description=f"{name} must produce a registered tool-backed deliverable",
                    observation_check=checker,
                ),
                verification_policy=(
                    VerificationPolicy.REQUIRED
                    if name in {"job-matching", "resume-tailoring"}
                    else VerificationPolicy.OPTIONAL
                ),
                context_keys=(
                    frozenset()
                    if name == "job-discovery"
                    else frozenset({"confirmed_profile_facts", "preferences"})
                ),
                package_path=str(package.path) if package else None,
                package_instructions=package.instructions if package else "",
                input_ports=_skill_input_ports(name),
                output_ports=_skill_output_ports(name),
                metadata={
                    "requires_evidence": manifest.requires_evidence,
                    "supports_user_data": manifest.supports_user_data,
                    "package_description": package.description if package else None,
                    "tool_count": len(
                        [item for item in tool_registry.definitions if item.skill_name == name]
                    ),
                },
            )
        )
    return SkillRegistry(definitions, error_policy=career_error_policy())


def _skill_input_ports(name: str) -> tuple[ArtifactPort, ...]:
    """Compile the reviewed cross-Skill artifact contract at startup."""
    if name == "job-matching":
        return (ArtifactPort("job-evidence", frozenset({"public_job_page", "structured_job_details"})),)
    if name in {"resume-tailoring", "career-planning"}:
        return (
            ArtifactPort(
                "job-evidence",
                frozenset({"public_job_page", "structured_job_details", "job_matching_report"}),
            ),
        )
    return ()


def _skill_output_ports(name: str) -> tuple[ArtifactPort, ...]:
    if name == "job-discovery":
        return (
            ArtifactPort(
                "discovery-evidence",
                frozenset({"public_job_page", "job_search_results", "structured_job_details"}),
            ),
        )
    if name == "job-matching":
        return (ArtifactPort("match-report", frozenset({"job_matching_report"})),)
    if name == "resume-tailoring":
        return (ArtifactPort("tailoring-brief", frozenset({"resume_tailoring_brief"})),)
    if name == "career-planning":
        return (ArtifactPort("preparation-plan", frozenset({"career_preparation_plan"})),)
    return ()


def _build_tools() -> ToolRegistry:
    """Avoid importing the large career tool module at module-import time."""
    from backend.app.services.career_skills.registry import build_career_tool_registry

    return build_career_tool_registry()
