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
``RUN_SKILL_TEN_URL=1`` and an OpenAI-compatible API key
(``DEEPSEEK_API_KEY`` or ``OPENAI_API_KEY``) are set.

Run::

    $env:RUN_SKILL_TEN_URL='1'
    .\\.venv\\Scripts\\python.exe -X utf8 tests/manual/run_skill_ten_url_eval.py

Smoke (one URL, proves the harness wires up)::

    $env:SKILL_EVAL_LIMIT='1'
    $env:RUN_SKILL_TEN_URL='1'
    .\\.venv\\Scripts\\python.exe -X utf8 tests/manual/run_skill_ten_url_eval.py

Resumable: per-URL results are written to ``tests/manual/_skill_ten_url_<slug>.json``
and reused on re-run, so a stalled URL (e.g. xiaomi) can be killed without losing
the others. ``SKILL_EVAL_ONLY=<slug>`` selects a URL but still reuses its prior
record. A paid fresh run requires the additional, explicit
``SKILL_EVAL_FORCE_FRESH=<same-slug>`` gate.
"""

# ruff: noqa: E402  (sys.path bootstrap must precede project imports)

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[2]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))


def _utf8_requirement_message() -> str:
    return (
        "SKIP: start this Windows eval with `python -X utf8 "
        "tests/manual/run_skill_ten_url_eval.py` to prevent GBK decoding."
    )

# Force UTF-8 stdout/stderr on Windows so trace printing of message content
# containing the Unicode replacement char (�, from garbled browse text)
# or CJK does not crash with ``UnicodeEncodeError: 'gbk' codec can't encode``.
# v1.3 xiaomi hit this mid-stream, leaving candidates on disk but marking the
# run with an error note. Reconfigure with errors='replace' so printing is
# always safe; the on-disk candidate files are already written as UTF-8 by
# write_candidates.py and are unaffected.
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[attr-defined]
except (AttributeError, OSError):
    pass

from langchain_core.messages import HumanMessage, ToolMessage
from langchain_core.tools import tool
from deepagents import create_deep_agent
from deepagents.backends.filesystem import FilesystemBackend
from deepagents.middleware.filesystem import FilesystemPermission
from deepagents.middleware.subagents import SubAgent

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
_SKILL_SCRIPTS = ("browse", "validate", "normalize", "deduplicate", "ocr_image", "state",
                  "read_evidence", "write_candidates")
# Browse.py can take minutes on a 16-page site (xiaomi). Per-call ceiling.
_SCRIPT_TIMEOUT_SEC = 900
# Cap the inlined page text so a huge list cannot blow the model context. Raised
# from 30k -> 60k for click-mode pagination (a 16-page Mioffice crawl yields far
# more text than a single page); the lenient parser recovers whatever the
# 8192-token output cap can emit.
_MAX_PAGE_TEXT_CHARS = 60_000


@tool
def run_skill_script(script: str, cli_args: str = "", stdin: str = "") -> str:
    """Run one of the job-discovery skill's helper scripts.

    The skill (read /job-discovery/SKILL.md for full instructions) orchestrates
    browsing + extraction by invoking these scripts. This tool is the ONLY way to
    execute them - the built-in ``execute`` tool is disabled on this backend.

    Args:
        script: One of: browse, validate, normalize, deduplicate, ocr_image,
            state, read_evidence, write_candidates.
        cli_args: Command-line arguments as a single string, e.g.
            ``"--mode list --out output/evidence <url>"``. Use the same argument
            syntax the SKILL.md documents.
        stdin: Optional string piped to the script's stdin. Used by
            ``write_candidates`` (which reads a candidate JSON document from
            stdin to avoid the Windows ~32k command-line length limit). Pass the
            full JSON document here; do NOT put large JSON in ``cli_args``.

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
    child_env = {
        **os.environ,
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    }
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(SKILL_DIR),
            capture_output=True,
            text=True,
            timeout=_SCRIPT_TIMEOUT_SEC,
            encoding="utf-8",
            errors="replace",
            input=stdin if stdin else None,
            env=child_env,
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
You are a job-discovery PLANNER/VERIFIER for a single career-site URL. You
orchestrate per-page extraction; the scripts do the rendering and a
`jd_extractor` sub-agent does the per-page JD extraction. You do NOT extract
JDs yourself and you do NOT emit the candidate list - that is what avoids the
LLM output-cap loss on large sites (151 jobs / 16 pages).

WORKFLOW - load it first, then follow it exactly:
1. Read the workflow doc ONCE:
   `read_file(file_path="/job-discovery/references/single-url-extraction.md", limit=1200)`
   It documents the full planner -> executor -> verifier flow. Do NOT read
   SKILL.md - it documents the SmartSheet batch workflow (not needed here) and
   is large.
2. Follow that doc: read schema.md, browse with `--mode parallel-fetch` (v1.6:
   detects URL-keyed pagination and fetches all pages concurrently via a thread
   pool; auto-falls back to serial click for load-more sites, and returns a thin
   `spa_shell_no_pagination` result for card-SPAs), retry ONCE with
   `--mode search-interact` if `[PAGE_TEXT]` is < ~500 chars, then fan out ONE
   `jd_extractor` task per page file in a SINGLE message (parallel), then
   `deduplicate`-merge.

KEY POINTS (also in the workflow doc - repeated because they are the common
failure modes):
- After browse, the result JSON carries `page_files` (paths to
  `output/evidence/pages/page_NN.txt`) and `page_count`. Use `page_files` to
  dispatch one `jd_extractor` per page. Each task description must give the
  sub-agent its page file path AND its output path
  (`output/candidates/page_NN.json`) AND the company name.
- Dispatch ALL page tasks in ONE assistant message (multiple `task` tool calls)
  so they run in parallel. One task per page file.
- The `jd_extractor` reads its own page file via `read_evidence` - do NOT pass
  the page text in the task description (keep your context lean).
- VERIFY-RETRY (the verifier step - recovers pages whose first extraction
  failed): after the jd_extractor tasks return, INSPECT each task's final
  JSON summary. A page is FAILED if its summary is not
  `{"status":"ok",...}` with `written` > 0 (i.e. status is "error" or
  "blocked", OR `written` is 0/missing, OR the page file was never created).
  For every FAILED page, RE-DISPATCH ONE `jd_extractor` task for that page
  in a SINGLE assistant message (parallel) - the evidence file
  `output/evidence/pages/page_NN.txt` still exists, so the retry sub-agent can
  re-read it. Do at most ONE retry round. Then run deduplicate.
- After verify-retry (or if no pages failed), run:
  `run_skill_script(script="deduplicate", cli_args="output/candidates/*.json --out output/candidates_merged.json")`
- If `[PAGE_TEXT]` from browse is missing/< ~500 chars, retry ONCE with
  `--mode search-interact`; if still empty, emit
  `{"status":"blocked","reason":"page did not render job content"}` and stop.
- HARD LIMITS: at most ONE `parallel-fetch` browse and ONE search-interact
  retry. (`parallel-fetch` already covers the list + click paths internally -
  do NOT also issue separate `--mode list` or `--mode click` calls.) If
  `parallel-fetch` returns `page_count == 1` with thin text and search-interact
  is also empty, STOP and dispatch `jd_extractor` on whatever pages you have -
  do NOT loop on browse variants. NEVER `read_file`/`ls`/`glob` anything under
  `output/evidence/` (especially `.png` screenshots) - that returns empty/image
  bytes which crash the run. Page text is ONLY under `[PAGE_TEXT]` or via
  `read_evidence` on a `output/evidence/pages/page_NN.txt` path.

CRITICAL - OUTPUT DISCIPLINE:
- Your FINAL message must be ONLY a small JSON summary, e.g.
  `{"status":"done","pages":16,"candidates_file":"output/candidates_merged.json","merged_count":151}`
  The harness reads `output/candidates_merged.json` off disk. Do NOT emit the
  candidates themselves - that re-hits the output cap this design avoids. Do
  NOT emit a prose "planning" message either: the agent loop ENDS on any
  non-tool message, so a prose message means you output nothing.
- If the page is a login/captcha/anti-bot wall, emit instead
  `{"status":"blocked","reason":"<one short line>"}` and stop.

CONSTRAINTS:
- Total tool calls <= 14. The `task` calls count but run in parallel.
- The virtual filesystem is READ-ONLY: `write_file`/`edit_file`/`str_replace`
  are DENIED everywhere under /job-discovery/** (they return a
  permission-denied error). Do NOT attempt them and do NOT invent or run
  your own writer scripts (`write_jobs.py`, `write_pageNN.py`, etc.) - they
  pollute the skill and will not run. Candidate files are written ONLY by
  `jd_extractor` sub-agents via `write_candidates.py` (a `run_skill_script`
  subprocess on real disk, which the FS permissions do not gate). If a
  candidate file looks wrong, do NOT fix it by hand - just run
  `deduplicate`, which skips malformed files. Your only write to the
  candidates tree is the `deduplicate`-merge.
- Run helper scripts ONLY via `run_skill_script`. Allowed: browse, validate,
  normalize, deduplicate, ocr_image, state, read_evidence, write_candidates.
- Never bypass login / captcha / anti-bot. If blocked, emit the blocked JSON.
- Use the company name you are given for `company_name`.
- Campus / 提前批 / 校招 is the default recruitment_type unless the page says
  otherwise (社招 / 实习).
"""


_JD_EXTRACTOR_PROMPT = """\
You are a JD EXTRACTOR sub-agent. You extract structured job postings from ONE
page-text file and persist them to disk. You handle exactly ONE page - do NOT
browse, do NOT paginate, do NOT dispatch further sub-agents (you are depth-2).

INPUT: your `task` description gives you (a) the page file path to read, e.g.
`output/evidence/pages/page_03.txt`, (b) the output path to write, e.g.
`output/candidates/page_03.json`, and (c) the company name to use.

SCHEMA ( condensed - produce one JSON object per distinct job on the page ):
{
  "title": "<required, job title>",
  "company_name": "<required, the company name from your task description>",
  "department": "<optional>",
  "description_text": "<short summary of the role>",
  "responsibilities": "<职责, full text from the page>",
  "requirements": "<任职要求, full text from the page>",
  "locations": ["<city>"],
  "recruitment_types": ["校园招聘"],
  "apply_url": "<optional, if on the page>",
  "evidence_refs": [{"content_hash": "", "evidence_type": "browsed_list_page_text"}]
}
- Default recruitment_types to ["校园招聘"] (campus) unless the page says 社招 / 实习.
- For full field details you MAY `read_file("/job-discovery/references/schema.md")`
  on demand, but the condensed schema above is enough for most pages - skip the
  read to save a round-trip when the fields are clear.

STEPS (strict tool budget):
1. Read your page text ONCE:
   `run_skill_script(script="read_evidence", cli_args="output/evidence/pages/page_03.txt")`
   If the `[READ_SUMMARY]` says `status: error`, your final message must be
   `{"status":"error","page":"page_03","reason":"<the reason>"}` - do NOT call
   write_candidates.
2. WRITE the extracted candidates to your assigned output path. To stay under
   the generation cap, write in BATCHES of <=6 candidates per call, using
   `--append` on every call after the first:
   - 1st batch:  `run_skill_script(script="write_candidates", cli_args="--out output/candidates/page_03.json", stdin="<JSON array of up to 6 candidates>")`
   - 2nd+ batch: `run_skill_script(script="write_candidates", cli_args="--out output/candidates/page_03.json --append", stdin="<next up to 6>")`
   - Continue until all candidates on the page are written.
   PASS ONLY THE JSON ARRAY ON `stdin` - no prose, no code fence, no
   "here are the candidates" prefix. The script leniently recovers complete
   objects even if a batch is truncated mid-object.
3. Your FINAL message must be ONLY a short JSON summary, e.g.
   `{"status":"ok","page":"page_03","written":12,"file":"output/candidates/page_03.json"}`
   (use the `total_in_file` from the last write_candidates result as `written`).

HARD RULES - do not break these:
- The virtual filesystem is READ-ONLY. `write_file`/`edit_file`/`str_replace`
  are DENIED (they return permission-denied). Persist candidates ONLY via
  `write_candidates.py` (`run_skill_script`). NEVER invent, `write_file`, or
  run your own writer script (`write_jobs.py`, `write_pageNN.py`, an inline
  `python -c`, etc.) to write candidate JSON - it bypasses the dedup/redirect
  logic and pollutes the skill. The ONLY write call you make is
  `run_skill_script(script="write_candidates", ...)`.
- NO TEST/PLACEHOLDER DATA. NEVER write dummy candidates (title like `test`,
  `test1`, `job1`, `placeholder`, `test123`, `test_clear`, `测试`, `算法组`,
  `算法组` alone, a bare category label, etc.). If the page has NO real job
  postings (login/captcha/anti-bot wall, empty list, no JD text), call
  write_candidates ONCE with `stdin="[]"` and your final message is
  `{"status":"blocked","page":"page_NN","reason":"<one line>"}`. Writing fake
  candidates to look busy is the worst failure mode - it poisons the dataset.
- ORIGINAL LANGUAGE ONLY. Keep `title`, `company_name`, `department` in the
  EXACT language they appear in on the page (Chinese for 小米/xiaomi, etc.).
  Do NOT translate to English and do NOT romanize to pinyin (e.g. write
  `鼎尖英杰`, never `Dingjian Yingjie`; write `AI编译器工程师`, never
  `AI Compiler Engineer` unless the page itself uses the English form).
  `description_text`/`responsibilities`/`requirements` are quoted from the
  page verbatim - same rule.
- Write ONLY to the EXACT `--out` path from your task description
  (`output/candidates/page_NN.json`). Do NOT invent a suffixed filename
  (`_new`, `_v2`, `_temp`, `_final`, `_batch`, `_clean`, `_test`); there is
  never a reason to have more than one `page_NN.json` per page. If you ever do
  write a suffixed path, write_candidates REDIRECTS it to `page_NN.json`
  automatically (always-append + identity-dedup) so candidates are never lost -
  but you should still write the exact path you were given.
- If write_candidates returns `status:error`, the file is NOT corrupt - your
  JSON INPUT was malformed/truncated. Retry the SAME `--out` path ONCE with
  `--append` and a SMALLER batch (<=3 candidates). Do NOT invent a new
  filename. The script dedups by identity, so `--append` never double-counts.
- Call write_candidates at most ~4 times. If a page has >24 candidates, stop
  after 4 batches (that is already far more than the cap would have allowed).
- Do NOT re-emit the candidates in your final message - they are on disk.
- Do NOT emit a prose planning message; the loop ENDS on any non-tool message.
- If the page is a login/captcha/anti-bot wall (no JDs), still call
  write_candidates once with `stdin="[]"` and report `{"status":"blocked",...}`.
- Never bypass login / captcha / anti-bot. You are depth-2: do NOT call `task`.
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
    """Build the skill-orchestrated DeepAgent (planner/verifier + jd_extractor).

    - FilesystemBackend (virtual_mode=True) confines file access to the skills
      tree and lets ``read_file``/``ls`` reach the skill files (progressive
      disclosure: the agent loads ``references/single-url-extraction.md`` and
      ``references/schema.md`` on demand instead of carrying them in the prompt).
    - ``skills=["/"]`` makes SkillsMiddleware load job-discovery/SKILL.md.
    - ``tools=[run_skill_script]`` is the bounded bash/py execution path.
    - ``subagents=[jd_extractor]``: the planner fans out ONE jd_extractor per
      page file (parallel via multiple ``task`` calls in one message). The
      sub-agent has no ``subagents`` of its own -> max depth 2 (no sub-sub-agents).
    """
    backend = FilesystemBackend(root_dir=str(SKILL_PARENT), virtual_mode=True)
    # READ-ONLY filesystem (v1.3): allow read on the skill tree so the agent
    # can load references/schema/evidence on demand (progressive disclosure),
    # but EXPLICITLY DENY write. deepagents permissions are DEFAULT-ALLOW, so a
    # bare operations=["read"] rule would NOT block writes - the explicit deny
    # rule is required. This forces EVERY candidate write through
    # write_candidates.py (a run_skill_script subprocess on real disk, which the
    # virtual-FS permissions do not gate), so the suffix-redirect logic always
    # applies and sub-agents can no longer invent writer scripts (write_jobs.py,
    # write_pageNN.py) or write_file candidate JSON directly - both were observed
    # in v1.2 and bypassed the redirect. All legitimate writes (browse stash,
    # write_candidates, deduplicate-merge) are subprocesses unaffected here.
    permissions = [
        FilesystemPermission(
            operations=["read"],
            paths=["/job-discovery/**"],
        ),
        FilesystemPermission(
            operations=["write"],
            paths=["/job-discovery/**"],
            mode="deny",
        ),
    ]
    jd_extractor: SubAgent = {
        "name": "jd_extractor",
        "description": (
            "Extract structured JDs from ONE page-text file (output/evidence/"
            "pages/page_NN.txt) and persist them to output/candidates/page_NN.json. "
            "Dispatch one per page; the sub-agent reads its own page file."
        ),
        "system_prompt": _JD_EXTRACTOR_PROMPT,
        # tools omitted -> inherits [run_skill_script] from the parent.
        # No 'subagents' key -> this sub-agent cannot dispatch sub-sub-agents.
    }
    return create_deep_agent(
        model=model,
        tools=[run_skill_script],
        backend=backend,
        skills=["/"],
        permissions=permissions,
        system_prompt=_SKILL_SYSTEM_PROMPT,
        subagents=[jd_extractor],
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
                # Empty: None, empty/whitespace string, or empty list.
                empty_str = isinstance(c, str) and not c.strip()
                empty_list = isinstance(c, list) and len(c) == 0
                # Non-text list: a list whose blocks have no usable text. DeepSeek
                # rejects image/multimodal blocks inside a tool result with the
                # same "messages[N]: unknown" 400 as an empty tool result, so a
                # tool that returns an image (e.g. read_file on a .png) must also
                # be replaced with a plain-text placeholder.
                non_text_list = False
                if isinstance(c, list) and c:
                    texts = [
                        str(b.get("text", "")) for b in c
                        if isinstance(b, dict) and "text" in b
                    ]
                    non_text_list = not any(t.strip() for t in texts)
                if c is None or empty_str or empty_list or non_text_list:
                    if _TRACE:
                        print(f"  [trace] SANITIZED empty/non-text ToolMessage "
                              f"({getattr(m, 'name', '?')})", flush=True)
                    m = m.model_copy(
                        update={"content": "(tool returned empty or non-text output)"}
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
    strict = _without_terminal_status_objects(_strict_extract(content))
    if strict:
        return strict
    return _without_terminal_status_objects(_lenient_extract_objects(content))


def _without_terminal_status_objects(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Remove workflow terminal messages mistakenly shaped like a candidate."""
    terminal_statuses = {"blocked", "empty", "recursion", "crashed"}
    return [
        candidate
        for candidate in candidates
        if candidate.get("status") not in terminal_statuses
    ]


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
# v1.4 bumped 120 -> 150: the verify-retry round re-dispatches jd_extractor
# sub-agents for failed pages (page_03/08/10 in v1.3), adding ~3-5 sub-agent
# subgraphs on top of the 16-page first round. Each sub-agent's internal
# steps count toward the parent's recursion budget, so the retry round needs
# headroom to avoid a recursion_limit partial on otherwise-recoverable runs.
RECURSION_LIMIT = 150
_TRACE = bool(os.environ.get("SKILL_EVAL_TRACE"))


def _trace_msg(msg: Any) -> None:
    """Print a compact per-message trace so a looping/under-extracting agent is
    diagnosable without re-reading the full transcript. One line per message:
    AI tool calls, AI text (first 120 chars), or tool result (first 120 chars).
    """
    mt = type(msg).__name__
    # AIMessage: tool_calls + optional content
    tool_calls = getattr(msg, "tool_calls", None)
    content = getattr(msg, "content", "")
    if isinstance(content, list):
        content = "".join(str(b.get("text", "")) for b in content if isinstance(b, dict))
    if tool_calls:
        calls = ", ".join(
            f"{tc.get('name')}({str(tc.get('args',''))[:80]!r})"
            for tc in tool_calls
        )
        print(f"  [trace] AI->tools: {calls}", flush=True)
        if content:
            print(f"  [trace] AI text: {str(content)[:120]!r}", flush=True)
    elif mt == "ToolMessage":
        print(f"  [trace] TOOL[{getattr(msg,'name','?')}]: {str(content)[:120]!r}", flush=True)
    elif content:
        print(f"  [trace] AI text: {str(content)[:120]!r}", flush=True)


def _invoke_agent(agent: Any, prompt: str) -> tuple[str, str]:
    """Invoke the agent, streaming so partial state survives a recursion error.

    Returns ``(content, note)`` where ``note`` is "" on clean finish or a short
    diagnostic (e.g. "recursion_limit") when the agent did not converge. The
    last AI message content is always captured - mirroring how the production
    supervisor streams (``stream_mode="values"``) to preserve partial results.
    """
    last_content = ""
    note = ""
    _traced = 0  # messages already traced (values stream replays the full list each yield)
    try:
        for chunk in agent.stream(
            {"messages": [HumanMessage(content=prompt)]},
            config={"recursion_limit": RECURSION_LIMIT},
            stream_mode="values",
        ):
            msgs = chunk.get("messages", []) if isinstance(chunk, dict) else []
            if not msgs:
                continue
            if _TRACE:
                # Trace only NEW messages since the last yield. The values
                # stream replays the entire message list every step, so the
                # old `msgs[-2:]` window re-traced the previous message each
                # time -> a misleading 2x in the log that looked like the agent
                # double-calling (it does not - actual execution is single-pass).
                # Skip HumanMessage (the prompt) - it is not an agent action.
                for m in msgs[_traced:]:
                    if type(m).__name__ != "HumanMessage":
                        _trace_msg(m)
                _traced = len(msgs)
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


def _clean_persisted() -> None:
    """Clear per-URL persisted artifacts so a previous URL's candidates/pages
    cannot pollute the next URL's run.

    Clears ``output/candidates/`` (per-page JD files written by jd_extractor
    sub-agents), ``output/candidates_merged.json`` (the deduplicate output), and
    ``output/evidence/pages/`` (the per-page text stash from browse click/list
    mode - a shorter page would otherwise leave the longer prior run's
    ``page_NN.txt`` files in place). The content-addressed
    ``output/evidence/sha256_*.txt`` browse cache is KEPT so list-mode caching
    behaves consistently across runs (a different URL hashes to a different
    file, so there is no cross-URL collision).
    """
    for target in (
        SKILL_DIR / "output" / "candidates",
        SKILL_DIR / "output" / "evidence" / "pages",
    ):
        if target.exists():
            shutil.rmtree(target, ignore_errors=True)
    (SKILL_DIR / "output" / "candidates_merged.json").unlink(missing_ok=True)


def _load_persisted_candidates() -> list[dict[str, Any]]:
    """Load candidate JDs the sub-agents persisted to disk.

    Preference order:
      1. ``output/candidates_merged.json`` (the agent-run ``deduplicate`` output -
         already normalized, deduped, and packaged).
      2. ``output/candidates/*.json`` (per-page files written by jd_extractor
         sub-agents) - flattened; cross-page duplicates are removed by
         ``_unique_count`` / ``deduplicate`` semantics downstream.
      3. empty list (caller falls back to parsing the agent's final message).

    Never raises - a malformed file is skipped so one bad page file cannot sink
    the whole URL's result.
    """
    merged = SKILL_DIR / "output" / "candidates_merged.json"
    if merged.exists():
        try:
            data = json.loads(merged.read_text(encoding="utf-8"))
            if isinstance(data, list):
                return [c for c in data if isinstance(c, dict)]
            if isinstance(data, dict):
                return [data]
        except (json.JSONDecodeError, OSError):
            pass

    out: list[dict[str, Any]] = []
    cand_dir = SKILL_DIR / "output" / "candidates"
    if not cand_dir.exists():
        return out
    for f in sorted(cand_dir.glob("*.json")):
        try:
            data = json.loads(f.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, list):
            out.extend(c for c in data if isinstance(c, dict))
        elif isinstance(data, dict):
            out.append(data)
    return out


def _preserve_merged(slug: str) -> str | None:
    """Copy this run's merged candidates to the record dir so candidate
    QUALITY (detailed JDs) can be inspected post-run even if the working
    ``output/`` tree is later cleared by the next URL's ``_clean_persisted``.

    Preference order mirrors ``_load_persisted_candidates``: the merged file
    first, then a flattened per-page fallback. Returns the destination path
    or None. Never raises - preservation is best-effort, not on the critical
    path of the measurement.
    """
    dst = _OUT_DIR / f"_skill_ten_url_{slug}_merged.json"
    merged = SKILL_DIR / "output" / "candidates_merged.json"
    try:
        if merged.exists():
            shutil.copy2(merged, dst)
            return str(dst)
        cand_dir = SKILL_DIR / "output" / "candidates"
        out: list[dict[str, Any]] = []
        if cand_dir.exists():
            for f in sorted(cand_dir.glob("*.json")):
                try:
                    data = json.loads(f.read_text(encoding="utf-8"))
                except (json.JSONDecodeError, OSError):
                    continue
                if isinstance(data, list):
                    out.extend(c for c in data if isinstance(c, dict))
                elif isinstance(data, dict):
                    out.append(data)
        if out:
            dst.write_text(
                json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")
            return str(dst)
    except OSError:
        pass
    return None


def _classify_run_status(
    *,
    note: str,
    blocked: bool,
    candidates: list[dict[str, Any]],
) -> str:
    """Classify a run without treating an interrupted workflow as a success."""
    if note == "recursion_limit":
        return "partial" if candidates else "recursion"
    if note:
        return "partial" if candidates else "crashed"
    if blocked:
        return "blocked"
    return "succeeded" if candidates else "empty"


def _normalize_replayed_record(record: dict[str, Any]) -> dict[str, Any]:
    """Apply current status semantics to a cached result without re-running it."""
    normalized = dict(record)
    candidate_count = int(normalized.get("candidate_count", 0) or 0)
    normalized["status"] = _classify_run_status(
        note=str(normalized.get("note") or ""),
        blocked=bool(normalized.get("block_reason")),
        candidates=[{}] if candidate_count else [],
    )
    normalized["evaluation_mode"] = "replay"
    return normalized


def _run_one(slug: str, company: str, url: str, real_count: int | None,
             agent: Any) -> dict[str, Any]:
    print(f"\n{'='*70}\n  [{slug}] {company}  (real={real_count})\n  {url}\n{'='*70}",
          flush=True)
    # Fresh per-URL persisted state so prior candidates/pages don't leak in.
    _clean_persisted()
    prompt = (
        f"Extract ALL campus job postings from this URL as NormalizedJobCandidate "
        f"JSON objects, persisted to disk via the workflow.\n\n"
        f"URL: {url}\nCompany: {company}\n\n"
        f"Follow the single-url-extraction workflow: read it + schema.md, browse "
        f"the URL, paginate if needed, dispatch one jd_extractor per page, "
        f"deduplicate-merge to output/candidates_merged.json, then emit the "
        f"short summary JSON as your final message."
    )
    t0 = time.monotonic()
    content, note = _invoke_agent(agent, prompt)
    if note and note != "recursion_limit":
        print(f"  !! INVOKE ERROR: {note}", flush=True)
    elapsed = time.monotonic() - t0

    blocked = _is_blocked(content)
    # Prefer candidates persisted to disk by the sub-agents; fall back to
    # parsing the agent's final message for backward compatibility (older
    # single-pass behavior / blocked responses).
    persisted = _load_persisted_candidates()
    if persisted:
        cands = persisted
        source = "disk"
    else:
        cands = _extract_candidates(content)
        source = "message"
    raw_count = len(cands)
    # Snapshot the merged candidates into the record dir so JD quality can be
    # inspected later (the working output/ tree is cleared per-URL).
    merged_preserved = _preserve_merged(slug)
    unique = _unique_count(cands)
    status = _classify_run_status(note=note, blocked=blocked, candidates=cands)
    record: dict[str, Any] = {
        "slug": slug,
        "company": company,
        "url": url,
        "real_count": real_count,
        "status": status,
        "candidate_source": source,
        "merged_preserved": merged_preserved,
        "candidate_count": raw_count,
        "unique_listing_count": unique,
        "duplicate_count": raw_count - unique,
        "block_reason": "login/captcha/anti-bot" if blocked else None,
        "note": note or None,
        "evaluation_mode": "fresh_live",
        "elapsed_sec": round(elapsed, 1),
        "tail": (content or "")[-400:],
    }
    (_OUT_DIR / f"_skill_ten_url_{slug}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        f"  -> status={status} src={source} raw={raw_count} unique={unique} "
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
_READGZH_KEY = bool(os.environ.get("READGZH_API_KEY"))


def _has_llm_key() -> bool:
    """Match the job-discovery LLM factory's OpenAI-compatible key lookup."""
    return bool(os.environ.get("DEEPSEEK_API_KEY") or os.environ.get("OPENAI_API_KEY"))


def _parse_slug_set(value: str) -> set[str]:
    return {slug.strip() for slug in value.split(",") if slug.strip()}


def _select_eval_urls(
    urls: list[tuple[str, str, str, int | None]],
    *,
    limit: int,
    only: str,
    force_fresh: str,
) -> tuple[list[tuple[str, str, str, int | None]], set[str]]:
    """Select URLs and validate the separate, explicit refresh request."""
    selected = urls[:limit]
    only_slugs = _parse_slug_set(only)
    if only_slugs:
        selected = [row for row in selected if row[0] in only_slugs]

    selected_slugs = {row[0] for row in selected}
    refresh_slugs = _parse_slug_set(force_fresh)
    unknown = refresh_slugs - selected_slugs
    if unknown:
        raise ValueError(
            "SKILL_EVAL_FORCE_FRESH must name only a selected URL; "
            f"invalid={sorted(unknown)} selected={sorted(selected_slugs)}"
        )
    return selected, refresh_slugs


def _summary_path(
    urls: list[tuple[str, str, str, int | None]],
) -> Path:
    """Keep a focused replay or canary run from overwriting the suite summary."""
    if len(urls) == len(URLS) and {row[0] for row in urls} == {row[0] for row in URLS}:
        return _OUT_DIR / "_skill_ten_url_summary.json"
    suffix = "_".join(row[0] for row in urls) or "none"
    return _OUT_DIR / f"_skill_ten_url_summary_{suffix}.json"


def _main() -> int:
    if not SKILL_DIR.exists():
        print(f"ERROR: skill dir not found: {SKILL_DIR}")
        return 1

    limit = int(os.environ.get("SKILL_EVAL_LIMIT", "0")) or len(URLS)
    # Selecting a slug is safe and cache-preserving. Deleting a record requires
    # a second, explicit opt-in because it triggers paid model calls.
    only = os.environ.get("SKILL_EVAL_ONLY", "").strip()
    force_fresh = os.environ.get("SKILL_EVAL_FORCE_FRESH", "").strip()
    try:
        urls, refresh_slugs = _select_eval_urls(
            URLS, limit=limit, only=only, force_fresh=force_fresh,
        )
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    if only:
        print(f"SKILL_EVAL_ONLY={sorted(_parse_slug_set(only))} -> {len(urls)} url(s)")
    if refresh_slugs:
        print(f"FRESH PAID RUN: SKILL_EVAL_FORCE_FRESH={sorted(refresh_slugs)}")
    needs_live_run = any(
        slug in refresh_slugs or not (_OUT_DIR / f"_skill_ten_url_{slug}.json").exists()
        for slug, _, _, _ in urls
    )
    if needs_live_run and not _LIVE_ENABLED:
        print("SKIP: RUN_SKILL_TEN_URL is not set (fresh validation not run).")
        return 0
    if needs_live_run and not _has_llm_key():
        print("SKIP: an OpenAI-compatible API key is missing (fresh validation not run).")
        return 0
    if needs_live_run and not _READGZH_KEY:
        print("NOTE: READGZH_API_KEY not set; WeChat URLs (none in this set) "
              "would fall back to direct HTTP / Playwright.")
    if not needs_live_run:
        print("REPLAY ONLY: no model or browser calls will be made; this is not fresh validation.")

    if refresh_slugs:
        for slug in refresh_slugs:
            (_OUT_DIR / f"_skill_ten_url_{slug}.json").unlink(missing_ok=True)
    print(f"RUN_SKILL_TEN_URL={'1' if _LIVE_ENABLED else 'unset'}  "
          f"LLM_API_KEY={'set' if _has_llm_key() else 'MISSING'}  "
          f"urls={len(urls)} (limit={limit})")
    print(f"SKILL_DIR={SKILL_DIR}")

    agent = None
    if needs_live_run:
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
    summary_path = _summary_path(urls)
    for slug, company, url, real_count in urls:
        prior = _OUT_DIR / f"_skill_ten_url_{slug}.json"
        if prior.exists():  # Resumable: reuse a prior run's result.
            print(f"  [replay] {slug} (cached result; not fresh validation)", flush=True)
            record = _normalize_replayed_record(
                json.loads(prior.read_text(encoding="utf-8"))
            )
            rows.append(record)
        else:
            assert agent is not None
            record = _run_one(slug, company, url, real_count, agent)
            rows.append(record)
        _print_table(rows)
        summary_path.write_text(
            json.dumps({"rows": rows}, ensure_ascii=False, indent=2),
            encoding="utf-8")
    _print_table(rows)
    return 0


if __name__ == "__main__":
    # `deepagents` may spawn helper subprocesses with `text=True` but no
    # explicit encoding. On Windows that otherwise falls back to GBK and can
    # turn valid UTF-8 tool output into `None` / a downstream splitlines error.
    # Require an explicit interpreter flag rather than re-execing on Windows:
    # `os.execv` can detach the calling terminal's output pipe there, making a
    # paid eval appear to finish silently.
    if not sys.flags.utf8_mode:
        print(_utf8_requirement_message())
        sys.exit(2)
    sys.exit(_main())
