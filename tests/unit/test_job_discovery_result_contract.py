from __future__ import annotations

import json

from langchain_core.messages import AIMessage, ToolMessage

from backend.app.services.job_discovery.result_contract import (
    enforce_result_invariants,
    parse_agent_result,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    NormalizedJobCandidate,
    PageEvidence,
)


def test_parses_fenced_json_from_reverse_message_scan() -> None:
    raw = {
        "messages": [
            AIMessage(content='```json\n{"status":"succeeded","summary":"ok"}\n```'),
            ToolMessage(content="done", tool_call_id="call-1"),
        ]
    }

    assert parse_agent_result(raw).status == "succeeded"


def test_parses_text_content_block() -> None:
    raw = {
        "messages": [
            AIMessage(
                content=[
                    {
                        "type": "text",
                        "text": (
                            '{"status":"needs_manual_review",'
                            '"block_reason":"captcha"}'
                        ),
                    }
                ]
            )
        ]
    }

    assert parse_agent_result(raw).block_reason == "captcha"


def test_zero_candidate_partial_success_becomes_manual_review() -> None:
    result = DiscoveryRunResult(
        status="partial_success",
        evidence=[PageEvidence(evidence_type="job_list", content_hash="h")],
        candidates=[],
    )

    normalized = enforce_result_invariants(result)

    assert normalized.status == "needs_manual_review"
    assert normalized.block_reason == "parse_failed"


def test_success_requires_at_least_one_candidate() -> None:
    result = DiscoveryRunResult(status="succeeded", candidates=[])

    assert enforce_result_invariants(result).status == "failed"


def test_candidate_only_tool_recovery_is_succeeded() -> None:
    candidate = {
        "title": "Software Engineer",
        "company_name": "Example Corp",
        "idempotency_key": "candidate-key",
        "similarity_group_key": "group-key",
    }
    raw = {
        "messages": [
            ToolMessage(
                content=json.dumps([candidate]),
                name="package_candidates",
                tool_call_id="package-call",
            ),
            AIMessage(content="malformed final response"),
        ]
    }

    result = parse_agent_result(raw)

    assert result.status == "succeeded"
    assert result.block_reason is None
    # Dedup repackages tool-recovered candidates as NormalizedJobCandidate objects
    # (dict -> object); idempotency_key/similarity_group_key are packaging-only
    # fields, not dataclass fields, so they are dropped (worker recomputes them).
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert isinstance(cand, NormalizedJobCandidate)
    assert cand.title == "Software Engineer"
    assert cand.company_name == "Example Corp"
    assert result.evidence == []


def test_incomplete_tool_recovery_preserves_evidence_and_candidates() -> None:
    evidence = {
        "evidence_type": "page_text",
        "url": "https://example.test/jobs/1",
        "content_hash": "evidence-hash",
    }
    candidate = {
        "title": "Software Engineer",
        "company_name": "Example Corp",
        "idempotency_key": "candidate-key",
        "similarity_group_key": "group-key",
    }
    raw = {
        "messages": [
            ToolMessage(
                content=json.dumps({"evidence_pages": [evidence]}),
                name="run_web_navigation",
                tool_call_id="navigation-call",
            ),
            ToolMessage(
                content=json.dumps([candidate]),
                name="package_candidates",
                tool_call_id="package-call",
            ),
            AIMessage(content="malformed final response"),
        ]
    }

    result = parse_agent_result(raw)

    assert result.status == "succeeded"
    assert result.block_reason is None
    assert result.evidence == [evidence]
    assert len(result.candidates) == 1
    cand = result.candidates[0]
    assert isinstance(cand, NormalizedJobCandidate)
    assert cand.title == "Software Engineer"
    assert cand.company_name == "Example Corp"


def test_evidence_only_tool_recovery_requires_manual_review() -> None:
    evidence = {
        "evidence_type": "page_text",
        "url": "https://example.test/jobs/1",
        "content_hash": "evidence-hash",
    }
    raw = {
        "messages": [
            ToolMessage(
                content=json.dumps({"evidence_pages": [evidence]}),
                name="run_web_navigation",
                tool_call_id="navigation-call",
            ),
            AIMessage(content="malformed final response"),
        ]
    }

    result = parse_agent_result(raw)

    assert result.status == "needs_manual_review"
    assert result.block_reason == "parse_failed"
    assert result.evidence == [evidence]
    assert result.candidates == []
