"""Shared parsing and validation for discovery supervisor results."""

from __future__ import annotations

import json
import re
from dataclasses import asdict, fields as dataclass_fields, is_dataclass, replace
from typing import Any, Iterator

from backend.app.services.job_discovery.deduplication.canonical_job_deduplicator import (
    _cluster_by_title_substring,
    _identity_key,
)
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    NormalizedJobCandidate,
)


_RESULT_FIELDS = {"status", "block_reason", "evidence", "candidates", "summary"}
_FENCED_JSON_PATTERN = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)
# deepagents ``FilesystemMiddleware`` replaces oversized ``ToolMessage.content``
# with a placeholder when a tool result exceeds ``tool_token_limit_before_evict``
# (default 20000 tokens, ~80k chars). The real payload is offloaded to the
# filesystem state at ``raw["files"][path]`` (a ``FileData`` dict whose ``content``
# is the original JSON string). ``run_web_navigation`` on large career sites
# (xiaomi: ~166 evidence pages, ~422k-char result) always trips this, so its
# evidence/candidates never reach ``_coerce_json_value`` and the supervisor falls
# to ``parse_failed``. The placeholder path is recovered here.
_EVICTION_PATH_PATTERN = re.compile(
    r"saved in the filesystem at this path:\s*(\S+)"
)
_CANDIDATE_FIELDS = {f.name for f in dataclass_fields(NormalizedJobCandidate)}


class AgentResultParseError(ValueError):
    """Raised when an agent result does not contain a structured result."""

    def __init__(self, *, message_types: list[str], message_count: int) -> None:
        super().__init__("Could not parse structured output from agent result")
        self.message_types = message_types
        self.message_count = message_count


def parse_agent_result(raw: Any) -> DiscoveryRunResult:
    """Parse the first valid discovery result without retaining message bodies."""
    tool_evidence, tool_candidates = _collect_tool_outputs(raw)
    # Collapse duplicates the supervisor emits when it re-runs package_candidates
    # on evidence run_web_navigation already packaged (different idempotency
    # keys -> not byte-identical -> survive _unique_items). Applied here so both
    # the tool-only recovery path and the structured-merge path see deduped input.
    tool_candidates = _dedupe_candidate_dicts(tool_candidates)
    for candidate in _iter_result_candidates(raw):
        parsed = _coerce_result_dict(candidate)
        if parsed is not None:
            return _merge_tool_outputs(
                DiscoveryRunResult(**_known_result_fields(parsed)),
                evidence=tool_evidence,
                candidates=tool_candidates,
            )

    if tool_evidence or tool_candidates:
        # tool_candidates come from run_web_navigation's deterministic
        # _extract_and_verify_candidates_from_evidence (already verified +
        # packaged). They are authoritative; the supervisor LLM simply did not
        # emit a structured_response shell. Recovered candidates => succeeded.
        if tool_candidates:
            return DiscoveryRunResult(
                status="succeeded",
                block_reason=None,
                evidence=tool_evidence,
                candidates=_to_candidate_objects(tool_candidates),
                summary=(
                    f"Discovered {len(tool_candidates)} candidate(s) via "
                    "run_web_navigation (supervisor did not emit a structured "
                    "result; candidates recovered from tool outputs)."
                ),
            )
        return DiscoveryRunResult(
            status="needs_manual_review",
            block_reason="parse_failed",
            evidence=tool_evidence,
            candidates=tool_candidates,
            summary="Recovered incomplete discovery output from tool results",
        )

    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    if not isinstance(messages, list):
        messages = []
    raise AgentResultParseError(
        message_types=[type(item).__name__ for item in messages],
        message_count=len(messages),
    )


def enforce_result_invariants(result: DiscoveryRunResult) -> DiscoveryRunResult:
    """Ensure completed outcomes always carry at least one candidate."""
    if result.status == "succeeded" and not result.candidates:
        return replace(result, status="failed", block_reason="parse_failed")
    if result.status == "partial_success" and not result.candidates:
        return replace(
            result,
            status="needs_manual_review",
            block_reason=result.block_reason or "parse_failed",
        )
    return result


def _iter_result_candidates(raw: Any) -> Iterator[Any]:
    if isinstance(raw, dict):
        if "structured_response" in raw:
            yield raw["structured_response"]
        yield raw
        messages = raw.get("messages", [])
        if isinstance(messages, list):
            for message in reversed(messages):
                yield getattr(message, "content", message)
    else:
        yield raw


def _coerce_result_dict(candidate: Any) -> dict[str, Any] | None:
    if hasattr(candidate, "model_dump"):
        candidate = candidate.model_dump()
    if isinstance(candidate, dict):
        return candidate if "status" in candidate else None
    if isinstance(candidate, list):
        for block in candidate:
            if isinstance(block, dict) and block.get("type") == "text":
                parsed = _coerce_result_dict(block.get("text"))
                if parsed is not None:
                    return parsed
        return None
    if not isinstance(candidate, str):
        return None

    content = candidate.strip()
    fenced = _FENCED_JSON_PATTERN.match(content)
    if fenced:
        content = fenced.group(1).strip()
    try:
        parsed = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) and "status" in parsed else None


def _known_result_fields(parsed: dict[str, Any]) -> dict[str, Any]:
    return {name: value for name, value in parsed.items() if name in _RESULT_FIELDS}


def _collect_tool_outputs(raw: Any) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Collect persisted result data emitted by navigation and packaging tools."""
    messages = raw.get("messages", []) if isinstance(raw, dict) else []
    files = raw.get("files", {}) if isinstance(raw, dict) else {}
    if not isinstance(messages, list):
        return [], []

    evidence: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for message in messages:
        tool_name = getattr(message, "name", None)
        content = getattr(message, "content", None)
        payload = _coerce_json_value(content)
        # deepagents evicts oversized tool results (default > 20000 tokens) to
        # the filesystem state and leaves a placeholder in the ToolMessage;
        # recover the real payload so large navigation results (xiaomi: ~166
        # evidence pages) are not silently lost to ``parse_failed``.
        if payload is None:
            payload = _recover_evicted_payload(content, files)
        if tool_name in {"run_web_navigation", "extract_rendered_job_evidence"}:
            if isinstance(payload, dict):
                pages = payload.get("evidence_pages") or payload.get("evidence") or []
                if isinstance(pages, list):
                    evidence.extend(item for item in pages if isinstance(item, dict))
                cands = payload.get("candidates")
                if isinstance(cands, list):
                    candidates.extend(item for item in cands if isinstance(item, dict))
        elif tool_name == "package_candidates" and isinstance(payload, list):
            candidates.extend(item for item in payload if isinstance(item, dict))
    return evidence, candidates


def _coerce_json_value(candidate: Any) -> Any:
    if not isinstance(candidate, str):
        return None
    content = candidate.strip()
    fenced = _FENCED_JSON_PATTERN.match(content)
    if fenced:
        content = fenced.group(1).strip()
    try:
        return json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return None


def _recover_evicted_payload(content: Any, files: Any) -> Any:
    """Recover a tool payload deepagents evicted to the filesystem state.

    deepagents' ``FilesystemMiddleware`` offloads oversized tool results to
    ``raw["files"][path]`` and replaces ``ToolMessage.content`` with a
    ``TOO_LARGE_TOOL_MSG`` placeholder pointing at the path. This reads the
    offloaded file content (a ``FileData`` dict with a ``content`` str) and
    coerces it. Returns the parsed payload (dict/list) or ``None`` when the
    content is not an eviction placeholder or the file is missing.

    The agent (and its ``read_file``/``grep`` tools) cannot reliably inspect
    such results: they are single-line JSON of hundreds of KB, and the tools
    truncate at ~80k chars - hiding keys near the end (e.g. ``candidates``).
    Recovering the full payload programmatically avoids that truncation.
    """
    if not isinstance(content, str) or not isinstance(files, dict):
        return None
    match = _EVICTION_PATH_PATTERN.search(content)
    if not match:
        return None
    file_data = files.get(match.group(1))
    if not isinstance(file_data, dict):
        return None
    return _coerce_json_value(file_data.get("content"))


def _merge_tool_outputs(
    result: DiscoveryRunResult,
    *,
    evidence: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
) -> DiscoveryRunResult:
    # ``result.candidates`` (from the supervisor's structured response) may be
    # ``NormalizedJobCandidate`` objects or dicts; ``candidates`` (from tool
    # outputs) are dicts. Normalize to dicts, semantic-dedup, then convert back
    # to objects so ``DiscoveryRunResult.candidates`` stays ``list[NormalizedJobCandidate]``
    # regardless of which path produced it.
    merged_dicts = _dedupe_candidate_dicts(
        [*_as_candidate_dicts(result.candidates), *candidates]
    )
    return replace(
        result,
        evidence=_unique_items([*result.evidence, *evidence]),
        candidates=_to_candidate_objects(merged_dicts),
    )


def _unique_items(items: list[Any]) -> list[Any]:
    unique: list[Any] = []
    seen: set[str] = set()
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
        if key not in seen:
            seen.add(key)
            unique.append(item)
    return unique


def _candidate_to_dict(item: Any) -> dict[str, Any] | None:
    """Coerce a candidate (object or dict) to a plain dict for dedup."""
    if isinstance(item, dict):
        return item
    if is_dataclass(item) and not isinstance(item, type):
        return asdict(item)
    if hasattr(item, "model_dump"):
        return item.model_dump()
    return None


def _as_candidate_dicts(items: list[Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for item in items:
        coerced = _candidate_to_dict(item)
        if coerced is not None:
            out.append(coerced)
    return out


def _to_candidate_object(d: dict[str, Any]) -> NormalizedJobCandidate | None:
    """Build a ``NormalizedJobCandidate`` from a packaged candidate dict.

    Packaging dicts carry the dataclass fields plus ``idempotency_key`` /
    ``similarity_group_key``; only the dataclass fields are forwarded (the
    worker recomputes the keys for objects). Returns ``None`` for dicts that
    cannot be coerced (kept verbatim by the dict-level dedup, dropped only at
    the final object conversion).
    """
    fields = {name: value for name, value in d.items() if name in _CANDIDATE_FIELDS}
    try:
        return NormalizedJobCandidate(**fields)
    except Exception:
        return None


def _to_candidate_objects(dicts: list[dict[str, Any]]) -> list[NormalizedJobCandidate]:
    out: list[NormalizedJobCandidate] = []
    for d in dicts:
        obj = _to_candidate_object(d)
        if obj is not None:
            out.append(obj)
    return out


def _dedupe_candidate_dicts(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse duplicate candidate dicts via the canonical dedup.

    The supervisor frequently emits the same job twice: ``run_web_navigation``
    returns verified + packaged candidates AND the LLM re-runs
    ``package_candidates`` on the same evidence. ``_unique_items`` only collapses
    byte-identical JSON, so re-packaged duplicates (different idempotency keys)
    survive into the final result. This applies the production canonical dedup
    (title-only by normalized title; full-JD by company + ``core_hash`` with
    title-substring clustering) and keeps the first packaging dict per surviving
    candidate so idempotency / similarity keys are preserved through the dict
    stage. Mirrors ``deduplicate_candidates`` exactly but operates on dicts and
    retains the original packaging fields instead of producing merged objects.
    """
    if not candidates:
        return candidates
    pairs: list[tuple[NormalizedJobCandidate | None, dict[str, Any]]] = []
    for item in candidates:
        if not isinstance(item, dict):
            continue
        pairs.append((_to_candidate_object(item), item))
    if not pairs:
        return candidates

    # Bucket by canonical identity, preserving first-seen order.
    groups: dict[tuple | None, list[int]] = {}
    order: list[tuple | None] = []
    for index, (obj, _dict) in enumerate(pairs):
        key = _identity_key(obj) if obj is not None else None
        bucket = groups.get(key)
        if bucket is None:
            groups[key] = [index]
            order.append(key)
        else:
            bucket.append(index)

    out: list[dict[str, Any]] = []
    for key in order:
        member_idxs = groups[key]
        member_objs = [pairs[i][0] for i in member_idxs]
        # Full-JD groups partition by title-substring clustering; title-only,
        # single-member, and unparseable groups are a single cluster.
        if key is not None and key[0] == "jd" and len(member_idxs) > 1:
            obj_clusters = _cluster_by_title_substring(member_objs)
            obj_to_idx = {id(obj): idx for idx, obj in zip(member_idxs, member_objs)}
            idx_clusters = [
                [obj_to_idx[id(obj)] for obj in cluster] for cluster in obj_clusters
            ]
        else:
            idx_clusters = [member_idxs]
        for cluster in idx_clusters:
            out.append(pairs[cluster[0]][1])
    return out
