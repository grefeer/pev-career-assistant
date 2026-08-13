"""Evidence-gate rules: per-skill deliverable contracts and blocked taxonomy.

B1 of the round-1/B termination-and-retry-state work: pure deterministic
rules that decide (a) when a step's skill deliverable is already tool-backed
(the rescue gate) and (b) when an error code is human-gated / retry-futile
versus transient (the retry-downgrade gate).
"""

from __future__ import annotations

import pytest

from backend.app.services.agent_runtime.evidence_gate import (
    blocked_error_codes,
    has_blocked_evidence,
    is_blocked_error,
    step_contract_met,
)
from backend.app.services.agent_runtime.schemas import PlanStep, ToolObservation


def _step(*allowed_skills: str) -> PlanStep:
    return PlanStep(
        step_id="step-1",
        objective="完成步骤产出",
        allowed_skills=list(allowed_skills),
    )


def _observed(
    tool_name: str, *, status: str = "succeeded", output: dict | None = None, error_code: str | None = None
) -> ToolObservation:
    return ToolObservation(
        tool_name=tool_name, status=status, output=output, error_code=error_code
    )


def _page_evidence(source: str = "https://jobs.example/1") -> dict:
    return {
        "source_url": source,
        "content_hash": "a" * 64,
        "visible_text": "负责 AI Agent 开发，要求 Python 与 LangChain。",
    }


# ---------------------------------------------------------------------------
# blocked_error_codes / is_blocked_error taxonomy
# ---------------------------------------------------------------------------


def test_blocked_error_codes_expose_the_static_blocked_set() -> None:
    blocked = blocked_error_codes()
    assert "login_required" in blocked
    assert "captcha" in blocked
    assert "anti_bot" in blocked
    assert "needs_manual_review" in blocked
    assert "wechat_ocr_disabled" in blocked
    assert "wechat_ocr_failed" in blocked
    assert "unsafe_public_url" in blocked
    assert "adapter:url_not_allowlisted" in blocked
    assert "adapter:empty_result" in blocked
    assert "adapter:malformed_payload" in blocked
    assert "adapter:adapter_error" in blocked
    assert "adapter:adapter_unknown" in blocked
    assert "adapter:adapter_invalid" in blocked
    assert "adapter:allowlist_missing" in blocked
    # Transient codes are classified dynamically, never as static blocked.
    assert "public_fetch_failed" not in blocked
    assert "adapter:timeout" not in blocked


@pytest.mark.parametrize(
    "error_code",
    [
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
        "adapter:http_error:401",
        "adapter:http_error:403",
        "adapter:http_error:404",
    ],
)
def test_is_blocked_error_classifies_security_and_deterministic_codes(error_code: str) -> None:
    assert is_blocked_error(error_code) is True


@pytest.mark.parametrize(
    "error_code",
    [
        "public_fetch_failed",
        "public_search_failed",
        "public_host_unresolvable",
        "adapter:timeout",
        "adapter:dns_error",
        "adapter:transport_error",
        "adapter:http_error:408",
        "adapter:http_error:429",
        "adapter:http_error:500",
        "adapter:http_error:503",
        "duplicate_tool_call",
        "candidate_urls_already_supplied",
        "unknown_tool",
        "tool_skill_forbidden",
        "invalid_tool_input",
        "tool_execution_failed",
        "wall_clock_budget_exhausted",
        "",
    ],
)
def test_is_blocked_error_leaves_transient_and_harness_codes_unblocked(error_code: str) -> None:
    assert is_blocked_error(error_code) is False


def test_is_blocked_error_treats_unknown_adapter_codes_as_stable_blocked() -> None:
    """The adapter contract maps every failure to a stable blocked code."""
    assert is_blocked_error("adapter:future_code") is True


def test_is_blocked_error_treats_non_http_error_suffixes_as_transient() -> None:
    assert is_blocked_error("adapter:http_error:") is False
    assert is_blocked_error("adapter:http_error:abc") is False


# ---------------------------------------------------------------------------
# step_contract_met - per-skill deliverable contracts
# ---------------------------------------------------------------------------


def test_step_contract_met_requires_evidence_carrying_discovery_capture() -> None:
    with_evidence = _observed("fetch-public-job-page", output=_page_evidence())
    assert step_contract_met(_step("job-discovery"), [with_evidence]) is True
    bare = _observed("fetch-public-job-page", output={"title": "无正文"})
    assert step_contract_met(_step("job-discovery"), [bare]) is False


def test_step_contract_met_accepts_batch_fetch_pages_and_skips_malformed_entries() -> None:
    batch = _observed(
        "fetch-public-job-pages",
        output={
            "pages": ["not-a-dict", _page_evidence()],
            "failures": [],
        },
    )
    assert step_contract_met(_step("job-discovery"), [batch]) is True


def test_step_contract_does_not_close_on_search_or_sheet_index_only() -> None:
    empty_search = _observed(
        "search-public-job-pages",
        output={
            "query": "AI 岗位",
            "source_url": "https://search.example/",
            "content_hash": "b" * 64,
            "results": [],
        },
    )
    assert step_contract_met(_step("job-discovery"), [empty_search]) is False
    sheet = _observed(
        "query-career-sheet-records",
        output={
            "records": [{"company": "腾讯"}],
            "source_url": "https://sheet.example/",
            "content_hash": "c" * 64,
            "query": {},
        },
    )
    assert step_contract_met(_step("job-discovery"), [sheet]) is False


def test_step_contract_met_for_the_three_report_skills() -> None:
    match = _observed("match-observed-jobs", output={"matches": []})
    assert step_contract_met(_step("job-matching"), [match]) is True
    tailoring = _observed("build-resume-tailoring-brief", output={"safe_actions": []})
    assert step_contract_met(_step("resume-tailoring"), [tailoring]) is True
    planning = _observed("build-preparation-plan", output={"plan": []})
    assert step_contract_met(_step("career-planning"), [planning]) is True
    # A report tool from another skill does not satisfy this step's contract.
    assert step_contract_met(_step("job-matching"), [tailoring]) is False


def test_step_contract_met_ignores_failed_observations() -> None:
    failed = _observed(
        "fetch-public-job-page", status="failed", error_code="public_fetch_failed"
    )
    assert step_contract_met(_step("job-discovery"), [failed]) is False


def test_step_contract_met_excludes_blocked_outputs_from_wechat_tool() -> None:
    gated = _observed(
        "fetch-wechat-article",
        output={
            "url": "https://mp.weixin.qq.com/s/x",
            "status": "needs_manual_review",
            "channel": None,
            "candidates": [],
            "ocr_text": "",
            "needs_deep_crawl": False,
            "reason": "ocr_disabled",
        },
    )
    assert step_contract_met(_step("job-discovery"), [gated]) is False


def test_step_contract_met_requires_every_skill_of_a_multiskill_step() -> None:
    match = _observed("match-observed-jobs", output={"matches": []})
    tailoring = _observed("build-resume-tailoring-brief", output={"safe_actions": []})
    assert step_contract_met(_step("job-matching", "resume-tailoring"), [match]) is False
    assert (
        step_contract_met(_step("job-matching", "resume-tailoring"), [match, tailoring])
        is True
    )


def test_step_contract_met_never_satisfies_unknown_or_empty_skill_scopes() -> None:
    assert step_contract_met(_step("unknown-skill"), [_observed("fetch-public-job-page", output=_page_evidence())]) is False
    # Schema validation forbids an empty allowed_skills list, so construct the
    # step directly to exercise the harness's defensive empty-scope branch.
    empty_scope = PlanStep.model_construct(
        step_id="step-1", objective="完成步骤产出", allowed_skills=[]
    )
    assert step_contract_met(empty_scope, [_observed("fetch-public-job-page", output=_page_evidence())]) is False


# ---------------------------------------------------------------------------
# has_blocked_evidence - the three blocked-signal shapes
# ---------------------------------------------------------------------------


def test_has_blocked_evidence_detects_a_failed_observation_error_code() -> None:
    blocked = _observed(
        "fetch-public-job-page", status="failed", error_code="login_required"
    )
    assert has_blocked_evidence([blocked]) is True


def test_has_blocked_evidence_detects_nested_batch_fetch_failures() -> None:
    nested = _observed(
        "fetch-public-job-pages",
        output={
            "pages": [_page_evidence()],
            "failures": [
                {"source_url": "https://jobs.example/blocked", "error_code": "captcha"}
            ],
        },
    )
    assert has_blocked_evidence([nested]) is True
    transient = _observed(
        "fetch-public-job-pages",
        output={
            "pages": [],
            "failures": [
                {"source_url": "https://jobs.example/x", "error_code": "adapter:timeout"}
            ],
        },
    )
    assert has_blocked_evidence([transient]) is False


def test_has_blocked_evidence_skips_malformed_failure_entries() -> None:
    malformed = _observed(
        "fetch-public-job-pages",
        output={"pages": [], "failures": ["junk", {"no_code": True}]},
    )
    assert has_blocked_evidence([malformed]) is False


def test_has_blocked_evidence_detects_wechat_succeeded_output_markers() -> None:
    status_marker = _observed(
        "fetch-wechat-article",
        output={"url": "https://mp.weixin.qq.com/s/x", "status": "needs_manual_review"},
    )
    assert has_blocked_evidence([status_marker]) is True
    reason_marker = _observed(
        "fetch-wechat-article",
        output={"url": "https://mp.weixin.qq.com/s/x", "reason": "ocr_disabled"},
    )
    assert has_blocked_evidence([reason_marker]) is True


def test_has_blocked_evidence_returns_false_for_clean_evidence() -> None:
    clean = [
        _observed("fetch-public-job-page", output=_page_evidence()),
        _observed(
            "fetch-public-job-pages",
            status="failed",
            error_code="public_fetch_failed",
        ),
        _observed("extract-observed-job-details", output={"details": []}),
    ]
    assert has_blocked_evidence(clean) is False
    assert has_blocked_evidence([]) is False


def test_batch_fetch_with_evidence_and_blocked_failure_meets_contract_and_is_blocked() -> None:
    """The B4 no-downgrade precondition: contract satisfied, blocked present."""
    mixed = _observed(
        "fetch-public-job-pages",
        output={
            "pages": [_page_evidence()],
            "failures": [
                {"source_url": "https://jobs.example/x", "error_code": "wechat_ocr_disabled"}
            ],
        },
    )
    assert step_contract_met(_step("job-discovery"), [mixed]) is True
    assert has_blocked_evidence([mixed]) is True
