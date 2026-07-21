from __future__ import annotations

import json

from langchain_core.messages import AIMessage, ToolMessage

from backend.app.services.job_discovery.result_contract import (
    enforce_result_invariants,
    parse_agent_result,
)
from backend.app.services.job_discovery.schemas import DiscoveryRunResult, PageEvidence


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


def test_assembles_tool_outputs_when_final_message_is_malformed() -> None:
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
    assert result.evidence == [evidence]
    assert result.candidates == [candidate]
