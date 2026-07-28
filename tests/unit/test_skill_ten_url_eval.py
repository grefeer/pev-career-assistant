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

    verdict = runner._coverage_for_run(candidates=[], content="", expected_count=None)

    assert verdict["coverage_verified"] is False
    assert verdict["reasons"] == ["coverage_gate_execution_error:RuntimeError"]


def test_skill_does_not_instruct_large_site_truncation() -> None:
    skill = (Path(__file__).resolve().parents[2] / "skill" / "job-discovery" / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "Process first 3 pages only" not in skill
    assert "Process every discovered page" in skill




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
