from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path

from langgraph.checkpoint.memory import InMemorySaver

from backend.app.services.agent_runtime.schemas import ToolObservation
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.career_skills.job_discovery import (
    ExtractObservedJobDetailsOutput,
    ExtractedJobDetails,
    PublicJobFetchError,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs import (
    build_job_discovery_tool,
    workflow_thread_id,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs import (
    job_discovery_graph as jdg,
)
from backend.app.services.deepagents_runtime.tools.skill_graphs.browse_fetch import PageFile
from backend.app.services.deepagents_runtime.tools.skill_graphs.subprocess_runner import (
    SKILL_DIR,
)


def _fake_fetch(urls: list[str]) -> list[dict]:
    return [
        {
            "url": url,
            "source_url": url,
            "status": "succeeded",
            "content_hash": f"hash-{index}",
            "mode": "list",
            "page_files": [
                {
                    "path": f"output/evidence/run-0/pages/page_{index:02d}.txt",
                    "content_hash": f"page-file-{index}",
                    "text_length": 42,
                }
            ],
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
        # faithful to the real coverage_gate.py --manifest path: page_files
        # must exist and be non-empty, terminal_evidence must be a str, and
        # the verdict derives from evidence + JD bodies, not candidate
        # existence.  Manifest containment + evidence_refs checks are skipped
        # here (the real script enforces them; the fakes stay permissive).
        parts = cli_args.split()
        candidates = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
        manifest_path = Path(parts[parts.index("--manifest") + 1])
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        page_files = [
            str(Path(p).resolve())
            for p in manifest.get("page_files") or []
            if isinstance(p, str) and Path(p).is_file() and Path(p).stat().st_size > 0
        ]
        terminal = manifest.get("terminal_evidence")
        reasons: list[str] = []
        if not page_files:
            reasons.append("no_page_evidence")
        if not isinstance(terminal, str) or not terminal:
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
        expected_count = manifest.get("listing_count")
        if not isinstance(expected_count, int):
            expected_count = None
        if expected_count is not None and len(candidates) != expected_count:
            reasons.append("expected_count_mismatch")
        return json.dumps(
            {
                "coverage_verified": not reasons,
                "page_count": len(page_files),
                "candidate_count": len(candidates),
                "body_candidate_count": body_count,
                "unique_listing_count": len(candidates),
                "expected_count": expected_count,
                "terminal_evidence": (
                    terminal if isinstance(terminal, str) else None
                ),
                "reasons": reasons,
                "manifest_path": str(manifest_path),
            },
            ensure_ascii=False,
        )
    if script == "validate":
        return json.dumps({"ok": True})
    if script == "deduplicate":
        # faithful to the real script: merge the input files into --out and
        # report output_count = the merged candidate count (the graph re-reads
        # --out for the candidates channel, so the write must be real)
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        inputs = [Path(token) for token in parts[: parts.index("--out")]]
        merged: list[dict] = []
        for path in inputs:
            merged.extend(json.loads(path.read_text(encoding="utf-8")))
        out_path.write_text(json.dumps(merged, ensure_ascii=False), encoding="utf-8")
        return json.dumps(
            {
                "status": "ok",
                "stats": {
                    "input_count": len(inputs),
                    "garbage_dropped": 0,
                    "garbage_titles": [],
                    "output_count": len(merged),
                    "duplicates_removed": 0,
                    "shared_listing_urls_cleared": 0,
                },
                "load_errors": [],
                "verify_warnings_count": 0,
                "output_file": str(out_path),
            }
        )
    return "{}"


def _fake_extracted(title: str = "后端工程师") -> ExtractedJobDetails:
    return ExtractedJobDetails(
        title=title,
        company_name="示例公司",
        locations=["上海"],
        responsibilities="负责后端服务开发",
        requirements="精通 Python",
        recruitment_types=["校招"],
        apply_url=None,
        deadline_text=None,
        confidence=0.9,
        evidence_refs=[],
        normalization_warnings=[],
    )


def test_job_discovery_tool_returns_valid_tool_observation(tmp_path) -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch_with_pages(tmp_path),
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    # no thread bound -> config = {} branch (single-shot, no checkpointer)
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://job0.example.com"])}))
    )
    # the harness's _project_tool_observations validates every ToolMessage
    # against ToolObservation (extra="forbid"): tool_name/status are
    # required, results live in `output` - a bare results dict would be
    # silently dropped and stall the run
    assert set(out) == {"tool_name", "status", "output"}
    assert out["tool_name"] == "run-job-discovery-workflow"
    assert out["status"] == "succeeded"
    results = out["output"]
    # Task 11 output contract: status / pages / candidates_file /
    # merged_count / terminal_evidence / coverage / per_url_results /
    # candidates + errors (D2), with the Task 10 keys retained
    assert set(results) == {
        "status",
        "pages",
        "candidates_file",
        "merged_count",
        "terminal_evidence",
        "coverage",
        "per_url_results",
        "candidates",
        "dedup_stats",
        "normalize_keys",
        "errors",
    }
    assert results["status"] == "succeeded"
    assert results["per_url_results"][0]["status"] == "succeeded"
    # evidence promotion contract: succeeded per-url entries carry both
    # source_url and content_hash so harness evidence projection promotes
    # fetch evidence
    assert results["per_url_results"][0]["source_url"] == "https://job0.example.com"
    assert results["per_url_results"][0]["content_hash"] == hashlib.sha256(
        (tmp_path / "pages" / "page_00.txt").read_bytes()
    ).hexdigest()
    # browse provenance keys: the mode that produced the evidence + the
    # resolved page-file paths (Task 4 per_url_results shape + Task 7)
    assert results["per_url_results"][0]["mode"] == "list"
    assert results["per_url_results"][0]["page_files"] == [
        str(tmp_path / "pages" / "page_00.txt")
    ]
    assert results["candidates"][0]["title"] == "后端工程师"
    assert results["coverage"]["verified"] is True
    assert results["coverage"]["coverage_verified"] is True
    assert results["coverage"]["reasons"] == []
    # observed marker only, never synthesized; the graph's error channel is
    # empty on a clean run
    assert results["terminal_evidence"] == ["next_control_absent"]
    assert results["errors"] == []
    # the injected extract wrote no per-page files, so the dedup node has
    # nothing to merge: counts stay at their no-merge defaults and
    # candidates_file honestly points at the snapshot the gate read
    assert results["merged_count"] == 0
    assert results["dedup_stats"] == {}
    assert results["candidates_file"] == str(
        tmp_path / "output" / "work" / "candidates.json"
    )


def test_job_discovery_tool_threaded_invocation(tmp_path) -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        checkpointer=InMemorySaver(),
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
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


def test_job_discovery_tool_dict_input_path(tmp_path) -> None:
    # the deepagents ToolNode invokes tools with dict args ({"payload": ...});
    # verify the kwargs path maps the schema field to the func parameter
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch_with_pages(tmp_path),
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke({"payload": json.dumps(["https://job0.example.com"])})
    )
    assert [r["url"] for r in out["output"]["per_url_results"]] == [
        "https://job0.example.com"
    ]
    assert out["output"]["candidates"][0]["title"] == "后端工程师"
    assert out["output"]["coverage"]["verified"] is True


def test_job_discovery_tool_folds_invalid_payload(tmp_path) -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(tool.invoke(json.dumps({"payload": "not json"})))
    assert out["status"] == "failed"
    assert out["tool_name"] == "run-job-discovery-workflow"
    assert out["error_code"].startswith("workflow_error")


def test_job_discovery_tool_folds_graph_crash(tmp_path) -> None:
    def exploding_fetch(urls: list[str]) -> list[dict]:
        raise RuntimeError("boom")

    tool = build_job_discovery_tool(
        fetch_fn=exploding_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://a.com"])}))
    )
    assert out["status"] == "failed"
    assert out["error_code"] == "workflow_error: RuntimeError"


def test_job_discovery_tool_observation_is_always_str(tmp_path) -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    raw = tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    assert isinstance(raw, str)
    assert isinstance(json.loads(raw), dict)


def test_job_discovery_tool_success_parses_as_tool_observation(tmp_path) -> None:
    # regression for the review finding: the success dict must be a valid
    # ToolObservation (extra="forbid", required tool_name/status) or the
    # harness silently drops it and the run stalls
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch_with_pages(tmp_path),
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    raw = tool.invoke(json.dumps({"payload": json.dumps(["https://job0.example.com"])}))
    obs = ToolObservation.model_validate(json.loads(raw))
    assert obs.status == "succeeded"
    assert obs.error_code is None
    assert obs.output["coverage"]["verified"] is True


# ---------------------------------------------------------------------------
# extract_page: per-page gated extraction (observed-evidence registration)
# ---------------------------------------------------------------------------


def _page_file(tmp_path: Path, name: str, text: str) -> PageFile:
    path = tmp_path / "pages" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return PageFile(
        path=str(path),
        content_hash=f"hash-{name}",
        text_length=len(text.encode("utf-8")),
    )


def test_extract_page_registers_observed_evidence_and_returns_candidates(
    tmp_path,
) -> None:
    text = "岗位：后端工程师\n" + "职责：" + "x" * 3000
    page = _page_file(tmp_path, "page_01.txt", text)
    captured: dict = {}

    def fake_extract_fn(context, payload):
        captured["context"] = context
        captured["payload"] = payload
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash=payload.artifact_id,
            candidates=[_fake_extracted()],
        )

    candidates = jdg.extract_page(
        page,
        url="https://example.com/jobs",
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=fake_extract_fn,
    )
    assert candidates[0].title == "后端工程师"
    # evidence registration: bare content_hash as artifact_id (the payload
    # artifact_id resolves it via the first match arm of career_skills
    # _find_observed_evidence), source_url + bounded visible_text
    evidence = captured["context"].metadata["observed_public_evidence"]
    assert evidence == [
        {
            "artifact_id": "hash-page_01.txt",
            "source_url": "https://example.com/jobs",
            "content_hash": "hash-page_01.txt",
            "visible_text": text[: jdg._VISIBLE_TEXT_LIMIT],
        }
    ]
    assert captured["payload"].artifact_id == "hash-page_01.txt"


def test_extract_page_skips_missing_page_file(tmp_path) -> None:
    page = PageFile(path=str(tmp_path / "gone.txt"), content_hash="h", text_length=0)
    called = False

    def fake_extract_fn(context, payload):
        nonlocal called
        called = True
        return ExtractObservedJobDetailsOutput(
            source_artifact_id="", source_url="", content_hash="", candidates=[]
        )

    candidates = jdg.extract_page(
        page,
        url="https://example.com/jobs",
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=fake_extract_fn,
    )
    assert candidates == []
    assert not called


def test_extract_page_resolves_relative_page_path(tmp_path) -> None:
    # state paths may be relative to the run's out_dir; the file resolves
    path = tmp_path / "pages" / "page_01.txt"
    path.parent.mkdir()
    path.write_text("岗位：后端工程师", encoding="utf-8")
    page = PageFile(path="pages/page_01.txt", content_hash="h", text_length=8)

    def fake_extract_fn(context, payload):
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash=payload.artifact_id,
            candidates=[_fake_extracted()],
        )

    candidates = jdg.extract_page(
        page,
        url="https://example.com/jobs",
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=fake_extract_fn,
    )
    assert candidates[0].title == "后端工程师"


def test_extract_page_folds_extract_error(tmp_path) -> None:
    page = _page_file(tmp_path, "page_01.txt", "岗位：后端工程师")

    def raising_extract_fn(context, payload):
        raise PublicJobFetchError("observed_evidence_not_found")

    candidates = jdg.extract_page(
        page,
        url="https://example.com/jobs",
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=raising_extract_fn,
    )
    assert candidates == []


def test_extract_page_uses_llm_gate_when_extractor_present(tmp_path) -> None:
    # the regex extractor finds only a 0.35-confidence stub in generic text,
    # so the gate fires and the LLM extractor's candidates join via
    # strict-Pareto union
    page = _page_file(
        tmp_path,
        "page_01.txt",
        "欢迎来到我们的博客。今天我们讨论公司文化和团队建设，以及日常协作的流程。",
    )
    captured: dict = {}

    def fake_llm_extractor(context, payload):
        captured["context"] = context
        captured["payload"] = payload
        return ExtractObservedJobDetailsOutput(
            source_artifact_id=payload.artifact_id,
            source_url="https://example.com/jobs",
            content_hash=payload.artifact_id,
            candidates=[_fake_extracted(title="LLM 补充职位")],
        )

    candidates = jdg.extract_page(
        page,
        url="https://example.com/jobs",
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=jdg.extract_observed_job_details,
        llm_extractor=fake_llm_extractor,
    )
    assert captured["payload"].artifact_id == "hash-page_01.txt"
    # the strict-Pareto union keeps the regex stub too (its identity is
    # ("", "") so it is never deduped away): membership, not equality
    assert "LLM 补充职位" in [c.title for c in candidates]


def test_extract_page_gate_not_triggered_when_regex_confident(tmp_path) -> None:
    # feishu-style markers give the regex candidate a 0.6 confidence, which is
    # NOT below the 0.6 gate -> the LLM extractor is never consulted
    page = _page_file(
        tmp_path,
        "page_01.txt",
        "招聘岗位：算法工程师\n工作职责：负责推荐算法研发\n任职资格：硕士及以上",
    )

    class _ForbiddenLLM:
        def __call__(self, context, payload):
            raise AssertionError("gate must not fire on confident regex output")

    candidates = jdg.extract_page(
        page,
        url="https://example.com/jobs",
        out_dir=str(tmp_path),
        context=ToolContext(user_id="", run_id="", metadata={}),
        extract_fn=jdg.extract_observed_job_details,
        llm_extractor=_ForbiddenLLM(),
    )
    assert candidates
    assert candidates[0].title


# ---------------------------------------------------------------------------
# write_page_candidates: stdin contract to the write_candidates script
# ---------------------------------------------------------------------------


def test_write_page_candidates_pipes_stdin_and_returns_accepted(tmp_path) -> None:
    captured: dict = {}

    def runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        captured["script"] = script
        captured["cli_args"] = cli_args
        captured["stdin"] = stdin
        # faithful to the real script: persist the batch to --out
        parts = cli_args.split()
        out_path = Path(parts[parts.index("--out") + 1])
        out_path.write_text(stdin, encoding="utf-8")
        return json.dumps(
            {
                "status": "ok",
                "out": "output/candidates/page_01.json",
                "batch_received": 1,
                "batch_kept": 1,
                "batch_dropped_invalid": 0,
                "appended": 1,
                "total_in_file": 1,
                "mode": "overwrite",
            }
        )

    accepted = jdg.write_page_candidates(
        "page_01",
        [_fake_extracted()],
        runner=runner,
        candidates_dir=str(tmp_path),
    )
    assert accepted == 1
    assert captured["script"] == "write_candidates"
    assert captured["cli_args"] == f"--out {tmp_path / 'page_01.json'}"
    payload = json.loads(captured["stdin"])
    assert payload[0]["title"] == "后端工程师"
    assert (tmp_path / "page_01.json").exists()


def test_write_page_candidates_zero_on_unparsable_summary(tmp_path) -> None:
    def garbage_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        return "garbage not json"

    accepted = jdg.write_page_candidates(
        "page_01",
        [_fake_extracted()],
        runner=garbage_runner,
        candidates_dir=str(tmp_path),
    )
    assert accepted == 0


def test_write_page_candidates_zero_on_non_dict_summary(tmp_path) -> None:
    def list_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        return "[]"

    accepted = jdg.write_page_candidates(
        "page_01",
        [_fake_extracted()],
        runner=list_runner,
        candidates_dir=str(tmp_path),
    )
    assert accepted == 0


def test_write_page_candidates_default_runner_branch(monkeypatch, tmp_path) -> None:
    # runner=None resolves to the module-level run_skill_script: the
    # monkeypatched fake proves the default channel is used without ever
    # invoking a real skill script
    captured: dict = {}

    def fake_run_skill_script(script: str, cli_args: str = "", stdin: str = "") -> str:
        captured["script"] = script
        captured["cli_args"] = cli_args
        captured["stdin"] = stdin
        return json.dumps({"status": "ok", "batch_kept": 2})

    monkeypatch.setattr(jdg, "run_skill_script", fake_run_skill_script)
    accepted = jdg.write_page_candidates(
        "page_01", [_fake_extracted(), _fake_extracted()], candidates_dir=str(tmp_path)
    )
    assert accepted == 2
    assert captured["script"] == "write_candidates"
    assert captured["cli_args"] == f"--out {tmp_path / 'page_01.json'}"
    assert json.loads(captured["stdin"])[0]["title"] == "后端工程师"


def test_write_page_candidates_zero_on_error_summary(tmp_path) -> None:
    # the real script emits a status=error JSON (e.g. refused --out); no
    # batch_kept key means zero accepted, never a crash
    def error_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        return json.dumps({"status": "error", "reason": "refused: --out must be under output/"})

    accepted = jdg.write_page_candidates(
        "page_01",
        [_fake_extracted()],
        runner=error_runner,
        candidates_dir=str(tmp_path),
    )
    assert accepted == 0


def test_write_page_candidates_zero_on_non_int_kept(tmp_path) -> None:
    def string_kept_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        return json.dumps({"status": "ok", "batch_kept": "2"})

    accepted = jdg.write_page_candidates(
        "page_01",
        [_fake_extracted()],
        runner=string_kept_runner,
        candidates_dir=str(tmp_path),
    )
    assert accepted == 0


def test_write_page_candidates_default_dir_under_skill(tmp_path) -> None:
    # candidates_dir default = output/candidates under the skill dir (the
    # real script enforces --out under output/)
    captured: dict = {}

    def runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        captured["cli_args"] = cli_args
        return json.dumps({"status": "ok", "batch_kept": 0})

    jdg.write_page_candidates("page_01", [_fake_extracted()], runner=runner)
    assert str(SKILL_DIR / "output" / "candidates" / "page_01.json") in captured[
        "cli_args"
    ]


def test_write_page_candidates_relative_dir_resolved_under_skill(tmp_path) -> None:
    # an explicitly relative candidates_dir resolves under the skill dir too
    captured: dict = {}

    def runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        captured["cli_args"] = cli_args
        return json.dumps({"status": "ok", "batch_kept": 0})

    jdg.write_page_candidates(
        "page_01",
        [_fake_extracted()],
        runner=runner,
        candidates_dir="output/candidates",
    )
    assert str(SKILL_DIR / "output" / "candidates" / "page_01.json") in captured[
        "cli_args"
    ]


# ---------------------------------------------------------------------------
# Task 10 incremental mode: {"urls", "prior_metadata"} tool input
# ---------------------------------------------------------------------------


def test_job_discovery_tool_incremental_prior_metadata(tmp_path) -> None:
    calls: list[tuple[str, str]] = []

    def incremental_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        calls.append((script, cli_args))
        if script == "state" and cli_args.startswith("check "):
            # url a.com is already extracted at this update_time -> skip;
            # b.com needs extraction (real script would exit 0/1)
            return json.dumps({"exit_code": 0 if "https://a.com" in cli_args else 1})
        if script == "state":
            return json.dumps({"marked": True})
        if script == "normalize":
            return json.dumps({"input": "后端工程师", "normalized": "backend-engineer"})
        return _fake_runner(script, cli_args=cli_args, stdin=stdin)

    def incremental_extract(pages: list[dict]) -> tuple[list[dict], None]:
        # the injected extract_fn never writes per-page files; persist the
        # candidate to a page file so the dedup node's page_*.json glob
        # sees it and the merge path actually runs
        (tmp_path / "page_00.json").write_text(
            json.dumps(
                [
                    {
                        "title": "后端工程师",
                        "responsibilities": "负责后端服务开发",
                        "requirements": "精通 Python",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _fake_extract(pages)

    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=incremental_runner,
        extract_fn=incremental_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(
            json.dumps(
                {
                    "payload": json.dumps(
                        {
                            "urls": ["https://a.com", "https://b.com"],
                            "prior_metadata": {
                                "file_id": "f1",
                                "sheet_id": "s1",
                                "update_time": "2026-08-07",
                            },
                        }
                    )
                }
            )
        )
    )
    assert out["status"] == "succeeded"
    results = out["output"]
    by_url = {r["url"]: r for r in results["per_url_results"]}
    # state check before fetch: a.com skipped (its per-URL entry reflects
    # the skip status), b.com fetched and processed
    assert by_url["https://a.com"]["status"] == "skipped"
    assert by_url["https://a.com"]["reason"] == "update_time unchanged"
    assert by_url["https://b.com"]["status"] == "succeeded"
    # skipped entries extend after the fetched ones (every input URL still
    # has exactly one entry)
    assert [r["url"] for r in results["per_url_results"]] == [
        "https://b.com",
        "https://a.com",
    ]
    # one check per URL, carrying the run's update_time
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
    # comparison keys from the normalize node enter the tool output only
    assert results["normalize_keys"]["后端工程师"] == "backend-engineer"
    # the page-file merge still ran (single-shot semantics preserved under
    # incremental mode)
    assert results["merged_count"] == 1


def test_job_discovery_tool_incremental_dict_args_path(tmp_path) -> None:
    # C1 (review round 1): the harness ToolNode invokes tools with dict
    # args; the args_schema strips the {"payload": ...} wrapper BEFORE
    # run(), so run() receives the inner object string — the first unwrap
    # branch must never index "payload" blindly, or the incremental object
    # crashes with KeyError through the production invocation path
    calls: list[str] = []

    def incremental_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        calls.append(f"{script} {cli_args}")
        if script == "state" and cli_args.startswith("check "):
            return json.dumps({"exit_code": 1})
        if script == "state":
            return json.dumps({"marked": True})
        if script == "normalize":
            return json.dumps({"input": "后端工程师", "normalized": "backend-engineer"})
        return _fake_runner(script, cli_args=cli_args, stdin=stdin)

    def incremental_extract(pages: list[dict]) -> tuple[list[dict], None]:
        (tmp_path / "page_00.json").write_text(
            json.dumps(
                [
                    {
                        "title": "后端工程师",
                        "responsibilities": "负责后端服务开发",
                        "requirements": "精通 Python",
                    }
                ],
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return _fake_extract(pages)

    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=incremental_runner,
        extract_fn=incremental_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(
            {
                "payload": json.dumps(
                    {
                        "urls": ["https://a.com"],
                        "prior_metadata": {
                            "file_id": "f1",
                            "sheet_id": "s1",
                            "update_time": "2026-08-07",
                        },
                    }
                )
            }
        )
    )
    assert out["status"] == "succeeded"
    results = out["output"]
    # the full incremental pipeline ran through the dict-args path: state
    # check before fetch, mark after extraction, prior_metadata threaded
    assert results["per_url_results"][0]["status"] == "succeeded"
    assert "state check https://a.com 2026-08-07" in calls
    assert "state mark hash-0 https://a.com 2026-08-07 --file-id f1 --sheet-id s1" in calls
    assert results["normalize_keys"]["后端工程师"] == "backend-engineer"


def test_job_discovery_tool_dict_input_without_prior_metadata(tmp_path) -> None:
    # Task 10: a {"urls": [...]} object input without prior_metadata takes the
    # single-shot path — no state check/mark, no merged accumulation
    calls: list[str] = []

    def recording_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        calls.append(f"{script} {cli_args}")
        return _fake_runner(script, cli_args=cli_args, stdin=stdin)

    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=recording_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(
            json.dumps({"payload": json.dumps({"urls": ["https://a.com"]})})
        )
    )
    assert out["status"] == "succeeded"
    # absent prior_metadata -> the workflow never touches the state store
    assert not any(c.startswith("state ") for c in calls)


def _seed_fake_pages(
    tmp_path: Path, count: int = 1, marker: str = "next_control_absent"
) -> list[dict]:
    """Real on-disk page files (absolute paths) + an observed terminal marker.

    Mirrors browse.py's emitted page dicts: real non-empty ``page_NN.txt``
    files under a pages dir, sha256-hashed, with ``terminal_evidence`` =
    markers actually observed by browse (never synthesized by the caller).
    """
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir(parents=True, exist_ok=True)
    out: list[dict] = []
    for index in range(count):
        path = pages_dir / f"page_{index:02d}.txt"
        text = "招聘岗位：后端工程师\n岗位职责：负责后端服务开发\n任职要求：精通 Python"
        path.write_text(text, encoding="utf-8")
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        out.append(
            {
                "url": f"https://job{index}.example.com",
                "source_url": f"https://job{index}.example.com",
                "status": "succeeded",
                "content_hash": digest,
                "mode": "list",
                "page_files": [
                    {
                        "path": str(path),
                        "content_hash": digest,
                        "text_length": path.stat().st_size,
                    }
                ],
                "terminal_evidence": [marker],
                "visible_text": text,
            }
        )
    return out


def _fake_fetch_with_pages(tmp_path: Path):
    return lambda urls: _seed_fake_pages(tmp_path, count=len(urls))


def test_coverage_node_passes_real_manifest(tmp_path) -> None:
    # runner intercepts coverage_gate and asserts --manifest path exists and
    # its page_files point at real non-empty files; returns the real script's
    # manifest-mode output shape {coverage_verified: True, reasons: [], ...}
    captured: dict = {}

    def manifest_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            parts = cli_args.split()
            assert "--manifest" in parts
            manifest_path = Path(parts[parts.index("--manifest") + 1])
            assert manifest_path.is_file()
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            captured["manifest"] = manifest
            for page in manifest["page_files"]:
                assert Path(page).is_file()
                assert Path(page).stat().st_size > 0
        return _fake_runner(script, cli_args, stdin)

    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch_with_pages(tmp_path),
        script_runner=manifest_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://job0.example.com"])}))
    )
    assert out["status"] == "succeeded"
    coverage = out["output"]["coverage"]
    # honest gate-open: real page file + observed terminal marker + JD body
    assert coverage["verified"] is True
    assert coverage["reasons"] == []
    manifest = captured["manifest"]
    assert manifest["page_files"] == [str(tmp_path / "pages" / "page_00.txt")]
    # the observed marker is the ONLY terminal evidence (never synthesized)
    assert manifest["terminal_evidence"] == "next_control_absent"
    # dedup never ran (extract fake wrote no per-page files): the listing
    # count is honestly unknown, never fabricated from candidate count
    assert manifest["listing_count"] is None


def test_coverage_node_no_synthesized_terminal_evidence(tmp_path) -> None:
    # results with terminal_evidence == [] -> manifest.terminal_evidence == []
    # (never derived from the last page hash, never fabricated)
    captured: dict = {}

    def fetch_no_marker(urls: list[str]) -> list[dict]:
        pages = _seed_fake_pages(tmp_path, count=len(urls))
        for page in pages:
            page["terminal_evidence"] = []
        return pages

    def manifest_runner(script: str, cli_args: str = "", stdin: str = "") -> str:
        if script == "coverage_gate":
            parts = cli_args.split()
            manifest_path = Path(parts[parts.index("--manifest") + 1])
            captured["manifest"] = json.loads(
                manifest_path.read_text(encoding="utf-8")
            )
        return _fake_runner(script, cli_args, stdin)

    tool = build_job_discovery_tool(
        fetch_fn=fetch_no_marker,
        script_runner=manifest_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(json.dumps({"payload": json.dumps(["https://job0.example.com"])}))
    )
    assert out["status"] == "succeeded"
    assert captured["manifest"]["terminal_evidence"] == []
    # the observed-markers channel is also empty (never synthesized)
    assert out["output"]["terminal_evidence"] == []
    # honest gate: missing terminal evidence keeps the run off the
    # "coverage verified" claim
    assert out["output"]["coverage"]["verified"] is False
    assert "missing_terminal_evidence" in out["output"]["coverage"]["reasons"]


def test_tool_output_contract_blocked_all_urls(tmp_path) -> None:
    # all URLs blocked -> contract status "blocked", error_code "blocked" per
    # url; candidates/coverage stay empty/honest (never a fake success)
    def fetch_blocked(urls: list[str]) -> list[dict]:
        return [
            {
                "url": url,
                "source_url": url,
                "status": "blocked",
                "error_code": "blocked",
                "blocked_reason": "captcha",
            }
            for url in urls
        ]

    tool = build_job_discovery_tool(
        fetch_fn=fetch_blocked,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(
            json.dumps({"payload": json.dumps(["https://a.com", "https://b.com"])})
        )
    )
    assert out["status"] == "succeeded"
    results = out["output"]
    # ToolObservation itself rejects "blocked" as a status - the contract
    # status lives inside the output dict (documented deviation)
    assert results["status"] == "blocked"
    assert len(results["per_url_results"]) == 2
    assert all(r["error_code"] == "blocked" for r in results["per_url_results"])
    assert results["candidates"] == []
    assert results["errors"] == []
    assert results["coverage"]["verified"] is False


def test_tool_output_contract_merged_count_present(tmp_path) -> None:
    # succeeded path -> merged_count == len(merged candidates) and
    # candidates_file points at the merged store
    def extract_with_pages(pages: list[dict]) -> tuple[list[dict], None]:
        # persist per-page candidate files so the dedup merge path runs
        for index, page in enumerate(pages):
            (tmp_path / f"page_{index:02d}.json").write_text(
                json.dumps(
                    [
                        {
                            "title": f"职位{index}",
                            "responsibilities": "负责后端服务开发",
                            "requirements": "精通 Python",
                        }
                    ]
                ),
                encoding="utf-8",
            )
        return _fake_extract(pages)

    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch_with_pages(tmp_path),
        script_runner=_fake_runner,
        extract_fn=extract_with_pages,
        candidates_dir=str(tmp_path),
        state_dir=str(tmp_path),
    )
    out = json.loads(
        tool.invoke(
            json.dumps(
                {
                    "payload": json.dumps(
                        ["https://job0.example.com", "https://job1.example.com"]
                    )
                }
            )
        )
    )
    assert out["status"] == "succeeded"
    results = out["output"]
    assert results["status"] == "succeeded"
    assert results["merged_count"] == 2
    assert results["merged_count"] == len(results["candidates"])
    assert results["candidates_file"] == str(tmp_path / "merged_final.json")


def test_build_job_discovery_tools_run_scoped_dirs(monkeypatch) -> None:
    # D1: two factory calls with different run_ids yield different,
    # non-default dirs, keyed under the skill output (never the shared
    # run-0 defaults); the factory-built tool keeps production defaults
    from backend.app.services.deepagents_runtime.tools import skill_graphs as sg

    captured: list[dict] = []
    real_build = sg.build_job_discovery_tool

    def fake_build(**kwargs):
        captured.append(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(sg, "build_job_discovery_tool", fake_build)

    tools_a = sg.build_job_discovery_tools(
        "job-discovery", run_id="eval-1", budgets=None, tracker=None
    )
    tools_b = sg.build_job_discovery_tools(
        "job-discovery", run_id="eval-2", budgets=None, tracker=None
    )
    assert len(tools_a) == 1 and len(tools_b) == 1
    dirs_a, dirs_b = captured[0], captured[1]
    # different run_ids -> different run-scoped dirs
    for key in ("state_dir", "candidates_dir", "evidence_dir", "wechat_out_dir"):
        assert dirs_a[key] != dirs_b[key]
    # keyed under skill/job-discovery/output by run_id
    assert dirs_a["state_dir"] == str(
        SKILL_DIR / "output" / "runs" / "run-eval-1"
    )
    assert dirs_b["candidates_dir"] == str(
        SKILL_DIR / "output" / "runs" / "run-eval-2" / "output" / "candidates"
    )
    assert dirs_a["evidence_dir"] == str(
        SKILL_DIR / "output" / "evidence" / "run-eval-1"
    )
    assert dirs_a["wechat_out_dir"] == str(SKILL_DIR / "output" / "ocr" / "run-eval-1")
    # never the shared defaults (run-0 / bare output dirs)
    assert dirs_a["candidates_dir"] != str(SKILL_DIR / "output" / "candidates")
    assert dirs_a["evidence_dir"] != str(SKILL_DIR / "output" / "evidence" / "run-0")
    assert dirs_a["wechat_out_dir"] != str(SKILL_DIR / "output" / "ocr" / "run-0")
    # production defaults: real browse orchestration + real extraction +
    # in-memory subgraph checkpointer (no test seams)
    assert dirs_a["fetch_fn"] is None
    assert dirs_a["script_runner"] is sg.run_skill_script
    assert dirs_a["extract_fn"] is None
    assert dirs_a["checkpointer"] is None


def test_factory_tool_writes_merged_into_run_scoped_dir(monkeypatch) -> None:
    # D1: the tool built from the factory writes merged_final.json into the
    # run-scoped candidates dir (not the shared default store)
    from backend.app.services.deepagents_runtime.tools import skill_graphs as sg
    from backend.app.services.deepagents_runtime.tools.skill_graphs import (
        browse_fetch as bf,
    )

    run_id = "eval-merged"
    state_dir = Path(sg.resolve_run_output_dirs(run_id)["state_dir"])
    evidence_dir = Path(sg.resolve_run_output_dirs(run_id)["evidence_dir"])
    shutil.rmtree(state_dir, ignore_errors=True)
    shutil.rmtree(evidence_dir, ignore_errors=True)
    try:
        # production script seam (real run_skill_script would launch
        # Playwright): mirror the real scripts' contracts for the scripts
        # the run-scoped workflow invokes
        def fake_script(script: str, cli_args: str = "", stdin: str = "", **kwargs):
            if script == "browse":
                parts = cli_args.split()
                out_dir = Path(parts[parts.index("--out") + 1])
                out_dir.mkdir(parents=True, exist_ok=True)
                page = out_dir / "pages" / "page_01.txt"
                page.parent.mkdir(parents=True, exist_ok=True)
                page.write_text(
                    "招聘岗位：后端工程师\n岗位职责：负责后端服务开发\n任职要求：硕士及以上",
                    encoding="utf-8",
                )
                return json.dumps(
                    {
                        "status": "ok",
                        "mode": "list",
                        "page_count": 1,
                        "page_files": [str(page)],
                        "terminal_evidence": "next_control_absent",
                    }
                )
            if script == "write_candidates":
                parts = cli_args.split()
                out_path = Path(parts[parts.index("--out") + 1])
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(stdin, encoding="utf-8")
                return json.dumps(
                    {"status": "ok", "batch_kept": len(json.loads(stdin))}
                )
            if script == "validate":
                return json.dumps({"ok": True})
            if script == "deduplicate":
                parts = cli_args.split()
                out_path = Path(parts[parts.index("--out") + 1])
                inputs = [Path(token) for token in parts[: parts.index("--out")]]
                merged: list[dict] = []
                for path in inputs:
                    merged.extend(json.loads(path.read_text(encoding="utf-8")))
                out_path.parent.mkdir(parents=True, exist_ok=True)
                out_path.write_text(
                    json.dumps(merged, ensure_ascii=False), encoding="utf-8"
                )
                return json.dumps(
                    {
                        "status": "ok",
                        "stats": {
                            "input_count": len(inputs),
                            "garbage_dropped": 0,
                            "garbage_titles": [],
                            "output_count": len(merged),
                            "duplicates_removed": 0,
                            "shared_listing_urls_cleared": 0,
                        },
                        "load_errors": [],
                        "verify_warnings_count": 0,
                        "output_file": str(out_path),
                    }
                )
            if script == "normalize":
                return json.dumps(
                    {"input": "后端工程师", "normalized": "backend-engineer"}
                )
            if script == "coverage_gate":
                parts = cli_args.split()
                manifest_path = Path(parts[parts.index("--manifest") + 1])
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                candidates = json.loads(Path(parts[0]).read_text(encoding="utf-8"))
                page_files = [
                    p
                    for p in manifest["page_files"]
                    if Path(p).is_file() and Path(p).stat().st_size > 0
                ]
                terminal = manifest.get("terminal_evidence")
                reasons: list[str] = []
                if not page_files:
                    reasons.append("no_page_evidence")
                if not isinstance(terminal, str) or not terminal:
                    reasons.append("missing_terminal_evidence")
                if any(
                    not (
                        (c.get("responsibilities") or "").strip()
                        or (c.get("requirements") or "").strip()
                    )
                    for c in candidates
                ):
                    reasons.append("missing_jd_body")
                return json.dumps(
                    {
                        "coverage_verified": not reasons,
                        "page_count": len(page_files),
                        "candidate_count": len(candidates),
                        "body_candidate_count": len(candidates),
                        "unique_listing_count": len(candidates),
                        "expected_count": manifest.get("listing_count"),
                        "terminal_evidence": (
                            terminal if isinstance(terminal, str) else None
                        ),
                        "reasons": reasons,
                        "manifest_path": str(manifest_path),
                    },
                    ensure_ascii=False,
                )
            return "{}"

        monkeypatch.setattr(jdg, "run_skill_script", fake_script)
        monkeypatch.setattr(bf, "run_skill_script", fake_script)

        tool = sg.build_job_discovery_tools(
            "job-discovery", run_id=run_id, budgets=None, tracker=None
        )[0]
        out = json.loads(
            tool.invoke(
                json.dumps({"payload": json.dumps(["https://example.com/jobs"])})
            )
        )
        assert out["status"] == "succeeded"
        assert out["output"]["status"] == "succeeded"
        merged_file = state_dir / "output" / "candidates" / "merged_final.json"
        assert merged_file.is_file()
        assert out["output"]["candidates_file"] == str(merged_file)
        # the shared default store was never touched
        assert not (SKILL_DIR / "output" / "candidates" / "merged_final.json").exists()
    finally:
        shutil.rmtree(state_dir, ignore_errors=True)
        shutil.rmtree(evidence_dir, ignore_errors=True)
