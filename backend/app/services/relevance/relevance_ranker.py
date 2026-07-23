"""RelevanceRanker - cheap batched LLM scoring of JD candidates.

Scores each candidate 0-100 against the user's resume profile + job preferences
in a single LLM call per batch (``relevance_batch_size`` candidates). Output is
cached downstream (``job_relevance_scores``) so the expensive per-job
MatchService only runs on a ranked top-N.

The ranker is intentionally DB-free: it takes in-memory candidates + a profile
summary + a preferences summary and returns ``RankedCandidate`` objects. The
recommendation service owns persistence/caching.
"""

from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI

from backend.app.services.job_discovery.schemas import NormalizedJobCandidate

logger = logging.getLogger(__name__)

SYSTEM_PROMPT = """你是一个岗位相关性打分器。根据候选人的"简历画像"与"求职偏好"，对每个候选岗位打 0-100 的相关性分数。

分数标准：
- 90-100：研发岗位、城市与方向（agent / AI 应用）高度契合，硬性要求满足。
- 70-89：多数信号契合（如研发+目标城市，或方向契合但城市次优）。
- 40-69：部分契合（如方向相关但非研发，或研发但城市不在意愿地）。
- 0-39：不契合（岗位类型、城市、行业均不符）。

硬性排除：若公司命中 excluded_companies，或行业命中 excluded_industries，分数不超过 20。

matched_signals：用短语列出命中的正向信号（如 "研发岗位"、"北京"、"AI/agent方向"、"硕士匹配"）。
reason：用一句中文说明"为什么适合你"。

只返回 JSON，不要任何额外文字或解释。格式：
{"results":[{"index":0,"score":85,"reason":"...","matched_signals":["...","..."]}]}
必须为输入中的每一个候选岗位返回恰好一条结果，index 与输入一致。"""


@dataclass
class RankedCandidate:
    """One scored candidate. ``index`` aligns with the input batch position."""

    index: int
    title: str | None = None
    company_name: str | None = None
    department: str | None = None
    locations: list[str] = field(default_factory=list)
    apply_url: str | None = None
    score: float = 0.0
    reason: str = ""
    matched_signals: list[str] = field(default_factory=list)


def build_profile_summary(
    resume_text: str | None,
    evidence_candidates: list[Any],
) -> dict[str, Any]:
    """Compact, JSON-friendly view of the parsed resume for the ranker prompt."""
    facts: dict[str, Any] = {
        "name": None,
        "education": [],
        "experience": [],
        "projects": [],
        "skills": [],
        "awards": [],
        "certificates": [],
        "languages": [],
        "raw_excerpt": (resume_text or "")[:1500],
    }
    for ev in evidence_candidates:
        fp = getattr(ev, "field_path", None)
        val = getattr(ev, "candidate_value", None)
        if fp == "basics.name":
            facts["name"] = val
        elif fp in facts:
            if isinstance(val, list):
                facts[fp].extend(str(v) for v in val)
            elif val:
                facts[fp].append(str(val))
    return facts


def _get(obj: Any, key: str) -> Any:
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _truncate(value: Any, limit: int) -> str:
    if value is None:
        return ""
    text = str(value)
    return text if len(text) <= limit else text[:limit] + "…"


def _candidate_view(candidate: Any, index: int) -> dict[str, Any]:
    """Normalize a NormalizedJobCandidate (dataclass) or dict for the prompt."""
    return {
        "index": index,
        "title": _get(candidate, "title"),
        "company": _get(candidate, "company_name"),
        "department": _get(candidate, "department"),
        "locations": _get(candidate, "locations") or [],
        "recruitment_types": _get(candidate, "recruitment_types") or [],
        "industries": _get(candidate, "industries") or [],
        "requirements": _truncate(_get(candidate, "requirements"), 400),
        "responsibilities": _truncate(_get(candidate, "responsibilities"), 300),
        "description": _truncate(_get(candidate, "description_text"), 400),
    }


def _extract_json_object(text: str) -> dict[str, Any] | None:
    """Best-effort parse of a JSON object from an LLM response."""
    if not text:
        return None
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        if stripped[:4].lower() == "json":
            stripped = stripped[4:]
        stripped = stripped.strip()
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            return None
    return None


class RelevanceRanker:
    """Batched LLM relevance scorer."""

    def __init__(
        self,
        llm: ChatOpenAI,
        batch_size: int = 30,
    ) -> None:
        self.llm = llm
        self.batch_size = max(1, batch_size)

    def rank(
        self,
        candidates: list[Any],
        *,
        profile_summary: dict[str, Any],
        preferences: dict[str, Any],
    ) -> list[RankedCandidate]:
        """Score all candidates, preserving input order."""
        results: list[RankedCandidate] = []
        for start in range(0, len(candidates), self.batch_size):
            batch = candidates[start : start + self.batch_size]
            results.extend(
                self._score_batch(
                    batch,
                    base_index=start,
                    profile_summary=profile_summary,
                    preferences=preferences,
                )
            )
        return results

    def _score_batch(
        self,
        batch: list[Any],
        *,
        base_index: int,
        profile_summary: dict[str, Any],
        preferences: dict[str, Any],
    ) -> list[RankedCandidate]:
        views = [_candidate_view(c, i) for i, c in enumerate(batch)]
        payload = {
            "profile": profile_summary,
            "preferences": preferences,
            "candidates": views,
        }
        messages = [
            SystemMessage(content=SYSTEM_PROMPT),
            HumanMessage(content=json.dumps(payload, ensure_ascii=False)),
        ]

        # Default zeros if the LLM call fails - never crash the pipeline.
        scored: dict[int, dict[str, Any]] = {}
        try:
            response = self.llm.invoke(messages)
            content = getattr(response, "content", response)
            if isinstance(content, list):
                content = "".join(
                    getattr(part, "text", str(part)) for part in content
                )
            parsed = _extract_json_object(str(content))
            if parsed and isinstance(parsed.get("results"), list):
                for item in parsed["results"]:
                    if not isinstance(item, dict):
                        continue
                    idx = item.get("index")
                    if isinstance(idx, bool) or not isinstance(idx, int):
                        continue
                    scored[idx] = item
        except Exception as exc:
            logger.warning("relevance ranker LLM call failed: %s", exc)

        results: list[RankedCandidate] = []
        for local_index, candidate in enumerate(batch):
            item = scored.get(local_index)
            score = _clamp_score(item.get("score")) if item else 0.0
            results.append(
                RankedCandidate(
                    index=base_index + local_index,
                    title=_get(candidate, "title"),
                    company_name=_get(candidate, "company_name"),
                    department=_get(candidate, "department"),
                    locations=list(_get(candidate, "locations") or []),
                    apply_url=_get(candidate, "apply_url"),
                    score=score,
                    reason=(item.get("reason") or "") if item else "",
                    matched_signals=_as_str_list(item.get("matched_signals")) if item else [],
                )
            )
        return results

    # Convenience: rank NormalizedJobCandidate objects directly.
    def rank_candidates(
        self,
        candidates: list[NormalizedJobCandidate],
        *,
        profile_summary: dict[str, Any],
        preferences: dict[str, Any],
    ) -> list[RankedCandidate]:
        return self.rank(candidates, profile_summary=profile_summary, preferences=preferences)


def _clamp_score(value: Any) -> float:
    try:
        score = float(value)
    except (TypeError, ValueError):
        return 0.0
    if score < 0:
        return 0.0
    if score > 100:
        return 100.0
    return round(score, 1)


def _as_str_list(value: Any) -> list[str]:
    if isinstance(value, list):
        return [str(v) for v in value]
    if value is None:
        return []
    return [str(value)]


def ranked_to_dict(ranked: RankedCandidate) -> dict[str, Any]:
    """JSON-friendly view for API / smoke output."""
    return asdict(ranked)
