#!/usr/bin/env python3
"""generate.py - LLM interview-prep generation for the interview-prep skill.

Turns a target job snapshot + confirmed profile facts + preferences + match
analysis into a structured interview-prep kit (five sections) via a DeepSeek
(OpenAI-compatible) LLM. The LLM call is the only step; this script is the
self-contained, agent-callable mirror of the backend
``backend.app.services.interview_prep.generator.LLMInterviewPrepGenerator``
(same prompt, same tolerant JSON parse, same five normalized sections). It
writes the full result to ``--out`` and a one-line summary JSON to stdout so a
runtime can read the outcome the same way ``browse.py`` exposes its metadata.

Security: this is read-only study material. It never auto-submits anything and
never touches the candidate's interview-prep store. A missing key or an
unparseable LLM response surfaces as ``status=failed`` with a stable ``code``
(exit 0) so the caller gets a clean message instead of a crashed tool.

Usage:
  python scripts/generate.py --input output/input.json --out output/prep_kit.json
  cat input.json | python scripts/generate.py --out output/prep_kit.json

Input JSON shape:
  {"job_snapshot": {...}, "profile_facts": {...}?, "preferences": {...}?,
   "match_analysis": {...}?}
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
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
    return os.getenv("INTERVIEW_PREP_AGENT_VERSION", "1.0.0")


# ═══════════════════════════════════════════════════════════════════
# Tolerant LLM JSON extraction (mirrors backend.app.services.common.llm_json)
# ═══════════════════════════════════════════════════════════════════

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def extract_content(response: Any) -> str:
    """Coerce an LLM response (``.content`` str / list / plain str) into text."""
    if response is None:
        return ""
    content = getattr(response, "content", response)
    if isinstance(content, list):
        return "".join(
            block.get("text", "") if isinstance(block, dict) else str(block)
            for block in content
        )
    return content if isinstance(content, str) else str(content)


def _slice_between(text: str, open_ch: str, close_ch: str) -> str | None:
    start = text.find(open_ch)
    if start == -1:
        return None
    end = text.rfind(close_ch)
    if end <= start:
        return None
    return text[start : end + 1]


def try_parse_json(text: str) -> Any:
    stripped = text.strip()
    if not stripped:
        return None
    try:
        return json.loads(stripped)
    except (ValueError, TypeError):
        pass
    for open_ch, close_ch in (("{", "}"), ("[", "]")):
        obj = _slice_between(stripped, open_ch, close_ch)
        if obj is not None:
            try:
                return json.loads(obj)
            except (ValueError, TypeError):
                pass
    return None


def extract_json(content: str) -> Any:
    """Best-effort first-JSON-value extraction (fenced, whole, or bracket slice)."""
    match = _FENCE_RE.search(content)
    candidates = [match.group(1)] if match else []
    candidates.append(content)
    for candidate in candidates:
        result = try_parse_json(candidate)
        if result is not None:
            return result
    return None


# ═══════════════════════════════════════════════════════════════════
# Prompt + content parsing (mirrors interview_prep/generator.py)
# ═══════════════════════════════════════════════════════════════════

#: The five normalized sections of an interview-prep kit. The generator
#: guarantees each appears as a list in the output (defaulting to ``[]``).
CONTENT_KEYS: tuple[str, ...] = (
    "technical_questions",
    "behavioral_questions",
    "talking_points",
    "topics_to_review",
    "questions_to_ask",
)

_SYSTEM_PROMPT = (
    "You are an interview-prep coach. Given a target job snapshot, the "
    "candidate's confirmed profile facts, their stated preferences, and a match "
    "analysis (strengths/gaps), produce a structured interview-prep kit.\n\n"
    "Respond with ONLY a JSON object with these keys, each a list of concise "
    "strings:\n"
    '- "technical_questions": likely technical questions for this role\n'
    '- "behavioral_questions": likely behavioral/situational questions\n'
    '- "talking_points": strengths and stories to emphasize, grounded in the '
    "candidate's profile where possible\n"
    '- "topics_to_review": concepts/skills to brush up on before the interview\n'
    '- "questions_to_ask": thoughtful questions the candidate can ask the '
    "interviewer\n\n"
    "Tailor every section to the target job. Do not include prose, markdown "
    "fences, or commentary - only the JSON object."
)


class PrepParseError(RuntimeError):
    """Raised when the LLM output cannot be parsed into prep content."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


def build_messages(
    *,
    job_snapshot: dict[str, Any],
    profile_facts: dict[str, Any] | None = None,
    preferences: dict[str, Any] | None = None,
    match_analysis: dict[str, Any] | None = None,
) -> list[Any]:
    """Construct the System+Human message pair for the LLM."""
    from langchain_core.messages import HumanMessage, SystemMessage

    human_payload = {
        "job_snapshot": job_snapshot,
        "profile_facts": profile_facts or {},
        "preferences": preferences or {},
        "match_analysis": match_analysis or {},
    }
    return [
        SystemMessage(content=_SYSTEM_PROMPT),
        HumanMessage(content=json.dumps(human_payload, ensure_ascii=False)),
    ]


def coerce_content(payload: Any) -> dict[str, list[str]] | None:
    """Normalize a parsed JSON payload into the five content sections.

    Accepts a JSON object and returns a dict with every :data:`CONTENT_KEYS`
    entry as a list (non-list values dropped, non-string items dropped, unknown
    keys ignored). Returns ``None`` when ``payload`` is not a dict.
    """
    if not isinstance(payload, dict):
        return None
    result: dict[str, list[str]] = {}
    for key in CONTENT_KEYS:
        value = payload.get(key)
        result[key] = (
            [item for item in value if isinstance(item, str)]
            if isinstance(value, list)
            else []
        )
    return result


def parse_content(content: str) -> dict[str, list[str]]:
    """Parse LLM response text into a normalized content dict, raising ``PrepParseError``."""
    payload = extract_json(content)
    if payload is None:
        raise PrepParseError(
            "interview_prep_parse_error",
            "LLM response did not contain a parseable JSON object.",
        )
    normalized = coerce_content(payload)
    if normalized is None:
        raise PrepParseError(
            "interview_prep_parse_error",
            "LLM response JSON was not an object.",
        )
    if not any(normalized.values()):
        raise PrepParseError(
            "interview_prep_empty_content",
            "LLM response contained no recognized interview-prep content.",
        )
    return normalized


def invoke_llm(
    messages: list[Any], *, model: str, api_key: str, base_url: str
) -> Any:
    """Build a bounded ChatOpenAI and invoke it. Lazy-imported for testability."""
    from langchain_openai import ChatOpenAI

    kwargs: dict[str, Any] = {
        "model": model,
        "temperature": 0.3,
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
    """Run one generation. Returns the result dict written to ``--out``."""
    job_snapshot = input_payload.get("job_snapshot") or {}
    profile_facts = input_payload.get("profile_facts")
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
            "code": "interview_prep_interrupted",
            "last_error": str(exc)[:500],
            "agent_version": version,
        }

    content = extract_content(response)
    try:
        prep = parse_content(content)
    except PrepParseError as exc:
        return {
            "status": "failed",
            "code": exc.code,
            "last_error": str(exc),
            "agent_version": version,
        }

    return {"status": "ok", "content": prep, "agent_version": version}


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
        description="Generate an interview-prep kit for a target job via LLM."
    )
    parser.add_argument("--input", help="Path to input JSON (default: stdin)")
    parser.add_argument("--out", default="output/prep_kit.json", help="Output path")
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
        "section_count": sum(len(v) for v in result["content"].values())
        if result["status"] == "ok"
        else 0,
        "agent_version": result.get("agent_version"),
        "out": args.out,
    }
    print(json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry, exercised by the smoke test
    sys.exit(main())
