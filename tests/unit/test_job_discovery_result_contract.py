from __future__ import annotations

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
