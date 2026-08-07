"""LLM extraction gate backend for the job-discovery subgraph (spec §4.3).

``LLMJobExtractor`` is the optional low-confidence extraction path behind
``extract_with_gate``: the deterministic regex extractor runs first and
costs zero tokens; when its output is empty or low-confidence, this
model-backed extractor fills the gap.  The prompt transcribes the
``skill/job-discovery/references/extraction-guide.md`` contract (field
list + output discipline); parsing is lenient (code fences, surrounding
prose, truncated JSON all tolerated); every failure mode folds to an
honest empty output.  The extractor NEVER raises, so a drifting or failing
model degrades to "no candidates" instead of crashing the run.
"""

from __future__ import annotations

import json
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from backend.app.services.agent_runtime.provider_config import get_api_key
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
    ExtractedJobDetails,
)

#: Placeholder api_key so a keyless construction is possible (the harness's
#: settings never carry an API key; the provider_config read is the single
#: source).  A real invocation without a configured key fails at call time
#: and folds to an honest empty output - it never crashes at construction.
_UNCONFIGURED_API_KEY = "not-configured"

#: The extraction-guide.md contract transcribed into the system prompt:
#: one distinct role per posting, the per-job field list (title,
#: company_name, locations, responsibilities, requirements, recruitment
#: types, apply_url, deadline_text), responsibilities/requirements
#: separation, normalization rules, the confidence scale, and the output
#: discipline (a JSON array; never invent fields absent from the text).
_EXTRACTION_PROMPT = """你是一个职位信息结构化提取器。从给定的招聘页面文本中识别出所有职位，并为每个职位输出一个 JSON 对象。输出必须是 JSON 数组，不要包含任何其他文字、代码块标记或解释。

对每个职位，提取以下字段（文本中没有的字段一律省略，绝不编造）：
- title: 职位名称（必填；如 "算法工程师"）
- company_name: 公司名称（必填；来自页面页头或域名，不能从正文推断时留空）
- locations: 工作地点列表（如 ["北京", "上海"]；未提及城市则留空数组）
- responsibilities: 岗位职责（"岗位职责"/"工作内容"/"职位描述" 下的内容；动词开头：负责/参与/设计/开发/优化）
- requirements: 任职要求（"任职要求"/"岗位要求"/Qualifications 下的内容；学历、技能、经验等）
- recruitment_types: 招聘类型（如 ["校园招聘", "提前批"]；无则留空数组）
- apply_url: 申请链接或详情页 URL（无则省略）
- deadline_text: 截止日期原文（无则省略）
- confidence: 0.50-0.95 的置信度；职责与要求清晰分节=0.95，整体清晰但需少量推断=0.85，职责/要求需人工拆分=0.75，重度推断或大量字段缺失=0.60，非常模糊（仅标题）需人工复核=0.50

规范：
1. 一个职位 = 一个独立岗位。不同城市、不同部门、实习 vs 全职都是独立职位。
2. 职责与要求混排时（如 "我们需要一位精通Python的工程师负责后端开发"），把 "负责后端开发" 放入 responsibilities，"精通Python" 放入 requirements，并在 normalization_warnings 中注明 "职责和要求在原文中未显式分开，由LLM分割"。
3. 标题规范化：去除年份/批次前缀（"2026届-算法工程师" -> title="算法工程师"，recruitment_types 增加 "校园招聘"）；保留必要的级别词（"高级"）。
4. 地点规范化：去掉省/区后缀（"北京市朝阳区" -> "北京"）；拆分组合地点（"北京/上海/深圳" -> ["北京","上海","深圳"]）。
5. 常见陷阱：不要把 "公司介绍" 当作职位描述；不同角色（嵌入式软件工程师 vs 嵌入式硬件工程师）不要合并；仅提取到标题时 confidence 打低并注明 "仅提取到岗位名称，页面未显示详细JD"。
6. 页面只有标题没有描述时，confidence 使用 0.50。

只输出 JSON 数组本身。"""


def _find_evidence(
    context: ToolContext, payload: ExtractObservedJobDetailsInput
) -> dict[str, Any] | None:
    """Locate the registered observed evidence for a payload artifact.

    Mirrors career_skills ``_find_observed_evidence`` (read-only module):
    an evidence item matches when its ``artifact_id`` equals the payload's,
    or when the payload's artifact_id is the ``observed:``-prefixed form of
    the item's ``content_hash``.  Non-dict entries are skipped.
    """
    for item in context.metadata.get("observed_public_evidence", []):
        if not isinstance(item, dict):
            continue
        if item.get("artifact_id") == payload.artifact_id:
            return item
        if item.get("content_hash") and (
            f"observed:{item.get('content_hash')}" == payload.artifact_id
        ):
            return item
    return None


def _balanced_span(raw: str, opener: str, closer: str, start: int) -> int | None:
    """Index of the closer matching the opener at ``start`` (string-aware).

    JSON strings are skipped, so braces/quotes inside a field value never
    confuse the scan; an unbalanced span returns None.
    """
    depth = 0
    in_string = False
    escaped = False
    for index in range(start, len(raw)):
        ch = raw[index]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return index
    return None


def _lenient_json(raw: str) -> Any:
    """Parse model output as JSON, tolerating fences/prose/truncation.

    Stage order: strip ```fences``` -> whole-document load -> balanced
    ``[...]`` -> balanced ``{...}``.  A standalone brace-scan stage would be
    redundant: the balanced scan always consumes the first complete object,
    so a truncated array already recovers its first object via the balanced
    ``[...]`` stage.
    """
    fenced = re.search(r"```(?:json)?\s*(.*?)```", raw, flags=re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    candidates = [raw]
    for opener, closer in (("[", "]"), ("{", "}")):
        start = raw.find(opener)
        end = _balanced_span(raw, opener, closer, start) if start != -1 else None
        if end is not None:
            candidates.append(raw[start : end + 1])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    raise ValueError("no JSON value found")


def _normalize_items(parsed: Any) -> list[dict[str, Any]]:
    """Coerce a parsed payload into a list of candidate dicts."""
    if isinstance(parsed, dict):
        wrapped = parsed.get("candidates")
        if isinstance(wrapped, list):
            return [item for item in wrapped if isinstance(item, dict)]
        return [parsed]
    if isinstance(parsed, list):
        return [item for item in parsed if isinstance(item, dict)]
    return []


def _to_candidate(
    item: dict[str, Any],
    payload: ExtractObservedJobDetailsInput,
    source_url: str,
) -> ExtractedJobDetails | None:
    """Map a parsed item to ExtractedJobDetails; invalid items are dropped.

    Missing fields fall back to empty defaults; the evidence refs are always
    tool-bound to the payload artifact (model-proposed refs are never
    trusted).  A structurally invalid item (wrong field types) is dropped,
    never raised.
    """
    try:
        return ExtractedJobDetails(
            title=item.get("title"),
            company_name=item.get("company_name"),
            locations=item.get("locations") or [],
            responsibilities=item.get("responsibilities") or "",
            requirements=item.get("requirements") or "",
            recruitment_types=item.get("recruitment_types") or [],
            apply_url=item.get("apply_url"),
            deadline_text=item.get("deadline_text"),
            confidence=item.get("confidence", 0.5),
            evidence_refs=[
                {
                    "artifact_id": payload.artifact_id,
                    "source_url": source_url,
                    "content_hash": payload.artifact_id,
                }
            ],
            normalization_warnings=item.get("normalization_warnings") or [],
        )
    except (ValidationError, TypeError, ValueError):
        return None


def _fold(payload: ExtractObservedJobDetailsInput) -> ExtractObservedJobDetailsOutput:
    """Honest empty output: no source URL, no candidates (never raises)."""
    return ExtractObservedJobDetailsOutput(
        source_artifact_id=payload.artifact_id,
        source_url="",
        content_hash=payload.artifact_id,
        candidates=[],
    )


class LLMJobExtractor:
    """Model-backed low-confidence extraction for the job-discovery gate.

    Same model/params as the eval path (``agent_harness_model``,
    temperature 0, max_tokens 4096).  The model is invoked with the page
    text from the registered observed evidence - never with a model-proposed
    URI - and any model or parse failure folds to an empty output.
    """

    def __init__(self, settings) -> None:
        self._model = ChatOpenAI(
            model=settings.agent_harness_model,
            temperature=0,
            max_tokens=4096,
            # keyless construction must not crash; a real invocation without
            # a configured key fails at call time and folds to empty output
            api_key=get_api_key() or _UNCONFIGURED_API_KEY,
        )

    def __call__(
        self,
        context: ToolContext,
        payload: ExtractObservedJobDetailsInput,
    ) -> ExtractObservedJobDetailsOutput:
        evidence = _find_evidence(context, payload)
        if evidence is None:
            return _fold(payload)
        text = evidence.get("visible_text")
        if not text:
            return _fold(payload)
        source_url = evidence.get("source_url", "")
        try:
            response = self._model.invoke(
                [
                    SystemMessage(content=_EXTRACTION_PROMPT),
                    HumanMessage(content=text),
                ]
            )
        except Exception:  # noqa: BLE001 - fold, never raise
            return _fold(payload)
        try:
            parsed = _lenient_json(response.content)
        except ValueError:
            return _fold(payload)
        candidates: list[ExtractedJobDetails] = []
        for item in _normalize_items(parsed):
            candidate = _to_candidate(item, payload, source_url)
            if candidate is not None:
                candidates.append(candidate)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url=source_url,
            content_hash=payload.artifact_id,
            candidates=candidates,
        )


def build_llm_extractor(settings) -> LLMJobExtractor | None:
    """Build the gate's LLM extractor iff the feature flag is on.

    Returns None when ``deepagents_llm_extraction_enabled`` is False, which
    keeps the gate permanently off in tests and in default deployments.
    """
    if not settings.deepagents_llm_extraction_enabled:
        return None
    return LLMJobExtractor(settings)
