from __future__ import annotations

import hashlib
import json
from pathlib import Path

from backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph import (
    build_job_discovery_graph,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.wechat_slice import (
    WechatResult,
)
from tests.conftest import settings_override


def _fake_fetch(urls: list[str]) -> list[dict]:
    # batch fast-path pages: evidence with a content_hash but no page files
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
    if script == "write_candidates":
        # faithful to the real script: persist the stdin batch to --out
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        batch = json.loads(stdin) if stdin else []
        out_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
        return json.dumps(
            {
                "status": "ok",
                "out": str(out_path),
                "batch_received": len(batch),
                "batch_kept": len(batch),
                "batch_dropped_invalid": 0,
                "appended": len(batch),
                "total_in_file": len(batch),
                "mode": "overwrite",
            }
        )
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
        return json.dumps(
            {
                "status": "ok",
                "stats": {
                    "input_count": 1,
                    "garbage_dropped": 0,
                    "garbage_titles": [],
                    "output_count": 1,
                    "duplicates_removed": 0,
                    "shared_listing_urls_cleared": 0,
                },
                "load_errors": [],
                "verify_warnings_count": 0,
                "output_file": str(out_path),
            }
        )
    return "{}"


def _seed_page_file(tmp_path: Path) -> None:
    # the dedup node globs page_*.json BEFORE invoking the script; dedup
    # tests must seed the candidates dir so the node actually runs
    (tmp_path / "page_01.json").write_text("[]", encoding="utf-8")


def test_workflow_runs_end_to_end_with_seams(tmp_path) -> None:
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["per_url_results"][0]["status"] == "succeeded"
    assert final["candidates"]
    assert final["coverage"]["verified"] is True
    assert final["error"] is None
    # batch fast-path wrote no per-page files: the dedup node skips and the
    # merged_count channel is absent (tool default 0)
    assert final.get("merged_count") is None


def test_fetch_failure_recorded_per_url_not_fatal(tmp_path) -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": url, "status": "failed", "error_code": "blocked"} for url in urls]

    graph = build_job_discovery_graph(
        fetch_fn=fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://a.com", "https://b.com"]})
    assert final["per_url_results"][0]["status"] == "failed"
    assert final["candidates"] == []
    assert final["coverage"]["verified"] is False
    # the real gate reports the evidence gaps it observed
    assert "no_page_evidence" in final["coverage"]["reasons"]
    assert "missing_terminal_evidence" in final["coverage"]["reasons"]


def test_extract_failure_records_error(tmp_path) -> None:
    def failing_extract(pages: list[dict]) -> tuple[list[dict], str]:
        return [], "extract failed: evidence not found"

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=failing_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "extract failed: evidence not found"
    assert final["candidates"] == []


def test_default_extract_handles_missing_evidence(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import PublicJobFetchError

    def raising_batch(context, payload):
        raise PublicJobFetchError("observed_evidence_not_found")

    monkeypatch.setattr(jdg, "extract_observed_job_details_batch", raising_batch)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] is not None
    assert final["candidates"] == []


def test_script_error_recorded_not_fatal(tmp_path) -> None:
    def failing_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "validate":
            return "ERROR: validation failed"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=failing_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] is not None
    assert final["coverage"] is not None


def test_fetch_page_missing_status_defaults_failed(tmp_path) -> None:
    def fetch(urls: list[str]) -> list[dict]:
        return [{"url": url, "content_hash": "h"} for url in urls]

    graph = build_job_discovery_graph(
        fetch_fn=fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://a.com"]})
    assert final["per_url_results"][0]["status"] == "failed"


def test_dedup_error_recorded_not_fatal(tmp_path) -> None:
    def failing_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return "ERROR: dedup crashed"
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=failing_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate failed: ERROR: dedup crashed"


def test_dedup_no_merged_file_recorded(tmp_path) -> None:
    def no_output_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            # valid stdout summary, but the script never wrote the merged file
            return json.dumps(
                {
                    "status": "ok",
                    "stats": {"input_count": 1, "output_count": 1},
                    "load_errors": [],
                    "verify_warnings_count": 0,
                    "output_file": "output/candidates/merged_final.json",
                }
            )
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=no_output_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate produced no merged file"


def test_dedup_merged_unparsable_recorded(tmp_path) -> None:
    def unparsable_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text("not json", encoding="utf-8")
            return json.dumps(
                {"status": "ok", "stats": {"input_count": 1, "output_count": 1}}
            )
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=unparsable_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output unparsable"


def test_dedup_stdout_unparsable_recorded(tmp_path) -> None:
    def garbage_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return "garbage not json"
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=garbage_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output unparsable"


def test_dedup_stdout_non_object_recorded(tmp_path) -> None:
    def list_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return "[]"
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=list_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output unparsable"


def test_dedup_no_stats_recorded(tmp_path) -> None:
    def no_stats_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return json.dumps({"status": "ok"})
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=no_stats_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output has no stats"


def test_dedup_no_output_count_recorded(tmp_path) -> None:
    def no_count_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            return json.dumps({"status": "ok", "stats": {"input_count": 1}})
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=no_count_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output has no output_count"


def test_dedup_dict_with_candidates_accepted(tmp_path) -> None:
    def dict_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text(
                json.dumps({"candidates": [{"title": "前端工程师"}]}), encoding="utf-8"
            )
            return json.dumps(
                {"status": "ok", "stats": {"input_count": 1, "output_count": 3}}
            )
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=dict_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["candidates"][0]["title"] == "前端工程师"
    assert final["merged_count"] == 3
    assert final["dedup_stats"] == {"input_count": 1, "output_count": 3}


def test_dedup_scalar_merged_has_no_candidates_list(tmp_path) -> None:
    def scalar_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text("42", encoding="utf-8")
            return json.dumps({"status": "ok", "stats": {"output_count": 1}})
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=scalar_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output has no candidates list"


def test_dedup_dict_without_candidates_recorded(tmp_path) -> None:
    def bad_dict_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text(json.dumps({"candidates": 1}), encoding="utf-8")
            return json.dumps({"status": "ok", "stats": {"output_count": 1}})
        return _fake_runner(script, cli_args, stdin)

    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=bad_dict_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["error"] == "deduplicate output has no candidates list"


def test_dedup_success_reports_merged_count_and_stats(tmp_path) -> None:
    _seed_page_file(tmp_path)
    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["candidates"][0]["title"] == "后端工程师"
    assert final["merged_count"] == 1
    stats = final["dedup_stats"]
    assert stats["output_count"] == 1
    assert stats["input_count"] == 1
    assert stats["duplicates_removed"] == 0


def test_coverage_unparsable_output_falls_back(tmp_path) -> None:
    def garbage_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            return "garbage not json"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=garbage_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["coverage"] == {
        "verified": False,
        "error": "unparsable coverage_gate output",
    }


def test_coverage_non_object_output_falls_back(tmp_path) -> None:
    def list_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            return "[]"
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch, script_runner=list_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["coverage"] == {
        "verified": False,
        "error": "non-object coverage_gate output",
    }


def test_coverage_passes_terminal_evidence_and_maps_real_keys(tmp_path) -> None:
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
        fetch_fn=_fake_fetch, script_runner=recording_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert "--terminal-evidence hash-0" in recorded["coverage_gate"]
    assert "--pages https://example.com/jobs" in recorded["coverage_gate"]
    assert final["coverage"]["coverage_verified"] is True
    assert final["coverage"]["verified"] is True
    assert final["coverage"]["reasons"] == []
    assert final["coverage"]["page_count"] == 1


def test_coverage_without_content_hash_omits_terminal_evidence(tmp_path) -> None:
    def fetch_without_hash(urls: list[str]) -> list[dict]:
        return [
            {"url": url, "source_url": url, "status": "succeeded"} for url in urls
        ]

    graph = build_job_discovery_graph(
        fetch_fn=fetch_without_hash, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://a.com"]})
    assert final["candidates"] == []
    assert final["coverage"]["verified"] is False
    assert "missing_terminal_evidence" in final["coverage"]["reasons"]


def test_default_seams_used_without_injection(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import (
        ExtractObservedJobDetailsOutput,
        ExtractedJobDetails,
    )
    from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import (
        PageFile,
        UrlFetchResult,
    )

    # the default fetch runs the real browse chain: a succeeded URL resolves
    # its evidence from an on-disk page file (full-sha256 + bounded
    # visible_text), a blocked URL folds to error_code="blocked", and a
    # WeChat URL routes into the Task 9 OCR slice (never browsed; the slice
    # itself is stubbed - no live HTTP in unit tests)
    page_file = tmp_path / "pages" / "page_01.txt"
    page_file.parent.mkdir()
    page_text = "岗位：后端工程师\n" + "职责：" + "x" * 3000
    page_file.write_text(page_text, encoding="utf-8")
    content_hash = hashlib.sha256(page_file.read_bytes()).hexdigest()

    monkeypatch.setattr(
        jdg,
        "browse_fetch_urls",
        lambda urls, **kwargs: [
            UrlFetchResult(
                url="https://example.com/jobs",
                site_class="list",
                mode="list",
                status="succeeded",
                page_files=[
                    PageFile(
                        path=str(page_file),
                        content_hash=content_hash,
                        text_length=len(page_text.encode("utf-8")),
                    )
                ],
            ),
            UrlFetchResult(
                url="https://blocked.example.com",
                site_class="probe",
                mode="list",
                status="blocked",
                blocked_reason="captcha",
                error_code="blocked",
            ),
            UrlFetchResult(
                url="https://mp.weixin.qq.com/s/x",
                site_class="wechat",
                mode=None,
                status="wechat_pending",
            ),
        ],
    )
    # per-page extraction seam: the page registers its bare content_hash as
    # the evidence artifact_id, and the payload artifact_id must equal it
    captured: list[str] = []

    def fake_per_page(context, payload):
        captured.append(payload.artifact_id)
        assert payload.artifact_id == content_hash
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash=payload.artifact_id,
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
                    evidence_refs=[],
                    normalization_warnings=[],
                )
            ],
        )

    monkeypatch.setattr(jdg, "extract_observed_job_details", fake_per_page)
    monkeypatch.setattr(jdg, "run_skill_script", _fake_runner)
    # Task 9: wechat_pending URLs route into the slice before the extract
    # node; the per-URL entry surfaces the slice's classification
    monkeypatch.setattr(
        jdg,
        "run_wechat_slice",
        lambda url, **kwargs: WechatResult(
            url=url,
            status="needs_manual_review",
            channel="C",
            candidates=[],
            application_channel_json=None,
            needs_deep_crawl=False,
            reason="仅含联系方式",
        ),
    )

    graph = build_job_discovery_graph(
        candidates_dir=str(tmp_path), state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke(
        {
            "urls": [
                "https://example.com/jobs",
                "https://blocked.example.com",
                "https://mp.weixin.qq.com/s/x",
            ]
        }
    )
    by_url = {r["url"]: r for r in final["per_url_results"]}
    # evidence promotion: source_url + content_hash (full sha256 of the page
    # file bytes) + the bounded visible_text projection (≤1200 chars)
    ok = by_url["https://example.com/jobs"]
    assert ok["status"] == "succeeded"
    assert ok["source_url"] == "https://example.com/jobs"
    assert ok["content_hash"] == content_hash
    assert ok["mode"] == "list"
    assert ok["page_files"] == [str(page_file)]
    assert ok["visible_text"] == page_text[:1200]
    assert len(ok["visible_text"]) <= 1200
    # error folding: blocked URLs map to per-url error_code="blocked" and
    # forward blocked_reason so manual-review triage sees the block cause
    blocked = by_url["https://blocked.example.com"]
    assert blocked["status"] == "blocked"
    assert blocked["error_code"] == "blocked"
    assert blocked["blocked_reason"] == "captcha"
    # Task 9: a WeChat URL routes into the OCR slice (stubbed: contact-only
    # classification) -> per-URL entry surfaces needs_manual_review + channel
    wechat = by_url["https://mp.weixin.qq.com/s/x"]
    assert wechat["status"] == "needs_manual_review"
    assert wechat["error_code"] == "needs_manual_review"
    assert wechat["channel"] == "C"
    assert wechat["reason"] == "仅含联系方式"
    assert wechat["candidate_count"] == 0
    # per-page extraction: exactly one call with the bare page hash, and the
    # candidates were persisted -> the dedup node merged them
    assert captured == [content_hash]
    assert final["candidates"]
    assert final["merged_count"] == 1
    assert final["dedup_stats"]["output_count"] == 1
    assert final["coverage"]["verified"] is True
    assert final["error"] is None


def test_default_fetch_page_file_edges(monkeypatch, tmp_path) -> None:
    # succeeded without page files (e.g. a cache hit): no evidence hash and
    # no visible_text; a page file that vanished between hash and read:
    # empty visible_text (never crashes the fetch); page_files surface as
    # JSON-safe dicts (Task 8 per-page fan-out input)
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import (
        PageFile,
        UrlFetchResult,
    )

    monkeypatch.setattr(
        jdg,
        "browse_fetch_urls",
        lambda urls, **kwargs: [
            UrlFetchResult(
                url="https://cached.example.com",
                site_class="list",
                mode="list",
                status="succeeded",
                cached=True,
            ),
            UrlFetchResult(
                url="https://ghost.example.com",
                site_class="list",
                mode="list",
                status="succeeded",
                page_files=[
                    PageFile(
                        path=str(tmp_path / "gone.txt"), content_hash="h", text_length=0
                    )
                ],
            ),
        ],
    )

    pages = jdg._default_fetch(
        ["https://cached.example.com", "https://ghost.example.com"]
    )
    by_url = {page["url"]: page for page in pages}
    assert by_url["https://cached.example.com"]["content_hash"] is None
    assert by_url["https://cached.example.com"]["visible_text"] == ""
    assert by_url["https://cached.example.com"]["page_files"] == []
    assert by_url["https://ghost.example.com"]["content_hash"] == "h"
    assert by_url["https://ghost.example.com"]["visible_text"] == ""
    assert by_url["https://ghost.example.com"]["page_files"] == [
        {"path": str(tmp_path / "gone.txt"), "content_hash": "h", "text_length": 0}
    ]


def test_per_page_fanout_writes_and_merges_page_files(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import (
        ExtractObservedJobDetailsBatchOutput,
        ExtractObservedJobDetailsOutput,
        ExtractedJobDetails,
    )

    # three page-bearing URLs + one evidence-only URL (no page files)
    page_paths = []
    for index in range(3):
        path = tmp_path / f"page_{index}.txt"
        path.write_text(
            f"岗位：职位{index}\n岗位职责：职责{index}\n任职要求：要求{index}",
            encoding="utf-8",
        )
        page_paths.append(path)
    pages: list[dict] = []
    for index in range(3):
        pages.append(
            {
                "url": f"https://job{index}.example.com",
                "source_url": f"https://job{index}.example.com",
                "status": "succeeded",
                "content_hash": f"page-hash-{index}",
                "visible_text": "岗位：职位",
                "page_files": [
                    {
                        "path": str(page_paths[index]),
                        "content_hash": f"page-hash-{index}",
                        "text_length": 10,
                    }
                ],
            }
        )
    pages.append(
        {
            "url": "https://evidence-only.example.com",
            "source_url": "https://evidence-only.example.com",
            "status": "succeeded",
            "content_hash": "evidence-hash",
            "visible_text": "岗位：批量职位",
        }
    )

    extracted: dict[str, list[str]] = {"per_page": [], "batch": []}

    def fake_per_page(context, payload):
        extracted["per_page"].append(payload.artifact_id)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://job0.example.com",
            content_hash=payload.artifact_id,
            candidates=[
                ExtractedJobDetails(
                    title=f"职位-{payload.artifact_id}",
                    company_name="示例公司",
                    locations=[],
                    responsibilities="职责",
                    requirements="要求",
                    recruitment_types=[],
                    apply_url=None,
                    deadline_text=None,
                    confidence=0.9,
                    evidence_refs=[],
                    normalization_warnings=[],
                )
            ],
        )

    def fake_batch(context, payload):
        extracted["batch"].extend(payload.artifact_ids)
        return ExtractObservedJobDetailsBatchOutput(
            details=[
                ExtractObservedJobDetailsOutput(
                    source_artifact_id=artifact_id,
                    source_url="https://evidence-only.example.com",
                    content_hash=artifact_id,
                    candidates=[
                        ExtractedJobDetails(
                            title=f"批量-{artifact_id}",
                            company_name="示例公司",
                            locations=[],
                            responsibilities="职责",
                            requirements="要求",
                            recruitment_types=[],
                            apply_url=None,
                            deadline_text=None,
                            confidence=0.9,
                            evidence_refs=[],
                            normalization_warnings=[],
                        )
                    ],
                )
                for artifact_id in payload.artifact_ids
            ],
        )

    monkeypatch.setattr(jdg, "extract_observed_job_details", fake_per_page)
    monkeypatch.setattr(jdg, "extract_observed_job_details_batch", fake_batch)

    calls: dict[str, list[str]] = {"write_candidates": [], "deduplicate": []}

    def recording_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script in calls:
            calls[script].append(cli_args)
        if script == "write_candidates":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            batch = json.loads(stdin)
            out_path.write_text(json.dumps(batch, ensure_ascii=False), encoding="utf-8")
            return json.dumps(
                {
                    "status": "ok",
                    "batch_received": len(batch),
                    "batch_kept": len(batch),
                }
            )
        if script == "deduplicate":
            parts = cli_args.split()
            out_path = Path(parts[parts.index("--out") + 1])
            out_path.write_text(json.dumps([{"title": "合并职位"}]), encoding="utf-8")
            return json.dumps(
                {
                    "status": "ok",
                    "stats": {
                        "input_count": 3,
                        "garbage_dropped": 0,
                        "garbage_titles": [],
                        "output_count": 1,
                        "duplicates_removed": 2,
                        "shared_listing_urls_cleared": 0,
                    },
                    "load_errors": [],
                    "verify_warnings_count": 0,
                    "output_file": str(out_path),
                }
            )
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=lambda urls: pages,
        script_runner=recording_runner,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": ["https://job0.example.com"]})

    # per-page fan-out: one gated call per page file with the bare hash
    assert extracted["per_page"] == ["page-hash-0", "page-hash-1", "page-hash-2"]
    # evidence-only page keeps the prefixed batch path
    assert extracted["batch"] == ["observed:evidence-hash"]
    # one write_candidates call per page id, with the stdin contract
    written = {
        Path(cli.split("--out ", 1)[1]).name for cli in calls["write_candidates"]
    }
    assert written == {"page_00.json", "page_01.json", "page_02.json"}
    assert len(calls["write_candidates"]) == 3
    # dedup consumes exactly the per-page files + the merged output path
    dedup_cli = calls["deduplicate"][0]
    assert "--out " + str(tmp_path / "merged_final.json") in dedup_cli
    for index in range(3):
        assert str(tmp_path / f"page_0{index}.json") in dedup_cli
    assert final["candidates"][0]["title"] == "合并职位"
    assert final["merged_count"] == 1
    assert final["dedup_stats"]["duplicates_removed"] == 2
    assert final["error"] is None


def test_per_page_llm_gate_triggered_through_graph(monkeypatch, tmp_path) -> None:
    import backend.app.services.deepagents_runtime.tools.skill_graphs.job_discovery_graph as jdg
    from backend.app.services.career_skills.job_discovery import (
        ExtractObservedJobDetailsOutput,
        ExtractedJobDetails,
    )

    # generic blog text: the regex extractor yields only a low-confidence
    # stub, so the gate fires and the LLM extractor's candidates join
    page_text = "欢迎来到我们的博客。今天我们讨论公司文化和团队建设，以及日常协作的流程。"
    page_file = tmp_path / "page_0.txt"
    page_file.write_text(page_text, encoding="utf-8")

    calls: list[str] = []

    def fake_llm_extractor(context, payload):
        calls.append(payload.artifact_id)
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash=payload.artifact_id,
            candidates=[
                ExtractedJobDetails(
                    title="LLM 补充职位",
                    company_name="示例公司",
                    locations=[],
                    responsibilities="职责",
                    requirements="要求",
                    recruitment_types=[],
                    apply_url=None,
                    deadline_text=None,
                    confidence=0.9,
                    evidence_refs=[],
                    normalization_warnings=[],
                )
            ],
        )

    # NEVER construct a real ChatOpenAI in unit tests: the fake replaces
    # build_llm_extractor at the graph seam
    monkeypatch.setattr(jdg, "build_llm_extractor", lambda settings: fake_llm_extractor)

    def fetch(urls: list[str]) -> list[dict]:
        return [
            {
                "url": "https://example.com/jobs",
                "source_url": "https://example.com/jobs",
                "status": "succeeded",
                "content_hash": "page-hash-0",
                "visible_text": page_text[:1200],
                "page_files": [
                    {
                        "path": str(page_file),
                        "content_hash": "page-hash-0",
                        "text_length": len(page_text.encode("utf-8")),
                    }
                ],
            }
        ]

    graph = build_job_discovery_graph(
        fetch_fn=fetch,
        script_runner=_fake_runner,
        settings=settings_override(deepagents_llm_extraction_enabled=True),
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert calls == ["page-hash-0"]
    # the dedup node replaces the in-memory candidates with the merged file
    # (fake writes 后端工程师), so the LLM candidate is asserted on the
    # persisted per-page file, which is what the merge consumed
    page_out = json.loads((tmp_path / "page_00.json").read_text(encoding="utf-8"))
    assert "LLM 补充职位" in [c["title"] for c in page_out]
    assert final["error"] is None


def test_default_extract_page_file_missing_on_disk(tmp_path) -> None:
    # a page file that vanished between fetch and extract: the page
    # contributes nothing, the run never crashes, nothing is written
    def fetch(urls: list[str]) -> list[dict]:
        return [
            {
                "url": "https://example.com/jobs",
                "source_url": "https://example.com/jobs",
                "status": "succeeded",
                "content_hash": "page-hash-0",
                "visible_text": "",
                "page_files": [
                    {
                        "path": str(tmp_path / "gone.txt"),
                        "content_hash": "page-hash-0",
                        "text_length": 0,
                    }
                ],
            }
        ]

    graph = build_job_discovery_graph(
        fetch_fn=fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["candidates"] == []
    assert final["error"] is None
    assert not list(tmp_path.glob("page_*.json"))


def test_default_extract_zero_candidates_page(tmp_path) -> None:
    # an empty page file extracts zero candidates: write_page_candidates is
    # skipped (no page_*.json appears) and the run completes cleanly
    page_file = tmp_path / "empty.txt"
    page_file.write_text("", encoding="utf-8")

    def fetch(urls: list[str]) -> list[dict]:
        return [
            {
                "url": "https://example.com/jobs",
                "source_url": "https://example.com/jobs",
                "status": "succeeded",
                "content_hash": "page-hash-0",
                "visible_text": "",
                "page_files": [
                    {
                        "path": str(page_file),
                        "content_hash": "page-hash-0",
                        "text_length": 0,
                    }
                ],
            }
        ]

    graph = build_job_discovery_graph(
        fetch_fn=fetch, script_runner=_fake_runner, candidates_dir=str(tmp_path),
        state_dir=str(tmp_path)
    ).compile()
    final = graph.invoke({"urls": ["https://example.com/jobs"]})
    assert final["candidates"] == []
    assert final["error"] is None
    assert not list(tmp_path.glob("page_*.json"))
    # the faithful coverage fake derives its verdict from evidence, not
    # candidate existence: terminal evidence is present, so no reasons
    assert final["coverage"]["reasons"] == []


# ---------------------------------------------------------------------------
# Task 10 incremental mode: state check/mark + merged accumulation
# ---------------------------------------------------------------------------


def test_graph_incremental_state_check_skip_and_mark(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def incremental_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        calls.append((script, cli_args))
        if script == "state" and cli_args.startswith("check "):
            # a.com is already extracted at this update_time -> skip;
            # b.com needs extraction (real state.py exits 0/1)
            return json.dumps({"exit_code": 0 if "https://a.com" in cli_args else 1})
        if script == "state":
            return json.dumps({"marked": True})
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch,
        script_runner=incremental_runner,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke(
        {
            "urls": ["https://a.com", "https://b.com"],
            "prior_metadata": {
                "file_id": "f1",
                "sheet_id": "s1",
                "update_time": "2026-08-07",
            },
        }
    )
    by_url = {r["url"]: r for r in final["per_url_results"]}
    assert by_url["https://a.com"]["status"] == "skipped"
    assert by_url["https://a.com"]["reason"] == "update_time unchanged"
    assert by_url["https://b.com"]["status"] == "succeeded"
    # one check per input URL, carrying the run's update_time
    checks = [a for s, a in calls if s == "state" and a.startswith("check ")]
    assert checks == [
        "check https://a.com 2026-08-07",
        "check https://b.com 2026-08-07",
    ]
    # mark after extraction: only the processed URL, the real state.py form
    # mark <content_hash> <url> <update_time> with the run's file/sheet
    # flags (the script derives entry_id = content_hash[:16]_url_hash8
    # itself, so the full hash is passed positionally)
    marks = [a for s, a in calls if s == "state" and a.startswith("mark ")]
    assert len(marks) == 1
    assert marks[0] == "mark hash-0 https://b.com 2026-08-07 --file-id f1 --sheet-id s1"
    # comparison keys from the normalize node (empty with the {} fake)
    assert final["normalize_keys"] == {}


def test_graph_incremental_dedup_accumulates_prior(tmp_path) -> None:
    # seed the cumulative store: prior candidates at the state store's
    # output/candidates/merged_final.json
    out = tmp_path / "output" / "candidates"
    out.mkdir(parents=True)
    (out / "merged_final.json").write_text(
        json.dumps([{"title": "历史职位", "company": "老公司"}]), encoding="utf-8"
    )
    # this run's per-page files (the dedup node globs page_*.json)
    _seed_page_file(tmp_path)
    (tmp_path / "page_00.json").write_text(
        json.dumps([{"title": "新职位", "company": "新公司"}]), encoding="utf-8"
    )
    calls: list[tuple[str, str, str]] = []

    def recording_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        calls.append((script, cli_args, stdin))
        if script == "state":
            return json.dumps({"exit_code": 1})
        if script == "normalize":
            return json.dumps({"input": "x", "normalized": "x"})
        return _fake_runner(script, cli_args, stdin)

    graph = build_job_discovery_graph(
        fetch_fn=_fake_fetch,
        script_runner=recording_runner,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    ).compile()
    final = graph.invoke(
        {
            "urls": ["https://example.com/jobs"],
            "prior_metadata": {
                "file_id": "f",
                "sheet_id": "s",
                "update_time": "2026-08-07",
            },
        }
    )
    # the prior store is staged via write_candidates --append (identity-merge
    # semantics: re-appending the same candidates is a no-op)
    appends = [
        (cli, stdin)
        for s, cli, stdin in calls
        if s == "write_candidates" and "--append" in cli
    ]
    assert len(appends) == 1
    assert "--out " + str(tmp_path / "prior_merged.json") in appends[0][0]
    assert json.loads(appends[0][1]) == [{"title": "历史职位", "company": "老公司"}]
    assert (tmp_path / "prior_merged.json").exists()
    # deduplicate consumes prior + this run's per-page files
    dedup_cli = [cli for s, cli, _stdin in calls if s == "deduplicate"][0]
    assert str(tmp_path / "prior_merged.json") in dedup_cli
    assert str(tmp_path / "page_00.json") in dedup_cli
    assert str(tmp_path / "page_01.json") in dedup_cli
    assert final["merged_count"] == 1
    assert final["error"] is None


def test_build_resolves_default_state_dir_to_skill_dir() -> None:
    # Task 10: state_dir=None resolves to the skill dir itself (the stable
    # store lands at SKILL_DIR/output/...); build-only, never invoked, so no
    # writes touch the repo tree
    from backend.app.services.deepagents_runtime.tools.skill_graphs import (
        job_discovery_graph as jdg,
    )

    graph = build_job_discovery_graph().compile()
    assert graph is not None
    assert jdg._resolve_state_dir(None) == jdg.SKILL_DIR


def test_build_resolves_relative_state_dir_under_skill_dir(tmp_path) -> None:
    # Task 10: a relative state_dir resolves under the skill dir too;
    # absolute paths (tests) pass through untouched
    from backend.app.services.deepagents_runtime.tools.skill_graphs import (
        job_discovery_graph as jdg,
    )

    graph = build_job_discovery_graph(state_dir="rel/state").compile()
    assert graph is not None
    assert jdg._resolve_state_dir("rel/state") == jdg.SKILL_DIR / "rel/state"
    assert jdg._resolve_state_dir(str(tmp_path)) == tmp_path
