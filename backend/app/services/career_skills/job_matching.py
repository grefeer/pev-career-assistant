"""Evidence-bound matching tool for the PEV ``job-matching`` Skill."""

from __future__ import annotations

import re
from typing import Any, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, Field, field_validator, model_validator

from backend.app.services.agent_runtime.tool_context import ToolContext

#: Data-driven mapping of model-facing ranking criteria onto the canonical
#: Literal domain. Identity entries make case normalization a pure lookup;
#: Chinese entries map only enum-synonymous criteria. An unknown value is left
#: untouched so Literal validation still rejects it (no bypass, no semantic
#: change). Every lenient conversion is recorded in ``normalization_warnings``.
_RANKING_CRITERIA_ALIASES: dict[str, str] = {
    "skills": "skills",
    "location": "location",
    "salary": "salary",
    "recency": "recency",
    "company_type": "company_type",
    "技能": "skills",
    "技能匹配": "skills",
    "技能匹配度": "skills",
    "匹配度": "skills",
    "地点": "location",
    "位置": "location",
    "工作地点": "location",
    "城市": "location",
    "薪资": "salary",
    "薪酬": "salary",
    "工资": "salary",
    "薪水": "salary",
    "薪资待遇": "salary",
    "时效": "recency",
    "时效性": "recency",
    "发布时间": "recency",
    "发布日期": "recency",
    "新近度": "recency",
    "公司类型": "company_type",
    "企业类型": "company_type",
    "单位类型": "company_type",
    "公司性质": "company_type",
}

#: Separators accepted when ``profile_keywords`` arrives as one string.
_KEYWORD_SEPARATORS = re.compile(r"[，,;；、\n]+")

# Historical model outputs used both English aliases and the old camelCase
# names. Normalize them before canonical Pydantic validation.
_INPUT_ALIASES: dict[str, tuple[str, ...]] = {
    "profile_keywords": ("keywords", "profileKeywords", "skills"),
    "preferred_locations": ("locations", "preferredLocations", "cities"),
    "ranking_criteria": ("criteria", "rankingCriteria", "sort_by"),
}


class MatchObservedJobsInput(BaseModel):
    """Confirmed user capabilities or preferences selected by the Executor."""

    profile_keywords: list[str] = Field(default_factory=list, max_length=30)
    preferred_locations: list[str] = Field(default_factory=list, max_length=20)
    ranking_criteria: list[
        Literal["skills", "location", "salary", "recency", "company_type"]
    ] = Field(default_factory=lambda: ["skills"])
    #: The cap mirrors the extraction cap (``_MAX_CANDIDATES_PER_PAGE``) so a
    #: full card-list extraction can be ranked in one call without dropping
    #: captured jobs.
    limit: int = Field(default=100, ge=1, le=100)
    #: Human-readable record of every lenient input conversion applied to this
    #: payload (string keywords split, Chinese/case-variant criteria mapped).
    #: Empty when the payload arrived in canonical shape.
    normalization_warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="before")
    @classmethod
    def tolerate_model_facing_shapes(cls, values: Any) -> Any:
        """Coerce model-generated input shapes before canonical validation.

        Converts a string ``profile_keywords`` into a keyword list and maps
        Chinese / case-variant ``ranking_criteria`` onto the canonical enum via
        the data-driven alias table. Unknown criteria and structurally invalid
        payloads keep today's ``ValidationError`` behavior.
        """
        if isinstance(values, MatchObservedJobsInput):
            values = values.model_dump()
        if not isinstance(values, dict):
            return values
        values = dict(values)
        warnings: list[str] = []
        for canonical, aliases in _INPUT_ALIASES.items():
            if canonical not in values:
                for alias in aliases:
                    if alias in values:
                        values[canonical] = values[alias]
                        warnings.append(f"{alias} normalized to {canonical}")
                        break
        keywords = values.get("profile_keywords")
        if isinstance(keywords, str):
            split_keywords = [
                part for part in _KEYWORD_SEPARATORS.split(keywords) if part
            ]
            values["profile_keywords"] = split_keywords
            warnings.append(
                f"profile_keywords coerced from string to {len(split_keywords)} keyword(s)"
            )
        locations = values.get("preferred_locations")
        if isinstance(locations, str):
            split_locations = [
                part.strip()
                for part in _KEYWORD_SEPARATORS.split(locations)
                if part.strip()
            ]
            values["preferred_locations"] = split_locations
            warnings.append(
                f"preferred_locations coerced from string to {len(split_locations)} location(s)"
            )
        criteria = values.get("ranking_criteria")
        if isinstance(criteria, str):
            split_criteria = [
                part.strip()
                for part in _KEYWORD_SEPARATORS.split(criteria)
                if part.strip()
            ]
            values["ranking_criteria"] = split_criteria
            criteria = split_criteria
            warnings.append(
                f"ranking_criteria coerced from string to {len(split_criteria)} criterion/criteria"
            )
        if isinstance(criteria, list):
            normalized_criteria: list[Any] = []
            for raw in criteria:
                if isinstance(raw, str):
                    canonical = _RANKING_CRITERIA_ALIASES.get(raw.strip().lower())
                    if canonical is not None:
                        normalized_criteria.append(canonical)
                        if canonical != raw:
                            warnings.append(
                                f"ranking_criteria {raw!r} normalized to {canonical!r}"
                            )
                        continue
                # Unknown criteria keep today's Literal ValidationError.
                normalized_criteria.append(raw)
            values["ranking_criteria"] = normalized_criteria
        # The business contract is fixed at 100. Model-provided caps are
        # compatibility noise, not a reason to reject an otherwise valid call.
        raw_limit = values.get("limit")
        if raw_limit is not None and raw_limit != 100:
            warnings.append("limit normalized to fixed business limit 100")
        values["limit"] = 100
        if warnings:
            existing = values.get("normalization_warnings")
            values["normalization_warnings"] = [
                *(existing if isinstance(existing, list) else []),
                *warnings,
            ]
        return values

    @field_validator("profile_keywords")
    @classmethod
    def normalize_keywords(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip().lower() for value in values if value.strip()))

    @field_validator("preferred_locations")
    @classmethod
    def normalize_locations(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))

    @field_validator("ranking_criteria")
    @classmethod
    def deduplicate_ranking_criteria(
        cls, values: list[Literal["skills", "location", "salary", "recency", "company_type"]]
    ) -> list[Literal["skills", "location", "salary", "recency", "company_type"]]:
        return list(dict.fromkeys(values))


class ObservedJobMatch(BaseModel):
    """A transparent score over one immutable public JD artifact."""

    artifact_id: str
    candidate_id: str | None = None
    source_artifact_id: str | None = None
    source_url: str
    title: str | None
    score: int = Field(ge=0, le=100)
    matched_keywords: list[str]
    matched_locations: list[str] = Field(default_factory=list)
    compensation_text: str | None = None
    observed_company_types: list[str] = Field(default_factory=list)
    unverified_ranking_criteria: list[str] = Field(default_factory=list)
    evidence_excerpt: str


class MatchObservedJobsOutput(BaseModel):
    """Ranked output with no candidates outside the supplied evidence context."""

    matches: list[ObservedJobMatch]
    unresolved_ranking_criteria: list[str] = Field(default_factory=list)


_COMPENSATION_RE = re.compile(
    r"(?i)(?:[¥￥]|rmb)?\s*\d+(?:\.\d+)?\s*(?:k|千|万)"
    r"(?:\s*[-~至]\s*(?:[¥￥]|rmb)?\s*\d+(?:\.\d+)?\s*(?:k|千|万))?"
    r"(?:\s*(?:/\s*(?:月|年)|月薪|年薪))?"
)
_COMPANY_TYPE_LABELS = ("国企", "民营", "外企", "事业单位")
_GOAL_ROLE_TERMS = (
    "产品经理",
    "项目经理",
    "后端开发",
    "前端开发",
    "应用开发",
    "算法工程师",
    "开发工程师",
    "工程师",
    "大模型",
    "AIGC",
    "AI",
    "Java",
    "Python",
)
_GOAL_LOCATION_TERMS = (
    "北京", "上海", "广州", "深圳", "杭州", "南京", "苏州", "成都",
    "武汉", "西安", "重庆", "天津", "长沙", "郑州", "济南", "青岛",
    "合肥", "厦门", "大连", "东莞", "佛山",
)
_INVALID_TITLE_MARKERS = (
    "如您应聘",
    "温馨提示",
    "平台内招聘方",
    "举报",
    "安全防范",
    "查看全部",
)


def match_observed_jobs(
    context: ToolContext, payload: MatchObservedJobsInput
) -> MatchObservedJobsOutput:
    """Rank only public evidence already observed in this authenticated PEV run.

    Structured candidates (``extract-observed-job-details`` output) take
    priority: each captured job is scored individually against the confirmed
    profile, so a card-list page produces per-job ranked matches instead of
    one aggregated entry. Raw page evidence remains the fallback for runs
    with no structured extraction (single-JD pages, evidence-only evals).
    """
    candidates = context.metadata.get("structured_job_candidates", [])
    goal_role_terms = _goal_role_terms(context.metadata.get("task_goal"))
    matches: list[ObservedJobMatch] = []
    if isinstance(candidates, list) and candidates:
        for candidate in candidates:
            if not isinstance(candidate, dict):
                continue
            if not _is_primary_detail_candidate(candidate):
                continue
            if not _candidate_meets_goal_constraints(
                candidate,
                context.metadata.get("task_goal"),
                context.metadata.get("confirmed_profile_facts"),
            ):
                continue
            if not _source_allowed_for_goal(
                candidate.get("source_url"), context.metadata.get("task_goal")
            ):
                continue
            match = _match_candidate(candidate, payload, goal_role_terms=goal_role_terms)
            if match is not None:
                matches.append(match)
    else:
        matches = _match_raw_evidence(context, payload, goal_role_terms=goal_role_terms)
    matches = _prefer_goal_role_matches(matches, context.metadata.get("task_goal"))
    matches.sort(key=lambda match: (-match.score, match.title or "", match.artifact_id))
    selected_matches = matches[: payload.limit]
    unresolved = [
        criterion
        for criterion in payload.ranking_criteria
        if any(criterion in match.unverified_ranking_criteria for match in selected_matches)
    ]
    return MatchObservedJobsOutput(
        matches=selected_matches,
        unresolved_ranking_criteria=unresolved,
    )


def _match_raw_evidence(
    context: ToolContext,
    payload: MatchObservedJobsInput,
    *,
    goal_role_terms: list[str] | None = None,
) -> list[ObservedJobMatch]:
    """Score whole-page evidence items as jobs (fallback path)."""
    raw_evidence = context.metadata.get("observed_public_evidence", [])
    if not isinstance(raw_evidence, list):
        return []
    matches: list[ObservedJobMatch] = []
    for item in raw_evidence:
        if not isinstance(item, dict):
            continue
        if item.get("quality") in {"list_only", "js_shell", "empty"}:
            # Index pages and shell pages are routing evidence, not a JD that
            # can support a matching score.
            continue
        artifact_id = item.get("artifact_id")
        source_url = item.get("source_url")
        visible_text = item.get("visible_text")
        if not all(
            isinstance(value, str) and value
            for value in (artifact_id, source_url, visible_text)
        ):
            continue
        if not _source_allowed_for_goal(source_url, context.metadata.get("task_goal")):
            continue
        title = item.get("title")
        normalized_title = title if isinstance(title, str) else None
        searchable = f"{normalized_title or ''}\n{visible_text}".lower()
        matches.append(
            _score_job(
                artifact_id=artifact_id,
                source_url=source_url,
                title=normalized_title,
                searchable=searchable,
                excerpt=visible_text,
                payload=payload,
                goal_role_terms=goal_role_terms,
            )
        )
    return matches


def _match_candidate(
    item: dict[str, Any],
    payload: MatchObservedJobsInput,
    *,
    goal_role_terms: list[str] | None = None,
) -> ObservedJobMatch | None:
    """Score one structured job candidate against the confirmed profile."""
    if item.get("source_quality") in {"list_only", "js_shell", "empty"}:
        return None
    artifact_id = item.get("artifact_id")
    source_url = item.get("source_url")
    if not (
        isinstance(artifact_id, str)
        and artifact_id
        and isinstance(source_url, str)
        and source_url
    ):
        return None
    title = item.get("title")
    normalized_title = title if isinstance(title, str) else None
    locations = item.get("locations") if isinstance(item.get("locations"), list) else []
    responsibilities = item.get("responsibilities")
    requirements = item.get("requirements")
    section_text = "\n".join(
        part for part in (responsibilities, requirements) if isinstance(part, str) and part
    )
    company_name = item.get("company_name")
    searchable = "\n".join(
        part
        for part in (
            normalized_title,
            " ".join(locations),
            company_name if isinstance(company_name, str) else None,
            section_text,
        )
        if part
    ).lower()
    excerpt = section_text or normalized_title or ""
    return _score_job(
        artifact_id=artifact_id,
        candidate_id=(
            item.get("candidate_id")
            if isinstance(item.get("candidate_id"), str)
            else None
        ),
        source_artifact_id=(
            item.get("source_artifact_id")
            if isinstance(item.get("source_artifact_id"), str)
            else None
        ),
        source_url=source_url,
        title=normalized_title,
        searchable=searchable,
        excerpt=excerpt,
        payload=payload,
        goal_role_terms=goal_role_terms,
    )


def _is_primary_detail_candidate(candidate: dict[str, Any]) -> bool:
    """Ignore recommendation cards extracted from a full JD detail page."""
    source_quality = candidate.get("source_quality")
    candidate_id = candidate.get("candidate_id")
    if source_quality != "jd_complete" or not isinstance(candidate_id, str):
        return True
    source_url = candidate.get("source_url")
    page_source_url = candidate.get("page_source_url")
    page_title = candidate.get("page_title")
    page_text_prefix = candidate.get("page_text_prefix")
    title = candidate.get("title")
    if isinstance(page_title, str) and isinstance(title, str) and page_title.strip():
        if title.strip().lower() not in page_title.lower():
            return False
    if (
        isinstance(page_text_prefix, str)
        and isinstance(title, str)
        and page_text_prefix.strip()
        and title.strip().lower() not in page_text_prefix.lower()
    ):
        return False
    if (
        isinstance(source_url, str)
        and isinstance(page_source_url, str)
        and source_url != page_source_url
    ):
        return False
    return candidate_id.endswith(":candidate:0")


def _source_allowed_for_goal(source_url: object, goal: object) -> bool:
    """Enforce explicit source/channel constraints without inventing evidence."""
    if not isinstance(source_url, str) or not source_url:
        return False
    if not isinstance(goal, str) or not goal.strip():
        return True
    goal_lower = goal.lower()
    channel_required = any(
        marker in goal_lower
        for marker in ("国聘", "官网", "中国移动", "中国联通", "10086", "10010")
    )
    if not channel_required:
        return True
    host = (urlparse(source_url).hostname or "").lower().rstrip(".")
    allowed_hosts = (
        "iguopin.com",
        "10086.cn",
        "10010.com",
        "chinaunicom.com",
        "chinaunicom.cn",
    )
    return any(host == allowed or host.endswith("." + allowed) for allowed in allowed_hosts)


def _score_job(
    *,
    artifact_id: str,
    candidate_id: str | None = None,
    source_artifact_id: str | None = None,
    source_url: str,
    title: str | None,
    searchable: str,
    excerpt: str,
    payload: MatchObservedJobsInput,
    goal_role_terms: list[str] | None = None,
) -> ObservedJobMatch:
    """Score one job unit from its searchable text, exposing unverified criteria."""
    matched = [keyword for keyword in payload.profile_keywords if keyword in searchable]
    matched_locations = [
        location for location in payload.preferred_locations if location.lower() in searchable
    ]
    compensation_match = _COMPENSATION_RE.search(searchable)
    compensation_text = compensation_match.group(0).strip() if compensation_match else None
    observed_company_types = [label for label in _COMPANY_TYPE_LABELS if label in searchable]
    unverified = _unverified_criteria(
        payload.ranking_criteria,
        matched_locations=matched_locations,
        compensation_text=compensation_text,
        observed_company_types=observed_company_types,
    )
    role_hits = sum(1 for term in goal_role_terms or [] if term.lower() in searchable)
    score = min(100, len(matched) * 34 + role_hits * 15)
    return ObservedJobMatch(
        artifact_id=artifact_id,
        candidate_id=candidate_id,
        source_artifact_id=source_artifact_id,
        source_url=source_url,
        title=title,
        score=score,
        matched_keywords=matched,
        matched_locations=matched_locations,
        compensation_text=compensation_text,
        observed_company_types=observed_company_types,
        unverified_ranking_criteria=unverified,
        evidence_excerpt=excerpt[:500],
    )


def _candidate_meets_goal_constraints(
    candidate: dict[str, Any], goal: object, profile_facts: object = None
) -> bool:
    """Apply only explicit role/location/experience constraints from the goal.

    Ranking criteria remain soft and are reported as unverified. These three
    constraints are different: returning a clearly wrong role, city, or
    minimum experience as the recommendation is a false positive, so the
    candidate is excluded before scoring. Missing evidence remains eligible
    and is surfaced through the normal unverified fields.
    """
    if not isinstance(goal, str) or not goal.strip():
        return True
    title = candidate.get("title")
    title_text = title.strip() if isinstance(title, str) else ""
    if title_text and any(marker in title_text for marker in _INVALID_TITLE_MARKERS):
        return False
    searchable = "\n".join(
        str(candidate.get(key) or "")
        for key in ("title", "company_name", "locations", "responsibilities", "requirements")
    ).lower()
    goal_lower = goal.lower()
    if "产品经理" in goal_lower or "aigc" in goal_lower:
        role_terms = ("产品经理", "aigc")
    elif "大模型应用开发" in goal_lower or "llm 应用" in goal_lower or "llm应用" in goal_lower:
        role_terms = ("大模型", "应用开发", "llm", "agent")
    elif "前端开发" in goal_lower:
        role_terms = ("前端", "frontend")
    elif "java 后端" in goal_lower or "java后端" in goal_lower:
        role_terms = ("java", "后端")
    else:
        role_terms = ()
    profile_role_terms = _profile_role_terms(profile_facts)
    if profile_role_terms:
        role_terms = tuple(dict.fromkeys((*role_terms, *profile_role_terms)))
    if role_terms and not any(term.lower() in searchable for term in role_terms):
        return False

    requested_locations = [term for term in _GOAL_LOCATION_TERMS if term in goal]
    if requested_locations and not any(term.lower() in searchable for term in requested_locations):
        return False

    experience_match = re.search(r"(\d+)\s*年(?:经验|工作经验)", goal)
    target_years = int(experience_match.group(1)) if experience_match else None
    if target_years is None:
        return True
    ranges = [
        (int(low), int(high))
        for low, high in re.findall(r"(?<!\d)(\d+)\s*[-~至]\s*(\d+)\s*年", searchable)
    ]
    minimum_years = [low for low, _high in ranges]
    minimum_years.extend(
        int(value)
        for value in re.findall(r"(?<!\d)(\d+)\s*年(?:及以上|以上)", searchable)
    )
    # Multiple explicit minimums in one captured JD are treated
    # conservatively: a candidate is not eligible when any mandatory-looking
    # minimum exceeds the user's stated experience.
    return not minimum_years or max(minimum_years) <= target_years


def _profile_role_terms(profile_facts: object) -> tuple[str, ...]:
    """Extract only explicit role-family markers from confirmed profile facts."""
    if not isinstance(profile_facts, dict):
        return ()
    text = "\n".join(
        str(value) for key, value in profile_facts.items() if "name" in str(key).lower()
    ).lower()
    if "aigc" in text or "产品经理" in text:
        return ("产品经理", "aigc")
    if "大模型" in text or "llm" in text or "agent" in text:
        return ("大模型", "应用开发", "llm", "agent")
    if "前端" in text or "frontend" in text:
        return ("前端", "frontend")
    if "java" in text and "后端" in text:
        return ("java", "后端")
    return ()


def _goal_role_terms(value: object) -> list[str]:
    """Extract only explicit role tokens from the user goal for tie-breaking."""
    if not isinstance(value, str):
        return []
    lowered = value.lower()
    return [term for term in _GOAL_ROLE_TERMS if term.lower() in lowered]


def _prefer_goal_role_matches(
    matches: list[ObservedJobMatch], goal: object
) -> list[ObservedJobMatch]:
    """Keep role-compatible candidates when the captured set contains them."""
    if not isinstance(goal, str):
        return matches
    lowered_goal = goal.lower()
    if any(marker in lowered_goal for marker in ("产品经理", "aigc")):
        terms = ("产品经理", "aigc")
    elif any(marker in lowered_goal for marker in ("大模型应用开发", "llm 应用", "llm应用")):
        terms = ("大模型", "应用开发", "llm", "agent")
    elif "前端开发" in lowered_goal:
        terms = ("前端", "frontend")
    elif "java 后端" in lowered_goal or "java后端" in lowered_goal:
        terms = ("java", "后端")
    else:
        return matches
    compatible = [
        match
        for match in matches
        if any(
            term in f"{match.title or ''}\n{match.evidence_excerpt}".lower()
            for term in terms
        )
    ]
    return compatible or matches


def _unverified_criteria(
    ranking_criteria: list[str],
    *,
    matched_locations: list[str],
    compensation_text: str | None,
    observed_company_types: list[str],
) -> list[str]:
    """Surface omitted evidence rather than silently ranking from assumptions."""
    unverified: list[str] = []
    for criterion in ranking_criteria:
        if criterion == "location" and not matched_locations:
            unverified.append(criterion)
        elif criterion == "salary" and compensation_text is None:
            unverified.append(criterion)
        elif criterion == "company_type" and not observed_company_types:
            unverified.append(criterion)
        elif criterion == "recency":
            # Captured pages currently do not preserve an authoritative publish time.
            unverified.append(criterion)
    return unverified
