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
Extraction fans out per page file (Task 8): each page's candidates are
persisted to ``page_NN.json`` via the ``write_candidates`` script (stdin
contract), and the dedup node consumes exactly those per-page files and
emits ``merged_final.json``, surfacing the merged count + dedup stats in
the tool output (U10 ruling).  Deterministic phases run the allowlisted
skill scripts through ``run_skill_script`` with candidates exchanged via
file paths (matching the scripts' real CLI).
"""

from __future__ import annotations

import functools
import json
import tempfile
from pathlib import Path
from typing import Any, Callable, TypedDict

from langgraph.graph import END, START, StateGraph

from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsBatchInput,
    ExtractObservedJobDetailsInput,
    ExtractedJobDetails,
    PublicJobFetchError,
    extract_observed_job_details,
    extract_observed_job_details_batch,
)
from backend.app.services.deepagents_runtime.tools.extract_gate import extract_with_gate
from backend.app.services.deepagents_runtime.tools.llm_extractor import (
    build_llm_extractor,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import (
    PageFile,
    browse_fetch_urls,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
    run_skill_script,
)

#: Upper bound for the per-URL visible-text projection the harness's
#: evidence promotion reads (mirrors the observation projection's excerpt).
_VISIBLE_TEXT_LIMIT = 1200

#: Default per-page candidate output directory under the skill dir (the
#: write_candidates script enforces --out under output/).
_CANDIDATES_DIR = "output/candidates"


class JobDiscoveryWorkflowState(TypedDict):
    urls: list[str]
    pages: list[dict[str, Any]]
    per_url_results: list[dict[str, Any]]
    candidates: list[dict[str, Any]]
    coverage: dict[str, Any]
    error: str | None
    merged_count: int
    dedup_stats: dict[str, Any]


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


def _resolve_candidates_dir(candidates_dir: str | None) -> Path:
    """Resolve the per-page candidates dir under the skill dir.

    None -> ``output/candidates`` under the skill dir (the write_candidates
    script enforces ``--out`` under ``output/``); a relative value resolves
    under the skill dir too; absolute values pass through as-is (tests use
    tmp dirs so the repo tree stays clean).
    """
    if candidates_dir is None:
        return SKILL_DIR / _CANDIDATES_DIR
    resolved = Path(candidates_dir)
    if not resolved.is_absolute():
        resolved = SKILL_DIR / resolved
    return resolved


def _default_fetch(urls: list[str]) -> list[dict[str, Any]]:
    """Browse-backed fetch: classify -> mode fallback chain -> per-URL evidence.

    Orchestrates ``browse_fetch_urls`` (site classification + the allowlisted
    ``browse`` script through run_skill_script).  Per-URL evidence = the
    first page file's full sha256; ``visible_text`` is the first page text
    bounded to ≤1200 chars.  Page files surface as JSON-safe dicts for the
    per-page extraction fan-out.  ``blocked`` URLs map to
    ``error_code="blocked"`` (the stall-breaker treats them as no-progress);
    ``wechat_pending`` URLs are carried through untouched for Task 9.  A
    per-URL failure never aborts the run (layered failure recovery,
    spec §4.2).
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
                "page_files": [
                    {
                        "path": pf.path,
                        "content_hash": pf.content_hash,
                        "text_length": pf.text_length,
                    }
                    for pf in result.page_files
                ],
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


def extract_page(
    page: PageFile,
    *,
    url: str,
    out_dir: str,
    context: ToolContext,
    extract_fn: Callable[[ToolContext, ExtractObservedJobDetailsInput], Any],
    llm_extractor: Callable | None = None,
) -> list[ExtractedJobDetails]:
    """Per-page gated extraction (Task 8 fan-out).

    Reads the page text from its resolved path (relative paths resolve
    against ``out_dir``), registers it as observed evidence with the bare
    ``content_hash`` as artifact_id - the same registration contract
    career_skills ``_find_observed_evidence`` resolves (it matches on
    artifact_id first, so the per-page payload artifact_id resolves
    directly) - and runs exactly one gated extraction for the page.  A
    missing/unreadable page file or a PublicJobFetchError yields ``[]``:
    the page contributes nothing and the run never crashes.
    """
    path = Path(page.path)
    if not path.is_absolute():
        path = Path(out_dir) / path
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    context.metadata.setdefault("observed_public_evidence", []).append(
        {
            "artifact_id": page.content_hash,
            "source_url": url,
            "content_hash": page.content_hash,
            "visible_text": text[:_VISIBLE_TEXT_LIMIT],
        }
    )
    payload = ExtractObservedJobDetailsInput(artifact_id=page.content_hash)
    try:
        if llm_extractor is not None:
            output = extract_with_gate(
                context, payload, enabled=True, llm_extractor=llm_extractor
            )
        else:
            output = extract_fn(context, payload)
    except PublicJobFetchError:
        return []
    return list(output.candidates)


def write_page_candidates(
    page_id: str,
    candidates: list[ExtractedJobDetails],
    *,
    runner: Callable | None = None,
    candidates_dir: str | None = None,
) -> int:
    """Pipe a page's candidates to the write_candidates script via stdin.

    Returns the accepted count (title+company+body survivors) reported by
    the script's ``batch_kept``; an unparsable summary, a non-dict summary,
    or a non-int count folds to 0 (never crashes).
    """
    out_dir = _resolve_candidates_dir(candidates_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        [c.model_dump(mode="json") for c in candidates], ensure_ascii=False
    )
    out = (runner or run_skill_script)(
        "write_candidates", f"--out {out_dir / f'{page_id}.json'}", stdin=payload
    )
    try:
        summary = json.JSONDecoder().raw_decode(out, 0)[0]
    except ValueError:
        return 0
    if not isinstance(summary, dict):
        return 0
    kept = summary.get("batch_kept", 0)
    return kept if isinstance(kept, int) else 0


def _default_extract(
    pages: list[dict[str, Any]],
    *,
    settings=None,
    script_runner: Callable | None = None,
    candidates_dir: str | None = None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Per-page gated extraction over every page file (Task 8).

    Each page file registers its own observed evidence (bare content_hash)
    and gets one gated extraction call; per-page candidates are persisted to
    ``page_NN.json`` for the dedup node.  Batch extraction survives only for
    evidence without page files (static fast-path compat, X1 ruling), with
    the same ``observed:``-prefixed registration as before.
    """
    llm_extractor = build_llm_extractor(settings) if settings is not None else None
    runner = script_runner or run_skill_script
    context = ToolContext(
        user_id="", run_id="", metadata={"observed_public_evidence": []}
    )
    candidates: list[dict[str, Any]] = []
    batch_pages: list[dict[str, Any]] = []
    for index, page in enumerate(pages):
        page_files = page.get("page_files") or []
        if page_files:
            for raw in page_files:
                page_candidates = extract_page(
                    PageFile(**raw),
                    url=page.get("url", ""),
                    out_dir=str(SKILL_DIR),
                    context=context,
                    extract_fn=extract_observed_job_details,
                    llm_extractor=llm_extractor,
                )
                if page_candidates:
                    write_page_candidates(
                        f"page_{index:02d}",
                        page_candidates,
                        runner=runner,
                        candidates_dir=candidates_dir,
                    )
                candidates.extend(
                    c.model_dump(mode="json") for c in page_candidates
                )
        else:
            batch_pages.append(page)
    if batch_pages:
        for page in batch_pages:
            context.metadata["observed_public_evidence"].append(
                {
                    "artifact_id": f"observed:{page['content_hash']}",
                    "source_url": page.get("source_url"),
                    "content_hash": page.get("content_hash"),
                    "visible_text": page.get("visible_text", ""),
                }
            )
        try:
            output = extract_observed_job_details_batch(
                context,
                ExtractObservedJobDetailsBatchInput(
                    # the observed: prefix must match the metadata artifact_id
                    # registered above (career_skills `_find_observed_evidence`
                    # matches on artifact_id, not on content_hash alone)
                    artifact_ids=[
                        f"observed:{page['content_hash']}" for page in batch_pages
                    ][:10]
                ),
            )
        except PublicJobFetchError as exc:
            return candidates, f"extract failed: {exc}"
        # flatten: the batch output is one detail per requested artifact,
        # each carrying its own candidates list; the candidates channel must
        # hold flat candidate dicts (same shape as the per-page path and as
        # the real coverage_gate.py / validate scripts expect), so each
        # detail's candidates are extended, not the detail wrapper itself
        candidates.extend(
            c.model_dump(mode="json")
            for detail in output.details
            for c in detail.candidates
        )
    return candidates, None


def build_job_discovery_graph(
    *,
    fetch_fn=None,
    script_runner=None,
    extract_fn=None,
    settings=None,
    candidates_dir=None,
) -> StateGraph:
    """Assemble the workflow graph with injectable seams for tests."""
    fetch_fn = fetch_fn or _default_fetch
    script_runner = script_runner or run_skill_script
    extract_fn = extract_fn or functools.partial(
        _default_extract,
        settings=settings,
        script_runner=script_runner,
        candidates_dir=candidates_dir,
    )
    candidates_dir = _resolve_candidates_dir(candidates_dir)

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
                    "page_files": [
                        pf["path"] for pf in page.get("page_files") or []
                    ],
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
        page_files = sorted(candidates_dir.glob("page_*.json"))
        if not page_files:
            # no per-page files (batch fast-path): nothing to merge; the
            # in-memory candidates flow through un-deduped and merged_count
            # stays at its tool default (0) - an honest "no merge ran"
            return {}
        merged = candidates_dir / "merged_final.json"
        out = script_runner(
            "deduplicate", " ".join(str(f) for f in page_files) + f" --out {merged}"
        )
        if "ERROR" in out:
            return {"error": f"deduplicate failed: {out[:500]}"}
        try:
            summary = json.JSONDecoder().raw_decode(out, 0)[0]
        except ValueError:
            return {"error": "deduplicate output unparsable"}
        if not isinstance(summary, dict):
            return {"error": "deduplicate output unparsable"}
        stats = summary.get("stats")
        if not isinstance(stats, dict):
            return {"error": "deduplicate output has no stats"}
        output_count = stats.get("output_count")
        if not isinstance(output_count, int):
            return {"error": "deduplicate output has no output_count"}
        if not merged.exists():
            return {"error": "deduplicate produced no merged file"}
        try:
            merged_candidates = json.loads(merged.read_text(encoding="utf-8"))
        except ValueError:
            return {"error": "deduplicate output unparsable"}
        if isinstance(merged_candidates, list):
            return {
                "candidates": merged_candidates,
                "merged_count": output_count,
                "dedup_stats": stats,
            }
        if not isinstance(merged_candidates, dict):
            return {"error": "deduplicate output has no candidates list"}
        if not isinstance(merged_candidates.get("candidates"), list):
            return {"error": "deduplicate output has no candidates list"}
        return {
            "candidates": merged_candidates["candidates"],
            "merged_count": output_count,
            "dedup_stats": stats,
        }

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
