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


def test_utf8_reexec_command_enables_utf8_before_deepagents_spawns_children() -> None:
    runner = _load_runner()

    command = runner._utf8_reexec_command(["tests/manual/run_skill_ten_url_eval.py"])

    assert command[1:3] == ["-X", "utf8"]
    assert command[-1] == "tests/manual/run_skill_ten_url_eval.py"




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
