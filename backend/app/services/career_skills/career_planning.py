"""JD-grounded preparation planning for the PEV ``career-planning`` Skill."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext


class BuildPreparationPlanInput(BaseModel):
    """One evidence-backed target JD plus topics the Agent wants validated."""

    target_artifact_id: str = Field(min_length=1, max_length=80)
    focus_keywords: list[str] = Field(min_length=1, max_length=30)

    @field_validator("focus_keywords")
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
            raise ValueError("focus_keywords must include a non-empty value")
        return cleaned


class PreparationPlanOutput(BaseModel):
    """Concise interview preparation plan with public-JD provenance."""

    target_artifact_id: str
    source_url: str
    jd_topics: list[str]
    actions: list[str]


def build_preparation_plan(
    context: ToolContext, payload: BuildPreparationPlanInput
) -> PreparationPlanOutput:
    """Produce actions only for topics that the selected observed JD contains."""
    target = _find_target(context.metadata.get("observed_public_evidence"), payload.target_artifact_id)
    if target is None:
        raise ValueError("target_evidence_not_found")
    source_url = target.get("source_url")
    visible_text = target.get("visible_text")
    if not isinstance(source_url, str) or not isinstance(visible_text, str):
        raise ValueError("target_evidence_incomplete")
    searchable = f"{target.get('title') or ''}\n{visible_text}".lower()
    topic_pairs = [
        (keyword, keyword.lower())
        for keyword in payload.focus_keywords
        if keyword.lower() in searchable
    ]
    topics = [normalized for _display, normalized in topic_pairs]
    topic_text = "、".join(display for display, _normalized in topic_pairs)
    actions = []
    if topics:
        actions = [
            f"为 {topic_text} 各准备一个可量化的项目案例，并标明你的具体贡献。",
            f"围绕 JD 中的 {topic_text} 做一次 30 分钟技术讲解演练，准备架构取舍与故障排查追问。",
        ]
    return PreparationPlanOutput(
        target_artifact_id=payload.target_artifact_id,
        source_url=source_url,
        jd_topics=topics,
        actions=actions,
    )


def _find_target(raw_evidence: object, artifact_id: str) -> dict[str, Any] | None:
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
            return item
    return None
