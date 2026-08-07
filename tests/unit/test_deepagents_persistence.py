from __future__ import annotations

import json
import tempfile

import pytest

from backend.app.services.deepagents_runtime.tools.skill_graphs import persistence
from backend.app.services.deepagents_runtime.tools.skill_graphs.persistence import (
    state_check,
    state_mark,
    load_prior_candidates,
    append_errors_jsonl,
    normalize_candidates,
)


class FakeStateRunner:
    def __init__(self, exit_codes: dict[str, int]) -> None:
        self.exit_codes = exit_codes
        self.calls: list[tuple[str, str, str]] = []

    def __call__(self, script, *, cli_args="", stdin=""):
        self.calls.append((script, cli_args, stdin))
        if script == "state" and cli_args.startswith("check "):
            return json.dumps({"exit_code": self.exit_codes.get(cli_args, 0)})
        if script == "state":
            return json.dumps({"marked": True})
        return json.dumps({"ok": True})


def test_state_check_exit_zero_means_skip() -> None:
    runner = FakeStateRunner({"check https://a/1 2026-01-01": 0})
    assert state_check("https://a/1", "2026-01-01", runner=runner, state_dir="x") is True
    runner2 = FakeStateRunner({"check https://a/1 2026-01-01": 1})
    assert state_check("https://a/1", "2026-01-01", runner=runner2, state_dir="x") is False


def test_state_mark_requires_file_and_sheet_id() -> None:
    runner = FakeStateRunner({})
    with pytest.raises(ValueError):
        state_mark("https://a/1", ["h1_u1"], runner=runner, state_dir="x", file_id="", sheet_id="f")
    with pytest.raises(ValueError):
        state_mark("https://a/1", ["h1_u1"], runner=runner, state_dir="x", file_id="f", sheet_id="")
    state_mark("https://a/1", ["h1_u1"], runner=runner, state_dir="x", file_id="f", sheet_id="s")
    assert runner.calls[-1][0] == "state"
    assert "--file-id f" in runner.calls[-1][1] and "--sheet-id s" in runner.calls[-1][1]


def test_load_prior_candidates_missing_file() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        assert load_prior_candidates(state_dir=tmp) == []


def test_load_prior_candidates_reads_merged_final(tmp_path) -> None:
    out = tmp_path / "output" / "candidates"
    out.mkdir(parents=True)
    (out / "merged_final.json").write_text(json.dumps({"candidates": [{"title": "A"}]}), encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == [{"title": "A"}]


def test_append_errors_jsonl_appends_lines(tmp_path) -> None:
    append_errors_jsonl({"url": "u1", "cause": "c1"}, runner=FakeStateRunner({}), state_dir=str(tmp_path))
    append_errors_jsonl({"url": "u2", "cause": "c2"}, runner=FakeStateRunner({}), state_dir=str(tmp_path))
    lines = (tmp_path / "output" / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_normalize_candidates_returns_comparison_keys() -> None:
    def runner(script, *, cli_args="", stdin=""):
        assert script == "normalize"
        return json.dumps({"normalized_title": "java-工程师", "key": "java工程师"})
    keys = normalize_candidates([{"title": " Java工程师 "}], runner=runner)
    assert keys["key"] == "java工程师"


# ---------------------------------------------------------------------------
# state_check variants (exit-code seam + real script's action contract)
# ---------------------------------------------------------------------------


def test_state_check_real_script_action_contract() -> None:
    # the real state.py check prints {"action": "skip"|"extract"}; the
    # exit_code JSON is the test seam's contract
    def runner_skip(script, *, cli_args="", stdin=""):
        assert script == "state"
        assert cli_args == "check https://a/1 2026-01-01"
        return json.dumps({"action": "skip"})

    assert state_check("https://a/1", "2026-01-01", runner=runner_skip, state_dir="x") is True

    def runner_extract(script, *, cli_args="", stdin=""):
        return json.dumps({"action": "extract"})

    assert state_check("https://a/1", "2026-01-01", runner=runner_extract, state_dir="x") is False


def test_state_check_tolerates_unparsable_or_non_object_output() -> None:
    # a broken state must never silently skip a URL: unparsable and
    # non-object output both fold to False (extract)
    for raw in ("not json", "[]", "42"):
        def runner(script, *, cli_args="", stdin="", _raw=raw):
            return _raw

        assert state_check("https://a/1", "2026-01-01", runner=runner, state_dir="x") is False


def test_state_mark_calls_once_per_entry_id() -> None:
    runner = FakeStateRunner({})
    state_mark(
        "https://a/1",
        ["h1_u1", "h2_u2"],
        runner=runner,
        state_dir="x",
        file_id="f",
        sheet_id="s",
    )
    assert len(runner.calls) == 2
    for script, cli_args, _stdin in runner.calls:
        assert script == "state"
        assert cli_args.startswith("mark https://a/1 ")
        assert "--file-id f" in cli_args and "--sheet-id s" in cli_args
    assert "h2_u2" in runner.calls[-1][1]


def test_state_default_runner_resolves_run_skill_script(monkeypatch) -> None:
    # runner=None resolves to the module-level run_skill_script: the
    # monkeypatched fake proves the default channel is used without ever
    # invoking a real skill script
    captured: dict = {}
    monkeypatch.setattr(
        persistence,
        "run_skill_script",
        lambda script, cli_args="", stdin="": (
            captured.update(script=script, cli_args=cli_args)
            or json.dumps({"exit_code": 0})
        ),
    )
    assert state_check("https://a/1", "2026-01-01", state_dir="x") is True
    assert captured == {"script": "state", "cli_args": "check https://a/1 2026-01-01"}
    state_mark("https://a/1", ["h1_u1"], state_dir="x", file_id="f", sheet_id="s")
    assert captured["cli_args"] == "mark https://a/1 h1_u1 --file-id f --sheet-id s"


# ---------------------------------------------------------------------------
# load_prior_candidates shape variants
# ---------------------------------------------------------------------------


def test_load_prior_candidates_shapes(tmp_path) -> None:
    out = tmp_path / "output" / "candidates"
    out.mkdir(parents=True)
    merged = out / "merged_final.json"
    # list form (the real write_candidates/deduplicate output shape)
    merged.write_text(json.dumps([{"title": "A"}]), encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == [{"title": "A"}]
    # unparsable JSON -> []
    merged.write_text("not json", encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == []
    # scalar JSON -> []
    merged.write_text(json.dumps("x"), encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == []
    # dict without a candidates list -> []
    merged.write_text(json.dumps({"candidates": "nope"}), encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == []
    merged.write_text(json.dumps({"other": 1}), encoding="utf-8")
    assert load_prior_candidates(state_dir=str(tmp_path)) == []


# ---------------------------------------------------------------------------
# append_errors_jsonl (moved from test_deepagents_wechat_slice.py, Task 10)
# ---------------------------------------------------------------------------


def test_needs_deep_crawl_appends_errors_jsonl(tmp_path) -> None:
    def fake_runner(script, *, cli_args="", stdin=""):
        return json.dumps({"ok": True})  # state/ocr scripts are faked in unit tests

    append_errors_jsonl(
        {"url": "https://mp.weixin.qq.com/s/abc", "cause": "needs_deep_crawl"},
        runner=fake_runner,
        state_dir=str(tmp_path),
    )
    lines = (
        (tmp_path / "output" / "errors.jsonl")
        .read_text(encoding="utf-8")
        .strip()
        .splitlines()
    )
    assert len(lines) == 1
    assert json.loads(lines[0])["url"] == "https://mp.weixin.qq.com/s/abc"


def test_append_errors_jsonl_idempotent_by_url_and_cause(tmp_path) -> None:
    entry = {"url": "https://a.example.com/1", "cause": "needs_deep_crawl", "status": "needs_deep_crawl"}
    append_errors_jsonl(entry, state_dir=str(tmp_path))
    append_errors_jsonl(entry, state_dir=str(tmp_path))  # same url + cause -> skip
    append_errors_jsonl({**entry, "cause": "another_cause"}, state_dir=str(tmp_path))
    lines = (tmp_path / "output" / "errors.jsonl").read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 2


def test_append_errors_jsonl_creates_dirs_and_tolerates_corrupt_lines(tmp_path) -> None:
    state_dir = tmp_path / "nested" / "dir"
    (state_dir / "output").mkdir(parents=True)
    (state_dir / "output" / "errors.jsonl").write_text("not json\n", encoding="utf-8")
    append_errors_jsonl({"url": "https://a.example.com/1", "cause": "needs_deep_crawl"}, state_dir=str(state_dir))
    lines = (state_dir / "output" / "errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[1])["url"] == "https://a.example.com/1"


def test_append_errors_jsonl_tolerates_non_dict_lines(tmp_path) -> None:
    (tmp_path / "output").mkdir(parents=True)
    (tmp_path / "output" / "errors.jsonl").write_text("not json\n42\n", encoding="utf-8")
    append_errors_jsonl(
        {"url": "https://a.example.com/1", "cause": "needs_deep_crawl"},
        state_dir=str(tmp_path),
    )
    lines = (tmp_path / "output" / "errors.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 3
    assert json.loads(lines[2])["url"] == "https://a.example.com/1"


# ---------------------------------------------------------------------------
# normalize_candidates variants
# ---------------------------------------------------------------------------


def test_normalize_candidates_skips_empty_titles() -> None:
    calls: list[str] = []

    def runner(script, *, cli_args="", stdin=""):
        assert script == "normalize"
        calls.append(cli_args)
        return json.dumps({"normalized_title": "x", "key": "y"})

    keys = normalize_candidates([{"title": ""}, {"title": " "}, {"title": "职位"}], runner=runner)
    assert len(calls) == 2  # falsy titles never invoke the script
    assert keys == {"normalized_title": "x", "key": "y"}


def test_normalize_candidates_tolerates_unparsable_and_non_object() -> None:
    def runner(script, *, cli_args="", stdin=""):
        return "not json" if cli_args.startswith("--title A") else "[]"

    keys = normalize_candidates([{"title": "A"}, {"title": "B"}], runner=runner)
    assert keys == {}


def test_normalize_candidates_maps_real_script_input_normalized() -> None:
    # the real normalize.py --title contract: {"input": <title>, "normalized": <key>}
    def runner(script, *, cli_args="", stdin=""):
        assert script == "normalize"
        return json.dumps({"input": " Java工程师 ", "normalized": "java工程师"})

    keys = normalize_candidates([{"title": " Java工程师 "}], runner=runner)
    assert keys[" Java工程师 "] == "java工程师"


def test_normalize_candidates_skips_non_string_or_empty_values() -> None:
    def runner(script, *, cli_args="", stdin=""):
        return json.dumps({"normalized_title": None, "key": ""})

    keys = normalize_candidates([{"title": "职位"}], runner=runner)
    assert keys == {}


def test_normalize_candidates_default_runner_branch(monkeypatch) -> None:
    captured: dict = {}
    monkeypatch.setattr(
        persistence,
        "run_skill_script",
        lambda script, cli_args="", stdin="": (
            captured.update(script=script, cli_args=cli_args)
            or json.dumps({"normalized_title": "java-工程师", "key": "java工程师"})
        ),
    )
    keys = normalize_candidates([{"title": " Java工程师 "}])
    assert captured == {"script": "normalize", "cli_args": "--title  Java工程师  --json"}
    assert keys["key"] == "java工程师"
