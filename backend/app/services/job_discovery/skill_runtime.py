"""Bounded ``create_deep_agent + Skill + tool + subagent`` discovery runtime."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import shlex
import subprocess
import sys
import time
import threading
from uuid import uuid4
from typing import Any

from langchain_core.tools import StructuredTool

from backend.app.config import Settings
from backend.app.services.job_discovery.schemas import (
    DiscoveryRunResult,
    DiscoveryTaskInput,
    NormalizedJobCandidate,
    PageEvidence,
)
from backend.app.services.job_discovery.role_preferences import (
    DEFAULT_ROLE_PREFERENCES,
    filter_candidates_for_preferences,
)
from backend.app.services.job_discovery.preference_expansion import (
    preference_search_terms,
)
from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore
from backend.app.services.job_discovery.skill_spec import (
    JOB_DISCOVERY_SCRIPTS,
    get_skill_spec,
)


#: Allowlist of scripts the job-discovery coordinator may invoke.  Sourced from
#: ``skill_spec`` so a parallel skill declares its own set without editing here.
_ALLOWED_SCRIPTS = JOB_DISCOVERY_SCRIPTS


@dataclass(frozen=True)
class SkillRuntimeResult:
    result: DiscoveryRunResult
    trace_steps: list[dict[str, Any]]
    artifact_root: Path
    coverage_verified: bool
    role_preferences: tuple[str, ...] = DEFAULT_ROLE_PREFERENCES
    preferred_candidates: list[NormalizedJobCandidate] = field(default_factory=list)
    # Every evidence-backed JD observed by the Skill.  ``result.candidates``
    # may be the smaller, immediately recommendable preference subset.
    discovered_candidates: list[NormalizedJobCandidate] = field(default_factory=list)


@dataclass
class SkillToolPolicy:
    """Per-run limits for the only executable Agent capability."""

    max_browse_calls: int = 2
    max_coverage_gate_calls: int = 1
    max_pages: int = 20
    max_candidates: int = 10
    script_timeout_seconds: int = 240
    interaction_browse_timeout_seconds: int = 90
    browse_calls: int = 0
    coverage_gate_calls: int = 0


class SkillDiscoveryRuntime:
    """Runs one task in an isolated bundled Skill clone.

    The only executable capability exposed to the model is an allowlisted
    helper-script tool. Candidate and evidence persistence are deliberately
    performed by the Worker, from files below this task's artifact root.
    """

    def __init__(
        self, settings: Settings, *, artifact_root: Path | None = None,
        object_store: Any | None = None, role_preferences: tuple[str, ...] = DEFAULT_ROLE_PREFERENCES,
    ) -> None:
        self.settings = settings
        # The default path resolves the job-discovery spec so the shared
        # artifact store and script tool exercise the extension point; a
        # parallel skill resolves its own spec the same way.
        self.spec = get_skill_spec("job-discovery")
        configured_root = getattr(settings, "job_discovery_skill_artifact_root", "var/job-discovery-skill")
        self.artifact_root = artifact_root or Path(configured_root)
        self.object_store = object_store
        self.role_preferences = role_preferences

    def run(self, task: DiscoveryTaskInput, *, task_id: str) -> SkillRuntimeResult:
        store = SkillArtifactStore(
            task_id, self.artifact_root, run_id=uuid4().hex,
            skill_name=self.spec.name, skill_source=self.spec.source_path,
        )
        skill_dir = store.prepare()
        try:
            self._invoke(task=task, skill_dir=skill_dir)
        except Exception as exc:  # agent infrastructure errors are not candidate data
            return SkillRuntimeResult(
                result=DiscoveryRunResult(status="failed", block_reason="skill_runtime_error", summary=f"Skill runtime failed: {type(exc).__name__}"),
                trace_steps=_read_trace(skill_dir), artifact_root=skill_dir, coverage_verified=False,
            )
        outcome = _result_from_artifacts(
            task, store,
            max_candidates=self.settings.job_discovery_max_candidates_per_task,
            role_preferences=self.role_preferences,
            preference_judge_llm=_build_preference_judge_llm(self.settings),
        )
        if self.object_store is None:
            return outcome
        try:
            published = store.publish_evidence(self.object_store)
        except Exception:
            return SkillRuntimeResult(
                result=DiscoveryRunResult(
                    status="needs_manual_review", block_reason="artifact_upload_failed",
                    evidence=outcome.result.evidence, candidates=outcome.result.candidates,
                    summary="Skill artifacts could not be retained in encrypted object storage",
                ),
                trace_steps=outcome.trace_steps, artifact_root=store.skill_dir,
                coverage_verified=False,
                role_preferences=self.role_preferences,
                preferred_candidates=outcome.preferred_candidates,
                discovered_candidates=outcome.discovered_candidates,
            )
        for evidence in outcome.result.evidence:
            relative = (evidence.metadata or {}).get("relative_path")
            if not isinstance(relative, str):
                continue
            uri = published.get(skill_dir / relative)
            if uri:
                evidence.metadata = {**(evidence.metadata or {}), "storage_uri": uri}
        return outcome

    def _invoke(self, *, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        """Invoke the validated DeepAgent topology for a single URL."""
        from deepagents import create_deep_agent
        from deepagents.backends import FilesystemBackend
        from deepagents.middleware.filesystem import FilesystemPermission
        from langchain_core.messages import HumanMessage
        from backend.app.services.job_discovery.llm_factory import build_job_discovery_llm

        tool = _script_tool(skill_dir, SkillToolPolicy(
            # This runtime is a targeted recommendation path.  A bounded
            # initial window keeps a very large public portal from spending
            # the entire task lease extracting unrelated openings before any
            # preference-matched JD can be delivered.  Coverage remains
            # explicitly unverified when the source has more pages.
            max_pages=min(50, self.settings.job_discovery_max_pages_per_task),
            max_candidates=self.settings.job_discovery_max_candidates_per_task,
            script_timeout_seconds=max(
                30, min(240, self.settings.job_discovery_task_timeout_seconds // 2),
            ),
            # Layered extraction may issue up to three browse calls: a
            # preference-targeted search (primary), then a parallel listing
            # crawl (fallback), then an optional declared-page-count expansion.
            max_browse_calls=3,
        ), allowed_scripts=self.spec.allowed_scripts)
        backend = FilesystemBackend(root_dir=str(skill_dir.parent), virtual_mode=True)
        permissions = [
            FilesystemPermission(operations=["read"], paths=["/job-discovery/**"]),
            FilesystemPermission(operations=["write"], paths=["/job-discovery/**"], mode="deny"),
        ]
        # The coordinator is deterministic.  An LLM supervisor repeatedly
        # deciding whether to dispatch its workers proved unreliable in live
        # runs; page-level extraction remains an isolated Agent subtask.
        task_company_name = _task_company_name(task)
        search_terms = preference_search_terms(self.role_preferences)
        page_files: list[Path] = []
        # Layered extraction: a preference-targeted search is the PRIMARY path.
        # Typing preference-derived terms into the portal search box returns a
        # small, on-target evidence set directly, instead of crawling the whole
        # portal.  ``first_match`` cascades specific -> broad (the full
        # preference before its broad role markers) and stops at the first term
        # that yields results.  A generic/unknown preference expands to no
        # terms and skips search, falling straight through to the full crawl.
        if search_terms:
            terms_arg = _search_terms_cli_value(search_terms)
            if terms_arg:
                tool.invoke({
                    "script": "browse",
                    "cli_args": (
                        f"{task.source_url} --mode search-interact --wait 1200 --max-pages 1 "
                        f"--max-cards 12 --search-terms {terms_arg} "
                        "--search-strategy first_match --fallback none --out output/evidence"
                    ),
                })
                page_files = list((skill_dir / "output" / "evidence" / "pages").glob("page_*.txt"))
        # FALLBACK: when the targeted search returned nothing (no search box, no
        # matches, or a preferenceless run), crawl the listing in parallel.
        # This preserves recall for portals whose search does not cooperate and
        # is the only path when no preference terms were derived.
        if not page_files:
            tool.invoke({
                "script": "browse",
                "cli_args": (
                    f"{task.source_url} --mode parallel-fetch --wait 800 --max-pages 10 "
                    "--out output/evidence"
                ),
            })
            page_files = list((skill_dir / "output" / "evidence" / "pages").glob("page_*.txt"))
            # Start with a small targeted window, then expand only when the
            # browser itself declares a modest finite page count.  This preserves
            # recall for ordinary 16-page portals without repeating ByteDance's
            # unbounded/unknown multi-thousand-listing crawl.
            declared_pages = _declared_total_pages(skill_dir)
            if page_files and declared_pages is not None and 10 < declared_pages <= 50:
                tool.invoke({
                    "script": "browse",
                    "cli_args": (
                        f"{task.source_url} --mode parallel-fetch --wait 800 "
                        f"--max-pages {declared_pages} --out output/evidence"
                    ),
                })
                page_files = list((skill_dir / "output" / "evidence" / "pages").glob("page_*.txt"))
        if not page_files:
            return
        # A targeted recommendation may safely use complete JDs from the
        # pages already observed even when pagination proves the whole portal
        # is larger than our budget.  The artifact result keeps coverage false;
        # clearing these candidates would turn an honest partial crawl into a
        # false zero-result regression.
        extractor_prompt = (
                "You are a JD extractor sub-agent. The coordinator gives you the exact text of one "
                "assigned page in the task message. Do not browse, call task, or call read_evidence: "
                "the text is already your complete source. Call run_skill_script(write_candidates) "
                "with cli_args '--out output/candidates/page_NN.json' and stdin containing only a "
                "JSON array of real JDs. Each object requires title and responsibilities or requirements. "
                "Use a company_name only when it is explicit in the supplied page or task context; "
                "otherwise use null. Use [校园招聘] as the default "
                "recruitment type. If no JD exists, write []. Do not call any tool after writing."
        )
        agent_pages: list[Path] = []
        for page in page_files:
            deterministic_candidates = (
                _public_json_candidates(page, task_company_name)
                or _detail_evidence_candidates(page, task_company_name)
                or _search_card_candidates(page, task_company_name)
            )
            if deterministic_candidates:
                for start in range(0, len(deterministic_candidates), 6):
                    batch = deterministic_candidates[start:start + 6]
                    suffix = " --append" if start else ""
                    tool.invoke({
                        "script": "write_candidates",
                        "cli_args": f"--out output/candidates/{page.stem}.json{suffix}",
                        # ASCII JSON avoids Windows subprocess text-mode
                        # surrogate corruption; write_candidates restores the
                        # original Unicode through json.loads.
                        "stdin": json.dumps(batch, ensure_ascii=True),
                    })
                continue
            agent_pages.append(page)

        def extract_page(page: Path) -> None:
            # DeepAgent instances keep graph state; one instance per parallel
            # page avoids cross-page message contamination while the shared
            # tool remains constrained to unique page output paths.
            try:
                extractor = create_deep_agent(
                    model=build_job_discovery_llm(self.settings), tools=[tool], backend=backend,
                    skills=["/"], permissions=permissions, name="jd_extractor_subagent",
                    system_prompt=extractor_prompt,
                )
                page_text = page.read_text(encoding="utf-8", errors="replace")[:50_000]
                extractor.invoke(
                    {"messages": [HumanMessage(content=(
                        f"Extract the following evidence into output/candidates/{page.stem}.json "
                        f"with task company context {task_company_name or 'unknown'}.\n\n"
                        f"--- BEGIN EVIDENCE ({page.relative_to(skill_dir).as_posix()}) ---\n"
                        f"{page_text}\n--- END EVIDENCE ---"
                    ))]},
                    config={"recursion_limit": 24},
                )
            except Exception:
                # A bounded extractor may exhaust its loop budget after writing
                # useful batches.  Preserve those files and let the manifest
                # gate make the authoritative completeness decision.
                pass
        # Page evidence is independent.  Four workers keep a 44-page public
        # portal bounded without making unbounded model requests or allowing
        # agents to share filesystem state.
        if agent_pages:
            with ThreadPoolExecutor(max_workers=min(4, len(agent_pages))) as pool:
                futures = [pool.submit(extract_page, page) for page in agent_pages]
                for future in as_completed(futures):
                    future.result()
            # A transient tool/model failure may leave one otherwise complete
            # pagination page short. Retry only pages whose browser-declared
            # capacity is not met, once, and never replace a better first pass.
            for page in agent_pages:
                expected = _expected_page_candidate_count(skill_dir, page)
                if expected is None or _page_candidate_count(skill_dir, page) >= expected:
                    continue
                candidate_path = skill_dir / "output" / "candidates" / f"{page.stem}.json"
                original = candidate_path.read_bytes() if candidate_path.is_file() else None
                original_count = _page_candidate_count(skill_dir, page)
                extract_page(page)
                if original is not None and _page_candidate_count(skill_dir, page) < original_count:
                    candidate_path.write_bytes(original)
        # The model is not trusted to correctly cite its input.  Each extractor
        # output belongs to exactly one evidence page, so attach that page's
        # immutable content hash before candidate files are merged.  This also
        # keeps the deterministic public-JSON route and the LLM route subject
        # to the same evidence contract.
        _bind_page_candidate_evidence(skill_dir)
        tool.invoke({
            "script": "deduplicate",
            "cli_args": "output/candidates/*.json --out output/candidates_merged.json",
        })
        _deduplicate_exact_body_candidates(skill_dir)
        tool.invoke({"script": "coverage_gate"})


_PUBLIC_JOB_BLOCK = re.compile(
    r"^=== PUBLIC JOB \d+ ===\n(\{.*?\})(?=\n=== PUBLIC JOB|\s*\Z)",
    re.MULTILINE | re.DOTALL,
)
_DETAIL_EVIDENCE_BLOCK = re.compile(
    r"^=== DETAIL \d+ \((?P<url>[^)]+)\) ===\n(?P<body>.*?)(?=^=== DETAIL |\Z)",
    re.MULTILINE | re.DOTALL,
)
_DETAIL_TITLE = re.compile(
    r"(?:职位名称|岗位名称)\s*\n+(?:职位名称|岗位名称)\s*\n+(?P<title>[^\n]+)",
)
_ROLE_TITLE_HINT = re.compile(r"(?:agent|ai|人工智能|大模型|智能体)", re.IGNORECASE)
_LOCATION_LINE = re.compile(r"(?:北京市|上海市|杭州市|深圳市|广州市|武汉市|珠海市|南京市|成都|北京|上海)")
_NON_TITLE_CARD_TEXT = re.compile(r"(?:岗位职责|职位描述|工作职责|\b(?:负责|参与|我们)\b|[：:。；;])")


def _task_company_name(task: DiscoveryTaskInput) -> str | None:
    """Return an explicit company field, never the technical source key.

    ``source_key`` identifies an import/source family and is not employer
    evidence.  Treating it as a company corrupts candidate attribution for
    user-submitted URLs and can influence later company-aware deduplication.
    """
    company_labels = {"公司", "公司名称", "企业", "企业名称", "招聘单位", "employer", "companyname"}
    for raw_field in task.record_fields:
        if not isinstance(raw_field, dict):
            continue
        label = next((raw_field.get(key) for key in ("field_name", "name", "label", "key", "column") if raw_field.get(key)), "")
        if not isinstance(label, str) or label.replace(" ", "").casefold() not in company_labels:
            continue
        value = next((raw_field.get(key) for key in ("value", "field_value", "text", "content") if raw_field.get(key) is not None), None)
        cleaned = _safe_utf8_text(value).strip()
        if 1 <= len(cleaned) <= 100:
            return cleaned
    return None


def _search_card_candidates(page: Path, company_name: str | None) -> list[dict[str, Any]]:
    """Recover complete JD cards rendered as title/location/body text.

    This is a format-based fallback for public search results: cards commonly
    expose a role title, a location line, then a full responsibility paragraph,
    but no JSON payload or detail URL.  It intentionally accepts only cards
    whose body is present; title-only listings remain excluded.
    """
    try:
        lines = [line.strip() for line in page.read_text(encoding="utf-8", errors="replace").splitlines()]
    except OSError:
        return []
    candidates: list[dict[str, Any]] = []
    for index, title in enumerate(lines):
        # Card titles are a compact single line.  Responsibility paragraphs
        # also contain role keywords, but treating them as a following card
        # creates fabricated title/body pairs when a page has many cards.
        if (
            len(title) > 100
            or not _ROLE_TITLE_HINT.search(title)
            or _NON_TITLE_CARD_TEXT.search(title)
            or re.match(r"^\d+[.、]", title)
        ):
            continue
        location_index = next(
            (cursor for cursor in range(index + 1, min(index + 4, len(lines))) if _LOCATION_LINE.search(lines[cursor])),
            None,
        )
        if location_index is None:
            continue
        after_location = lines[location_index + 1:min(location_index + 10, len(lines))]
        numbered = [value for value in after_location if re.match(r"^\d+[、.]", value)]
        body_parts: list[str] = numbered
        if not body_parts:
            for value in after_location:
                if value and not value.startswith(("团队介绍", "ByteIntern：", "日常实习：")):
                    body_parts.append(value)
                    break
        body = " ".join(body_parts).strip()
        if len(body) < 20:
            continue
        candidates.append({
            "title": _safe_utf8_text(title), "company_name": company_name,
            "locations": [lines[location_index]], "responsibilities": _safe_utf8_text(body),
            "recruitment_types": ["校园招聘"],
            "evidence_refs": [{"evidence_type": "public_search_card", "content_hash": hashlib.sha256(page.read_bytes()).hexdigest(), "relative_path": f"output/evidence/pages/{page.name}"}],
        })
    return candidates


def _public_json_candidates(page: Path, company_name: str | None) -> list[dict[str, Any]]:
    """Convert browser-observed public JSON job evidence without an LLM."""
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    candidates: list[dict[str, Any]] = []
    for match in _PUBLIC_JOB_BLOCK.finditer(text):
        try:
            raw = json.loads(match.group(1))
        except json.JSONDecodeError:
            continue
        title = _safe_utf8_text(raw.get("title"))
        responsibilities = _safe_utf8_text(raw.get("responsibilities"))
        if not isinstance(title, str) or not title.strip() or not isinstance(responsibilities, str) or not responsibilities.strip():
            continue
        location = _safe_utf8_text(raw.get("location"))
        page_company_name = _safe_utf8_text(
            raw.get("company_name") or raw.get("company") or raw.get("employer"),
        ).strip() or company_name
        candidates.append({
            "title": title.strip(),
            "company_name": page_company_name,
            "department": _safe_utf8_text(raw.get("department")) or None,
            "responsibilities": responsibilities.strip(),
            "locations": [location] if isinstance(location, str) and location.strip() else [],
            "recruitment_types": ["校园招聘"],
            "evidence_refs": [{
                "evidence_type": "public_json_job",
                "content_hash": hashlib.sha256(page.read_bytes()).hexdigest(),
                "relative_path": f"output/evidence/pages/{page.name}",
            }],
        })
    return candidates


def _detail_evidence_candidates(page: Path, company_name: str | None) -> list[dict[str, Any]]:
    """Extract browser-captured ``=== DETAIL`` blocks without an LLM.

    The bundled browser records every successfully opened job card in a stable
    evidence format.  Those blocks already contain the full JD, and delegating
    them back to an Agent is both slower and less reliable than preserving the
    observed title/body directly.  This is format-driven, not host-specific:
    any site using the Skill browser's detail-evidence writer benefits.
    """
    try:
        text = page.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    candidates: list[dict[str, Any]] = []
    for match in _DETAIL_EVIDENCE_BLOCK.finditer(text):
        body = match.group("body").strip()
        title_match = _DETAIL_TITLE.search(body)
        if title_match is None:
            continue
        title = _safe_utf8_text(title_match.group("title")).strip()
        if not title:
            continue
        # ``职位描述`` is the browser-visible JD boundary.  Keep the whole
        # section through the job-information footer: it is auditable source
        # text and satisfies the full-JD body requirement without guessing
        # nested section labels that vary by site.
        description_marker = body.find("职位描述")
        detail_text = body[description_marker + len("职位描述"):].strip() if description_marker >= 0 else ""
        if not detail_text:
            continue
        candidates.append({
            "title": title,
            "company_name": company_name,
            "description_text": detail_text,
            "responsibilities": detail_text,
            "locations": [],
            "recruitment_types": ["校园招聘"],
            "apply_url": match.group("url"),
            "confidence": 0.8,
            "evidence_refs": [],
            "normalization_warnings": [],
        })
    return candidates


def _safe_utf8_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return value.encode("utf-8", errors="replace").decode("utf-8")


def _script_tool(
    skill_dir: Path, policy: SkillToolPolicy, *,
    allowed_scripts: frozenset[str] | None = None,
) -> StructuredTool:
    trace_lock = threading.Lock()
    allowlist = allowed_scripts if allowed_scripts is not None else _ALLOWED_SCRIPTS

    def run_skill_script(script: str, cli_args: str = "", stdin: str = "") -> str:
        started = time.monotonic()
        script_failed = False
        if script not in allowlist:
            return f"ERROR: unsupported Skill script {script!r}"
        try:
            args = shlex.split(cli_args, posix=(sys.platform != "win32")) if cli_args else []
        except ValueError as exc:
            return f"ERROR: invalid cli_args: {exc}"
        if any(part == ".." or Path(part).is_absolute() for part in args if not part.startswith("http")):
            return "ERROR: Skill paths must be relative and stay under output/"
        if script == "browse":
            if policy.browse_calls >= policy.max_browse_calls:
                return "ERROR: browse call limit reached"
            policy.browse_calls += 1
            args = _bounded_browse_args(args, policy.max_pages)
        if script == "coverage_gate":
            if policy.coverage_gate_calls >= policy.max_coverage_gate_calls:
                return "ERROR: coverage_gate call limit reached"
            policy.coverage_gate_calls += 1
            # Coverage proof comes solely from immutable wrapper-written browse
            # metadata and persisted candidates.  Ignore Agent-controlled page
            # counts/terminal signals so it cannot self-attest a complete crawl.
            args = [
                "output/candidates_merged.json", "--manifest",
                "output/evidence/browse_metadata.json",
            ]
        if not _valid_output_args(script, args):
            return "ERROR: Skill output path is not allowed"
        script_path = skill_dir / "scripts" / f"{script}.py"
        if not script_path.is_file():
            return f"ERROR: Skill script missing: {script}"
        try:
            normalized_stdin = stdin
            if script == "write_candidates" and stdin:
                try:
                    normalized_stdin = json.dumps(json.loads(stdin), ensure_ascii=True)
                except json.JSONDecodeError:
                    # Preserve the writer's lenient recovery for partial model
                    # output; only valid JSON gets the Windows-safe transport.
                    pass
            timeout_seconds = policy.script_timeout_seconds
            # The coordinator's second pass is deliberately a different,
            # role-focused interaction path.  A repeat of the long listing
            # crawl after it has already timed out only burns the task budget;
            # keep this diagnostic recovery bounded so it can either recover
            # public cards quickly or leave a useful failure trace.
            if script == "browse" and _browse_mode(args) in {"interact", "search-interact"}:
                timeout_seconds = min(timeout_seconds, policy.interaction_browse_timeout_seconds)
            completed = subprocess.run(
                [sys.executable, str(script_path), *args], cwd=skill_dir,
                input=normalized_stdin or None,
                capture_output=True, text=True, encoding="utf-8", errors="replace",
                timeout=timeout_seconds,
            )
            output = (completed.stdout or "") + (("\n[stderr]\n" + completed.stderr[-2000:]) if completed.stderr else "")
            if completed.returncode:
                script_failed = True
                output += f"\n[exit code {completed.returncode}]"
        except subprocess.TimeoutExpired:
            script_failed = True
            output = f"ERROR: {script} timed out"
        if script == "browse":
            _write_json_from_output(skill_dir / "output" / "evidence" / "browse_metadata.json", output)
            _complete_browse_manifest(skill_dir)
        elif script == "coverage_gate":
            _write_json_from_output(skill_dir / "output" / "evidence" / "coverage_gate_result.json", output)
        with trace_lock:
            failed = output.startswith("ERROR:") or script_failed
            _append_trace(skill_dir, {
                "tool": script,
                "duration_ms": round((time.monotonic() - started) * 1000, 1),
                "status": "failed" if failed else "ok",
                # Keep a bounded, redacted tool failure reason in the durable
                # trace. Previously all failures collapsed to a bare status,
                # making live-site remediation guesswork.
                "error": output[:1000] if failed else None,
            })
        return output or "(tool returned no text)"

    return StructuredTool.from_function(run_skill_script, name="run_skill_script", description="Run an allowlisted job-discovery Skill script.")


def _build_preference_judge_llm(settings: Settings) -> Any | None:
    """Build the generic preference judge LLM, or None when unavailable.

    The judge is a progressive enhancement: when no model or credentials are
    configured (e.g. unit tests), the filter falls back to its deterministic
    stages.  Never a hard dependency on the LLM.
    """
    try:
        from backend.app.services.job_discovery.llm_factory import build_preference_judge_llm
        return build_preference_judge_llm(settings)
    except Exception:
        return None


def _result_from_artifacts(
    task: DiscoveryTaskInput, store: SkillArtifactStore, *, max_candidates: int,
    role_preferences: tuple[str, ...] = DEFAULT_ROLE_PREFERENCES,
    preference_judge_llm: Any | None = None,
) -> SkillRuntimeResult:
    skill_dir = store.skill_dir
    candidates = _read_candidates(skill_dir)
    evidence = _read_evidence(store, task.source_url)
    browse = _read_json(skill_dir / "output" / "evidence" / "browse_metadata.json")
    gate = _read_json(skill_dir / "output" / "evidence" / "coverage_gate_result.json")
    terminal = bool((browse or {}).get("terminal_evidence") or (browse or {}).get("terminal_signal"))
    gate_passed = bool((gate or {}).get("passed") or (gate or {}).get("coverage_verified"))
    page_extraction_complete = _all_evidence_pages_extracted(skill_dir)
    coverage_verified = terminal and gate_passed and page_extraction_complete
    # Discovery is a recommendation service, not a full-site archival crawl.
    # A complete JD and direct apply URL for a requested role is sufficient to
    # recommend it; full-site coverage remains diagnostic metadata only.
    preference_matches = filter_candidates_for_preferences(
        candidates, role_preferences, llm=preference_judge_llm,
    )
    source_apply_url = task.source_url if task.source_url.startswith(("https://", "http://")) else None
    recommended_candidates = []
    for candidate in preference_matches:
        if candidate.apply_url and candidate.apply_url.startswith(("https://", "http://")):
            recommended_candidates.append(candidate)
        elif source_apply_url is not None:
            recommended_candidates.append(replace(candidate, apply_url=source_apply_url))
    # The raw candidate ceiling protects persistence and later ranking from a
    # pathological listing response.  A preference hit must not bypass it.
    if len(candidates) > max_candidates:
        result = DiscoveryRunResult(status="needs_manual_review", block_reason="candidate_limit_exceeded", evidence=evidence, candidates=[], summary="Skill candidate count exceeded configured task limit")
        discovered_candidates: list[NormalizedJobCandidate] = []
    elif recommended_candidates:
        result = DiscoveryRunResult(
            status="succeeded", evidence=evidence, candidates=recommended_candidates,
            summary="Skill discovery found preference-matched jobs with JD and apply links",
        )
        discovered_candidates = candidates
    elif not page_extraction_complete:
        result = DiscoveryRunResult(status="needs_manual_review", block_reason="page_extraction_incomplete", evidence=evidence, candidates=candidates, summary="At least one evidence page has no extractor artifact")
        discovered_candidates = candidates
    elif not coverage_verified:
        result = DiscoveryRunResult(status="needs_manual_review", block_reason="coverage_unverified", evidence=evidence, candidates=candidates, summary="Skill artifacts did not prove complete coverage")
        discovered_candidates = candidates
    elif candidates:
        result = DiscoveryRunResult(status="succeeded", evidence=evidence, candidates=candidates, summary="Skill discovery completed with verified coverage")
        discovered_candidates = candidates
    else:
        result = DiscoveryRunResult(status="partial_success", evidence=evidence, candidates=[], summary="Skill discovery completed but found no publishable JD")
        discovered_candidates = candidates
    return SkillRuntimeResult(
        result=result, trace_steps=_read_trace(skill_dir), artifact_root=skill_dir,
        coverage_verified=coverage_verified, role_preferences=role_preferences,
        preferred_candidates=preference_matches, discovered_candidates=discovered_candidates,
    )


def _read_candidates(skill_dir: Path) -> list[NormalizedJobCandidate]:
    raw = _read_json(skill_dir / "output" / "candidates_merged.json")
    if not isinstance(raw, list):
        return []
    values: list[NormalizedJobCandidate] = []
    for item in raw:
        if not isinstance(item, dict) or not (item.get("responsibilities") or item.get("requirements")):
            continue
        values.append(NormalizedJobCandidate(**{key: item[key] for key in NormalizedJobCandidate.__dataclass_fields__ if key in item}))
    return values


def _bind_page_candidate_evidence(skill_dir: Path) -> None:
    """Attach the exact source-page hash to every per-page candidate.

    Candidate files are created per evidence page by the coordinator.  A
    candidate cannot legitimately cite a different page at this stage, so this
    deterministic binding prevents a sub-agent from omitting or inventing its
    evidence reference.  Malformed candidate files are left untouched; the
    writer/coverage checks then reject the incomplete extraction normally.
    """
    pages_dir = skill_dir / "output" / "evidence" / "pages"
    candidates_dir = skill_dir / "output" / "candidates"
    for page in sorted(pages_dir.glob("page_*.txt")):
        candidate_path = candidates_dir / f"{page.stem}.json"
        if not candidate_path.is_file():
            continue
        try:
            rows = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if not isinstance(rows, list):
            continue
        source_hash = hashlib.sha256(page.read_bytes()).hexdigest()
        source_ref = {
            "evidence_type": "skill_page_text",
            "content_hash": source_hash,
            "relative_path": f"output/evidence/pages/{page.name}",
        }
        changed = False
        for row in rows:
            if not isinstance(row, dict):
                continue
            refs = row.get("evidence_refs")
            if not isinstance(refs, list):
                refs = []
            if not any(
                isinstance(ref, dict) and ref.get("content_hash") == source_hash
                for ref in refs
            ):
                row["evidence_refs"] = [*refs, source_ref]
                changed = True
        if changed:
            candidate_path.write_text(
                json.dumps(rows, ensure_ascii=False), encoding="utf-8",
            )


def _deduplicate_exact_body_candidates(skill_dir: Path) -> None:
    """Merge only demonstrably identical URL-less postings after Skill dedup.

    We intentionally include normalized body text in the key: title-only
    merging loses legitimate same-title openings from different teams.
    """
    path = skill_dir / "output" / "candidates_merged.json"
    rows = _read_json(path)
    if not isinstance(rows, list):
        return
    kept: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        body = str(row.get("responsibilities") or row.get("requirements") or "")
        normalized_body = re.sub(r"[\W_]", "", body.casefold())
        key = (
            str(row.get("apply_url") or "").strip(),
            str(row.get("title") or "").strip(),
            str(row.get("department") or "").strip(),
            normalized_body,
        )
        if key in seen:
            continue
        seen.add(key)
        kept.append(row)
    if len(kept) != len(rows):
        path.write_text(json.dumps(kept, ensure_ascii=False), encoding="utf-8")


def _page_candidate_count(skill_dir: Path, page: Path) -> int:
    value = _read_json(skill_dir / "output" / "candidates" / f"{page.stem}.json")
    return len(value) if isinstance(value, list) else 0


def _expected_page_candidate_count(skill_dir: Path, page: Path) -> int | None:
    """Return an evidence-derived expected card count for a paginated page."""
    metadata = _read_json(skill_dir / "output" / "evidence" / "browse_metadata.json")
    if not isinstance(metadata, dict):
        return None
    size = metadata.get("size_val")
    total = metadata.get("listing_count")
    pages = metadata.get("total_pages")
    match = re.fullmatch(r"page_(\d+)", page.stem)
    if not (isinstance(size, int) and isinstance(total, int) and isinstance(pages, int) and match):
        return None
    number = int(match.group(1))
    if number < 1 or number > pages:
        return None
    if number < pages:
        return size
    remainder = total - size * (pages - 1)
    return remainder if 1 <= remainder <= size else size


def _browse_manifest_is_truncated(skill_dir: Path) -> bool:
    manifest = _read_json(skill_dir / "output" / "evidence" / "browse_metadata.json")
    if not isinstance(manifest, dict):
        return False
    if manifest.get("truncated_by_max_pages"):
        return True
    declared = manifest.get("declared_total_pages")
    collected = manifest.get("pages_collected")
    return isinstance(declared, int) and isinstance(collected, int) and declared > collected


def _declared_total_pages(skill_dir: Path) -> int | None:
    manifest = _read_json(skill_dir / "output" / "evidence" / "browse_metadata.json")
    if not isinstance(manifest, dict):
        return None
    value = manifest.get("declared_total_pages")
    return value if isinstance(value, int) and value > 0 else None


def _write_empty_page_extractions(skill_dir: Path, pages: list[Path]) -> None:
    """Make an explicit, auditable no-extraction result for a known truncation."""
    candidates_dir = skill_dir / "output" / "candidates"
    candidates_dir.mkdir(parents=True, exist_ok=True)
    for page in pages:
        (candidates_dir / f"{page.stem}.json").write_text("[]", encoding="utf-8")


def _all_evidence_pages_extracted(skill_dir: Path) -> bool:
    pages = sorted((skill_dir / "output" / "evidence" / "pages").glob("page_*.txt"))
    if not pages:
        # The coverage gate reports this separately as no_page_evidence.
        return True
    candidates_dir = skill_dir / "output" / "candidates"
    return all((candidates_dir / f"{page.stem}.json").is_file() for page in pages)


def _read_evidence(store: SkillArtifactStore, url: str) -> list[PageEvidence]:
    evidence: list[PageEvidence] = []
    for artifact in store.iter_evidence():
        try:
            text = artifact.path.read_text(encoding="utf-8", errors="replace") if artifact.path.suffix.lower() in {".txt", ".json", ".jsonl"} else ""
        except OSError:
            continue
        evidence.append(PageEvidence(evidence_type=artifact.evidence_type, url=url, content_hash=hashlib.sha256(artifact.path.read_bytes()).hexdigest(), text_excerpt=text[:4000] or None, metadata={"storage_uri": store.artifact_uri(artifact.path), "relative_path": artifact.relative_path}))
    return evidence


def _read_trace(skill_dir: Path) -> list[dict[str, Any]]:
    path = skill_dir / "output" / "evidence" / "tool_trace.jsonl"
    if not path.is_file():
        return []
    items: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            raw = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(raw, dict):
            items.append({"tool": raw.get("tool", raw.get("script", "skill")), "status": raw.get("status", "ok"), "duration_ms": raw.get("duration_ms", 0), "params": {}})
    return items


def _append_trace(skill_dir: Path, entry: dict[str, Any]) -> None:
    path = skill_dir / "output" / "evidence" / "tool_trace.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def _read_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _write_json_from_output(path: Path, output: str) -> None:
    """Persist the last structured helper response without model narration."""
    decoder = json.JSONDecoder()
    last: dict[str, Any] | None = None
    for index, char in enumerate(output):
        if char != "{":
            continue
        try:
            value, _ = decoder.raw_decode(output[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            last = value
    if last is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(last, ensure_ascii=False), encoding="utf-8")


def _complete_browse_manifest(skill_dir: Path) -> None:
    """Bind browser metadata to the evidence files actually written on disk.

    Some browse modes report pagination counters but omit a ``page_files``
    array.  The Agent must never supply that provenance.  The wrapper can
    derive it safely from the task-scoped evidence directory after browse has
    completed, and can attest a terminal pagination signal only when the
    browser's own collected/total counters agree.
    """
    path = skill_dir / "output" / "evidence" / "browse_metadata.json"
    metadata = _read_json(path)
    if not isinstance(metadata, dict):
        return
    pages_dir = skill_dir / "output" / "evidence" / "pages"
    pages = sorted(pages_dir.glob("page_*.txt"))
    # Search/interact modes legitimately produce one consolidated evidence
    # document (``text_path``) rather than a paginated page file.  Normalize
    # that browser-owned artifact into the same page contract consumed by the
    # extractor; otherwise a successful targeted fallback is silently treated
    # as "no evidence" and cannot produce any recommendation.
    if not pages:
        text_path = metadata.get("text_path")
        if isinstance(text_path, str):
            source = Path(text_path)
            if not source.is_absolute():
                source = skill_dir / source
            try:
                source = source.resolve()
                evidence_root = (skill_dir / "output" / "evidence").resolve()
                source.relative_to(evidence_root)
                if source.is_file():
                    pages_dir.mkdir(parents=True, exist_ok=True)
                    normalized = pages_dir / "page_001.txt"
                    normalized.write_bytes(source.read_bytes())
                    pages = [normalized]
            except OSError:
                pass
    if not pages:
        # Some browser modes emit a progress JSON object after the final result,
        # so stdout parsing can retain metadata without ``text_path`` even
        # though the browser has already persisted its immutable text evidence.
        # The task-scoped evidence directory is authoritative in that case.
        evidence_root = skill_dir / "output" / "evidence"
        sources = sorted(
            (path for path in evidence_root.glob("*.txt") if path.name != "tool_trace.txt"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )
        if sources:
            try:
                pages_dir.mkdir(parents=True, exist_ok=True)
                normalized = pages_dir / "page_001.txt"
                normalized.write_bytes(sources[0].read_bytes())
                pages = [normalized]
            except OSError:
                pass
    if pages:
        metadata["page_files"] = [
            page.relative_to(skill_dir).as_posix() for page in pages
        ]
        if not isinstance(metadata.get("listing_count"), int):
            # Read a public aggregate from evidence, never from an Agent claim.
            try:
                first_page = pages[0].read_text(encoding="utf-8", errors="replace")
            except OSError:
                first_page = ""
            counts = [
                int(value) for value in re.findall(
                    r"(?:开启新的工作|在招职位|职位|岗位|results?|positions?)\s*[（(]\s*(\d{1,5})\s*[）)]",
                    first_page, re.IGNORECASE,
                )
            ]
            if counts:
                metadata["listing_count"] = max(counts)
    total = metadata.get("total_pages")
    collected = metadata.get("pages_collected")
    if (
        not metadata.get("terminal_evidence")
        and isinstance(total, int) and isinstance(collected, int)
        and total > 0 and total == collected
    ):
        metadata["terminal_evidence"] = "browser_reported_pages_exhausted"
    path.write_text(json.dumps(metadata, ensure_ascii=False), encoding="utf-8")


def _bounded_browse_args(args: list[str], max_pages: int) -> list[str]:
    """Clamp model-controlled page count to the task configuration."""
    bounded = list(args)
    if "--max-pages" in bounded:
        index = bounded.index("--max-pages")
        if index + 1 < len(bounded):
            try:
                bounded[index + 1] = str(min(max_pages, max(1, int(bounded[index + 1]))))
            except ValueError:
                bounded[index + 1] = str(max_pages)
    else:
        bounded.extend(["--max-pages", str(max_pages)])
    return bounded


def _browse_mode(args: list[str]) -> str | None:
    """Return the explicit browse mode without trusting any other argument."""
    try:
        index = args.index("--mode")
    except ValueError:
        return None
    return args[index + 1] if index + 1 < len(args) else None


def _search_terms_cli_value(terms: list[str]) -> str:
    """Build the comma-separated ``--search-terms`` value for browse.py.

    Terms containing a space are dropped: browse.py splits ``--search-terms``
    on commas and the coordinator passes ``cli_args`` through shlex, where a
    space would fragment a multi-word term.  Space-free preference-derived
    terms (the common Chinese case) are unaffected; the rare English
    multi-word marker (e.g. ``product manager``) is skipped rather than
    corrupted.
    """
    return ",".join(term for term in terms if " " not in term)


def _valid_output_args(script: str, args: list[str]) -> bool:
    values: list[str] = []
    for index, arg in enumerate(args):
        if arg == "--out":
            if index + 1 >= len(args):
                return False
            values.append(args[index + 1])
        elif arg.startswith("--out="):
            values.append(arg.split("=", 1)[1])
    if not values:
        return True
    if len(values) != 1:
        return False
    output = values[0].replace("\\", "/")
    if script == "browse":
        return output == "output/evidence"
    if script == "deduplicate":
        return output == "output/candidates_merged.json"
    return output.startswith("output/")
