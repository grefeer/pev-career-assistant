from __future__ import annotations

import json
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
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
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
    assert set(results) == {
        "per_url_results",
        "candidates",
        "coverage",
        "merged_count",
        "dedup_stats",
    }
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
    # the injected extract wrote no per-page files, so the dedup node has
    # nothing to merge: counts stay at their no-merge defaults
    assert results["merged_count"] == 0
    assert results["dedup_stats"] == {}


def test_job_discovery_tool_threaded_invocation(tmp_path) -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        checkpointer=InMemorySaver(),
        candidates_dir=str(tmp_path),
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
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
    )
    out = json.loads(tool.invoke({"payload": json.dumps(["https://example.com/jobs"])}))
    assert [r["url"] for r in out["output"]["per_url_results"]] == [
        "https://example.com/jobs"
    ]
    assert out["output"]["candidates"][0]["title"] == "后端工程师"
    assert out["output"]["coverage"]["verified"] is True


def test_job_discovery_tool_folds_invalid_payload(tmp_path) -> None:
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
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
    )
    raw = tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
    assert isinstance(raw, str)
    assert isinstance(json.loads(raw), dict)


def test_job_discovery_tool_success_parses_as_tool_observation(tmp_path) -> None:
    # regression for the review finding: the success dict must be a valid
    # ToolObservation (extra="forbid", required tool_name/status) or the
    # harness silently drops it and the run stalls
    tool = build_job_discovery_tool(
        fetch_fn=_fake_fetch,
        script_runner=_fake_runner,
        extract_fn=_fake_extract,
        candidates_dir=str(tmp_path),
    )
    raw = tool.invoke(json.dumps({"payload": json.dumps(["https://example.com/jobs"])}))
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
