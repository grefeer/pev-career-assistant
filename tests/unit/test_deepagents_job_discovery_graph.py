from __future__ import annotations

import json
from pathlib import Path

from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
)


def _fake_fetch(urls: list[str]) -> list[dict]:
    return [
        {
            "url": url,
            "source_url": url,
            "status": "succeeded",
            "content_hash": f"hash-{index}",
            "visible_text": "岗位：后端工程师\n岗位职责：负责后端服务开发\n任职要求：精通 Python",
        }
        for index, url in enumerate(urls)
    ]


def _fake_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
    if script == "coverage_gate":
        # faithful to the real coverage_gate.py (non-manifest path): emits
        # coverage_verified/page_count/.../reasons, never a bare `verified`,
        # and reports missing_terminal_evidence when --terminal-evidence is
        # absent (the verdict derives from evidence, not candidate existence)
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


def test_workflow_runs_end_to_end_with_seams() -> None:
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["per_url_results"][0]["status"] == "succeeded"
    assert final["coverage"]["verified"] is True
    assert final["error"] is None


def test_fetch_failure_recorded_per_url_not_fatal() -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": url, "status": "failed", "error_code": "blocked"} for url in urls]

    graph = build_job_discovery_graph(fetch_fn=fetch, script_runner=_fake_runner).compile()
    final = graph.invoke({"urls": ["https://a.com", "https://b.com"]})
    assert final["per_url_results"][0]["status"] == "failed"
    assert final["candidates"] == []
    assert final["coverage"]["verified"] is False
    # the real gate reports the evidence gaps it observed
    assert "no_page_evidence" in final["coverage"]["reasons"]
    assert "missing_terminal_evidence" in final["coverage"]["reasons"]


def test_extract_failure_records_error() -> None:
    def failing_extract(pages: list[dict]) -> tuple[list[dict], str]:
        return [], "extract failed: evidence not found"

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=failing_extract,
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "extract failed: evidence not found"
    assert final["candidates"] == []


def test_default_extract_handles_missing_evidence(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import PublicJobFetchError

    def raising_batch(context, payload):
        raise PublicJobFetchError("observed_evidence_not_found")

    monkeypatch.setattr(jdg, "extract_observed_job_details_batch", raising_batch)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] is not None
    assert final["candidates"] == []


def test_script_error_recorded_not_fatal() -> None:
    def failing_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "validate":
            return "ERROR: validation failed"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=failing_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] is not None
    assert final["coverage"] is not None


def test_fetch_page_missing_status_defaults_failed() -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": url, "content_hash": "h"} for url in urls]

    graph = build_job_discovery_graph(fetch_fn=fetch, script_runner=_fake_runner).compile()
    final = graph.invoke({"urls": ["https://a.com"]})
    assert final["per_url_results"][0]["status"] == "failed"


def test_dedup_error_recorded_not_fatal() -> None:
    def failing_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return "ERROR: dedup crashed"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=failing_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate failed: ERROR: dedup crashed"


def test_dedup_no_merged_file_recorded() -> None:
    def no_output_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return "ok"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=no_output_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate produced no merged file"


def test_dedup_unparsable_output_recorded() -> None:
    def unparsable_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text("not json", encoding="utf-8")
            return "ok"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=unparsable_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output unparsable"


def test_dedup_dict_with_candidates_accepted() -> None:
    def dict_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text(
                json.dumps({"candidates": [{"title": "前端工程师"}]}), encoding="utf-8"
            )
            return "ok"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=dict_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["candidates"][0]["title"] == "前端工程师"


def test_dedup_dict_without_candidates_recorded() -> None:
    def bad_dict_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text(json.dumps({"foo": 1}), encoding="utf-8")
            return "ok"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=bad_dict_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output has no candidates list"


def test_coverage_unparsable_output_falls_back() -> None:
    def garbage_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            return "garbage not json"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=garbage_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["coverage"] == {
        "verified": False,
        "error": "unparsable coverage_gate output",
    }


def test_coverage_non_object_output_falls_back() -> None:
    def list_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            return "[]"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=list_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["coverage"] == {
        "verified": False,
        "error": "non-object coverage_gate output",
    }


def test_coverage_passes_terminal_evidence_and_maps_real_keys() -> None:
    # regression for the review finding: the graph must pass
    # --terminal-evidence (the real gate always reports
    # missing_terminal_evidence without it) and expose the mapped `verified`
    # bool beside the real script's coverage_verified key
    recorded: dict[str, str] = {}

    def recording_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            recorded["coverage_gate"] = cli_args
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=recording_runner
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert "--terminal-evidence hash-0" in recorded["coverage_gate"]
    assert "--pages https://example.com/jobs" in recorded["coverage_gate"]
    assert final["coverage"]["coverage_verified"] is True
    assert final["coverage"]["verified"] is True
    assert final["coverage"]["reasons"] == []
    assert final["coverage"]["page_count"] == 1


def test_coverage_without_content_hash_omits_terminal_evidence() -> None:
    def fetch_without_hash(urls: list[str]) -> list[dict]:
        return [
            {"url": url, "source_url": url, "status": "succeeded"} for url in urls
        ]

    graph = build_job_discovery_graph(
        fetch_fn=fetch_without_hash, script_runner=_fake_runner
    ).compile()
    final = graph.invoke({"urls": ["https://a.com"]})
    assert final["candidates"] == []
    assert final["coverage"]["verified"] is False
    assert "missing_terminal_evidence" in final["coverage"]["reasons"]


def test_default_seams_used_without_injection(monkeypatch) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import (
        ExtractObservedJobDetailsBatchOutput,
        ExtractObservedJobDetailsOutput,
        ExtractedJobDetails,
        FetchPublicJobPageOutput,
        FetchPublicJobPagesOutput,
        PublicJobPageFetchFailure,
    )

    monkeypatch.setattr(
        jdg,
        "fetch_public_job_pages",
        lambda context, payload: FetchPublicJobPagesOutput(
            pages=[
                FetchPublicJobPageOutput(
                    artifact_id="observed:h1",
                    source_url="https://example.com/jobs",
                    title="后端工程师",
                    visible_text="岗位：后端工程师",
                    content_hash="h1",
                )
            ],
            failures=[
                PublicJobPageFetchFailure(
                    source_url="https://blocked.example.com", error_code="blocked"
                )
            ],
        ),
    )
    monkeypatch.setattr(
        jdg,
        "extract_observed_job_details_batch",
        lambda context, payload: ExtractObservedJobDetailsBatchOutput(
            details=[
                ExtractObservedJobDetailsOutput(
                    source_artifact_id="h1",
                    source_url="https://example.com/jobs",
                    content_hash="h1",
                    candidates=[
                        ExtractedJobDetails(
                            title="后端工程师",
                            company_name="示例公司",
                            locations=["上海"],
                            responsibilities="岗位职责",
                            requirements="精通 Python",
                            recruitment_types=["校招"],
                            apply_url=None,
                            deadline_text=None,
                            confidence=0.9,
                            evidence_refs=[
                                {
                                    "artifact_id": "h1",
                                    "source_url": "https://example.com/jobs",
                                    "content_hash": "h1",
                                }
                            ],
                            normalization_warnings=[],
                        )
                    ],
                )
            ],
        ),
    )
    monkeypatch.setattr(jdg, "run_skill_script", _fake_runner)

    graph = build_job_discovery_graph().compile()
    final = graph.invoke(
        {"urls": ["https://example.com/jobs", "https://blocked.example.com"]}
    )
    statuses = [r["status"] for r in final["per_url_results"]]
    assert "succeeded" in statuses
    assert "failed" in statuses
    assert final["candidates"]
    assert final["coverage"]["verified"] is True
    assert final["error"] is None
