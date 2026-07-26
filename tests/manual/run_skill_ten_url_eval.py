"""Skill-based 10-URL eval: ``create_deep_agent`` + the project-local
``<repo>/skill/job-discovery`` skill vs the current ``backend/app/services/
job_discovery`` PATH C supervisor.

This is the counterpart of ``tests/integration/job_discovery/
test_supervisor_ten_url_eval.py``. Where that module runs the real PATH C
supervisor (baseline: legacy=6 / blocked=4, PEV pass=0 by construction), this
script runs a *skill-orchestrated* DeepAgent over the SAME 10 URLs so the two
approaches can be compared apples-to-apples.

Architecture
------------
``create_deep_agent`` is wired with:

- ``backend=FilesystemBackend(root_dir=<skills parent>, virtual_mode=True)`` so the
  auto-registered filesystem tools (``read_file``/``ls``/``glob``/``grep``) can read
  the skill files, confined to the skills tree (traversal blocked).
- ``skills=["/"]`` -> ``SkillsMiddleware`` loads ``job-discovery/SKILL.md`` and
  injects its name + description into the system prompt (progressive disclosure).
  The agent then reads the full SKILL.md via ``read_file``.
- ``tools=[run_skill_script]`` - a **bounded** helper that runs ONLY the skill's six
  scripts (browse/validate/normalize/deduplicate/ocr_image/state) with
  ``cwd=SKILL_DIR``. This is the "skill needs bash/py" execution path. It is
  deliberately NOT ``LocalShellBackend``: that grants arbitrary shell on the host
  (security hard-gate conflict). ``run_skill_script`` runs the allowlisted scripts
  only, controls cwd so the skill's relative ``output/`` paths resolve, and inlines
  the rendered page text so the agent never has to resolve evidence paths itself.
- ``model`` = DeepSeek via the same ``_build_job_discovery_llm`` as PATH C, so the
  comparison isolates *approach* (skill vs supervisor), not model.

Gating
------
Live LLM + Playwright run. Skips (never reports PASS) unless both
``RUN_SKILL_TEN_URL=1`` and ``DEEPSEEK_API_KEY`` are set.

Run::

    $env:RUN_SKILL_TEN_URL='1'
    .\\.venv\\Scripts\\python.exe tests/manual/run_skill_ten_url_eval.py

Smoke (one URL, proves the harness wires up)::

    $env:SKILL_EVAL_LIMIT='1'
    $env:RUN_SKILL_TEN_URL='1'
    .\\.venv\\Scripts\\python.exe tests/manual/run_skill_ten_url_eval.py

Resumable: per-URL results are written to ``tests/manual/_skill_ten_url_<slug>.json``
and reused on re-run, so a stalled URL (e.g. xiaomi) can be killed without losing
the others. Delete those files to force a re-crawl.
"""

# ruff: noqa: E402  (sys.path bootstrap must precede project imports)

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission

from backend.app.config import Settings
from backend.app.services.job_discovery.deepagents_runner import _build_job_discovery_llm
from backend.app.services.job_discovery.normalization.jd_normalizer import normalize_title

# ---------------------------------------------------------------------------
# Skill location + execution tool
# ---------------------------------------------------------------------------

# Project-local skill dir (``<repo>/skill/job-discovery``). The skill ships
# inside the repo now - previously it lived in the external pi tree at
# ``~/.pi/agent/skills/job-discovery``. Resolving relative to __file__ keeps it
# portable across machines instead of hardcoding a Windows absolute path.
SKILL_DIR = Path(__file__).resolve().parents[2] / "skill" / "job-discovery"
SKILL_PARENT = SKILL_DIR.parent
# The six helper scripts the skill ships. run_skill_script refuses anything else
# so an agent can never coax it into running arbitrary code.
_SKILL_SCRIPTS = ("browse", "validate", "normalize", "deduplicate", "ocr_image", "state")
# Browse.py can take minutes on a 16-page site (xiaomi). Per-call ceiling.
_SCRIPT_TIMEOUT_SEC = 900
# Cap the inlined page text so a huge list cannot blow the model context. Raised
# from 30k -> 60k for click-mode pagination (a 16-page Mioffice crawl yields far
# more text than a single page); the lenient parser recovers whatever the
# 8192-token output cap can emit.
_MAX_PAGE_TEXT_CHARS = 60_000


@tool
def run_skill_script(script: str, cli_args: str = "") -> str:
    """Run one of the job-discovery skill's helper scripts.

    The skill (read /job-discovery/SKILL.md for full instructions) orchestrates
    browsing + extraction by invoking these scripts. This tool is the ONLY way to
    execute them - the built-in ``execute`` tool is disabled on this backend.

    Args:
        script: One of: browse, validate, normalize, deduplicate, ocr_image, state.
        cli_args: Command-line arguments as a single string, e.g.
            ``"--mode list --out output/evidence <url>"``. Use the same argument
            syntax the SKILL.md documents.

    Returns:
        The script's stdout. For ``browse`` the rendered page text is appended
        under a ``[PAGE_TEXT]`` marker so the caller can extract JDs directly
        without resolving evidence file paths.
    """
    if script not in _SKILL_SCRIPTS:
        return (
            f"ERROR: unknown script {script!r}. "
            f"Allowed: {', '.join(_SKILL_SCRIPTS)}"
        )
    script_path = SKILL_DIR / "scripts" / f"{script}.py"
    if not script_path.exists():
        return f"ERROR: script not found at {script_path}"
    try:
        parts = shlex.split(cli_args, posix=(os.name != "nt")) if cli_args else []
    except ValueError as exc:  # noqa: BLE001 - malformed quoting
        return f"ERROR: could not parse cli_args {cli_args!r}: {exc}"
    cmd = [sys.executable, str(script_path), *parts]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SKILL_DIR),
            capture_output=True,
            text=True,
            timeout=_SCRIPT_TIMEOUT_SEC,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: {script} timed out after {_SCRIPT_TIMEOUT_SEC}s"
    out = proc.stdout or ""
    if proc.stderr:
        out += "\n[stderr]\n" + proc.stderr[-2000:]
    if proc.returncode != 0:
        out += f"\n[exit code {proc.returncode}]"

    # browse.py prints a JSON object with text_path. Inline the rendered text so
    # the agent never has to resolve a (cwd-relative) evidence path through the
    # virtual filesystem backend - that path mapping is fragile and would cost a
    # second tool round-trip per page.
    if script == "browse":
        text = _read_browse_text(out)
        if text:
            out += "\n[PAGE_TEXT]\n" + text[:_MAX_PAGE_TEXT_CHARS]
        else:
            # browse returned no text (status ok but empty innerText = SPA shell
            # that did not render job content; or status error/blocked). Steer
            # the agent AWAY from read_file/ls on the evidence dir: the cached
            # evidence file is 0 bytes, and an empty ToolMessage content makes
            # DeepSeek reject the whole request (BadRequestError 400). The agent
            # must emit the blocked JSON and stop rather than go hunting.
            out += (
                "\n[PAGE_TEXT]\n(empty - the page rendered NO visible job text. "
                "Either the SPA did not load content in headless, or the URL is "
                "dead (404/blank). Do NOT read_file / ls / glob the evidence "
                "paths - the evidence file is 0 bytes and reading it will crash "
                "the session. Retry browse ONCE with --mode search-interact; if "
                "still empty, output "
                '{\"status\":\"blocked\",\"reason\":\"page did not render job '
                'content\"} and stop immediately.)'
            )
    return out


def _read_browse_text(browse_stdout: str) -> str:
    """Extract and read the text_path file reported by browse.py.

    browse.py writes its result JSON to stdout (possibly with leading log lines).
    The JSON carries ``text_path`` (relative to cwd = SKILL_DIR). Resolve it and
    read the rendered page text.
    """
    # Pull the last {...} block from stdout - browse.py may print logs first.
    matches = re.findall(r"\{.*\}", browse_stdout, flags=re.DOTALL)
    if not matches:
        return ""
    try:
        data = json.loads(matches[-1])
    except json.JSONDecodeError:
        return ""
    text_path = data.get("text_path")
    if not text_path:
        return ""
    p = Path(text_path)
    if not p.is_absolute():
        p = SKILL_DIR / text_path
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# 10-URL eval set (mirrors test_supervisor_ten_url_eval.URLS exactly)
# ---------------------------------------------------------------------------

# (slug, company, url, real_count_or_None)
URLS: list[tuple[str, str, str, int | None]] = [
    ("deeproute", "元戎启行",
     "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home", 21),
    ("pdd", "拼多多",
     "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x", 22),
    ("feishu-xiaopeng", "小鹏汽车",
     "https://xiaopeng.jobs.feishu.cn/campus/position/list", None),
    ("inovance", "汇川技术",
     "https://recruit.inovance.com/#/jobs", None),
    ("xiaohongshu", "小红书",
     "https://job.xiaohongshu.com/campus/position", None),
    ("didi", "滴滴",
     "https://talent.didiglobal.com/campus/", None),
    ("netease", "网易",
     "https://hr.163.com/campus.html", None),
    ("baidu", "百度",
     "https://talent.baidu.com/jobs/campus/list", None),
    ("bytedance", "字节跳动",
     "https://jobs.bytedance.com/campus/position", None),
    # xiaomi runs LAST: 151 jobs / 16 listing pages is slow under the live LLM.
    ("xiaomi", "小米",
     "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", 151),
]


# ---------------------------------------------------------------------------
# Candidate counting (mirrors canonical_job_deduplicator identity)
# ---------------------------------------------------------------------------

def _loc_signature(locations: Any) -> str:
    """Stable location signature mirroring canonical_job_deduplicator._loc_key."""
    if not locations:
        return ""
    norms: list[str] = []
    for loc in locations:
        s = str(loc or "").strip()
        if not s:
            continue
        for suf in ("自治区", "省", "市"):
            if s.endswith(suf) and len(s) > len(suf):
                s = s[: -len(suf)]
                break
        norms.append(s)
    return "|".join(sorted(set(norms)))


def _unique_count(candidates: list[dict[str, Any]]) -> int:
    """Unique count mirroring the production split identity.

    Full-JD candidates (with responsibilities/requirements body) are counted by
    ``(normalized_title, location_signature)``; title-only by normalized title.
    """
    seen: set = set()
    for c in candidates:
        title = normalize_title(c.get("title"))
        has_body = bool((c.get("responsibilities") or "").strip()
                        or (c.get("requirements") or "").strip())
        if has_body:
            seen.add((title, _loc_signature(c.get("locations"))))
        else:
            seen.add(title)
    return len(seen)


# ---------------------------------------------------------------------------
# Agent construction
# ---------------------------------------------------------------------------

_SKILL_SYSTEM_PROMPT = """\
You are a job-discovery agent that uses the `job-discovery` skill's helper scripts
to extract structured job postings from a career-site URL. You orchestrate; the
scripts do the mechanical Playwright rendering.

WORKFLOW (follow exactly - do NOT deviate):
1. Read the output schema ONCE:
   `read_file(file_path="/job-discovery/references/schema.md", limit=1000)`
   Do NOT read SKILL.md - it documents the SmartSheet workflow (not needed here)
   and is large. The workflow below is all you need.
2. Render the page to text:
   `run_skill_script(script="browse", cli_args="<URL> --mode list --max-pages 3 --out output/evidence")`
   - The tool output ends with the rendered page text under a `[PAGE_TEXT]` marker.
   - Do NOT separately read_file the evidence path - the text is already inline.
   - If [PAGE_TEXT] is missing or < ~500 chars (common on Moka/feishu/zhiye SPAs),
     retry ONCE: `run_skill_script(script="browse", cli_args="<URL> --mode search-interact --out output/evidence")`.
   - If [PAGE_TEXT] says `(empty - the page rendered NO visible job text ...)` the
     page is an SPA shell that did not load job data, or a dead URL (404/blank).
     Retry browse ONCE with `--mode search-interact`; if STILL empty, output
     `{"status":"blocked","reason":"page did not render job content"}` and stop.
     NEVER respond to an empty [PAGE_TEXT] by calling `read_file`/`ls`/`glob` on
     the evidence path - the cached file is 0 bytes and reading it corrupts the
     request. The text you need is ONLY ever under [PAGE_TEXT].
   - Do NOT call `execute` - it is disabled. Use `run_skill_script` only.
2b. PAGINATE if the page has more jobs than [PAGE_TEXT] shows. Signals: a total
    count in the text (共151 / (151) / 151 职位 / 151 results) that exceeds the
    jobs you can see, OR a paginator control (下一页 / 加载更多 / page numbers
    1 2 3 ... 16 / a next arrow). To load the rest:
    `run_skill_script(script="browse", cli_args="<URL> --mode click --click-auto --click-count 15 --out output/evidence")`
    - `--click-auto` lets browse re-detect the next-page arrow each click (use for
      icon-only arrows, e.g. Mioffice/atsx sites like xiaomi). If the paginator is
      a text button instead, use `--click-text "下一页"` or `--click-text "加载更多"`
      in place of `--click-auto`.
    - Returns the CUMULATIVE text of all loaded pages under [PAGE_TEXT] (pages
      joined by `--- PAGE BREAK ---`). Set `--click-count` high (e.g. 15) - it
      stops early when the paginator is exhausted (`end_reached: true`).
    - Skip 2b if [PAGE_TEXT] already shows all the jobs (no paginator visible and
      no "total" larger than what you see), or if step 2 already used search-interact.
3. Extract EVERY visible job posting from the [PAGE_TEXT] as a JSON array of
   NormalizedJobCandidate objects (per schema.md). One object per distinct job.
   Each must include title, company_name, responsibilities, requirements,
   locations, recruitment_types. Do not invent jobs not on the page.
4. Your FINAL message must contain ONLY the JSON array (no prose, no code fence).
   If the page is a login/captcha/anti-bot wall, output instead:
   `{"status":"blocked","reason":"<one short line>"}` and stop.

CRITICAL - OUTPUT DISCIPLINE:
- After your last browse/click call, your VERY NEXT message MUST be the JSON array
  itself. Do NOT emit a prose "planning" message ("Let me extract...", "I'll now
  parse..."). The agent loop ENDS on any non-tool message, so a prose message
  means you output nothing and lose every job. Go straight to the JSON array.
- If the page has more jobs than fit in one response, output as many COMPLETE
  objects as fit, starting from the top of the list - do NOT abandon the whole
  list. The harness's lenient parser recovers every complete object you emit, so
  a truncated array still yields real candidates. Emitting zero is the only
  failure.

CONSTRAINTS:
- Total tool calls <= 9. Be decisive: one schema read, one browse (list), one
  click pagination if needed, then output the JSON. Do not loop or re-read files.
- Run helper scripts ONLY via `run_skill_script`. Allowed scripts: browse,
  validate, normalize, deduplicate, ocr_image, state.
- Never bypass login / captcha / anti-bot. If blocked, emit the blocked JSON.
- Use the company name you are given for `company_name`.
- Campus / 提前批 / 校招 is the default recruitment_type unless the page says
  otherwise (社招 / 实习).
"""


def _build_settings() -> Settings:
    return Settings(
        app_auth_secret="test-secret-with-at-least-32-characters",
        database_url="sqlite+pysqlite:///:memory:",
        redis_url="redis://localhost:6379/15",
        object_encryption_key="AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
        job_discovery_enabled=True,
        job_discovery_task_timeout_seconds=600,
        job_discovery_max_pages_per_task=20,
        job_discovery_ocr_enabled=True,
    )


def build_skill_agent(model: Any) -> Any:
    """Build the skill-orchestrated DeepAgent.

    - FilesystemBackend (virtual_mode=True) confines file access to the skills
      tree and lets ``read_file``/``ls`` reach the skill files.
    - ``skills=["/"]`` makes SkillsMiddleware load job-discovery/SKILL.md.
    - ``tools=[run_skill_script]`` is the bounded bash/py execution path.
    """
    backend = FilesystemBackend(root_dir=str(SKILL_PARENT), virtual_mode=True)
    permissions = [
        FilesystemPermission(
            operations=["read", "write"],
            paths=["/job-discovery/**"],
        ),
    ]
    return create_deep_agent(
        model=model,
        tools=[run_skill_script],
        backend=backend,
        skills=["/"],
        permissions=permissions,
        system_prompt=_SKILL_SYSTEM_PROMPT,
        name="skill_job_discovery",
    )


def _install_toolmsg_sanitizer(model: Any) -> Any:
    """Patch a ChatOpenAI so no empty ``ToolMessage`` content reaches DeepSeek.

    DeepSeek rejects any request containing a ``role=tool`` message whose
    ``content`` is empty/null with ``BadRequestError 400: messages[N]: unknown``.
    The skill's helper scripts and the virtual filesystem legitimately return
    empty output (ocr_image on a 0-byte PNG cached for a dead SPA, read_file on
    an empty evidence cache, grep/ls with no results). Rather than chase every
    source, normalize at the model boundary: any tool result whose content is
    empty becomes a short placeholder before the request is serialized. This is
    the deepagents<->DeepSeek tool-message sanitization layer - defense-in-depth
    on top of the browse empty-page guidance; it does not change tool semantics
    and never mutates the graph's own message objects (``model_copy`` per msg).
    """
    import types

    def _sanitize(messages: Any) -> list[Any]:
        out: list[Any] = []
        for m in messages:
            if isinstance(m, ToolMessage):
                c = m.content
                empty = (
                    c is None
                    or (isinstance(c, str) and not c.strip())
                    or (isinstance(c, list) and len(c) == 0)
                )
                if empty:
                    m = m.model_copy(
                        update={"content": "(tool returned empty output)"}
                    )
            out.append(m)
        return out

    orig_gen = model._generate
    orig_str = model._stream
    orig_agen = model._agenerate
    orig_astr = model._astream

    def _gen(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        return orig_gen(
            _sanitize(messages), stop=stop, run_manager=run_manager, **kwargs
        )

    def _str(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        yield from orig_str(
            _sanitize(messages), stop=stop, run_manager=run_manager, **kwargs
        )

    async def _agen(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        return await orig_agen(
            _sanitize(messages), stop=stop, run_manager=run_manager, **kwargs
        )

    async def _astr(self, messages, stop=None, run_manager=None, **kwargs):  # noqa: ANN001
        async for chunk in orig_astr(
            _sanitize(messages), stop=stop, run_manager=run_manager, **kwargs
        ):
            yield chunk

    model._generate = types.MethodType(_gen, model)
    model._stream = types.MethodType(_str, model)
    model._agenerate = types.MethodType(_agen, model)
    model._astream = types.MethodType(_astr, model)
    return model


# ---------------------------------------------------------------------------
# Output parsing
# ---------------------------------------------------------------------------

def _extract_candidates(content: str) -> list[dict[str, Any]]:
    """Parse the agent's final message into a candidate list.

    Two strategies, strict first then lenient:

    1. **Strict** - strip a ```json fence and ``json.loads`` the whole payload /
       outermost span. Fast and exact when the agent emitted clean JSON.
    2. **Lenient** - if strict yields nothing, brace-depth-scan the payload for
       individual ``{...}`` objects and ``json.loads`` each independently. This
       recovers candidates when ONE bad object would otherwise sink the whole
       batch: an unescaped quote / raw newline in a long JD string, a truncated
       array (agent hit max_tokens mid-object), or a missing closing fence. Only
       the malformed object is dropped, not the entire site's output.

    A ``blocked`` object yields an empty list - the caller checks status
    separately via ``_is_blocked``.
    """
    if not content:
        return []
    strict = _strict_extract(content)
    if strict:
        return strict
    return _lenient_extract_objects(content)


def _strict_extract(content: str) -> list[dict[str, Any]]:
    """Whole-payload / outermost-span json.loads. Returns [] on any failure."""
    # Strip ```json ... ``` / ``` ... ``` fences (closed fence only).
    fence = re.search(r"```(?:json)?\s*(.*?)```", content, flags=re.DOTALL)
    payload = fence.group(1) if fence else content
    # Try the whole payload first, then the outermost [ ... ] / { ... }.
    for candidate in (payload, *_outermost_json(payload)):
        candidate = candidate.strip()
        if not candidate:
            continue
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, list):
            return [c for c in parsed if isinstance(c, dict)]
        if isinstance(parsed, dict):
            # Single object or a wrapper like {"candidates": [...]}.
            if "candidates" in parsed and isinstance(parsed["candidates"], list):
                return [c for c in parsed["candidates"] if isinstance(c, dict)]
            return [parsed]
    return []


def _outermost_json(text: str) -> list[str]:
    """Yield the outermost [...] then {...} spans in text, widest first."""
    spans: list[str] = []
    for op, cl in (("[", "]"), ("{", "}")):
        start = text.find(op)
        end = text.rfind(cl)
        if start != -1 and end != -1 and end > start:
            spans.append(text[start : end + 1])
    return spans


def _lenient_extract_objects(content: str) -> list[dict[str, Any]]:
    """Recover individual ``{...}`` objects from a (possibly malformed /
    truncated / fenced) payload by brace-depth scanning; parse each
    independently so one bad object does not sink the rest."""
    # Drop an opening ```json fence even when no closing fence is present
    # (truncated output stops before the closing ```).
    m = re.search(r"```(?:json)?\s*", content)
    body = content[m.end():] if m else content
    body = body.rsplit("```", 1)[0] if "```" in body else body
    arr_start = body.find("[")
    if arr_start == -1:
        # Maybe a bare object (no array wrapper).
        obj_start = body.find("{")
        if obj_start == -1:
            return []
        body = body[obj_start:]
    else:
        body = body[arr_start + 1:]  # inside the outermost [...]
    out: list[dict[str, Any]] = []
    for obj in _split_top_level_objects(body):
        obj = obj.strip()
        if not obj:
            continue
        try:
            parsed = json.loads(obj)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            out.append(parsed)
    return out


def _split_top_level_objects(body: str) -> list[str]:
    """Split the inside of a JSON array into individual top-level ``{...}``
    object strings, respecting string/escape state. Truncation-safe: a
    final object whose braces do not re-balance to depth 0 is dropped."""
    objs: list[str] = []
    depth = 0
    cur_start: int | None = None
    in_str = False
    esc = False
    for i, ch in enumerate(body):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                if depth == 0:
                    cur_start = i
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0 and cur_start is not None:
                    objs.append(body[cur_start: i + 1])
                    cur_start = None
    return objs


def _is_blocked(content: str) -> bool:
    if not content:
        return False
    m = re.search(r"\{.*\}", content, flags=re.DOTALL)
    if not m:
        return False
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return False
    return isinstance(obj, dict) and obj.get("status") == "blocked"


# ---------------------------------------------------------------------------
# Per-URL run + summary
# ---------------------------------------------------------------------------

_OUT_DIR = Path(__file__).resolve().parent
RECURSION_LIMIT = 80


def _invoke_agent(agent: Any, prompt: str) -> tuple[str, str]:
    """Invoke the agent, streaming so partial state survives a recursion error.

    Returns ``(content, note)`` where ``note`` is "" on clean finish or a short
    diagnostic (e.g. "recursion_limit") when the agent did not converge. The
    last AI message content is always captured - mirroring how the production
    supervisor streams (``stream_mode="values"``) to preserve partial results.
    """
    last_content = ""
    note = ""
    try:
        for chunk in agent.stream(
            {"messages": [HumanMessage(content=prompt)]},
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode="values",
        ):
            msgs = chunk.get("messages", []) if isinstance(chunk, dict) else []
            if not msgs:
                continue
            content = msgs[-1].content
            if isinstance(content, list):  # multimodal content blocks
                content = "".join(
                    str(b.get("text", "")) for b in content if isinstance(b, dict)
                )
            if content:
                last_content = content
    except Exception as exc:  # noqa: BLE001 - capture partial + classify
        name = type(exc).__name__
        if "Recursion" in name:
            note = "recursion_limit"
        else:
            note = f"{name}: {str(exc)[:120]}"
    return last_content, note


def _run_one(slug: str, company: str, url: str, real_count: int | None,
             agent: Any) -> dict[str, Any]:
    print(f"\n{'='*70}\n  [{slug}] {company}  (real={real_count})\n  {url}\n{'='*70}",
          flush=True)
    prompt = (
        f"Extract ALL campus job postings from this URL as a NormalizedJobCandidate "
        f"JSON array.\n\nURL: {url}\nCompany: {company}\n\n"
        f"Follow the skill workflow: read schema.md, browse the URL, then output "
        f"the JSON array. Output ONLY the final JSON array in your last message."
    )
    t0 = time.monotonic()
    content, note = _invoke_agent(agent, prompt)
    if note and note != "recursion_limit":
        print(f"  !! INVOKE ERROR: {note}", flush=True)
    elapsed = time.monotonic() - t0

    blocked = _is_blocked(content)
    cands = _extract_candidates(content)
    raw_count = len(cands)
    unique = _unique_count(cands)
    if note == "recursion_limit" and cands:
        status = "partial"  # did not converge but produced candidates
    elif note == "recursion_limit":
        status = "recursion"
    elif blocked:
        status = "blocked"
    elif cands:
        status = "succeeded"
    else:
        status = "empty"
    record: dict[str, Any] = {
        "slug": slug,
        "company": company,
        "url": url,
        "real_count": real_count,
        "status": status,
        "candidate_count": raw_count,
        "unique_listing_count": unique,
        "duplicate_count": raw_count - unique,
        "block_reason": "login/captcha/anti-bot" if blocked else None,
        "note": note or None,
        "elapsed_sec": round(elapsed, 1),
        "tail": (content or "")[-400:],
    }
    (_OUT_DIR / f"_skill_ten_url_{slug}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  -> status={status} raw={raw_count} unique={unique} "
        f"dups={record['duplicate_count']} elapsed={record['elapsed_sec']}s"
        + (f" note={note}" if note else ""),
        flush=True)
    return record


def _print_table(rows: list[dict[str, Any]]) -> None:
    print("\n" + "=" * 70)
    print("  SKILL-BASED 10-URL EVAL  (create_deep_agent + job-discovery skill)")
    print("=" * 70)
    print(f"  {'slug':<16} {'status':<10} {'raw':>5} {'uniq':>5} "
          f"{'dups':>5} {'real':>5} {'sec':>7}")
    for r in rows:
        real = "" if r["real_count"] is None else str(r["real_count"])
        print(f"  {r['slug']:<16} {r['status']:<10} {r['candidate_count']:>5} "
              f"{r['unique_listing_count']:>5} {r['duplicate_count']:>5} "
              f"{real:>5} {r['elapsed_sec']:>7}")
    succeeded = sum(1 for r in rows if r["status"] == "succeeded")
    partial = sum(1 for r in rows if r["status"] == "partial")
    blocked = sum(1 for r in rows if r["status"] == "blocked")
    failed = sum(1 for r in rows if r["status"] in ("recursion", "empty", "crashed"))
    print("\n  Buckets: "
          f"succeeded={succeeded}  partial={partial}  blocked={blocked}  "
          f"failed/empty={failed}  total={len(rows)}")
    print("  Baseline (PATH C supervisor): legacy=6 / blocked=4 / pev_pass=0")
    print("=" * 70)
    # Real-count drift diagnostic (informational; never raises).
    for r in rows:
        if r["real_count"] is not None:
            match = "OK" if r["unique_listing_count"] == r["real_count"] else "DRIFT"
            print(f"  [diag] {r['slug']}: unique {r['unique_listing_count']} vs "
                  f"real {r['real_count']} -> {match}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

_LIVE_ENABLED = bool(os.environ.get("RUN_SKILL_TEN_URL"))
_DEEPSEEK_KEY = bool(os.environ.get("DEEPSEEK_API_KEY"))
_READGZH_KEY = bool(os.environ.get("READGZH_API_KEY"))
_LIVE_READY = _LIVE_ENABLED and _DEEPSEEK_KEY


def _main() -> int:
    if not _LIVE_ENABLED:
        print("SKIP: RUN_SKILL_TEN_URL is not set (not PASS).")
        return 0
    if not _DEEPSEEK_KEY:
        print("SKIP: DEEPSEEK_API_KEY is missing (not PASS).")
        return 0
    if not _READGZH_KEY:
        print("NOTE: READGZH_API_KEY not set; WeChat URLs (none in this set) "
              "would fall back to direct HTTP / Playwright.")

    if not SKILL_DIR.exists():
        print(f"ERROR: skill dir not found: {SKILL_DIR}")
        return 1

    limit = int(os.environ.get("SKILL_EVAL_LIMIT", "0")) or len(URLS)
    urls = URLS[:limit]
    print(f"RUN_SKILL_TEN_URL=1  DEEPSEEK_API_KEY={'set' if _DEEPSEEK_KEY else 'MISSING'}  "
          f"urls={len(urls)} (limit={limit})")
    print(f"SKILL_DIR={SKILL_DIR}")

    settings = _build_settings()
    # DeepSeek's default max_tokens (~4096) truncates a large single-response
    # extraction (xiaohongshu has 346 jobs -> ~52k tokens, inherently beyond one
    # response). 8192 is the model ceiling; the lenient parser recovers whatever
    # fit before truncation. PATH C baseline extracts per-page so is unaffected.
    model = _build_job_discovery_llm(settings=settings).model_copy(
        update={"max_tokens": 8192}
    )
    # Sanitize empty ToolMessage content at the DeepSeek boundary (A-fix): a
    # 0-byte tool result otherwise yields BadRequestError 400 (messages[N]:
    # unknown) and sinks the whole run - see _install_toolmsg_sanitizer.
    model = _install_toolmsg_sanitizer(model)
    agent = build_skill_agent(model)

    rows: list[dict[str, Any]] = []
    for slug, company, url, real_count in urls:
        prior = _OUT_DIR / f"_skill_ten_url_{slug}.json"
        if prior.exists():  # Resumable: reuse a prior run's result.
            print(f"  [skip] {slug} (reuse prior result)", flush=True)
            rows.append(json.loads(prior.read_text(encoding="utf-8")))
        else:
            rows.append(_run_one(slug, company, url, real_count, agent))
        _print_table(rows)
        (_OUT_DIR / "_skill_ten_url_summary.json").write_text(
            json.dumps({"rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    _print_table(rows)
    return 0


if __name__ == "__main__":
    sys.exit(_main())
