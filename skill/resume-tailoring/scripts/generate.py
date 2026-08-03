#!/usr/bin/env python3
"""generate.py - LLM resume-draft generation for the resume-tailoring skill.

Turns a target job snapshot + confirmed profile facts + preferences + match
analysis into a list of resume diff operations via a DeepSeek (OpenAI-
compatible) LLM. The LLM call is the only step; this script is the
self-contained, agent-callable mirror of the backend
``backend.app.services.resume_tailoring.generator.LLMDraftGenerator`` (same
prompt, same tolerant JSON parse). It writes the full result to ``--out`` and
a one-line summary JSON to stdout so a runtime can read the outcome the same
way ``browse.py`` exposes ``browse_metadata``.

Security: this is read-only generation. It never auto-submits anything and
never touches the candidate's profile store. A missing key or an unparseable
LLM response surfaces as ``status=failed`` with a stable ``code`` (exit 0) so
the caller gets a clean message instead of a crashed tool.

Usage:
  python scripts/generate.py --input output/input.json --out output/draft_diffs.json
  cat input.json | python scripts/generate.py --out output/draft_diffs.json

Input JSON shape:
  {"job_snapshot": {...}, "profile_facts": {...},
   "preferences": {...}?, "match_analysis": {...}?}

All functions are importable; the LLM client is imported lazily so the module
loads cleanly without ``langchain_openai`` installed (the pure parse helpers
and ``main`` are exercised by unit tests with the LLM monkeypatched).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

# ═══════════════════════════════════════════════════════════════════
# Credential + model resolution (mirrors src/utils.py + llm_factory.py)
# ═══════════════════════════════════════════════════════════════════

DEFAULT_BASE_URL = "https://api.deepseek.com"
DEFAULT_MODEL = "deepseek-v4-flash"


def _windows_user_env(name: str) -> str | None:
    """Read a var from the Windows CURRENT_USER Environment (User scope).

    Mirrors ``src.utils._get_windows_user_env`` so a key configured only in User
    scope (not exported into the process env) still resolves, the same way the
    backend's in-process LLM factory finds it.
    """
    if os.name != "nt":
        return None
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            value, _ = winreg.QueryValueEx(key, name)
    except OSError:
        return None
    return value if isinstance(value, str) and value else None


def resolve_api_key() -> str | None:
    """DeepSeek key first, then OpenAI; falls back to Windows User scope."""
    return (
        os.getenv("DEEPSEEK_API_KEY")
        or os.getenv("OPENAI_API_KEY")
        or _windows_user_env("DEEPSEEK_API_KEY")
        or _windows_user_env("OPENAI_API_KEY")
    )


def resolve_base_url() -> str:
    return os.getenv("OPENAI_BASE_URL", DEFAULT_BASE_URL)


def resolve_model(override: str | None) -> str:
    return override or os.getenv("OPENAI_MODEL") or DEFAULT_MODEL


def agent_version() -> str:
    return os.getenv("RESUME_TAILORING_AGENT_VERSION", "1.0.0")


# ═══════════════════════════════════════════════════════════════════
# Tolerant LLM JSON extraction (shared from skill/_common/llm_json.py)
# ═══════════════════════════════════════════════════════════════════
# Resolve the shared lib from this file's location so the import works whether
# the script is run as ``python skill/resume-tailoring/scripts/generate.py`` or
# ``python scripts/generate.py`` with cwd=skill/resume-tailoring.
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "_common"))
from llm_json import extract_content, extract_json  # noqa: E402


# ═══════════════════════════════════════════════════════════════════
# Prompt + diff parsing (mirrors resume_tailoring/generator.py)
# ═══════════════════════════════════════════════════════════════════

_SYSTEM_PROMPT = (
    "You are a resume-tailoring assistant. Given a target job snapshot, the "
    "candidate's confirmed profile facts, their stated preferences, and a match "
    "analysis (strengths/gaps), produce a concise list of resume diff operations "
    "that tailor the resume to the job.\n\n"
    "Each diff object MUST have:\n"
    '- "op": one of reorder, rephrase, summarize, omit, highlight\n'
    '- "section": a non-empty resume section name '
    '(e.g. "work_experience", "projects", "skills", "summary")\n'
    '- "fact_ref": a key that EXISTS in profile_facts '
    "(use one of the provided valid_fact_refs verbatim)\n"
    '- "before": the current text (may be empty)\n'
    '- "after": the proposed tailored text (may be empty for "omit")\n'
    '- "evidence_ids": an optional list (may be empty or omitted)\n\n'
    "Respond with ONLY a JSON object of the form:\n"
    '{"diffs": [ { "op": ..., "section": ..., "fact_ref": ..., '
    '"before": ..., "after": ... }, ... ]}\n\n'
    "Do not include prose, markdown fences, or commentary."
)


def build_messages(
    *,
    job_snapshot: dict[str, Any],
    profile_facts: dict[str, Any],
    preferences: dict[str, Any] | None = None,
    match_analysis: dict[str, Any] | None = None,
) -> list[Any]:
    """Construct the System+Human message pair for the LLM."""
    from langchain_core.messages import HumanMessage, SystemMessage

    valid_fact_refs = (
        list(profile_facts.keys()) if isinstance(profile_facts, dict) else []
    )
    human_payload = {
        "job_snapshot": job_snapshot,
        "profile_facts": profile_facts,
        "valid_fact_refs": valid_fact_refs,
        "preferences": preferences or {},
        "match_analysis": match_analysis or {},
    }
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(human_payload, ensure_ascii=False)),
    ]


def coerce_diffs(payload: Any) -> list[dict[str, Any]] | None:
    """Normalize a parsed JSON payload into a list of diff dicts.

    Accepts a bare list or ``{"diffs": [...]}``. Returns ``None`` when no diffs
    list is present. Non-dict entries are dropped.
    """
    if isinstance(payload, list):
        return [d for d in payload if isinstance(d, dict)]
    if isinstance(payload, dict):
        diffs = payload.get("diffs")
        if isinstance(diffs, list):
            return [d for d in diffs if isinstance(d, dict)]
    return None


def parse_diffs(content: str) -> list[dict[str, Any]]:
    """Parse LLM response text into a diffs list, raising ``DraftParseError``."""
    payload = extract_json(content)
    if payload is None:
        raise DraftParseError(
            "draft_generation_parse_error",
            "LLM response did not contain a parseable JSON object.",
        )
    diffs = coerce_diffs(payload)
    if diffs is None:
        raise DraftParseError(
            "draft_generation_parse_error",
            "LLM response JSON did not contain a 'diffs' list.",
        )
    return diffs


class DraftParseError(RuntimeError):
    """Raised when the LLM output cannot be parsed into a diffs list."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def invoke_llm(
    messages: list[Any], *, model: str, api_key: str, base_url: str
) -> Any:
    """Build a bounded ChatOpenAI and invoke it. Lazy-imported for testability."""
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.2,
        "request_timeout": 120,
        "max_retries": 2,
        "api_key": api_key,
        "base_url": base_url,
    }
    # deepseek-v4 models expose a "thinking" mode whose interleaved reasoning
    # tags break JSON parsing; disable it for reliable structured output.
    if "deepseek" in base_url.lower() and model.startswith("deepseek-v4"):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    return ChatOpenAI(**kwargs).invoke(messages)


# ═══════════════════════════════════════════════════════════════════
# Orchestration
# ═══════════════════════════════════════════════════════════════════

def _write_json(path: str, payload: dict[str, Any]) -> None:
    import pathlib

    p = pathlib.Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def generate(input_payload: dict[str, Any], *, model_override: str | None = None) -> dict[str, Any]:
    """Run one generation. Returns the result dict written to ``--out``.

    Pure orchestration over ``build_messages`` / ``invoke_llm`` / ``parse_diffs``
    so a unit test can cover every branch by monkeypatching ``invoke_llm``.
    """
    job_snapshot = input_payload.get("job_snapshot") or {}
    profile_facts = input_payload.get("profile_facts") or {}
    preferences = input_payload.get("preferences")
    match_analysis = input_payload.get("match_analysis")
    version = agent_version()

    api_key = resolve_api_key()
    if not api_key:
        return {
            "status": "failed",
            "code": "missing_api_key",
            "last_error": "DEEPSEEK_API_KEY/OPENAI_API_KEY is not set",
            "agent_version": version,
        }

    model = resolve_model(model_override)
    base_url = resolve_base_url()
    messages = build_messages(
        job_snapshot=job_snapshot,
        profile_facts=profile_facts,
        preferences=preferences,
        match_analysis=match_analysis,
    )
    try:
        response = invoke_llm(messages, model=model, api_key=api_key, base_url=base_url)
    except Exception as exc:  # network/auth/timeout - hard interruption
        return {
            "status": "failed",
            "code": "draft_generation_interrupted",
            "last_error": str(exc)[:500],
            "agent_version": version,
        }

    content = extract_content(response)
    try:
        diffs = parse_diffs(content)
    except DraftParseError as exc:
        return {
            "status": "failed",
            "code": exc.code,
            "last_error": str(exc),
            "agent_version": version,
        }

    return {"status": "ok", "diffs": diffs, "agent_version": version}


def _load_input(args: argparse.Namespace) -> dict[str, Any]:
    if args.input:
        import pathlib

        text = pathlib.Path(args.input).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    payload = json.loads(text)
    if not isinstance(payload, dict):
        raise ValueError("input must be a JSON object")
    return payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate resume diff operations for a target job via LLM."
    )
    parser.add_argument("--input", help="Path to input JSON (default: stdin)")
    parser.add_argument("--out", default="output/draft_diffs.json", help="Output path")
    parser.add_argument("--model", help="LLM model override (default: env OPENAI_MODEL)")
    args = parser.parse_args(argv)

    try:
        input_payload = _load_input(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        result = {"status": "failed", "code": "bad_input", "last_error": str(exc)[:500]}
        _write_json(args.out, result)
        print(json.dumps({**result, "out": args.out}, ensure_ascii=False))
        return 0

    result = generate(input_payload, model_override=args.model)
    _write_json(args.out, result)
    summary = {
        "status": result["status"],
        "code": result.get("code"),
        "diff_count": len(result["diffs"]) if result["status"] == "ok" else 0,
        "agent_version": result.get("agent_version"),
        "out": args.out,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry, exercised by the smoke test
    sys.exit(main())
