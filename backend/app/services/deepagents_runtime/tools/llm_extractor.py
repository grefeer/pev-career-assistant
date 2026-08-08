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

C1 (docs/findjobs-optimization-plan.zh-CN.md §6.1): the outbound call is
wired through ``build_agent_chat_model`` - the same provider transport as
the PEV decision gateways (deepseek-v4: thinking disabled + json_mode) -
and invocations climb the gateway's drift ladder: one call, then up to two
corrective-hint retries, then ``AgentModelGatewayError``, which folds to
the honest empty output instead of a bare exception.
"""

from __future__ import annotations

import json
import logging
import re
from typing import Any

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_openai import ChatOpenAI
from pydantic import ValidationError

from backend.app.services.agent_runtime.model_gateway import (
    AgentModelGatewayConfigError,
    AgentModelGatewayError,
    build_agent_chat_model,
)
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsInput,
    ExtractObservedJobDetailsOutput,
    ExtractedJobDetails,
)
from backend.app.services.job_discovery.tools.job_strength import analyze_job_strength
from backend.app.services.job_discovery.tools.skill_validator import (
    load_skill_tags,
    validate_skills,
)
from backend.app.services.job_discovery.tools.taxonomy import taxonomy_tags

#: Placeholder api_key so a keyless construction is possible (the harness's
#: settings never carry an API key; the provider_config read is the single
#: source).  A real invocation without a configured key fails at call time
#: and folds to an honest empty output - it never crashes at construction.
_UNCONFIGURED_API_KEY = "not-configured"

#: C1 corrective hint appended on each malformed-completion retry (same
#: bounded budget as the gateway's local-JSON ladder: three total attempts).
_RECOVERY_HINT = (
    "你上一次的回复没有包含可解析的 JSON 职位数组。请只输出一个 JSON 数组，"
    "不要任何前后说明文字、代码块标记或结尾标点。"
)

logger = logging.getLogger(__name__)

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
- min_degree: 学历要求（仅限：大专/本科/硕士/博士/不限；文本未提学历则省略）
- priority: 学历/要求是必须还是加分（仅限 "must" / "preferred"；文本出现 "必须/必备/要求具备" 用 must，"优先/加分项" 用 preferred；未提及则省略）
- skills: 从技能闭集中选择（最多 8 项，只能从下方闭集列表里选，绝不编造；文本明确提到的技能才选；无明确技能则给空数组）
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
    requirements = item.get("requirements") or ""
    responsibilities = item.get("responsibilities") or ""
    try:
        return ExtractedJobDetails(
            title=item.get("title"),
            company_name=item.get("company_name"),
            locations=item.get("locations") or [],
            responsibilities=responsibilities,
            requirements=requirements,
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
            # A2: closed-set validation with a deterministic JD-text fallback
            # (FindJobs --min-skills 3); illegal/low-information tags never leak.
            skills=validate_skills(
                item.get("skills") or [],
                fallback_text=f"{responsibilities}\n{requirements}",
                min_tags=3,
            ),
            min_degree=item.get("min_degree"),
            priority=item.get("priority", "unknown"),
            # B1: strength is derived deterministically from the extracted
            # sections (same input as the regex path), never trusted from
            # the model.
            strength=analyze_job_strength(
                f"{responsibilities}\n{requirements}"
            ).to_dict(),
            # B2: taxonomy derived deterministically from the model-extracted
            # sections, never trusted from the model.
            taxonomy=taxonomy_tags(
                f"{item.get('title') or ''}\n{responsibilities}\n{requirements}"
            ),
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
        try:
            # C1: the same provider transport as the PEV decision gateways
            # (deepseek-v4: thinking disabled + json_mode), with the
            # extraction-specific output cap.
            self._model, self._structured_method = build_agent_chat_model(
                settings, max_tokens=4096
            )
        except AgentModelGatewayConfigError:
            # keyless construction must not crash; a real invocation without
            # a configured key fails at call time and folds to empty output
            self._model = ChatOpenAI(
                model=settings.agent_harness_model,
                temperature=0,
                max_tokens=4096,
                api_key=_UNCONFIGURED_API_KEY,
            )
            self._structured_method = "json_mode"

    def _invoke_with_recovery(
        self, messages: list[SystemMessage | HumanMessage]
    ) -> Any:
        """Ordinary-JSON drift ladder (C1): 1 call + up to 2 corrective retries.

        For json_mode providers the wire call carries ``response_format``
        ``json_object`` (the json_mode protocol).  Transport failures raise
        ``model_request_failed``; a non-string completion raises
        ``invalid_model_response``; a completion that still parses to no JSON
        after the last attempt raises ``invalid_model_response``.  The caller
        folds every error to an honest empty output, never a bare exception.
        """
        attempt = 0
        while True:
            try:
                if self._structured_method == "json_mode":
                    raw_result = self._model.invoke(
                        messages, response_format={"type": "json_object"}
                    )
                else:
                    raw_result = self._model.invoke(messages)
            except Exception as exc:  # noqa: BLE001 - provider boundary.
                raise AgentModelGatewayError("model_request_failed") from exc
            content = getattr(raw_result, "content", raw_result)
            if not isinstance(content, str):
                raise AgentModelGatewayError("invalid_model_response")
            try:
                return _lenient_json(content)
            except ValueError:
                if attempt == 2:
                    logger.warning(
                        "extractor exhausted recovery ladder; model=%s attempts=3",
                        getattr(self._model, "model", "?"),
                    )
                    raise AgentModelGatewayError("invalid_model_response")
                logger.warning(
                    "extractor recovery needed; attempt=%s model=%s",
                    attempt + 1,
                    getattr(self._model, "model", "?"),
                )
                messages = [*messages, SystemMessage(content=_RECOVERY_HINT)]
                attempt += 1

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
            # A2: the reviewed closed set is injected into the system prompt so
            # the model only ever proposes members (max 8, same as FindJobs).
            # The join is per-call but load_skill_tags is lru_cached; both sit
            # inside the try so a missing/corrupt data file folds to empty.
            closed_set = ", ".join(load_skill_tags())
            parsed = self._invoke_with_recovery(
                [
                    SystemMessage(
                        content=(
                            f"{_EXTRACTION_PROMPT}\n\n"
                            f"技能闭集（只能从这些标签中选择，最多 8 项）：{closed_set}"
                        )
                    ),
                    HumanMessage(content=text),
                ]
            )
        except Exception:  # noqa: BLE001 - fold, never raise
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
