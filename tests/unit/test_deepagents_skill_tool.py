from __future__ import annotations

import json
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

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
            "visible_text": "岗位：后端工程师\n任职要求：精通 Python",
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
            }
        ],
        None,
    )


def _fake_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
    if script == "coverage_gate":
        parts = cli_args.split()
        candidates = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
        return json.dumps(
            {"verified": bool(candidates), "pages": 1, "candidates": len(candidates)}
        )
    if script == "validate":
        return json.dumps({"ok": True})
    if script == "deduplicate":
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        out_path.write_text(json.dumps([{"title": "后端工程师"}]), encoding="utf-8")
        return "ok"
    return "{}"


def test_job_discovery_tool_returns_partial_results_contract() -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    # no thread bound -> config = {} branch (single-shot, no checkpointer)
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    )
    assert set(out) == {"per_url_results", "candidates", "coverage"}
    assert out["per_url_results"][0]["status"] == "succeeded"
    assert out["candidates"][0]["title"] == "后端工程师"
    assert out["coverage"]["verified"] is True


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
    assert [r["url"] for r in first["per_url_results"]] == ["https://a.com"]
    assert [r["url"] for r in second["per_url_results"]] == ["https://b.com"]


def test_job_discovery_tool_dict_input_path() -> None:
    # the deepagents ToolNode invokes tools with dict args ({"payload": ...});
    # verify the kwargs path maps the schema field to the func parameter
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, extract_fn=_fake_extract
    )
    out = json.loads(tool.invoke({"payload": json.dumps(["https://example.com/jobs"])}))
    assert [r["url"] for r in out["per_url_results"]] == ["https://example.com/jobs"]
    assert out["candidates"][0]["title"] == "后端工程师"
    assert out["coverage"]["verified"] is True


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
