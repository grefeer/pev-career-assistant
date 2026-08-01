"""Unit tests for the resume-tailoring ``generate.py`` skill script.

Loaded as an importable module (the same way ``test_company_research_browse``
loads ``browse.py``) so the pure helpers and ``main`` are exercised without a
subprocess. The LLM client is monkeypatched; the lazy ``langchain_openai``
import path is covered by a fake ``ChatOpenAI``.
"""

from __future__ import annotations

import importlib.util
import json
import os
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

_GEN_PATH = (
    Path(__file__).resolve().parents[2]
    / "skill"
    / "resume-tailoring"
    / "scripts"
    / "generate.py"
)


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("resume_tailoring_generate", _GEN_PATH)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


# ═══════════════════════════════════════════════════════════════════
# Credential / model resolution
# ═══════════════════════════════════════════════════════════════════

def test_resolve_base_url_defaults_to_deepseek(gen, monkeypatch):
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)
    assert gen.resolve_base_url() == "https://api.deepseek.com"


def test_resolve_base_url_honors_env(gen, monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://other.example/v1")
    assert gen.resolve_base_url() == "https://other.example/v1"


def test_resolve_model_precedence(gen, monkeypatch):
    assert gen.resolve_model(None) == "deepseek-v4-flash"
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-pro")
    assert gen.resolve_model(None) == "deepseek-v4-pro"
    assert gen.resolve_model("override-model") == "override-model"


def test_resolve_api_key_prefers_deepseek_env(gen, monkeypatch):
    monkeypatch.setenv("DEEPSEEK_API_KEY", "ds-key")
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
    monkeypatch.setattr(gen, "_windows_user_env", lambda *_a, **_k: None)
    assert gen.resolve_api_key() == "ds-key"


def test_resolve_api_key_falls_back_to_openai_env(gen, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "oai-key")
    monkeypatch.setattr(gen, "_windows_user_env", lambda *_a, **_k: None)
    assert gen.resolve_api_key() == "oai-key"


def test_resolve_api_key_uses_windows_user_scope(gen, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(gen, "_windows_user_env", lambda name: "win-ds" if name == "DEEPSEEK_API_KEY" else None)
    assert gen.resolve_api_key() == "win-ds"


def test_resolve_api_key_none_when_nothing_set(gen, monkeypatch):
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.setattr(gen, "_windows_user_env", lambda *_a, **_k: None)
    assert gen.resolve_api_key() is None


# ═══════════════════════════════════════════════════════════════════
# _windows_user_env (all three branches, platform-independent via fake winreg)
# ═══════════════════════════════════════════════════════════════════

class _FakeWinregOk:
    HKEY_CURRENT_USER = "HKCU"

    class _Key:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def OpenKey(self, root, subkey):
        return self._Key()

    def QueryValueEx(self, key, name):
        return ("win-value", 0)


class _FakeWinregMissing:
    HKEY_CURRENT_USER = "HKCU"

    def OpenKey(self, root, subkey):
        raise FileNotFoundError("missing key")

    def QueryValueEx(self, key, name):
        raise AssertionError("should not be reached")


def test_windows_user_env_returns_value_on_nt(gen, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    sys.modules["winreg"] = _FakeWinregOk()  # type: ignore[assignment]
    try:
        assert gen._windows_user_env("DEEPSEEK_API_KEY") == "win-value"
    finally:
        sys.modules.pop("winreg", None)


def test_windows_user_env_returns_none_on_missing_key(gen, monkeypatch):
    monkeypatch.setattr(os, "name", "nt")
    sys.modules["winreg"] = _FakeWinregMissing()  # type: ignore[assignment]
    try:
        assert gen._windows_user_env("NOPE") is None
    finally:
        sys.modules.pop("winreg", None)


def test_windows_user_env_returns_none_off_windows(gen, monkeypatch):
    monkeypatch.setattr(os, "name", "posix")
    assert gen._windows_user_env("DEEPSEEK_API_KEY") is None


# ═══════════════════════════════════════════════════════════════════
# LLM content + JSON extraction
# ═══════════════════════════════════════════════════════════════════

def test_extract_content_none(gen):
    assert gen.extract_content(None) == ""


def test_extract_content_plain_str(gen):
    assert gen.extract_content("hello") == "hello"


def test_extract_content_object_with_content_str(gen):
    assert gen.extract_content(SimpleNamespace(content="hi")) == "hi"


def test_extract_content_object_with_content_list(gen):
    resp = SimpleNamespace(content=[{"text": "a"}, {"text": "b"}])
    assert gen.extract_content(resp) == "ab"


def test_extract_content_object_with_content_non_str(gen):
    assert gen.extract_content(SimpleNamespace(content=42)) == "42"


def test_slice_between_finds_object(gen):
    assert gen._slice_between("prefix {\"a\":1} suffix", "{", "}") == "{\"a\":1}"


def test_slice_between_no_open(gen):
    assert gen._slice_between("no braces here", "{", "}") is None


def test_slice_between_close_before_open(gen):
    assert gen._slice_between("} before {", "{", "}") is None


def test_try_parse_json_empty(gen):
    assert gen.try_parse_json("   ") is None


def test_try_parse_json_valid(gen):
    assert gen.try_parse_json('{"a": 1}') == {"a": 1}


def test_try_parse_json_bracket_slice(gen):
    assert gen.try_parse_json('noise {"a": 2} trailing') == {"a": 2}


def test_try_parse_json_bracket_slice_unparseable(gen):
    # A slice is found between { and } but is not valid JSON -> None.
    assert gen.try_parse_json("{broken json}") is None


def test_try_parse_json_unparseable(gen):
    assert gen.try_parse_json("totally not json at all") is None


def test_extract_json_fenced(gen):
    text = '```json\n{"diffs": [{"op": "highlight"}]}\n```'
    assert gen.extract_json(text) == {"diffs": [{"op": "highlight"}]}


def test_extract_json_plain(gen):
    assert gen.extract_json('{"diffs": []}') == {"diffs": []}


def test_extract_json_none_when_unparseable(gen):
    assert gen.extract_json("no json here") is None


# ═══════════════════════════════════════════════════════════════════
# Prompt + diff coercion / parsing
# ═══════════════════════════════════════════════════════════════════

def test_build_messages_includes_valid_fact_refs(gen):
    msgs = gen.build_messages(
        job_snapshot={"title": "AI Engineer"},
        profile_facts={"projects": "x", "skills": "y"},
        preferences={"role_family": "AI"},
        match_analysis={"strengths": ["a"]},
    )
    assert len(msgs) == 2
    payload = json.loads(msgs[1].content)
    assert payload["valid_fact_refs"] == ["projects", "skills"]
    assert payload["job_snapshot"]["title"] == "AI Engineer"


def test_build_messages_handles_non_dict_facts(gen):
    msgs = gen.build_messages(job_snapshot={}, profile_facts="not-a-dict")
    assert json.loads(msgs[1].content)["valid_fact_refs"] == []


def test_coerce_diffs_bare_list(gen):
    assert gen.coerce_diffs([{"op": "highlight"}, "drop"]) == [{"op": "highlight"}]


def test_coerce_diffs_wrapper_object(gen):
    assert gen.coerce_diffs({"diffs": [{"op": "omit"}]}) == [{"op": "omit"}]


def test_coerce_diffs_no_diffs_key(gen):
    assert gen.coerce_diffs({"other": 1}) is None


def test_coerce_diffs_non_dict_non_list(gen):
    assert gen.coerce_diffs("nope") is None


def test_parse_diffs_ok(gen):
    diffs = gen.parse_diffs('{"diffs": [{"op": "highlight", "section": "s"}]}')
    assert diffs == [{"op": "highlight", "section": "s"}]


def test_parse_diffs_unparseable_raises(gen):
    with pytest.raises(gen.DraftParseError) as exc:
        gen.parse_diffs("no json")
    assert exc.value.code == "draft_generation_parse_error"


def test_parse_diffs_no_diffs_list_raises(gen):
    with pytest.raises(gen.DraftParseError) as exc:
        gen.parse_diffs('{"other": 1}')
    assert exc.value.code == "draft_generation_parse_error"


# ═══════════════════════════════════════════════════════════════════
# invoke_llm lazy-import path (fake ChatOpenAI; covers deepseek-v4 extra_body)
# ═══════════════════════════════════════════════════════════════════

class _FakeChat:
    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        _FakeChat.last_kwargs = kwargs

    def invoke(self, messages):
        return SimpleNamespace(content='{"diffs": [{"op": "highlight", "section": "s", "fact_ref": "f"}]}')


def test_invoke_llm_disables_thinking_for_deepseek_v4(gen, monkeypatch):
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChat)
    gen.invoke_llm([], model="deepseek-v4-flash", api_key="k", base_url="https://api.deepseek.com")
    assert _FakeChat.last_kwargs is not None
    assert _FakeChat.last_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert _FakeChat.last_kwargs["temperature"] == 0.2


def test_invoke_llm_omits_extra_body_for_non_deepseek(gen, monkeypatch):
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChat)
    gen.invoke_llm([], model="gpt-4o", api_key="k", base_url="https://api.other.example")
    assert _FakeChat.last_kwargs is not None
    assert "extra_body" not in _FakeChat.last_kwargs


def test_invoke_llm_omits_extra_body_for_non_v4_deepseek(gen, monkeypatch):
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChat)
    gen.invoke_llm([], model="deepseek-chat", api_key="k", base_url="https://api.deepseek.com")
    assert "extra_body" not in _FakeChat.last_kwargs


# ═══════════════════════════════════════════════════════════════════
# generate() orchestration (invoke_llm + resolve_api_key monkeypatched)
# ═══════════════════════════════════════════════════════════════════

_INPUT = {
    "job_snapshot": {"title": "AI Engineer", "requirements": ["python"]},
    "profile_facts": {"projects": "AI app", "skills": "python"},
    "preferences": {"role_family": "AI"},
    "match_analysis": {"strengths": ["python"], "gaps": []},
}


def test_generate_ok(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"diffs": [{"op": "highlight", "section": "projects", "fact_ref": "projects"}]}'
    ))
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "ok"
    assert result["diffs"][0]["op"] == "highlight"
    assert result["agent_version"]


def test_generate_missing_key(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: None)
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "failed"
    assert result["code"] == "missing_api_key"


def test_generate_invoke_exception(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")

    def boom(*a, **kw):
        raise RuntimeError("network down")

    monkeypatch.setattr(gen, "invoke_llm", boom)
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "failed"
    assert result["code"] == "draft_generation_interrupted"
    assert "network down" in result["last_error"]


def test_generate_parse_error(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(content="no json at all"))
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "failed"
    assert result["code"] == "draft_generation_parse_error"


# ═══════════════════════════════════════════════════════════════════
# main() end-to-end via the CLI
# ═══════════════════════════════════════════════════════════════════

def test_main_reads_input_file_and_writes_output(gen, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"diffs": [{"op": "omit", "section": "s", "fact_ref": "f"}]}'
    ))
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps(_INPUT), encoding="utf-8")
    out = tmp_path / "out" / "draft_diffs.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert written["diffs"][0]["op"] == "omit"
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "ok"
    assert summary["diff_count"] == 1


def test_main_reads_stdin(gen, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"diffs": [{"op": "highlight", "section": "s", "fact_ref": "f"}]}'
    ))
    out = tmp_path / "drafts.json"
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: json.dumps(_INPUT)))

    rc = gen.main(["--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "ok"


def test_main_bad_input_reports_failure(gen, tmp_path, capsys):
    inp = tmp_path / "input.json"
    inp.write_text("not json", encoding="utf-8")
    out = tmp_path / "drafts.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert written["code"] == "bad_input"
    assert json.loads(capsys.readouterr().out)["code"] == "bad_input"


def test_main_input_not_object_reports_failure(gen, tmp_path, capsys):
    inp = tmp_path / "input.json"
    inp.write_text("[1, 2, 3]", encoding="utf-8")
    out = tmp_path / "drafts.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"


def test_main_missing_key_path(gen, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: None)
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps(_INPUT), encoding="utf-8")
    out = tmp_path / "drafts.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "failed"
    assert written["code"] == "missing_api_key"
