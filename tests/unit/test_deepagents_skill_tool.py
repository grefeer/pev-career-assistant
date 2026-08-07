from __future__ import annotations

import json
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.deepagents_runtime.tools.skill_graphs import (
    build_job_discovery_tool,
    workflow_thread_id,
)


def _fake_fetch(urls: list[str]) -> list[dict]:
    return [
        {
            "url": url,
            "source_url": url,
            "status": "succeeded",
            "content_hash": f"hash-{index}",
            "mode": "list",
            "page_files": [f"output/evidence/run-0/pages/page_{index:02d}.txt"],
            "visible_text": "岗位：后端工程师\n岗位职责：负责后端服务开发\n任职要求：精通 Python",
        }
        for index, url in enumerate(urls)
    ]


def _fake_extract(pages: list[dict]) -> tuple[list[dict], None]:
    return (
        [
            {
                "title": "后端工程师",
                "company": "示例公司",
                "jd_url": pages[0]["url"],
                "content_hash": pages[0]["content_hash"],
                "responsibilities": "负责后端服务开发",
                "requirements": "精通 Python",
            }
        ],
        None,
    )


def _fake_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
    if script == "coverage_gate":
        # faithful to the real coverage_gate.py (non-manifest path): emits
        # coverage_verified/page_count/.../reasons and reports
        # missing_terminal_evidence when --terminal-evidence is absent
        parts = cli_args.split()
        candidates = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
        pages: list[str] = []
        terminal: str | None = None
        if "--pages" in parts:
            tail = parts[parts.index("--pages") + 1 :]
            if "--terminal-evidence" in tail:
                pages = tail[: tail.index("--terminal-evidence")]
            else:
                pages = tail
        if "--terminal-evidence" in parts:
            terminal = parts[parts.index("--terminal-evidence") + 1]
        reasons: list[str] = []
        if not pages:
            reasons.append("no_page_evidence")
        if not terminal:
            reasons.append("missing_terminal_evidence")
        body_count = sum(
            bool(
                (c.get("responsibilities") or "").strip()
                or (c.get("requirements") or "").strip()
            )
            for c in candidates
        )
        if body_count != len(candidates):
            reasons.append("missing_jd_body")
        return json.dumps(
            {
                "coverage_verified": not reasons,
                "page_count": len(pages),
                "candidate_count": len(candidates),
                "body_candidate_count": body_count,
                "unique_listing_count": len(candidates),
                "expected_count": None,
                "terminal_evidence": terminal,
                "reasons": reasons,
            },
            ensure_ascii=False,
        )
    if script == "validate":
        return json.dumps({"ok": True})
    if script == "deduplicate":
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        out_path.write_text(
            json.dumps(
                [
                    {
                        "title": "后端工程师",
                        "responsibilities": "负责后端服务开发",
                        "requirements": "精通 Python",
                    }
                ]
            ),
            encoding="utf-8",
        )
        return "ok"
    return "{}"


def test_job_discovery_tool_returns_valid_tool_observation() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    # no thread bound -> config = {} branch (single-shot, no checkpointer)
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    )
    # the harness's _project_tool_observations validates every ToolMessage
    # against ToolObservation (extra="forbid"): tool_name/status are
    # required, results live in `output` - a bare results dict would be
    # silently dropped and stall the run
    assert set(out) == {"tool_name", "status", "output"}
    assert out["tool_name"] == "run-job-discovery-workflow"
    assert out["status"] == "succeeded"
    results = out["output"]
    assert set(results) == {"per_url_results", "candidates", "coverage"}
    assert results["per_url_results"][0]["status"] == "succeeded"
    # evidence promotion contract: succeeded per-url entries carry both
    # source_url and content_hash so harness evidence projection promotes
    # fetch evidence
    assert results["per_url_results"][0]["source_url"] == "https://example.com/jobs"
    assert results["per_url_results"][0]["content_hash"] == "hash-0"
    # browse provenance keys: the mode that produced the evidence + the
    # resolved page-file paths (Task 4 per_url_results shape + Task 7)
    assert results["per_url_results"][0]["mode"] == "list"
    assert results["per_url_results"][0]["page_files"] == [
        "output/evidence/run-0/pages/page_00.txt"
    ]
    assert results["candidates"][0]["title"] == "后端工程师"
    assert results["coverage"]["verified"] is True
    assert results["coverage"]["coverage_verified"] is True
    assert results["coverage"]["reasons"] == []


def test_job_discovery_tool_threaded_invocation() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        checkpointer=InMemorySaver(),
    )
    with workflow_thread_id("run-1:0:workflow"):
        first = json.loads(
            tool.invoke(json.dumps({"payload": json.dumps(["https://a.com"])}))
        )
        second = json.loads(
            tool.invoke(json.dumps({"payload": json.dumps(["https://b.com"])}))
        )
    # thread config branch: second invoke re-runs from START with the new
    # input (last checkpoint is complete), so it fetches the new URL
    assert [r["url"] for r in first["output"]["per_url_results"]] == ["https://a.com"]
    assert [r["url"] for r in second["output"]["per_url_results"]] == ["https://b.com"]


def test_job_discovery_tool_dict_input_path() -> None:
    # the deepagents ToolNode invokes tools with dict args ({"payload": ...});
    # verify the kwargs path maps the schema field to the func parameter
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    out = json.loads(tool.invoke({"payload": json.dumps(["https://example.com/jobs"])}))
    assert [r["url"] for r in out["output"]["per_url_results"]] == [
        "https://example.com/jobs"
    ]
    assert out["output"]["candidates"][0]["title"] == "后端工程师"
    assert out["output"]["coverage"]["verified"] is True


def test_job_discovery_tool_folds_invalid_payload() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    out = json.loads(tool.invoke(json.dumps({"payload": "not json"})))
    assert out["status"] == "failed"
    assert out["tool_name"] == "run-job-discovery-workflow"
    assert out["error_code"].startswith("workflow_error")


def test_job_discovery_tool_folds_graph_crash() -> None:
    def exploding_fetch(urls: list[str]) -> list[dict]:
        raise RuntimeError("boom")

    tool = build_job_discovery_tool(
        fetch_fn=exploding_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://a.com"])}))
    )
    assert out["status"] == "failed"
    assert out["error_code"] == "workflow_error: RuntimeError"


def test_job_discovery_tool_observation_is_always_str() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    raw = tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    assert isinstance(raw, str)
    assert isinstance(json.loads(raw), dict)


def test_job_discovery_tool_success_parses_as_tool_observation() -> None:
    # regression for the review finding: the success dict must be a valid
    # ToolObservation (extra="forbid", required tool_name/status) or the
    # harness silently drops it and the run stalls
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    raw = tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    obs = ToolObservation.model_validate(json.loads(raw))
    assert obs.status == "succeeded"
    assert obs.error_code is None
    assert obs.output["coverage"]["verified"] is True
