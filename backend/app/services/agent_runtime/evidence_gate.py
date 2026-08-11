"""Deterministic evidence contracts and blocked/transient error taxonomy.

Pure rules for the adaptive PEV harness - no database, no model calls:

* ``step_contract_met`` - whether a step's skill deliverable is already
  tool-backed from its observation set (the evidence gate behind the
  verifier/executor termination rescues).
* ``blocked_error_codes`` / ``is_blocked_error`` - whether an error code is a
  human-gated (login/captcha/anti-bot/OCR-off) or deterministic-per-URL
  failure whose retry cannot change the outcome, versus a transient network
  failure where a retry within budget is legitimate.
* ``has_blocked_evidence`` - whether an observation set carries any blocked
  signal (failed error_code, nested batch failures, or a WeChat output marked
  ``needs_manual_review`` even though the tool observation itself succeeded).

Security stance: blocked evidence always ends human-in-the-loop. The rescues
never fire when blocked evidence exists; the retry downgrade converts only the
machine retry loop into a single clean human hand-off.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from backend.app.services.agent_runtime.schemas import PlanStep, ToolObservation

#: Stable blocked error codes. Retrying the same source cannot change any of
#: these outcomes, so the harness must never treat them as progress signals:
#: security-gated pages (never circumvented), the gated WeChat OCR channel,
#: deterministic WeChat mirror-wall failures, rejected private/cloud-metadata
#: redirects, and the adapter contract's stable per-URL blocked codes
#: (skill/job-discovery/scripts/adapters/base.py).
_BLOCKED_ERROR_CODES = frozenset(
    {
        "login_required",
        "captcha",
        "anti_bot",
        "needs_manual_review",
        "wechat_ocr_disabled",
        "wechat_ocr_failed",
        "unsafe_public_url",
        "adapter:url_not_allowlisted",
        "adapter:empty_result",
        "adapter:malformed_payload",
        "adapter:adapter_error",
        "adapter:adapter_unknown",
        "adapter:adapter_invalid",
        "adapter:allowlist_missing",
    }
)

#: Transient error codes. The source is reachable in principle; a retry within
#: budget is legitimate and may succeed.
_TRANSIENT_ERROR_CODES = frozenset(
    {
        "public_fetch_failed",
        "public_search_failed",
        "public_host_unresolvable",
        "adapter:timeout",
        "adapter:dns_error",
        "adapter:transport_error",
    }
)

#: Deterministic HTTP rejections inside ``adapter:http_error:<status>``: 4xx
#: except rate-limit 408/429 (the latter two and 5xx are transient).
_HTTP_ERROR_BLOCKED_STATUSES = frozenset(
    str(code) for code in range(400, 500) if code not in {408, 429}
)

#: Blocked markers a *succeeded* tool output can carry (WeChat direct tool:
#: status="needs_manual_review", reason="ocr_disabled" when the channel is
#: gated off; browse-fetch mirrors use status="blocked").
_BLOCKED_OUTPUT_STATUSES = frozenset({"needs_manual_review", "blocked"})
_BLOCKED_OUTPUT_REASONS = frozenset(
    {"ocr_disabled", "login_required", "captcha", "anti_bot"}
)

#: Tools whose succeeded observation carries the job-discovery step's
#: deliverable: page/WeChat text captures (evidence required in the output)
#: and the verified search/sheet/extract outcomes (empty results are a
#: verified provider limitation, not a failure).
_DISCOVERY_EVIDENCE_TOOLS = frozenset(
    {
        "fetch-public-job-pages",
        "fetch-public-job-page",
        "fetch-wechat-article",
    }
)
_DISCOVERY_VERIFIED_TOOLS = frozenset(
    {
        "search-public-job-pages",
        "query-career-sheet-records",
        "extract-observed-job-details",
        "extract-observed-job-details-batch",
    }
)
#: Per-skill deliverable tools. An unknown or empty skill set never satisfies
#: a contract, so the harness cannot rescue a step that was never authorized
#: to produce anything.
_SKILL_DELIVERABLE_TOOLS: dict[str, frozenset[str]] = {
    "job-discovery": _DISCOVERY_EVIDENCE_TOOLS | _DISCOVERY_VERIFIED_TOOLS,
    "job-matching": frozenset({"match-observed-jobs"}),
    "resume-tailoring": frozenset({"build-resume-tailoring-brief"}),
    "career-planning": frozenset({"build-preparation-plan"}),
}


def has_known_deliverable_attempt(observations: Sequence[ToolObservation]) -> bool:
    """Return whether the runtime saw a production deliverable tool attempt."""
    known = frozenset().union(*_SKILL_DELIVERABLE_TOOLS.values())
    return any(observation.tool_name in known for observation in observations)


def blocked_error_codes() -> frozenset[str]:
    """Return the stable blocked error-code set (security-gated or retry-futile).

    ``adapter:http_error:<status>`` codes are classified dynamically inside
    ``is_blocked_error`` (deterministic 4xx are blocked; 408/429 and 5xx are
    transient) and are therefore not listed statically.
    """
    return _BLOCKED_ERROR_CODES


def is_blocked_error(error_code: str) -> bool:
    """True for a human-gated or deterministic failure a retry cannot change.

    Blocked: security gates (login/captcha/anti-bot/OCR-off), deterministic
    per-URL failures (WeChat walls, unsafe URLs, adapter stable codes,
    deterministic 4xx HTTP rejections). Transient/neutral: everything else
    (network timeouts, DNS/transport failures, 5xx, harness codes such as
    ``duplicate_tool_call``, and any unknown code).
    """
    if error_code in _BLOCKED_ERROR_CODES:
        return True
    if error_code in _TRANSIENT_ERROR_CODES:
        return False
    if error_code.startswith("adapter:http_error:"):
        return (
            error_code.removeprefix("adapter:http_error:") in _HTTP_ERROR_BLOCKED_STATUSES
        )
    if error_code.startswith("adapter:"):
        # Adapter contract: any other adapter failure is a stable blocked code.
        return True
    return False


def step_contract_met(
    step: PlanStep, artifacts: Sequence[ToolObservation]
) -> bool:
    """True when the step's skill deliverable is already tool-backed.

    ``artifacts`` is the step's merged tool-observation set. For each skill in
    the step: job-discovery requires a succeeded evidence-carrying capture
    (page/WeChat text) or a verified search/sheet/extract outcome;
    job-matching / resume-tailoring / career-planning require their single
    report tool's success. A succeeded observation whose output itself carries
    a blocked marker (e.g. WeChat ``needs_manual_review``/``ocr_disabled``)
    never counts - it is not usable deliverable evidence. A step naming
    multiple skills must satisfy every skill's contract.
    """
    if not step.allowed_skills:
        return False
    return all(
        _skill_contract_met(skill_name, artifacts) for skill_name in step.allowed_skills
    )


def completion_evidence_gate(
    step: PlanStep, artifacts: Sequence[ToolObservation], *, summary: str | None = None
) -> bool:
    """Allow completion only when a tool-backed, unblocked deliverable exists.

    A model summary is not evidence. A verified empty search result remains
    valid because the search observation itself satisfies the discovery
    contract; a failed/empty fetch cannot be upgraded by optimistic wording.
    """
    if not isinstance(summary, str) or not summary.strip():
        return False
    return step_contract_met(step, artifacts) and not has_blocked_evidence(artifacts)


def has_blocked_evidence(observations: Sequence[ToolObservation]) -> bool:
    """True when any observation carries a blocked signal.

    Blocked signals appear in three shapes: a failed observation's
    ``error_code``, a batch fetch's nested ``output.failures[*].error_code``,
    and a WeChat direct-tool output marked ``needs_manual_review`` (reason
    ``ocr_disabled``) even though the tool observation itself succeeded.
    """
    for observation in observations:
        output = observation.output or {}
        failures = output.get("failures")
        if isinstance(failures, list):
            for failure in failures:
                if isinstance(failure, dict) and is_blocked_error(
                    str(failure.get("error_code") or "")
                ):
                    return True
        if is_blocked_error(observation.error_code or ""):
            return True
        if _output_blocked(output):
            return True
    return False


def _skill_contract_met(
    skill_name: str, artifacts: Sequence[ToolObservation]
) -> bool:
    deliverable_tools = _SKILL_DELIVERABLE_TOOLS.get(skill_name)
    if deliverable_tools is None:
        return False
    for observation in artifacts:
        if (
            observation.status != "succeeded"
            or observation.tool_name not in deliverable_tools
        ):
            continue
        output = observation.output or {}
        if _output_blocked(output):
            continue
        if (
            observation.tool_name in _DISCOVERY_EVIDENCE_TOOLS
            and not _carries_page_evidence(output)
        ):
            continue
        return True
    return False


def _carries_page_evidence(output: Mapping[str, Any]) -> bool:
    """True when the output carries at least one page-evidence record.

    Accepts both a batch fetch (``pages`` list) and a single-page output
    (evidence keys at the top level); a record counts only with non-empty
    source_url / content_hash / visible_text strings - the same shape the
    runtime persists as evidence artifacts.
    """
    raw_pages = output.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else [output]
    for page in pages:
        if not isinstance(page, dict):
            continue
        if all(
            isinstance(page.get(key), str) and page[key]
            for key in ("source_url", "content_hash", "visible_text")
        ):
            return True
    return False


def _output_blocked(output: Mapping[str, Any]) -> bool:
    """True when a succeeded output itself carries a blocked marker."""
    status = output.get("status")
    if isinstance(status, str) and status in _BLOCKED_OUTPUT_STATUSES:
        return True
    reason = output.get("reason")
    if isinstance(reason, str) and reason in _BLOCKED_OUTPUT_REASONS:
        return True
    return False
