"""PEV evidence-quality gate (validate-observed-candidates) unit tests."""

from __future__ import annotations

import pytest

from backend.app.services.career_skills.validate_candidates import (
    ValidateObservedCandidatesInput,
    ValidateObservedCandidatesOutput,
    validate_observed_candidates,
    _quality_issues,
)
from backend.app.services.agent_runtime.tool_context import ToolContext


def _evidence_metadata(artifact_id: str, text: str) -> dict:
    return {
        "observed_public_evidence": [
            {
                "artifact_id": artifact_id,
                "source_url": "https://jobs.example/x",
                "content_hash": artifact_id.removeprefix("observed:"),
                "visible_text": text,
                "title": None,
            }
        ]
    }


def test_validate_passes_clean_jd_evidence() -> None:
    text = (
        "岗位职责：构建智能体系统，负责招聘平台后端架构。"
        + "任职要求：5 年经验，职责包括需求分析、岗位评估、招聘流程优化。"
        + "职位要求：熟悉职位发布、简历筛选与面试安排，责任心强，具备良好的沟通协调能力。"
        + "岗位要求：熟悉招聘平台数据分析与职位关键词优化。"
    )
    artifact_id = "observed:abc"
    result = validate_observed_candidates(
        ToolContext(user_id="u", run_id="r", metadata=_evidence_metadata(artifact_id, text)),
        ValidateObservedCandidatesInput(artifact_ids=[artifact_id]),
    )
    assert result.valid is True
    assert result.issues == []


def test_validate_reports_stale_vague_and_non_jd_issues() -> None:
    stale = "岗位职责：系统维护。参考了 2019 年文档后更新。" + "职责要求：熟悉招聘流程。"
    result = validate_observed_candidates(
        ToolContext(user_id="u", run_id="r", metadata=_evidence_metadata("observed:s", stale)),
        ValidateObservedCandidatesInput(artifact_ids=["observed:s"]),
    )
    assert result.valid is False
    codes = {issue.code for issue in result.issues}
    assert "stale_year" in codes

    vague = "简介"
    result = validate_observed_candidates(
        ToolContext(user_id="u", run_id="r", metadata=_evidence_metadata("observed:v", vague)),
        ValidateObservedCandidatesInput(artifact_ids=["observed:v"]),
    )
    assert result.valid is False
    assert result.issues[0].code == "vague_description"
    assert "chars (min: 50)" in result.issues[0].detail

    non_jd = "x" * 150
    result = validate_observed_candidates(
        ToolContext(user_id="u", run_id="r", metadata=_evidence_metadata("observed:n", non_jd)),
        ValidateObservedCandidatesInput(artifact_ids=["observed:n"]),
    )
    assert result.valid is False
    assert result.issues[0].code == "non_jd_text"


def test_validate_reports_missing_and_incomplete_evidence() -> None:
    context = ToolContext(
        user_id="u",
        run_id="r",
        metadata={
            "observed_public_evidence": [
                {
                    "artifact_id": "observed:no-text",
                    "source_url": "https://jobs.example/x",
                    "content_hash": "no-text",
                    "visible_text": "",
                    "title": None,
                }
            ]
        },
    )
    result = validate_observed_candidates(
        context,
        ValidateObservedCandidatesInput(artifact_ids=["observed:ghost", "observed:no-text"]),
    )
    assert result.valid is False
    assert result.issues[0].code == "evidence_not_found"
    assert result.issues[0].artifact_id == "observed:ghost"
    assert result.issues[1].code == "evidence_incomplete"


def test_quality_issues_multiple_findings_on_one_artifact() -> None:
    text = "2018 年上线" + "。" * 120
    issues = _quality_issues("observed:m", text)
    codes = {issue.code for issue in issues}
    assert "stale_year" in codes
    assert "non_jd_text" in codes


def test_quality_issues_boundaries_do_not_fire() -> None:
    # Current/acceptable years never trigger staleness.
    assert not any(
        issue.code == "stale_year" for issue in _quality_issues("observed:y", "2024 年校招 2025 届"))
    # Exactly 100 chars does not trigger the non-JD gate (strictly > 100).
    assert not any(
        issue.code == "non_jd_text" for issue in _quality_issues("observed:b", "职" * 100))


def test_validate_input_rejects_blank_and_duplicate_ids() -> None:
    with pytest.raises(ValueError):
        ValidateObservedCandidatesInput(artifact_ids=["", "observed:a"])
    with pytest.raises(ValueError, match="unique"):
        ValidateObservedCandidatesInput(artifact_ids=["observed:a", "observed:a"])


def test_validate_output_model_roundtrip() -> None:
    out = ValidateObservedCandidatesOutput(
        valid=False,
        issues=[
            {"artifact_id": "observed:a", "code": "vague_description", "detail": "d"}
        ],
    )
    assert out.issues[0].code == "vague_description"
