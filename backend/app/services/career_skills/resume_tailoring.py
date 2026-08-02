"""Fact-grounded tailoring brief for the PEV ``resume-tailoring`` Skill."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext


class BuildResumeTailoringBriefInput(BaseModel):
    """One evidence-backed target JD and the terms the Agent wants checked."""

    target_artifact_id: str = Field(min_length=1, max_length=80)
    target_keywords: list[str] = Field(min_length=1, max_length=30)

    @field_validator("target_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for value in values:
            display_value = value.strip()
            normalized_value = display_value.lower()
            if display_value and normalized_value not in seen:
                seen.add(normalized_value)
                cleaned.append(display_value)
        if not cleaned:
            raise ValueError("target_keywords must include a non-empty value")
        return cleaned


class ResumeTailoringBriefOutput(BaseModel):
    """Safe rewrite guidance, never an auto-applied or unsupported resume diff."""

    target_artifact_id: str
    target_title: str | None
    source_url: str
    supported_keywords: list[str]
    missing_keywords: list[str]
    safe_actions: list[str]


def build_resume_tailoring_brief(
    context: ToolContext, payload: BuildResumeTailoringBriefInput
) -> ResumeTailoringBriefOutput:
    """Compare only one observed JD with facts already confirmed by the user."""
    target = _find_target(context.metadata.get("observed_public_evidence"), payload.target_artifact_id)
    if target is None:
        raise ValueError("target_evidence_not_found")
    source_url = target.get("source_url")
    visible_text = target.get("visible_text")
    if not isinstance(source_url, str) or not isinstance(visible_text, str):
        raise ValueError("target_evidence_incomplete")
    job_text = f"{target.get('title') or ''}\n{visible_text}".lower()
    required_keywords = [
        (keyword, keyword.lower())
        for keyword in payload.target_keywords
        if keyword.lower() in job_text
    ]
    confirmed_text = _flatten_text(context.metadata.get("confirmed_profile_facts")).lower()
    supported_pairs = [
        pair for pair in required_keywords if pair[1] in confirmed_text
    ]
    missing_pairs = [
        pair for pair in required_keywords if pair[1] not in confirmed_text
    ]
    supported = [normalized for _display, normalized in supported_pairs]
    missing = [normalized for _display, normalized in missing_pairs]
    actions: list[str] = []
    if supported:
        actions.append(
            "在项目经历中优先展示已确认的 "
            f"{'、'.join(display for display, _normalized in supported_pairs)} 事实，并量化可核验结果。"
        )
    if missing:
        actions.append(
            f"{'、'.join(display for display, _normalized in missing_pairs)} 尚无已确认事实：仅在能补充项目证据时添加，不得虚构。"
        )
    return ResumeTailoringBriefOutput(
        target_artifact_id=payload.target_artifact_id,
        target_title=target.get("title") if isinstance(target.get("title"), str) else None,
        source_url=source_url,
        supported_keywords=supported,
        missing_keywords=missing,
        safe_actions=actions,
    )


def _find_target(raw_evidence: object, artifact_id: str) -> dict[str, Any] | None:
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
            return item
    return None


def _flatten_text(value: object) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return "\n".join(_flatten_text(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return "\n".join(_flatten_text(item) for item in value)
    return ""
