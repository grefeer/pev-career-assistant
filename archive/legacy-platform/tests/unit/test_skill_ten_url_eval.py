from __future__ import annotations

import importlib.util
from pathlib import Path


_RUNNER_PATH = Path(__file__).resolve().parents[1] / "manual" / "run_skill_ten_url_eval.py"


def _load_runner():
    spec = importlib.util.spec_from_file_location("skill_ten_url_eval", _RUNNER_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_browse_probe():
    path = Path(__file__).resolve().parents[1] / "manual" / "run_skill_browse_probe.py"
    spec = importlib.util.spec_from_file_location("skill_browse_probe", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_extract_candidates_ignores_terminal_blocked_message() -> None:
    runner = _load_runner()

    assert runner._extract_candidates('{"status":"blocked","reason":"captcha"}') == []


def test_llm_key_gate_accepts_the_project_openai_compatible_key(monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "test-key")

    assert runner._has_llm_key()


def test_non_recursion_agent_error_with_candidates_is_partial_not_success() -> None:
    runner = _load_runner()

    assert runner._classify_run_status(
        note="AttributeError: tool result was None",
        blocked=False,
        candidates=[{"title": "岗位"}],
    ) == "partial"


def test_replay_normalizes_a_stale_false_success_record() -> None:
    runner = _load_runner()

    record = runner._normalize_replayed_record({
        "status": "succeeded",
        "candidate_count": 102,
        "note": "AttributeError: tool result was None",
        "block_reason": None,
    })

    assert record["status"] == "partial"
    assert record["evaluation_mode"] == "replay"


def test_utf8_requirement_explains_how_to_start_the_eval() -> None:
    runner = _load_runner()

    message = runner._utf8_requirement_message()

    assert "python -X utf8" in message


def test_terminal_evidence_requires_the_explicit_final_json_field() -> None:
    runner = _load_runner()

    assert runner._terminal_evidence_from_content('{"status":"done","terminal_evidence":"last_page_disabled"}') == "last_page_disabled"
    assert runner._terminal_evidence_from_content('{"status":"done"}') is None


def test_browse_metadata_reads_the_deterministic_tool_result_before_page_text() -> None:
    runner = _load_runner()

    metadata = runner._browse_metadata_from_output(
        '{"status":"ok","terminal_evidence":"finite_page_range_exhausted"}'
        '\n[PAGE_TEXT]\n岗位列表 {not json}'
    )

    assert metadata == {"status": "ok", "terminal_evidence": "finite_page_range_exhausted"}


def test_richer_detail_browse_metadata_replaces_a_list_shell() -> None:
    runner = _load_runner()

    shell = {"page_count": 3, "terminal_evidence": "next_control_absent", "text_length": 4000}
    details = {"page_count": 1, "jd_detail_evidence": True, "terminal_evidence": "detail_links_exhausted", "text_length": 19000}
    assert runner._browse_metadata_quality(details) > runner._browse_metadata_quality(shell)


def test_coverage_rejects_model_terminal_claim_without_browse_metadata(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    skill_dir = tmp_path / "job-discovery"
    page_dir = skill_dir / "output" / "evidence" / "pages"
    page_dir.mkdir(parents=True)
    (page_dir / "page_01.txt").write_text("岗位列表", encoding="utf-8")
    (skill_dir / "output" / "candidates_merged.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(runner, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(runner, "_BROWSE_METADATA_FILE", skill_dir / "output" / "evidence" / "browse_metadata.json")
    monkeypatch.setattr(runner, "_last_browse_metadata", None)

    verdict = runner._coverage_for_run(
        candidates=[],
        content='{"terminal_evidence":"invented_by_model"}',
        expected_count=None,
    )

    assert verdict["coverage_verified"] is False
    assert verdict["reasons"] == ["missing_observed_terminal_evidence"]


def test_tool_trace_records_timing_without_raw_tool_output(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    trace_path = tmp_path / "tool_trace.jsonl"
    monkeypatch.setattr(runner, "_TOOL_TRACE_FILE", trace_path)
    monkeypatch.setattr(runner, "_run_started_at", 10.0)
    monkeypatch.setattr(runner.time, "monotonic", lambda: 12.5)

    runner._append_tool_trace(script="browse", started_at=11.0, output="PAGE TEXT secret")

    event = __import__("json").loads(trace_path.read_text(encoding="utf-8"))
    assert event == {"script": "browse", "start_sec": 1.0, "duration_sec": 1.5, "error": False}


def test_unique_count_keeps_distinct_titles_with_a_shared_listing_url() -> None:
    runner = _load_runner()

    assert runner._unique_count([
        {"title": "算法工程师", "apply_url": "https://jobs.example/list", "responsibilities": "开发"},
        {"title": "产品经理", "apply_url": "https://jobs.example/list", "responsibilities": "规划"},
    ]) == 2


def test_outer_coverage_reuses_the_agent_one_shot_gate_result(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    skill_dir = tmp_path / "job-discovery"
    page_dir = skill_dir / "output" / "evidence" / "pages"
    page_dir.mkdir(parents=True)
    (page_dir / "page_01.txt").write_text("岗位列表", encoding="utf-8")
    (skill_dir / "output" / "candidates_merged.json").write_text("[]", encoding="utf-8")
    gate_file = skill_dir / "output" / "evidence" / "coverage_gate_result.json"
    gate_file.write_text('{"coverage_verified":true,"candidate_count":0,"page_count":1,"terminal_evidence":"next_control_absent","reasons":[]}', encoding="utf-8")
    monkeypatch.setattr(runner, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(runner, "_BROWSE_METADATA_FILE", skill_dir / "output" / "evidence" / "browse_metadata.json")
    monkeypatch.setattr(runner, "_COVERAGE_GATE_RESULT_FILE", gate_file)
    monkeypatch.setattr(runner, "_last_browse_metadata", {"terminal_evidence": "next_control_absent"})

    verdict = runner._coverage_for_run(candidates=[], content="", expected_count=None)

    assert verdict["coverage_verified"] is True


def test_outer_coverage_rejects_agent_gate_with_unobserved_terminal_marker(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    skill_dir = tmp_path / "job-discovery"
    page_dir = skill_dir / "output" / "evidence" / "pages"
    page_dir.mkdir(parents=True)
    (page_dir / "page_01.txt").write_text("岗位列表", encoding="utf-8")
    (skill_dir / "output" / "candidates_merged.json").write_text("[]", encoding="utf-8")
    gate_file = skill_dir / "output" / "evidence" / "coverage_gate_result.json"
    gate_file.write_text(
        '{"coverage_verified":true,"candidate_count":0,"page_count":1,"terminal_evidence":"invented","reasons":[]}',
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(runner, "_BROWSE_METADATA_FILE", skill_dir / "output" / "evidence" / "browse_metadata.json")
    monkeypatch.setattr(runner, "_COVERAGE_GATE_RESULT_FILE", gate_file)
    monkeypatch.setattr(runner, "_last_browse_metadata", {"terminal_evidence": "next_control_absent"})

    verdict = runner._coverage_for_run(candidates=[], content="", expected_count=None)

    assert verdict["coverage_verified"] is False
    assert verdict["reasons"] == ["coverage_artifact_terminal_evidence_mismatch"]


def test_observed_listing_count_takes_precedence_over_a_stale_reference(monkeypatch) -> None:
    runner = _load_runner()
    monkeypatch.setattr(runner, "_last_browse_metadata", {"listing_count": 20})

    assert runner._observed_listing_count() == 20


def test_planner_uses_interact_for_a_listing_without_jd_detail_evidence() -> None:
    runner = _load_runner()

    assert "jd_detail_evidence:false" in runner._SKILL_SYSTEM_PROMPT
    assert "--mode interact --max-cards 50 --wait 800" in runner._SKILL_SYSTEM_PROMPT


def test_coverage_without_page_artifacts_is_an_explicit_quality_failure(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    skill_dir = tmp_path / "job-discovery"
    (skill_dir / "output").mkdir(parents=True)
    (skill_dir / "output" / "candidates_merged.json").write_text("[]", encoding="utf-8")
    monkeypatch.setattr(runner, "SKILL_DIR", skill_dir)

    verdict = runner._coverage_for_run(
        candidates=[{"title": "算法", "responsibilities": "开发"}],
        content='{"status":"done","terminal_evidence":"last_page_disabled"}',
        expected_count=None,
    )

    assert verdict["coverage_verified"] is False
    assert verdict["reasons"] == ["no_page_evidence"]


def test_coverage_gate_uses_structured_tool_invocation(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    skill_dir = tmp_path / "job-discovery"
    page_dir = skill_dir / "output" / "evidence" / "pages"
    page_dir.mkdir(parents=True)
    (page_dir / "page_01.txt").write_text("岗位列表", encoding="utf-8")
    (skill_dir / "output" / "candidates_merged.json").write_text(
        '[{"title":"算法","apply_url":"https://jobs.example/a","responsibilities":"开发"}]',
        encoding="utf-8",
    )
    calls: list[dict] = []

    class FakeTool:
        def invoke(self, args):
            calls.append(args)
            return '{"coverage_verified": true}'

    monkeypatch.setattr(runner, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(runner, "run_skill_script", FakeTool())
    monkeypatch.setattr(runner, "_last_browse_metadata", {"terminal_evidence": "finite_page_range_exhausted"})
    monkeypatch.setattr(runner, "_COVERAGE_GATE_RESULT_FILE", skill_dir / "output" / "evidence" / "coverage_gate_result.json")

    verdict = runner._coverage_for_run(
        candidates=[{"title": "算法", "apply_url": "https://jobs.example/a", "responsibilities": "开发"}],
        content="",
        expected_count=1,
    )

    assert verdict == {"coverage_verified": True}
    assert calls and calls[0]["script"] == "coverage_gate"


def test_coverage_tool_exception_is_recorded_not_raised(tmp_path: Path, monkeypatch) -> None:
    runner = _load_runner()
    skill_dir = tmp_path / "job-discovery"
    page_dir = skill_dir / "output" / "evidence" / "pages"
    page_dir.mkdir(parents=True)
    (page_dir / "page_01.txt").write_text("岗位列表", encoding="utf-8")
    (skill_dir / "output" / "candidates_merged.json").write_text("[]", encoding="utf-8")

    class BrokenTool:
        def invoke(self, args):
            raise RuntimeError("unexpected wrapper failure")

    monkeypatch.setattr(runner, "SKILL_DIR", skill_dir)
    monkeypatch.setattr(runner, "run_skill_script", BrokenTool())
    monkeypatch.setattr(runner, "_last_browse_metadata", {"terminal_evidence": "next_control_absent"})
    monkeypatch.setattr(runner, "_COVERAGE_GATE_RESULT_FILE", skill_dir / "output" / "evidence" / "coverage_gate_result.json")

    verdict = runner._coverage_for_run(candidates=[], content="", expected_count=None)

    assert verdict["coverage_verified"] is False
    assert verdict["reasons"] == ["coverage_gate_execution_error:RuntimeError"]


def test_skill_does_not_instruct_large_site_truncation() -> None:
    skill = (Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Process first 3 pages only" not in skill
    assert "Process every discovered page" in skill


def test_planner_requires_exact_per_page_count_when_browse_proves_page_size() -> None:
    runner = _load_runner()

    assert "exact per-page cardinality" in runner._SKILL_SYSTEM_PROMPT
    assert "written == size_val" in runner._SKILL_SYSTEM_PROMPT
    assert "Do not cap page tasks with a fixed total-tool budget" in runner._SKILL_SYSTEM_PROMPT
    assert "auto-follows public card detail links" in runner._SKILL_SYSTEM_PROMPT
    assert "parallel-fetch --wait 800" in runner._SKILL_SYSTEM_PROMPT


def test_jd_extractor_distinguishes_detail_evidence_from_listing_evidence() -> None:
    runner = _load_runner()

    assert "`=== DETAIL N ===`" in runner._JD_EXTRACTOR_PROMPT
    assert "`browsed_detail_page`" in runner._JD_EXTRACTOR_PROMPT
    assert "Never construct JD text from a title-only" in runner._JD_EXTRACTOR_PROMPT


def test_planner_treats_coverage_rejection_as_terminal_not_a_cost_loop() -> None:
    runner = _load_runner()

    assert "Call the gate exactly ONCE" in runner._SKILL_SYSTEM_PROMPT
    assert "It is a terminal decision, not a new\n  planning loop" in runner._SKILL_SYSTEM_PROMPT




def test_slug_filter_reuses_cache_unless_force_fresh_is_explicit() -> None:
    runner = _load_runner()

    selected, refresh = runner._select_eval_urls(
        runner.URLS,
        limit=len(runner.URLS),
        only="bytedance",
        force_fresh="",
    )

    assert [row[0] for row in selected] == ["bytedance"]
    assert refresh == set()


def test_force_fresh_must_be_a_selected_slug() -> None:
    runner = _load_runner()

    try:
        runner._select_eval_urls(
            runner.URLS,
            limit=len(runner.URLS),
            only="bytedance",
            force_fresh="xiaomi",
        )
    except ValueError as exc:
        assert "selected URL" in str(exc)
    else:
        raise AssertionError("force refresh outside the selected set must fail")


def test_partial_run_uses_a_separate_summary_path() -> None:
    runner = _load_runner()

    assert runner._summary_path([runner.URLS[-1]]) != runner._OUT_DIR / "_skill_ten_url_summary.json"
    assert runner._summary_path(runner.URLS) == runner._OUT_DIR / "_skill_ten_url_summary.json"


def test_browse_probe_requires_parallel_nonempty_page_files(tmp_path: Path) -> None:
    probe = _load_browse_probe()
    page = tmp_path / "page_01.txt"
    page.write_text("职位列表", encoding="utf-8")

    assert probe._validate_probe_result({
        "status": "ok",
        "used_path": "parallel",
        "page_count": 1,
        "page_files": [str(page)],
    }, required_path="parallel") == []

    assert "used_path must be 'parallel', got 'click_fallback_no_detect'" in (
        probe._validate_probe_result({
            "status": "ok",
            "used_path": "click_fallback_no_detect",
            "page_count": 1,
            "page_files": [str(page)],
        }, required_path="parallel")
    )
