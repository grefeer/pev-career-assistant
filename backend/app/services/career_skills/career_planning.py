"""JD-grounded preparation planning for the PEV ``career-planning`` Skill.

C2 (docs/findjobs-optimization-plan.zh-CN.md §6.2): the tool additionally
accepts optional extra JD artifact ids + resume skills and reports the
cross-JD top-N missing-skill gaps (FindJobs ``top_skill_gaps`` port).  The
single-JD path is unchanged: aggregation runs only when extra ids are given.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from pydantic import BaseModel, Field, field_validator

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.target_evidence import resolve_target_evidence
from backend.app.services.job_discovery.tools.skill_validator import (
    normalize_skill,
    skills_from_text,
)


class CareerPlanningError(RuntimeError):
    """Stable, non-sensitive career-planning failure."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BuildPreparationPlanInput(BaseModel):
    """One evidence-backed target JD plus topics the Agent wants validated.

    C2: ``additional_target_artifact_ids`` / ``resume_skills`` / ``gap_limit``
    are optional; leaving them empty keeps the exact pre-C2 single-JD path.
    """

    target_artifact_id: str = Field(min_length=1, max_length=80)
    focus_keywords: list[str] = Field(min_length=1, max_length=30)
    time_budget_hours: int = Field(default=6, ge=1, le=80)
    target_date: date | None = None
    additional_target_artifact_ids: list[str] = Field(default_factory=list, max_length=20)
    resume_skills: list[str] = Field(default_factory=list, max_length=50)
    gap_limit: int = Field(default=5, ge=1, le=20)

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


class SkillGap(BaseModel):
    """One cross-JD missing-skill aggregation row (C2, FindJobs port)."""

    skill: str
    job_count: int


class PreparationPlanOutput(BaseModel):
    """Concise interview preparation plan with public-JD provenance."""

    target_artifact_id: str
    source_url: str
    jd_topics: list[str]
    actions: list[str]
    schedule_assumption: str
    plan_items: list["PreparationPlanItem"]
    skill_gaps: list[SkillGap] = Field(default_factory=list)


class PreparationPlanItem(BaseModel):
    """One bounded JD-grounded preparation action awaiting user execution."""

    topic: str
    priority: str
    time_budget_hours: int
    due_date: date
    completion_criteria: str
    review_checkpoint: str


def build_preparation_plan(
    context: ToolContext, payload: BuildPreparationPlanInput
) -> PreparationPlanOutput:
    """Produce actions only for topics that the selected observed JD contains."""
    target = resolve_target_evidence(
        context.metadata.get("observed_public_evidence"),
        context.metadata.get("structured_job_candidates"),
        payload.target_artifact_id,
    )
    if target is None:
        raise CareerPlanningError("target_evidence_not_found")
    source_url = target.get("source_url")
    visible_text = target.get("visible_text")
    if not isinstance(source_url, str) or not isinstance(visible_text, str):
        raise CareerPlanningError("target_evidence_incomplete")
    searchable = f"{target.get('title') or ''}\n{visible_text}".lower()
    if not _target_matches_goal(context.metadata.get("task_goal"), searchable):
        raise CareerPlanningError("target_role_mismatch")
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
    base_hours, remaining_hours = divmod(payload.time_budget_hours, len(topics)) if topics else (0, 0)
    due_date, schedule_assumption = _resolve_due_date(payload.target_date)
    plan_items = [
        PreparationPlanItem(
            topic=normalized,
            priority="P0" if index == 0 else "P1",
            time_budget_hours=base_hours + (1 if index < remaining_hours else 0),
            due_date=due_date,
            completion_criteria=(
                f"准备一个 {display} 相关项目案例，说明你的具体贡献和可核验结果。"
            ),
            review_checkpoint=(
                f"完成后用 JD 的 {display} 要求复盘：案例是否覆盖职责、取舍和追问。"
            ),
        )
        for index, (display, normalized) in enumerate(topic_pairs)
    ]
    skill_gaps: list[SkillGap] = []
    if payload.additional_target_artifact_ids:
        skill_gaps = _aggregate_skill_gaps(
            context.metadata.get("observed_public_evidence"),
            context.metadata.get("structured_job_candidates"),
            (payload.target_artifact_id, *payload.additional_target_artifact_ids),
            payload.resume_skills,
            payload.gap_limit,
        )
    return PreparationPlanOutput(
        target_artifact_id=payload.target_artifact_id,
        source_url=source_url,
        jd_topics=topics,
        actions=actions,
        schedule_assumption=schedule_assumption,
        plan_items=plan_items,
        skill_gaps=skill_gaps,
    )


def _aggregate_skill_gaps(
    raw_evidence: object,
    structured_candidates: object,
    artifact_ids: tuple[str, ...],
    resume_skills: list[str],
    gap_limit: int,
) -> list[SkillGap]:
    """Cross-JD top-N missing-skill gaps (C2, FindJobs ``top_skill_gaps``).

    Every JD (target + extra ids, in input order) contributes its closed-set
    demanded skills (``skills_from_text``, deterministic, LLM-free); a skill
    the resume already names (closed-set spelling or curated alias, exact
    match) is not a gap.  Each JD counts a skill at most once.  Ranking:
    job_count descending, then skill name ascending - stable across calls.
    Evidence items that are missing or lack a non-empty ``visible_text`` are
    skipped (a lost extra JD silently contributes nothing).
    """
    texts: list[str] = []
    for artifact_id in artifact_ids:
        item = resolve_target_evidence(raw_evidence, structured_candidates, artifact_id)
        if item is None:
            continue
        visible_text = item.get("visible_text")
        if isinstance(visible_text, str) and visible_text:
            texts.append(visible_text)
    if not texts:
        return []
    owned = {
        normalize_skill(skill) or skill.strip().lower()
        for skill in resume_skills
        if skill.strip()
    }
    counts: dict[str, int] = {}
    for text in texts:
        for skill in set(skills_from_text(text)) - owned:
            counts[skill] = counts.get(skill, 0) + 1
    ranked = sorted(counts.items(), key=lambda item: (-item[1], item[0]))
    return [
        SkillGap(skill=skill, job_count=count)
        for skill, count in ranked[:gap_limit]
    ]


def _find_target(raw_evidence: object, artifact_id: str) -> dict[str, Any] | None:
    if not isinstance(raw_evidence, list):
        return None
    for item in raw_evidence:
        if isinstance(item, dict) and item.get("artifact_id") == artifact_id:
            return item
    return None


def _target_matches_goal(goal: object, searchable: str) -> bool:
    """Reject a model-selected JD that is clearly for another requested role."""
    if not isinstance(goal, str) or not goal.strip():
        return True
    role_groups = (
        (("产品经理", "产品类", "aigc"), ("产品经理", "aigc")),
        (("大模型应用开发", "llm 应用", "llm应用"), ("大模型", "应用开发", "llm", "agent")),
        (("前端开发",), ("前端", "frontend")),
        (("java 后端", "java后端"), ("java", "后端")),
    )
    lowered_goal = goal.lower()
    for markers, evidence_terms in role_groups:
        if any(marker in lowered_goal for marker in markers):
            return any(term in searchable for term in evidence_terms)
    return True


def _resolve_due_date(target_date: date | None) -> tuple[date, str]:
    """Use a disclosed short planning window only when the user gave no deadline."""
    if target_date is not None:
        return target_date, "使用用户指定的目标日期。"
    return (
        date.today() + timedelta(days=7),
        "未提供目标日期；使用运行日后 7 天作为可修改的默认截止时间。",
    )
