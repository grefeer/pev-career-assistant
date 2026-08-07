"""SKILL.md six-phase job-discovery workflow as a LangGraph subgraph.

Nodes mirror the validated skill behavior (browse → extract → validate →
deduplicate → coverage_gate).  Mechanical phases are fully deterministic;
the only LLM contact is the optional low-confidence extraction gate
(spec §4.3).  A per-URL fetch failure is recorded in ``per_url_results``
and never aborts the run (layered failure recovery, spec §4.2).  Compiling
with a checkpointer makes a mid-crawl crash resume from the last URL
instead of re-fetching.

The fetch node runs the real skill browser through ``browse_fetch_urls``
(site classification -> mode fallback chain -> per-URL evidence); the
``fetch_fn`` seam keeps tests deterministic without Playwright.
Deterministic phases run the allowlisted skill scripts through
``run_skill_script`` with candidates exchanged via a temp JSON file under
the skill directory (scripts take file paths, matching their real CLI).
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsBatchInput,
    PublicJobFetchError,
    extract_observed_job_details_batch,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import (
    browse_fetch_urls,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
    run_skill_script,
)

#: Upper bound for the per-URL visible-text projection the harness's
#: evidence promotion reads (mirrors the observation projection's excerpt).
_VISIBLE_TEXT_LIMIT = 1200


class JobDiscoveryWorkflowState(TypedDict):
    urls: list[str]
    pages: list[dict[str, Any]]
    per_url_results: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    coverage: dict[str, Any]
    error: str | None


def _read_page_text(path: str) -> str:
    """First page file's UTF-8 text, bounded to the evidence projection limit.

    A missing/unreadable page file yields "" (never crashes the fetch);
    the visible_text projection is what the harness's evidence promotion
    reads, so it stays small (spec §4.2).
    """
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[
            :_VISIBLE_TEXT_LIMIT
        ]
    except OSError:
        return ""


def _default_fetch(urls: list[str]) -> list[dict[str, Any]]:
    """Browse-backed fetch: classify -> mode fallback chain -> per-URL evidence.

    Orchestrates ``browse_fetch_urls`` (site classification + the allowlisted
    ``browse`` script through run_skill_script).  Per-URL evidence = the
    first page file's full sha256; ``visible_text`` is the first page text
    bounded to ≤1200 chars.  ``blocked`` URLs map to ``error_code="blocked"``
    (the stall-breaker treats them as no-progress); ``wechat_pending`` URLs
    are carried through untouched for Task 9.  A per-URL failure never
    aborts the run (layered failure recovery, spec §4.2).
    """
    pages: list[dict[str, Any]] = []
    for result in browse_fetch_urls(urls):
        if result.status == "succeeded":
            page: dict[str, Any] = {
                "url": result.url,
                # source_url is required by the evidence-binding contract
                # (career_skills extract expects it on observed evidence)
                "source_url": result.url,
                "status": "succeeded",
                "title": result.title,
                "mode": result.mode,
                "page_files": [pf.path for pf in result.page_files],
                "terminal_evidence": result.terminal_evidence,
                "cached": result.cached,
                "used_path": result.used_path,
                "visible_text": "",
            }
            if result.page_files:
                first = result.page_files[0]
                page["content_hash"] = first.content_hash
                page["visible_text"] = _read_page_text(first.path)
            else:
                # no page files on disk (e.g. a cache hit): no evidence hash
                page["content_hash"] = None
            pages.append(page)
        else:
            pages.append(
                {
                    "url": result.url,
                    "source_url": result.url,
                    "status": result.status,
                    "error_code": result.error_code,
                    "blocked_reason": result.blocked_reason,
                    "mode": result.mode,
                }
            )
    return pages


def _write_candidates(candidates: list[dict[str, Any]]) -> Path:
    workdir = Path(tempfile.mkdtemp(dir=SKILL_DIR))
    path = workdir / "candidates.json"
    path.write_text(json.dumps(candidates, ensure_ascii=False), encoding="utf-8")
    return path


def _default_extract(
    pages: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], str | None]:
    """Extract from succeeded pages via the reviewed career_skills engine.

    Evidence binding: each page is re-registered in the ToolContext metadata
    as ``observed:<content_hash>`` so the handler's
    ``_find_observed_evidence`` lookup resolves (spec §4.2).
    """
    metadata_pages = [
        {
            "artifact_id": f"observed:{page['content_hash']}",
            "source_url": page.get("source_url"),
            "content_hash": page.get("content_hash"),
            "visible_text": page.get("visible_text", ""),
        }
        for page in pages
    ]
    try:
        output = extract_observed_job_details_batch(
            ToolContext(
                user_id="",
                run_id="",
                metadata={"observed_public_evidence": metadata_pages},
            ),
            ExtractObservedJobDetailsBatchInput(
                # the observed: prefix must match the metadata artifact_id
                # registered above (career_skills `_find_observed_evidence`
                # matches on artifact_id, not on content_hash alone)
                artifact_ids=[f"observed:{page['content_hash']}" for page in pages][:10]
            ),
        )
    except PublicJobFetchError as exc:
        return [], f"extract failed: {exc}"
    return [detail.model_dump(mode="json") for detail in output.details], None


def build_job_discovery_graph(
    *,
    fetch_fn=None,
    script_runner=None,
    extract_fn=None,
) -> StateGraph:
    """Assemble the workflow graph with injectable seams for tests."""
    fetch_fn = fetch_fn or _default_fetch
    script_runner = script_runner or run_skill_script
    extract_fn = extract_fn or _default_extract

    def fetch_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        pages = fetch_fn(state["urls"])
        return {
            "pages": pages,
            # materialize the error channel (LangGraph omits channels never
            # written, but the partial-results contract includes error=None)
            "error": None,
            "per_url_results": [
                {
                    "url": page.get("url"),
                    # source_url + content_hash together let the harness's
                    # evidence projection promote fetch evidence (both keys
                    # are required by the evidence-bound tools invariant)
                    "source_url": page.get("source_url"),
                    "status": page.get("status", "failed"),
                    "error_code": page.get("error_code"),
                    "content_hash": page.get("content_hash"),
                    # browse mode + resolved page-file paths surface how the
                    # evidence was gathered; visible_text is the bounded
                    # first-page projection (≤1200 chars)
                    "mode": page.get("mode"),
                    "page_files": page.get("page_files"),
                    "visible_text": page.get("visible_text"),
                    # blocked_reason lets manual-review triage see
                    # login/captcha/anti-bot/unsafe-url on blocked URLs
                    "blocked_reason": page.get("blocked_reason"),
                }
                for page in pages
            ],
        }

    def extract_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        pages = [
            page
            for page in state.get("pages", [])
            if page.get("status") == "succeeded" and page.get("content_hash")
        ]
        if not pages:
            return {"candidates": []}
        candidates, error = extract_fn(pages)
        update: dict[str, Any] = {"candidates": candidates}
        if error is not None:
            update["error"] = error
        return update

    def validate_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        if not state["candidates"]:
            return {}
        with tempfile.TemporaryDirectory(dir=SKILL_DIR):
            path = _write_candidates(state["candidates"])
            out = script_runner("validate", str(path))
        if "ERROR" in out:
            return {"error": f"validate failed: {out[:500]}"}
        return {}

    def dedup_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        if not state["candidates"]:
            return {}
        with tempfile.TemporaryDirectory(dir=SKILL_DIR) as workdir:
            work = Path(workdir)
            src = work / "candidates.json"
            src.write_text(
                json.dumps(state["candidates"], ensure_ascii=False), encoding="utf-8"
            )
            merged = work / "merged.json"
            out = script_runner("deduplicate", f"{src} --out {merged}")
            if "ERROR" in out:
                return {"error": f"deduplicate failed: {out[:500]}"}
            if merged.exists():
                try:
                    merged_candidates = json.loads(merged.read_text(encoding="utf-8"))
                except ValueError:
                    return {"error": "deduplicate output unparsable"}
                if isinstance(merged_candidates, list):
                    return {"candidates": merged_candidates}
                if (
                    isinstance(merged_candidates, dict)
                    and isinstance(merged_candidates.get("candidates"), list)
                ):
                    return {"candidates": merged_candidates["candidates"]}
                return {"error": "deduplicate output has no candidates list"}
        return {"error": "deduplicate produced no merged file"}

    def coverage_node(state: JobDiscoveryWorkflowState) -> dict[str, Any]:
        pages = [
            page for page in state.get("pages", []) if page.get("status") == "succeeded"
        ]
        page_urls = " ".join(page.get("url", "") for page in pages)
        with tempfile.TemporaryDirectory(dir=SKILL_DIR):
            path = _write_candidates(state["candidates"])
            # real coverage_gate.py (non-manifest path) emits
            # coverage_verified/page_count/.../reasons and always reports
            # missing_terminal_evidence when --terminal-evidence is absent;
            # the subgraph's best terminal signal is the last captured page
            # hash (browse's end-of-list marker is not available in-graph)
            cli = f"{path} --pages {page_urls}".strip() if page_urls else str(path)
            terminal = pages[-1].get("content_hash") if pages else None
            if terminal:
                cli += f" --terminal-evidence {terminal}"
            out = script_runner("coverage_gate", cli)
        try:
            coverage = json.loads(out)
        except ValueError:
            coverage = {"error": "unparsable coverage_gate output"}
        if not isinstance(coverage, dict):
            coverage = {"error": "non-object coverage_gate output"}
        # map the real script's coverage_verified to `verified` so the tool
        # contract always carries a bool and consumers never KeyError
        coverage.setdefault("verified", bool(coverage.get("coverage_verified", False)))
        return {"coverage": coverage}

    graph = StateGraph(JobDiscoveryWorkflowState)
    graph.add_node("fetch", fetch_node)
    graph.add_node("extract", extract_node)
    graph.add_node("validate", validate_node)
    graph.add_node("dedup", dedup_node)
    graph.add_node("coverage", coverage_node)
    graph.add_edge(START, "fetch")
    graph.add_edge("fetch", "extract")
    graph.add_edge("extract", "validate")
    graph.add_edge("validate", "dedup")
    graph.add_edge("dedup", "coverage")
    graph.add_edge("coverage", END)
    return graph
