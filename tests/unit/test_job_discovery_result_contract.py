from __future__ import annotations

import json

import pytest
from langchain_core.messages import AIMessage, ToolMessage

from backend.app.services.job_discovery.result_contract import (
    AgentResultParseError,
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


def test_evicted_run_web_navigation_payload_recovered_from_files() -> None:
    """deepagents evicts oversized tool results to ``raw["files"]``; recover them.

    Mirrors xiaomi: ``run_web_navigation`` returns ~166 evidence pages (~422k
    chars) which exceeds the 20000-token eviction threshold. The ToolMessage
    carries a placeholder pointing at the offloaded file; ``_collect_tool_outputs``
    must read the file content and parse the real payload so the crawl is not
    silently lost to ``parse_failed``.
    """
    evidence = {
        "evidence_type": "job_detail_json",
        "url": "https://example.test/jobs/1",
        "content_hash": "evidence-hash",
    }
    candidate = {
        "title": "Software Engineer",
        "company_name": "Example Corp",
        "idempotency_key": "candidate-key",
        "similarity_group_key": "group-key",
    }
    real_payload = {"evidence_pages": [evidence], "candidates": [candidate]}
    tool_call_id = "call_00_abc123"
    file_path = f"/large_tool_results/{tool_call_id}"
    # Placeholder text emitted by deepagents' _offload_tool_message_content
    # (TOO_LARGE_TOOL_MSG): points at the offloaded file path.
    placeholder = (
        "Tool result too large, the result of this tool call "
        f"{tool_call_id} was saved in the filesystem at this path: {file_path}"
        "\n\nYou can read the result from the filesystem by using the read_file "
        "tool, but make sure to only read part of the result at a time."
    )
    raw = {
        "messages": [
            ToolMessage(
                content=placeholder,
                name="run_web_navigation",
                tool_call_id=tool_call_id,
            ),
            AIMessage(content="malformed final response"),
        ],
        "files": {
            file_path: {
                "content": json.dumps(real_payload),
                "encoding": "utf-8",
            }
        },
    }

    result = parse_agent_result(raw)

    assert result.status == "succeeded"
    assert result.evidence == [evidence]
    assert len(result.candidates) == 1
    assert result.candidates[0].title == "Software Engineer"


def test_evicted_placeholder_without_file_falls_to_parse_failed() -> None:
    """When the evicted file is missing, no evidence/candidates are recovered."""
    tool_call_id = "call_00_missing"
    file_path = f"/large_tool_results/{tool_call_id}"
    placeholder = (
        "Tool result too large, the result of this tool call "
        f"{tool_call_id} was saved in the filesystem at this path: {file_path}"
    )
    raw = {
        "messages": [
            ToolMessage(
                content=placeholder,
                name="run_web_navigation",
                tool_call_id=tool_call_id,
            ),
            AIMessage(content="malformed final response"),
        ],
        "files": {},  # offloaded file is gone
    }

    with pytest.raises(AgentResultParseError):
        parse_agent_result(raw)
