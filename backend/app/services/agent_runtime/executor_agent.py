"""Autonomous Executor role for the adaptive PEV runtime."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
from typing import Any
from urllib.parse import urlsplit

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
from backend.app.services.agent_runtime.model_budget import (
    ModelCallBudget,
    estimate_input_tokens,
)
from backend.app.services.agent_runtime.prompt_rules import (
    COMMON_RUNTIME_RULES,
    EXECUTOR_RUNTIME_RULES,
)
from backend.app.services.agent_runtime.observation_projection import (
    observation_for_decision,
    record_observation,
    summarize_observations,
)
from backend.app.services.agent_runtime.schemas import (
    AgentTaskRequest,
    ExecutionPlan,
    ExecutorDecision,
    ExecutorResult,
    PlanStep,
    ToolObservation,
)
from backend.app.services.agent_runtime.skill_definition import SkillRegistry
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.context_manifest import (
    build_context_manifest,
    compute_evidence_chars,
)
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

logger = logging.getLogger(__name__)

# Generic runtime prompt. Career-specific capture, matching, tailoring, and
# planning rules live in canonical ``skill/*/SKILL.md`` packages.
_EXECUTOR_INSTRUCTION = (
    "## 角色\n"
    "You are the Executor role in a generic Planner-Executor-Verifier runtime.\n"
    "## 行为规则\n"
    "Work only toward the current step, use only advertised tools and Skill "
    "authority, inspect every observation, reuse successful calls, do not repeat a "
    "doomed call, honor typed step inputs and prior artifacts, and use confirmed "
    "private context when the activated Skill exposes it instead of asking the user "
    "to repeat server-held facts.\n"
    "## 流程\n"
    "Never claim an artifact absent from tool-backed observations. If evidence is "
    "blocked or a required input is unavailable, return a precise needs_user handoff.\n"
    "## 输出契约\n"
    "Choose complete only when the activated Skill contract and the step success "
    "criteria are satisfied.\n"
    "## 禁止项\n"
    "Do not invent evidence, bypass access controls, or perform an irreversible "
    "external action."
    "\n\n"
    + COMMON_RUNTIME_RULES
    + EXECUTOR_RUNTIME_RULES
)

# The executor is allowed a few identical re-issues before the harness
# concludes the loop is stalled. Consecutive no-progress decisions (deduped
# re-calls or blocked public search) consume turns but never produce new
# evidence; after this cap the agent stops and hands control to the human.
_MAX_CONSECUTIVE_STALLS = 3

# Per-step TOTAL wasted turns (NOT reset by interspersed success). Counts
# duplicate_tool_call + candidate_urls_already_supplied + any turn whose
# tool call did not append a new succeeded observation (catches alternating
# no-progress like search-empty/fetch-fail). After this cap the harness
# hands the step to the user: sustained no-progress even interspersed with
# success burns the wall clock without converging on the outcome.
_MAX_TOTAL_WASTED_TURNS = 3

# Stable failure codes whose identical re-issue is a doomed repeat: the sheet
# API is rate-limited or down (sheet_rate_limited / sheet_call_failed), the
# step's skill permanently excludes the tool (tool_skill_forbidden), the tool
# does not exist (unknown_tool), or the payload failed deterministic schema
# validation (invalid_tool_input). An identical re-issue of such a call is
# rejected as duplicate_tool_call WITHOUT incrementing total_wasted_turns and
# WITHOUT consuming budget, mirroring the succeeded-call dedup: an external
# rate limit must never be mislabeled as model waste, and a deterministic
# schema mismatch cannot be fixed by repeating the same payload (lenient input
# coercion handles the correct shape at the tool boundary; a genuinely new bad
# shape still counts once each toward the total-waste cap). Transient failures
# (tool_execution_failed) and blocked codes (login_required etc.) are NOT
# recorded, so a legitimate retry and a blocked-flow handoff keep today's
# behavior.
_STABLE_FAILURE_ERROR_CODES = frozenset(
    {
        "sheet_rate_limited",
        "sheet_call_failed",
        "tool_skill_forbidden",
        "unknown_tool",
        "invalid_tool_input",
        "route_already_consumed",
    }
)

# Once a source route has declared a run-wide outage (for example the daily
# smartsheet quota), changing query parameters cannot make that same route
# healthy.  Persist these names across verifier retries so the model can only
# choose an actual fallback route, never burn calls on a different payload.
_RUN_WIDE_UNAVAILABLE_CODES = frozenset(
    {
        "sheet_rate_limited",
        "sheet_call_failed",
        "sheet_bridge_unavailable",
        "route_already_consumed",
    }
)

# Per-URL failure codes that make a user-supplied candidate unusable for this
# run.  Access-blocked candidates are included deliberately: search fallback
# is safe only after the blocked domain is added to the run circuit breaker,
# which prevents the fallback from selecting the same blocked host again.
_CANDIDATE_FAILURE_ERROR_CODES = frozenset(
    {
        "public_fetch_failed",
        "empty_public_page",
        "public_page_content_insufficient",
        "dead_link",
        "anti_bot_challenge",
        "access_denied",
        "login_required",
        "captcha",
        "domain_temporarily_blocked",
    }
)

# Fetch tools whose failures carry per-URL attribution for the candidate-death
# ledger: batch fetches report each URL in output.failures (durable across
# verifier RETRY through the merged observations), single fetches are
# attributed in-flight from the decision payload.
_FETCH_TOOL_NAMES = frozenset({"fetch-public-job-pages", "fetch-public-job-page"})

# Dynamic listing pages may return a fresh content hash on every request even
# when no new job evidence was produced.  Bound repeated successful attempts
# to the same route so changing query parameters cannot consume the whole
# wall-clock budget.  A batch containing a genuinely new route remains legal.
_MAX_SUCCESSFUL_FETCH_ATTEMPTS_PER_ROUTE = 4

# Verifier-feedback fragments name the missing deliverable's tool. Fragments
# naming a tool outside this step's skill scope cannot be honored by any
# allowed tool; injecting them only pushes the executor toward a
# tool_skill_forbidden drift (R013 loop). The filter drops ONLY the
# tool-naming fragments and keeps in-domain content, so the executor still
# sees the verifier's substantive feedback. Boundaries are Chinese
# punctuation and newlines only: a period appears inside URLs
# (https://jobs.example/x.y), so splitting on "." would tear evidence URLs
# apart.
_FEEDBACK_FRAGMENT_BOUNDARIES = re.compile(r"[。！？；\n]")

# Compact character budget for each already_succeeded_calls entry's
# input_summary: enough for a URL list or query, not enough to bloat the
# decision state with large payloads.
_SUCCEEDED_INPUT_SUMMARY_CHARS = 200

# Cap the projected already_succeeded_calls list so a runaway step cannot
# bloat the decision state. The harness's dedup check is authoritative
# regardless of this projection, so capping only affects model awareness.
_MAX_PROJECTED_SUCCEEDED_CALLS = 10

# Cap the execution_state entries persisted across verifier RETRY
# re-invocations. The model projection stays bounded by
# _MAX_PROJECTED_SUCCEEDED_CALLS per invocation, but the authoritative dedup
# set must survive a long step without unbounded growth.
_MAX_PERSISTED_SUCCEEDED_CALLS = 40

# Human-readable questions for the two waste caps. The consecutive-stall cap
# fires on 3 identical no-progress decisions in a row; the total-waste cap
# fires on 3 wasted turns interspersed with success (R004 pattern) or
# alternating no-progress (Q057 pattern).
_CANDIDATE_URLS_STALL_QUESTION = (
    "连续尝试无效工具调用仍未取得进展。请人工确认当前已收集的岗位产出，"
    "或提供更精确的岗位页面后重试。"
)
_DUPLICATE_STALL_QUESTION = (
    "连续重复调用同一工具未产生新结果，无法继续自动完成该步骤。"
    "请人工确认当前产出，或补充缺失的岗位页面/信息后重试。"
)
_TOTAL_WASTE_QUESTION = (
    "累计多次无效或重复的工具调用未取得进展，无法继续自动完成该步骤。"
    "请人工确认当前产出，或补充缺失的岗位页面/信息后重试。"
)
_SOURCE_UNAVAILABLE_QUESTION = (
    "一个招聘来源在本次运行中已确认不可用，继续更换参数也不会恢复。"
    "请使用其他公开来源，或提供可访问的岗位页面/文本后重试。"
)


def _summarize_succeeded_call(
    tool_name: str, tool_input: dict[str, Any]
) -> dict[str, str]:
    """Compact identifier for an already-succeeded (tool, input) pair.

    The model needs enough to recognise "I already called this tool with
    these arguments" without seeing the full (possibly large) payload. The
    input is serialised to a compact JSON string and truncated; only the
    most recent ``_MAX_PROJECTED_SUCCEEDED_CALLS`` entries are projected.
    """
    input_repr = json.dumps(tool_input, ensure_ascii=False, separators=(",", ":"))
    if len(input_repr) > _SUCCEEDED_INPUT_SUMMARY_CHARS:
        input_repr = input_repr[: _SUCCEEDED_INPUT_SUMMARY_CHARS - 3] + "..."
    return {"tool": tool_name, "input_summary": input_repr}


def _input_hash(tool_input: dict[str, Any]) -> str:
    """Canonical SHA-256 of a tool input for cross-invocation dedup.

    The hash is stable across dict key order and unicode escapes, so an
    identical call issued by a later Executor invocation of the same step is
    recognized even though its payload dict may be rebuilt differently.
    """
    canonical = json.dumps(
        tool_input, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _carried_counter(value: object, *, default: int = 0) -> int:
    """Defensive bounded read of a counter carried across invocations."""
    if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
        return value
    return default


def _load_execution_state(
    task: AgentTaskRequest,
) -> tuple[list[dict[str, str]], int, int]:
    """Read the persisted cross-invocation state for this step, if any.

    Returns (prior_succeeded_calls, consecutive_stalls, total_wasted_turns)
    from ``task.execution_state``, which the runtime populated on a verifier
    RETRY. Malformed entries are dropped; counters are clamped defensively.
    """
    state = task.execution_state or {}
    prior_succeeded_calls: list[dict[str, str]] = []
    for entry in state.get("succeeded_calls", []):
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        digest = entry.get("hash")
        summary = entry.get("input_summary")
        if not isinstance(tool, str) or not isinstance(digest, str) or not digest:
            continue
        prior_succeeded_calls.append(
            {
                "tool": tool,
                "hash": digest,
                "input_summary": summary if isinstance(summary, str) else "",
            }
        )
    consecutive_stalls = _carried_counter(state.get("consecutive_stalls"))
    total_wasted_turns = _carried_counter(state.get("total_wasted_turns"))
    return prior_succeeded_calls, consecutive_stalls, total_wasted_turns


def _load_failed_candidate_urls(state: object) -> set[str]:
    """Read the durable single-fetch candidate-death ledger."""
    if not isinstance(state, dict):
        return set()
    raw = state.get("failed_candidate_urls")
    if not isinstance(raw, list):
        return set()
    return {url.strip() for url in raw if isinstance(url, str) and url.strip()}


def _load_stable_failed_calls(task: AgentTaskRequest) -> list[dict[str, str]]:
    """Read the persisted stable-failure dedup entries for this step, if any.

    Mirrors ``_load_execution_state`` for the succeeded-call set: entries
    carry ``(tool, hash)`` and malformed entries are dropped. The runtime
    carries them on a verifier RETRY, so an identical re-issue of a doomed
    call (rate-limited sheet API, forbidden tool, unknown tool) is deduped
    across invocations instead of re-hitting the failure in every re-run.
    """
    state = task.execution_state or {}
    prior: list[dict[str, str]] = []
    for entry in state.get("stable_failed_calls", []):
        if not isinstance(entry, dict):
            continue
        tool = entry.get("tool")
        digest = entry.get("hash")
        if not isinstance(tool, str) or not isinstance(digest, str) or not digest:
            continue
        prior.append({"tool": tool, "hash": digest})
    return prior


def _validation_signature(error_message: str | None) -> str | None:
    """Return a stable field/type signature without retaining submitted data."""
    if not isinstance(error_message, str):
        return None
    matches = re.findall(r"([A-Za-z_][A-Za-z0-9_.]*):\s*([a-z][a-z0-9_]*)", error_message)
    if not matches:
        return None
    return ";".join(f"{field}:{error_type}" for field, error_type in matches[:4])


def _load_invalid_input_signatures(task: AgentTaskRequest) -> list[str]:
    raw = (task.execution_state or {}).get("invalid_input_signatures", [])
    if not isinstance(raw, list):
        return []
    return [value for value in raw if isinstance(value, str) and value][:20]


def _load_unavailable_tools(task: AgentTaskRequest) -> set[str]:
    raw = (task.execution_state or {}).get("unavailable_tools", [])
    if not isinstance(raw, list):
        return set()
    return {value for value in raw if isinstance(value, str) and value}


def _snapshot_execution_state(
    *,
    succeeded_calls: list[tuple[str, dict[str, Any]]],
    prior_succeeded_calls: list[dict[str, str]],
    consecutive_stalls: int,
    total_wasted_turns: int,
    stable_failed_calls: list[tuple[str, dict[str, Any]]] | None = None,
    prior_stable_failed_calls: list[dict[str, str]] | None = None,
    failed_candidate_urls: set[str] | None = None,
    phase: str = "discover",
    candidate_status: str = "unknown",
    last_error_fingerprint: str | None = None,
    terminal_reason: str | None = None,
    blocked_public_domains: list[str] | None = None,
    public_search_query_hashes: list[str] | None = None,
    invalid_input_signatures: list[str] | None = None,
    unavailable_tools: set[str] | list[str] | None = None,
) -> dict[str, Any]:
    """Persistable execution state carried across verifier RETRY re-invocations.

    The runtime stores this on the task for the next Executor invocation of
    the same step, so the succeeded-call dedup set, the stable-failure dedup
    set and the per-invocation waste counters survive a verifier RETRY
    instead of restarting from zero (which tripled the effective waste
    budget and blinded dedup across retries). Entries are capped at
    ``_MAX_PERSISTED_SUCCEEDED_CALLS``, keeping the most recent calls.
    """
    entries = [
        {
            "tool": entry["tool"],
            "hash": entry["hash"],
            "input_summary": entry["input_summary"],
        }
        for entry in prior_succeeded_calls
    ]
    for name, payload in succeeded_calls:
        summary = _summarize_succeeded_call(name, payload)
        summary["hash"] = _input_hash(payload)
        entries.append(summary)
    if len(entries) > _MAX_PERSISTED_SUCCEEDED_CALLS:
        entries = entries[-_MAX_PERSISTED_SUCCEEDED_CALLS:]
    stable_entries = [
        {"tool": entry["tool"], "hash": entry["hash"]}
        for entry in (prior_stable_failed_calls or [])
    ]
    for name, payload in stable_failed_calls or []:
        stable_entries.append({"tool": name, "hash": _input_hash(payload)})
    if len(stable_entries) > _MAX_PERSISTED_SUCCEEDED_CALLS:
        stable_entries = stable_entries[-_MAX_PERSISTED_SUCCEEDED_CALLS:]
    return {
        "succeeded_calls": entries,
        "stable_failed_calls": stable_entries,
        "consecutive_stalls": consecutive_stalls,
        "total_wasted_turns": total_wasted_turns,
        "failed_candidate_urls": sorted(failed_candidate_urls or set()),
        "progress_ledger": {
            "phase": phase,
            "candidate_status": candidate_status,
            "last_error_fingerprint": last_error_fingerprint,
            "terminal_reason": terminal_reason,
        },
        "blocked_public_domains": sorted(
            domain for domain in (blocked_public_domains or [])
            if isinstance(domain, str) and domain
        ),
        "public_search_query_hashes": sorted(
            value for value in (public_search_query_hashes or [])
            if isinstance(value, str) and value
        ),
        "invalid_input_signatures": list(dict.fromkeys(
            value for value in (invalid_input_signatures or [])
            if isinstance(value, str) and value
        ))[-20:],
        "unavailable_tools": sorted(
            value for value in (unavailable_tools or [])
            if isinstance(value, str) and value
        ),
    }


def _progress_ledger(
    observations: list[ToolObservation],
    *,
    total_wasted_turns: int,
    candidate_urls: set[str],
    failed_candidate_urls: set[str],
) -> dict[str, object]:
    """Expose a small typed progress summary instead of raw counters alone."""
    last_failure = next(
        (
            f"{observation.tool_name}:{observation.error_code}"
            for observation in reversed(observations)
            if observation.status == "failed" and observation.error_code
        ),
        None,
    )
    if total_wasted_turns >= _MAX_TOTAL_WASTED_TURNS:
        terminal_reason = "no_progress"
    elif last_failure in {
        "fetch-public-job-pages:anti_bot_challenge",
        "fetch-public-job-page:anti_bot_challenge",
        "query-career-sheet-records:sheet_rate_limited",
        "query-career-sheet-records:sheet_call_failed",
        "query-career-sheet-records:sheet_bridge_unavailable",
        "query-career-sheet-records:source_unavailable",
    }:
        terminal_reason = "external_blocked"
    else:
        terminal_reason = None
    if candidate_urls and candidate_urls.issubset(failed_candidate_urls):
        candidate_status = "all_unusable"
    elif candidate_urls & failed_candidate_urls:
        candidate_status = "partially_processed"
    elif candidate_urls:
        candidate_status = "supplied"
    else:
        candidate_status = "unknown"
    tool_names = {observation.tool_name for observation in observations}
    phase = (
        "deliver" if "match-observed-jobs" in tool_names
        else "extract" if any("extract" in name for name in tool_names)
        else "capture" if any("fetch" in name for name in tool_names)
        else "discover"
    )
    return {
        "phase": phase,
        "candidate_status": candidate_status,
        "last_error_fingerprint": last_failure,
        "terminal_reason": terminal_reason,
    }


def _candidate_search_is_authorized(
    candidate_urls: frozenset[str], failed_candidate_urls: set[str]
) -> bool:
    """Allow public search only after every supplied candidate is unusable."""
    return bool(candidate_urls) and candidate_urls.issubset(failed_candidate_urls)


class ExecutorAgent:
    """Bounded perceive–decide–act–observe loop for a single plan step."""

    def __init__(
        self,
        *,
        gateway: AgentModelGateway,
        tools: ToolRegistry,
        skills: SkillRegistry | None = None,
    ) -> None:
        self._gateway = gateway
        self._tools = tools
        self._skills = skills

    def _scoped_out_tool_names(self, allowed_skills: frozenset[str]) -> frozenset[str]:
        """Tool names this step's skill scope can never invoke.

        The catalog advertises only in-scope tools (and ``invoke`` rejects
        anything else as ``tool_skill_forbidden``), so the difference between
        the executor universe and the scoped catalog is exactly the set of
        names the executor must never act on -- including demands that arrive
        through verifier feedback (W3).
        """
        catalog_names = {
            entry["name"]
            for entry in self._tools.tool_catalog(
                role=AgentRole.executor, allowed_skills=allowed_skills
            )
        }
        universe_names = {
            entry["name"]
            for entry in self._tools.tool_catalog(
                role=AgentRole.executor, allowed_skills=None
            )
        }
        return frozenset(universe_names - catalog_names)

    def run(
        self,
        *,
        task: AgentTaskRequest,
        plan: ExecutionPlan,
        step: PlanStep,
        context: ToolContext,
        trace: DecisionTrace | None = None,
        tool_budget: ToolCallBudget | None = None,
        turn_budget: AgentTurnBudget | None = None,
        model_budget: ModelCallBudget | None = None,
        deadline: float | None = None,
        prior_observations: list[ToolObservation] | None = None,
    ) -> ExecutorResult:
        """Execute a step without precomputing its tool sequence in the harness."""
        # Production uses the LangChain chat model held by
        # LangChainModelGateway, so it receives the DeepAgents executor with
        # native progressive Skill disclosure. Deterministic gateway doubles
        # used by the existing unit suite do not expose a chat model and keep
        # the old seam for focused loop/budget tests.
        if (
            getattr(self._gateway, "chat_model", None) is not None
            or getattr(self._gateway, "_model", None) is not None
        ):
            from pathlib import Path

            from backend.app.services.agent_runtime.deep_executor import (
                DeepExecutorAgent,
            )

            return DeepExecutorAgent(
                gateway=self._gateway,
                tools=self._tools,
                skills=self._skills,
                skill_root=Path(__file__).resolve().parents[4] / "skill",
            ).run(
                task=task,
                plan=plan,
                step=step,
                context=context,
                trace=trace,
                tool_budget=tool_budget,
                turn_budget=turn_budget,
                model_budget=model_budget,
                deadline=deadline,
                prior_observations=prior_observations,
            )
        observations: list[ToolObservation] = []
        tool_context = context
        # Every (tool_name, tool_input) pair that already succeeded in this
        # invocation. Tracked separately because ToolObservation carries no
        # input; only succeeded calls are recorded so a failed call may be
        # legitimately retried later (including after other calls).
        succeeded_calls: list[tuple[str, dict[str, Any]]] = []
        # Every (tool_name, tool_input) pair whose prior attempt failed with a
        # stable error code (rate-limited sheet API, forbidden tool, unknown
        # tool). An identical re-issue of such a call is a doomed repeat:
        # rejected as duplicate_tool_call WITHOUT spending the total-waste
        # budget, mirroring the succeeded-call dedup. Transient failures and
        # blocked codes are never recorded, so a legitimate retry or a
        # blocked-flow handoff keeps today's behavior.
        stable_failed_calls: list[tuple[str, dict[str, Any]]] = []
        # Consecutive no-progress decisions (deduped re-calls / blocked search)
        # reset on any real tool execution, complete, or needs_user. Carried
        # across verifier RETRY re-invocations, like the total-waste counter.
        consecutive_stalls = _carried_counter(task.execution_state.get("consecutive_stalls"))
        total_wasted_turns = _carried_counter(task.execution_state.get("total_wasted_turns"))
        # Cross-invocation state carried by the runtime on a verifier RETRY:
        # the succeeded-call dedup set and the waste counters survive the
        # re-run instead of restarting from zero, so retries cannot re-spend
        # the waste budget or re-issue an identical call that already
        # succeeded in a prior invocation.
        prior_succeeded_calls, _, _ = _load_execution_state(task)
        prior_succeeded_hashes = {
            (entry["tool"], entry["hash"]) for entry in prior_succeeded_calls
        }
        prior_stable_failed_calls = _load_stable_failed_calls(task)
        prior_stable_failed_hashes = {
            (entry["tool"], entry["hash"]) for entry in prior_stable_failed_calls
        }
        invalid_input_signatures = _load_invalid_input_signatures(task)
        unavailable_tools = _load_unavailable_tools(task)

        def current_state() -> dict[str, Any]:
            """Snapshot this invocation's state for the runtime to carry on RETRY."""
            ledger = _progress_ledger(
                [*(prior_observations or []), *observations],
                total_wasted_turns=total_wasted_turns,
                candidate_urls=candidate_urls,
                failed_candidate_urls=failed_candidate_urls,
            )
            return _snapshot_execution_state(
                succeeded_calls=succeeded_calls,
                prior_succeeded_calls=prior_succeeded_calls,
                stable_failed_calls=stable_failed_calls,
                prior_stable_failed_calls=prior_stable_failed_calls,
                failed_candidate_urls=failed_candidate_urls,
                consecutive_stalls=consecutive_stalls,
                total_wasted_turns=total_wasted_turns,
                **ledger,
                blocked_public_domains=(
                    context.metadata.get("blocked_public_domains", [])
                    if isinstance(context.metadata.get("blocked_public_domains", []), list)
                    else []
                ),
                public_search_query_hashes=(
                    context.metadata.get("public_search_query_hashes", [])
                    if isinstance(context.metadata.get("public_search_query_hashes", []), list)
                    else []
                ),
                invalid_input_signatures=invalid_input_signatures,
                unavailable_tools=unavailable_tools,
            )
        # Loop-invariant projections of the (immutable) plan/step: serialize
        # once instead of re-dumping and re-building the tool catalog every
        # turn. The catalog is also memoized inside ToolRegistry.
        plan_json = plan.model_dump(mode="json")
        step_json = step.model_dump(mode="json")
        allowed_skills = frozenset(step.allowed_skills)
        available_tools = self._tools.tool_catalog(
            role=AgentRole.executor, allowed_skills=allowed_skills
        )
        premature_need_user_retries = 0
        runtime_feedback: str | None = None
        # Tools the universe exposes but this step's skill scope can never
        # call: verifier feedback naming one is filtered from the decision
        # state (W3), so a scoped-out demand can never push the executor
        # toward a tool_skill_forbidden drift.
        scoped_out_tool_names = self._scoped_out_tool_names(allowed_skills)
        # Candidate URLs the user supplied for this run. Public search stays
        # forbidden while ANY of them remains unfailed; only when every
        # candidate has failed with a fetch/dead-link error does search become
        # authorized (W2). Single-fetch failures carry no URL in the
        # observation, so they are attributed in-flight from the decision
        # payload; batch failures are read from output.failures (durable
        # across verifier RETRY through the merged observation list).
        candidate_urls = {url for url in _candidate_urls(task)}
        failed_candidate_urls = _load_failed_candidate_urls(task.execution_state)
        prior_observations_for_decision = [
            observation_for_decision(observation)
            for observation in (prior_observations or [])
        ]
        # Decision projections are appended once per observation in lockstep
        # with the raw list, instead of re-projecting every observation each
        # turn (O(turns^2) ``model_dump`` calls on observations that may carry
        # large page text). Each turn reads a fresh shallow copy (the gateway
        # only serializes it).
        observations_for_decision: list[dict[str, object]] = []
        for _turn in range(task.budget.max_agent_turns):
            if deadline is not None and time.monotonic() >= deadline:
                return ExecutorResult(
                    status="failed",
                    summary="Wall-clock budget exhausted before the next decision.",
                    observations=observations,
                    error_code="wall_clock_budget_exhausted",
                    execution_state=current_state(),
                )
            if turn_budget is not None and not turn_budget.try_consume():
                return ExecutorResult(
                    status="failed",
                    summary="Agent-turn budget exhausted before the next decision.",
                    observations=observations,
                    error_code="agent_turn_budget_exhausted",
                    execution_state=current_state(),
                )
            # Bound the observation list the model sees: keep the most-recent
            # projections full (as projected per-item by observation_for_decision)
            # and collapse older ones to identifier-only summary lines when the
            # accumulated list exceeds the character budget. The model still sees
            # every observation's identity, but the oldest ones lose their
            # visible_text/pages/details so the list stays bounded in long chains.
            summarized_observations = summarize_observations(observations_for_decision)
            decision_state = {
                "goal": task.goal,
                "context": task.context,
                "private_context": (
                    self._skills.project_private_context(
                        step.allowed_skills, task.private_context
                    )
                    if self._skills is not None
                    else task.private_context
                ),
                "skill_policy": (
                    self._skills.prompt_policy(step.allowed_skills)
                    if self._skills is not None
                    else ""
                ),
                "remaining_tool_calls": (
                    tool_budget.remaining
                    if tool_budget is not None
                    else task.budget.max_tool_calls - len(observations)
                ),
                "remaining_agent_turns": (
                    turn_budget.remaining
                    if turn_budget is not None
                    else task.budget.max_agent_turns - _turn - 1
                ),
                "plan": plan_json,
                "step": step_json,
                "available_tools": available_tools,
                "observations": summarized_observations,
                "prior_observations": prior_observations_for_decision,
                "verifier_feedback": _scope_feedback_to_step_catalog(
                    task.context.get("verifier_feedback", []),
                    scoped_out_tool_names=scoped_out_tool_names,
                ),
                "already_succeeded_calls": [
                    *[
                        {
                            "tool": entry["tool"],
                            "input_summary": entry["input_summary"],
                        }
                        for entry in prior_succeeded_calls[-_MAX_PROJECTED_SUCCEEDED_CALLS:]
                    ],
                    *[
                        _summarize_succeeded_call(name, payload)
                        for name, payload in succeeded_calls[-_MAX_PROJECTED_SUCCEEDED_CALLS:]
                    ],
                ],
                "replan_state": task.replan_state.model_dump(mode="json"),
                "progress_ledger": _progress_ledger(
                    [*(prior_observations or []), *observations],
                    total_wasted_turns=total_wasted_turns,
                    candidate_urls=candidate_urls,
                    failed_candidate_urls=failed_candidate_urls,
                ),
                "invalid_input_signatures": invalid_input_signatures[-8:],
                "unavailable_tools": sorted(unavailable_tools),
            }
            if runtime_feedback:
                decision_state["runtime_feedback"] = runtime_feedback
            if model_budget is not None and not model_budget.try_reserve(
                estimate_input_tokens(_EXECUTOR_INSTRUCTION, decision_state)
            ):
                return ExecutorResult(
                    status="failed",
                    summary="Model budget exhausted before the next decision.",
                    observations=observations,
                    error_code="model_budget_exhausted",
                    execution_state=current_state(),
                )
            decision = self._gateway.decide(
                role=AgentRole.executor,
                instruction=_EXECUTOR_INSTRUCTION,
                state=decision_state,
                response_model=ExecutorDecision,
            )
            if model_budget is not None and not model_budget.record(self._gateway.last_usage):
                return ExecutorResult(
                    status="failed",
                    summary="Model budget exhausted after the latest decision.",
                    observations=observations,
                    error_code="model_budget_exhausted",
                    execution_state=current_state(),
                )
            if trace is not None:
                usage = self._gateway.last_usage
                if isinstance(usage, dict):
                    usage["context_manifest"] = build_context_manifest(
                        instruction=_EXECUTOR_INSTRUCTION,
                        available_tools=available_tools,
                        observations_for_decision=[
                            *prior_observations_for_decision,
                            *summarized_observations,
                        ],
                        evidence_chars=compute_evidence_chars(
                            context.metadata.get("observed_public_evidence")
                        ),
                        model_name=usage.get("model_name"),
                    )
                trace(
                    AgentRole.executor,
                    decision_summary(
                        action=decision.action, tool_name=decision.tool_name
                    ),
                    usage,
                )
            if decision.action == "call_tool":
                if (
                    decision.tool_name == "search-public-job-pages"
                    and _has_unfailed_candidate_urls(
                        candidate_urls,
                        failed_candidate_urls,
                        [*prior_observations, *observations]
                        if prior_observations is not None
                        else observations,
                    )
                ):
                    consecutive_stalls += 1
                    total_wasted_turns += 1
                    if consecutive_stalls >= _MAX_CONSECUTIVE_STALLS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_CANDIDATE_URLS_STALL_QUESTION,
                            execution_state=current_state(),
                        )
                    if total_wasted_turns >= _MAX_TOTAL_WASTED_TURNS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_TOTAL_WASTE_QUESTION,
                            execution_state=current_state(),
                        )
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name,
                            status="failed",
                            error_code="candidate_urls_already_supplied",
                        ),
                    )
                    continue
                if any(
                    decision.tool_name == name and decision.tool_input == payload
                    for name, payload in stable_failed_calls
                ) or (
                    decision.tool_name,
                    _input_hash(decision.tool_input),
                ) in prior_stable_failed_hashes:
                    # The identical call already failed with a stable error
                    # code (rate-limited sheet API, forbidden tool, unknown
                    # tool); repeating it cannot change the outcome. Reject
                    # it as duplicate_tool_call WITHOUT incrementing
                    # total_wasted_turns and WITHOUT consuming budget
                    # (mirroring the succeeded-call dedup), so an external
                    # rate limit is never mislabeled as model waste. The
                    # consecutive-stall cap still applies: an agent that
                    # keeps repeating the doomed call after the
                    # duplicate_tool_call guidance is genuinely stalled.
                    consecutive_stalls += 1
                    if consecutive_stalls >= _MAX_CONSECUTIVE_STALLS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_DUPLICATE_STALL_QUESTION,
                            execution_state=current_state(),
                        )
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name or "",
                            status="failed",
                            error_code="duplicate_tool_call",
                        ),
                    )
                    continue
                requested_fetch_urls = set(_payload_fetch_urls(decision.tool_input))
                fetch_observations = [*(prior_observations or []), *observations]
                observed_fetch_urls = _observed_fetch_urls(fetch_observations)
                observed_route_counts = _observed_fetch_route_counts(fetch_observations)
                if (
                    decision.tool_name in _FETCH_TOOL_NAMES
                    and requested_fetch_urls
                    and (
                        requested_fetch_urls.issubset(observed_fetch_urls)
                        or all(
                            observed_route_counts.get(_fetch_route_key(url), 0)
                            >= _MAX_SUCCESSFUL_FETCH_ATTEMPTS_PER_ROUTE
                            for url in requested_fetch_urls
                        )
                    )
                ):
                    # Semantic duplicate: the payload may have different
                    # filters, or even fresh dynamic hashes, but every
                    # requested URL already produced sufficient evidence or
                    # exhausted the route repetition allowance. A batch with
                    # one genuinely new route remains executable.
                    consecutive_stalls += 1
                    total_wasted_turns += 1
                    if consecutive_stalls >= _MAX_CONSECUTIVE_STALLS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_DUPLICATE_STALL_QUESTION,
                            execution_state=current_state(),
                        )
                    if total_wasted_turns >= _MAX_TOTAL_WASTED_TURNS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_TOTAL_WASTE_QUESTION,
                            execution_state=current_state(),
                        )
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name or "",
                            status="failed",
                            error_code="duplicate_tool_call",
                            error_message=(
                                "请求中的 URL 均已成功抓取；请改用尚未处理的 URL "
                                "或继续使用现有证据。"
                            ),
                        ),
                    )
                    continue
                if decision.tool_name in unavailable_tools:
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name or "",
                            status="failed",
                            error_code="source_unavailable",
                            error_message=(
                                "该工具所属来源已在本次运行中熔断，"
                                "请改用其他已授权来源。"
                            ),
                        ),
                    )
                    return ExecutorResult(
                        status="needs_user",
                        observations=observations,
                        user_question=_SOURCE_UNAVAILABLE_QUESTION,
                        execution_state=current_state(),
                    )
                if any(
                    decision.tool_name == name and decision.tool_input == payload
                    for name, payload in succeeded_calls
                ) or (
                    decision.tool_name,
                    _input_hash(decision.tool_input),
                ) in prior_succeeded_hashes:
                    consecutive_stalls += 1
                    total_wasted_turns += 1
                    if consecutive_stalls >= _MAX_CONSECUTIVE_STALLS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_DUPLICATE_STALL_QUESTION,
                            execution_state=current_state(),
                        )
                    if total_wasted_turns >= _MAX_TOTAL_WASTED_TURNS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_TOTAL_WASTE_QUESTION,
                            execution_state=current_state(),
                        )
                    record_observation(
                        observations,
                        observations_for_decision,
                        ToolObservation(
                            tool_name=decision.tool_name or "",
                            status="failed",
                            error_code="duplicate_tool_call",
                        ),
                    )
                    continue
                consecutive_stalls = 0
                if tool_budget is not None and not tool_budget.try_consume():
                    return ExecutorResult(
                        status="failed",
                        summary="Tool-call budget exhausted before executing the next action.",
                        observations=observations,
                        error_code="tool_budget_exhausted",
                        execution_state=current_state(),
                    )
                observation = self._tools.invoke(
                    role=AgentRole.executor,
                    name=decision.tool_name or "",
                    context=tool_context,
                    payload=decision.tool_input,
                    allowed_skills=allowed_skills,
                )
                record_observation(observations, observations_for_decision, observation)
                tool_context = _with_observed_page(tool_context, observation)
                if observation.error_code == "tool_skill_forbidden":
                    # This is a plan-scope defect, not a recoverable tool
                    # failure. Stop after the first rejected call so the
                    # runtime can replan instead of spending turns repeating
                    # an action this step can never execute.
                    return ExecutorResult(
                        status="needs_user",
                        observations=observations,
                        user_question=(
                            "当前步骤调用了不在其 Skill 范围内的工具，"
                            "需要重新规划为单一 Skill 步骤。"
                        ),
                        execution_state=current_state(),
                    )
                if observation.status == "succeeded":
                    succeeded_calls.append((decision.tool_name, decision.tool_input))
                    if (
                        decision.tool_name == "search-public-job-pages"
                        and isinstance(observation.output, dict)
                        and observation.output.get("terminal_reason") == "search_empty"
                    ):
                        # A network-successful empty search is not progress:
                        # expose it as bounded no-progress so the model cannot
                        # keep inventing queries until the wall-clock budget
                        # is exhausted.
                        total_wasted_turns += 1
                        if total_wasted_turns >= _MAX_TOTAL_WASTED_TURNS:
                            return ExecutorResult(
                                status="needs_user",
                                observations=observations,
                                user_question=(
                                    "公开搜索已完成但没有找到可核验岗位页面。"
                                    "请提供具体岗位链接或岗位文本后继续。"
                                ),
                                execution_state=current_state(),
                            )
                else:
                    if observation.error_code in _RUN_WIDE_UNAVAILABLE_CODES:
                        unavailable_tools.add(decision.tool_name or "")
                    if observation.error_code == "invalid_tool_input":
                        signature = _validation_signature(observation.error_message)
                        if signature:
                            invalid_input_signatures.append(signature)
                            same_signature_count = invalid_input_signatures.count(signature)
                            if same_signature_count >= 2:
                                return ExecutorResult(
                                    status="needs_user",
                                    observations=observations,
                                    user_question=(
                                        "工具输入连续违反同一字段契约，自动修正未取得进展。"
                                        "请补充该字段的合法值后重试。"
                                    ),
                                    execution_state=current_state(),
                                )
                    if (
                        decision.tool_name in _FETCH_TOOL_NAMES
                        and observation.error_code in _CANDIDATE_FAILURE_ERROR_CODES
                    ):
                        # Attribute a single-fetch failure to its candidate URL
                        # (the observation carries no tool input): the URL was
                        # proven dead, feeding the search-authorization ledger.
                        failed_candidate_urls.update(
                            url
                            for url in _payload_fetch_urls(decision.tool_input)
                            if url in candidate_urls
                        )
                    if observation.error_code in _STABLE_FAILURE_ERROR_CODES:
                        # A stable failure (rate-limited sheet API, forbidden
                        # tool, unknown tool) is recorded so an identical
                        # re-issue is deduped instead of re-hitting the doomed
                        # call. The first failure still counts once toward the
                        # total-waste cap: it produced no new evidence.
                        stable_failed_calls.append(
                            (decision.tool_name, decision.tool_input)
                        )
                    # The tool was invoked but did not produce a new succeeded
                    # observation. Count this as a wasted turn for the total
                    # cap (sustained no-progress even when interspersed with
                    # successful calls), but NOT for the consecutive-stall cap
                    # (a real execution is not a stall). A small number of
                    # exploratory failures stays under the cap; only sustained
                    # no-progress trips it.
                    total_wasted_turns += 1
                    if total_wasted_turns >= _MAX_TOTAL_WASTED_TURNS:
                        return ExecutorResult(
                            status="needs_user",
                            observations=observations,
                            user_question=_TOTAL_WASTE_QUESTION,
                            execution_state=current_state(),
                        )
                continue
            if decision.action == "complete":
                return ExecutorResult(
                    status="succeeded",
                    summary=decision.summary,
                    artifact_refs=decision.artifact_refs,
                    observations=observations,
                    execution_state=current_state(),
                )
            terminal_handoff_codes = {
                "anti_bot_challenge",
                "captcha",
                "login_required",
                "access_denied",
                "domain_temporarily_blocked",
                "source_unavailable",
            }
            has_terminal_handoff = any(
                observation.error_code in terminal_handoff_codes
                for observation in [*(prior_observations or []), *observations]
            )
            has_successful_observation = any(
                observation.status == "succeeded"
                for observation in [*(prior_observations or []), *observations]
            )
            if (
                available_tools
                and not has_terminal_handoff
                and not has_successful_observation
                and premature_need_user_retries < 1
                and _turn < task.budget.max_agent_turns - 1
            ):
                premature_need_user_retries += 1
                runtime_feedback = (
                    "Policy correction: this handoff is premature because no "
                    "terminal access block is recorded and permitted tools remain. "
                    "Re-check observations and call one tool that can produce "
                    "new evidence before asking the user."
                )
                continue
            return ExecutorResult(
                status="needs_user",
                observations=observations,
                user_question=decision.user_question,
                execution_state=current_state(),
            )
        return ExecutorResult(
            status="failed",
            observations=observations,
            summary="Executor turn budget exhausted before completing the step.",
            execution_state=current_state(),
        )


def _with_observed_page(
    context: ToolContext, observation: ToolObservation
) -> ToolContext:
    """Expose a successful page fetch to the next Executor-selected tool call."""
    output = observation.output or {}
    raw_pages = output.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else [output]
    existing = context.metadata.get("observed_public_evidence", [])
    evidence = list(existing) if isinstance(existing, list) else []
    seen_artifact_ids = {
        item.get("artifact_id") for item in evidence if isinstance(item, dict)
    }
    for page in pages:
        if not isinstance(page, dict) or not all(
            isinstance(page.get(key), str) and page[key]
            for key in ("artifact_id", "source_url", "content_hash", "visible_text")
        ):
            continue
        if page["artifact_id"] in seen_artifact_ids:
            continue
        evidence_item = {
            "artifact_id": page["artifact_id"],
            "source_url": page["source_url"],
            "content_hash": page["content_hash"],
            "visible_text": page["visible_text"],
            "title": page.get("title"),
        }
        if isinstance(page.get("effective_url"), str) and page["effective_url"]:
            evidence_item["effective_url"] = page["effective_url"]
        if isinstance(page.get("redirect_chain"), list) and page["redirect_chain"]:
            evidence_item["redirect_chain"] = page["redirect_chain"]
        if isinstance(page.get("http_status"), int):
            evidence_item["http_status"] = page["http_status"]
        evidence.append(evidence_item)
        seen_artifact_ids.add(page["artifact_id"])
    if not evidence:
        return context
    metadata = dict(context.metadata)
    metadata["observed_public_evidence"] = evidence
    return ToolContext(user_id=context.user_id, run_id=context.run_id, metadata=metadata)


def _candidate_urls(task: AgentTaskRequest) -> list[str]:
    """Deduplicated, non-empty user-supplied candidate URLs (empty when none)."""
    raw = task.context.get("candidate_urls")
    if not isinstance(raw, list):
        return []
    seen: set[str] = set()
    urls: list[str] = []
    for item in raw:
        if isinstance(item, str) and item.strip() and item.strip() not in seen:
            seen.add(item.strip())
            urls.append(item.strip())
    return urls


def _payload_fetch_urls(tool_input: object) -> list[str]:
    """URLs named in a fetch-tool payload (``urls`` list or single ``url``)."""
    if not isinstance(tool_input, dict):
        return []
    raw = tool_input.get("urls")
    if isinstance(raw, list):
        return [url for url in raw if isinstance(url, str) and url.strip()]
    single = tool_input.get("url")
    if isinstance(single, str) and single.strip():
        return [single]
    return []


def _observed_fetch_urls(observations: list[ToolObservation]) -> set[str]:
    """Return URLs already fetched successfully in the current step.

    Exact payload deduplication cannot catch a model that keeps changing
    filters while sending the same URL batch back to a fetch tool.  The fetch
    observation is the authoritative source for this semantic check; failed
    or merely proposed URLs are deliberately excluded.
    """
    observed: set[str] = set()
    for observation in observations:
        if observation.status != "succeeded" or observation.tool_name not in _FETCH_TOOL_NAMES:
            continue
        output = observation.output
        if not isinstance(output, dict):
            continue
        raw_pages = output.get("pages")
        pages = raw_pages if isinstance(raw_pages, list) else [output]
        for page in pages:
            if not isinstance(page, dict):
                continue
            for field in ("source_url", "effective_url"):
                url = page.get(field)
                if isinstance(url, str) and url.strip():
                    observed.add(url.strip())
    return observed


def _fetch_route_key(url: str) -> str:
    """Return a stable fetch route identity while ignoring query churn."""
    value = url.strip()
    parsed = urlsplit(value)
    if parsed.netloc and parsed.path:
        return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}{parsed.path}"
    return value


def _observed_fetch_route_counts(
    observations: list[ToolObservation],
) -> dict[str, int]:
    """Count successful fetch attempts by route, not volatile query string."""
    counts: dict[str, int] = {}
    for observation in observations:
        if observation.status != "succeeded" or observation.tool_name not in _FETCH_TOOL_NAMES:
            continue
        for url in _observed_fetch_urls_from_observation(observation):
            route = _fetch_route_key(url)
            counts[route] = counts.get(route, 0) + 1
    return counts


def _observed_fetch_urls_from_observation(
    observation: ToolObservation,
) -> set[str]:
    output = observation.output
    if not isinstance(output, dict):
        return set()
    raw_pages = output.get("pages")
    pages = raw_pages if isinstance(raw_pages, list) else [output]
    urls: set[str] = set()
    for page in pages:
        if not isinstance(page, dict):
            continue
        for field in ("source_url", "effective_url"):
            value = page.get(field)
            if isinstance(value, str) and value.strip():
                urls.add(value.strip())
    return urls


def _failed_candidate_urls(
    observations: list[ToolObservation], *, candidate_urls: frozenset[str]
) -> set[str]:
    """Candidate URLs already proven dead by a batch-fetch failure entry.

    Batch fetches report per-URL failures in ``output.failures``; those
    entries survive verifier RETRY through the merged observation list, so
    this is the durable candidate-death ledger. Single-fetch failures carry
    no URL in the observation and are attributed in-flight instead.
    """
    failed: set[str] = set()
    for observation in observations:
        # A batch fetch reports per-URL failures inside a SUCCEEDED
        # observation (the registry wraps handler output as succeeded; the
        # failure detail lives in output.failures), so the ledger reads the
        # output only -- entry-level error codes are authoritative.
        output = observation.output
        if not isinstance(output, dict):
            continue
        failures = output.get("failures")
        if not isinstance(failures, list):
            continue
        for entry in failures:
            if (
                isinstance(entry, dict)
                and isinstance(entry.get("source_url"), str)
                and entry["source_url"] in candidate_urls
                and entry.get("error_code") in _CANDIDATE_FAILURE_ERROR_CODES
            ):
                failed.add(entry["source_url"])
    return failed


def _has_unfailed_candidate_urls(
    candidate_urls: frozenset[str],
    in_flight_failed: set[str],
    observations: list[ToolObservation],
) -> bool:
    """True when at least one candidate URL is not yet proven dead.

    Public search stays forbidden while ANY candidate remains usable. Once
    every supplied candidate is dead or blocked, search may use another public
    host; the blocked-domain circuit breaker filters the original host.
    """
    if not candidate_urls:
        return False
    proven = in_flight_failed | _failed_candidate_urls(
        observations, candidate_urls=candidate_urls
    )
    return not _candidate_search_is_authorized(candidate_urls, proven)


def _scope_feedback_to_step_catalog(
    feedback: object, *, scoped_out_tool_names: frozenset[str]
) -> object:
    """Drop verifier-feedback fragments that name a tool outside the step scope.

    The stored ``verifier_feedback`` context is never modified -- this is a
    projection for the Executor's decision state only (W3). Non-list inputs
    and non-string entries pass through untouched; an entry whose fragments
    all name scoped-out tools is dropped entirely, since its only content
    was an unsatisfiable demand. Drops are logged under the
    ``verifier_feedback_tool_filtered`` token for observability.
    """
    if not isinstance(feedback, list) or not scoped_out_tool_names:
        return feedback
    filtered: list[object] = []
    for entry in feedback:
        if not isinstance(entry, str):
            filtered.append(entry)
            continue
        fragments = [
            fragment.strip()
            for fragment in _FEEDBACK_FRAGMENT_BOUNDARIES.split(entry)
            if fragment.strip()
        ]
        kept = [
            fragment
            for fragment in fragments
            if not any(tool_name in fragment for tool_name in scoped_out_tool_names)
        ]
        if len(kept) != len(fragments):
            logger.warning(
                "verifier_feedback_tool_filtered: dropped %d of %d fragments naming "
                "a tool outside the step scope",
                len(fragments) - len(kept),
                len(fragments),
            )
        if kept:
            filtered.append("。".join(kept))
    return filtered

