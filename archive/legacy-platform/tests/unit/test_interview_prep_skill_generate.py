"""Unit tests for the interview-prep ``generate.py`` skill script.

Loaded as an importable module (mirrors ``test_resume_tailoring_skill_generate``).
The LLM client is monkeypatched; the lazy ``langchain_openai`` import path is
covered by a fake ``ChatOpenAI``.
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
    / "interview-prep"
    / "scripts"
    / "generate.py"
)


@pytest.fixture(scope="module")
def gen():
    spec = importlib.util.spec_from_file_location("interview_prep_generate", _GEN_PATH)
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


def test_resolve_model_precedence(gen, monkeypatch):
    assert gen.resolve_model(None) == "deepseek-v4-flash"
    monkeypatch.setenv("OPENAI_MODEL", "deepseek-v4-pro")
    assert gen.resolve_model(None) == "deepseek-v4-pro"
    assert gen.resolve_model("override") == "override"


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


def test_slice_between(gen):
    assert gen._slice_between("prefix {\"a\":1} suffix", "{", "}") == "{\"a\":1}"
    assert gen._slice_between("no braces", "{", "}") is None
    assert gen._slice_between("} before {", "{", "}") is None


def test_try_parse_json(gen):
    assert gen.try_parse_json("   ") is None
    assert gen.try_parse_json('{"a": 1}') == {"a": 1}
    assert gen.try_parse_json('noise {"a": 2} trailing') == {"a": 2}
    assert gen.try_parse_json("{broken json}") is None
    assert gen.try_parse_json("totally not json") is None


def test_extract_json_fenced_plain_unparseable(gen):
    assert gen.extract_json('```json\n{"technical_questions": ["q"]}\n```') == {"technical_questions": ["q"]}
    assert gen.extract_json('{"behavioral_questions": []}') == {"behavioral_questions": []}
    assert gen.extract_json("no json here") is None


# ═══════════════════════════════════════════════════════════════════
# Prompt + content coercion / parsing
# ═══════════════════════════════════════════════════════════════════

def test_build_messages_serializes_payload(gen):
    msgs = gen.build_messages(
        job_snapshot={"title": "AI Engineer"},
        profile_facts={"exp": "x"},
        preferences={"role_family": "AI"},
        match_analysis={"strengths": ["a"]},
    )
    assert len(msgs) == 2
    payload = json.loads(msgs[1].content)
    assert payload["job_snapshot"]["title"] == "AI Engineer"
    assert payload["profile_facts"] == {"exp": "x"}


def test_build_messages_defaults_empty_optionals(gen):
    msgs = gen.build_messages(job_snapshot={"title": "x"})
    payload = json.loads(msgs[1].content)
    assert payload["profile_facts"] == {}
    assert payload["preferences"] == {}
    assert payload["match_analysis"] == {}


def test_coerce_content_normalizes_all_five_keys(gen):
    payload = {
        "technical_questions": ["q1", "q2"],
        "behavioral_questions": "not a list",  # -> []
        "talking_points": ["keep", 123, "drop-num"],  # 123 dropped
        "topics_to_review": ["t1"],
        "questions_to_ask": ["a1"],
        "unknown_key": ["ignored"],
    }
    result = gen.coerce_content(payload)
    assert set(result.keys()) == set(gen.CONTENT_KEYS)
    assert result["technical_questions"] == ["q1", "q2"]
    assert result["behavioral_questions"] == []
    assert result["talking_points"] == ["keep", "drop-num"]
    assert result["questions_to_ask"] == ["a1"]


def test_coerce_content_non_dict_returns_none(gen):
    assert gen.coerce_content(["not", "a", "dict"]) is None
    assert gen.coerce_content("nope") is None


def test_parse_content_ok(gen):
    content = '{"technical_questions": ["q1"], "questions_to_ask": ["a1"]}'
    parsed = gen.parse_content(content)
    assert parsed["technical_questions"] == ["q1"]
    assert parsed["behavioral_questions"] == []  # defaulted


def test_parse_content_unparseable_raises(gen):
    with pytest.raises(gen.PrepParseError) as exc:
        gen.parse_content("no json")
    assert exc.value.code == "interview_prep_parse_error"


def test_parse_content_non_object_raises(gen):
    with pytest.raises(gen.PrepParseError) as exc:
        gen.parse_content('["a", "b"]')
    assert exc.value.code == "interview_prep_parse_error"


def test_parse_content_all_empty_raises(gen):
    with pytest.raises(gen.PrepParseError) as exc:
        gen.parse_content('{"technical_questions": [], "behavioral_questions": []}')
    assert exc.value.code == "interview_prep_empty_content"


# ═══════════════════════════════════════════════════════════════════
# invoke_llm lazy-import path (fake ChatOpenAI; covers deepseek-v4 extra_body)
# ═══════════════════════════════════════════════════════════════════

class _FakeChat:
    last_kwargs: dict | None = None

    def __init__(self, **kwargs):
        _FakeChat.last_kwargs = kwargs

    def invoke(self, messages):
        return SimpleNamespace(
            content='{"technical_questions": ["q1"], "questions_to_ask": ["a1"]}'
        )


def test_invoke_llm_disables_thinking_for_deepseek_v4(gen, monkeypatch):
    monkeypatch.setattr("langchain_openai.ChatOpenAI", _FakeChat)
    gen.invoke_llm([], model="deepseek-v4-flash", api_key="k", base_url="https://api.deepseek.com")
    assert _FakeChat.last_kwargs is not None
    assert _FakeChat.last_kwargs["extra_body"] == {"thinking": {"type": "disabled"}}
    assert _FakeChat.last_kwargs["temperature"] == 0.3


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
# generate() orchestration
# ═══════════════════════════════════════════════════════════════════

_INPUT = {
    "job_snapshot": {"title": "AI Engineer", "requirements": ["python"]},
    "profile_facts": {"projects": "AI app"},
    "preferences": {"role_family": "AI"},
    "match_analysis": {"strengths": ["python"], "gaps": []},
}


def test_generate_ok(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"technical_questions": ["q1"], "questions_to_ask": ["a1"]}'
    ))
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "ok"
    assert result["content"]["technical_questions"] == ["q1"]
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
    assert result["code"] == "interview_prep_interrupted"
    assert "network down" in result["last_error"]


def test_generate_parse_error(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(content="no json"))
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "failed"
    assert result["code"] == "interview_prep_parse_error"


def test_generate_empty_content(gen, monkeypatch):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"technical_questions": [], "behavioral_questions": []}'
    ))
    result = gen.generate(dict(_INPUT))
    assert result["status"] == "failed"
    assert result["code"] == "interview_prep_empty_content"


# ═══════════════════════════════════════════════════════════════════
# main() end-to-end via the CLI
# ═══════════════════════════════════════════════════════════════════

def test_main_reads_input_file_and_writes_output(gen, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"technical_questions": ["q1", "q2"], "questions_to_ask": ["a1"]}'
    ))
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps(_INPUT), encoding="utf-8")
    out = tmp_path / "out" / "prep_kit.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    written = json.loads(out.read_text(encoding="utf-8"))
    assert written["status"] == "ok"
    assert written["content"]["technical_questions"] == ["q1", "q2"]
    summary = json.loads(capsys.readouterr().out)
    assert summary["status"] == "ok"
    assert summary["section_count"] == 3


def test_main_reads_stdin(gen, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: "k")
    monkeypatch.setattr(gen, "invoke_llm", lambda messages, **kw: SimpleNamespace(
        content='{"technical_questions": ["q1"]}'
    ))
    out = tmp_path / "prep.json"
    monkeypatch.setattr("sys.stdin", SimpleNamespace(read=lambda: json.dumps(_INPUT)))

    rc = gen.main(["--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == "ok"


def test_main_bad_input_reports_failure(gen, tmp_path, capsys):
    inp = tmp_path / "input.json"
    inp.write_text("not json", encoding="utf-8")
    out = tmp_path / "prep.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"


def test_main_input_not_object_reports_failure(gen, tmp_path, capsys):
    inp = tmp_path / "input.json"
    inp.write_text("[1, 2, 3]", encoding="utf-8")
    out = tmp_path / "prep.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "bad_input"


def test_main_missing_key_path(gen, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(gen, "resolve_api_key", lambda: None)
    inp = tmp_path / "input.json"
    inp.write_text(json.dumps(_INPUT), encoding="utf-8")
    out = tmp_path / "prep.json"

    rc = gen.main(["--input", str(inp), "--out", str(out)])

    assert rc == 0
    assert json.loads(out.read_text(encoding="utf-8"))["code"] == "missing_api_key"
