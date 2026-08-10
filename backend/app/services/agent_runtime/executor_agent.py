"""Autonomous Executor role for the adaptive PEV runtime."""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from backend.app.domain.agent_runtime import AgentRole
from backend.app.services.agent_runtime.model_gateway import AgentModelGateway
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
from backend.app.services.agent_runtime.tool_context import ToolContext
from backend.app.services.agent_runtime.tool_budget import ToolCallBudget
from backend.app.services.agent_runtime.tool_registry import ToolRegistry
from backend.app.services.agent_runtime.context_manifest import (
    build_context_manifest,
    compute_evidence_chars,
)
from backend.app.services.agent_runtime.tracing import DecisionTrace, decision_summary
from backend.app.services.agent_runtime.turn_budget import AgentTurnBudget

_EXECUTOR_INSTRUCTION = (
    "## 角色\n"
    "You are the Executor Agent. Work toward the current planned outcome using "
    "only its permitted Skills. Observe every tool result, including failures, "
    "and independently select the next allowed action. Do not claim an artifact "
    "that is absent from observations; ask the user if the goal cannot proceed. "
    "Do not complete a planned outcome until its stated success criteria and all "
    "user-requested deliverables assigned to this step have tool-backed results; "
    "if evidence cannot support one, state the limitation rather than silently "
    "omitting it. "
    "\n## 行为规则\n"
    "Already-succeeded calls are listed in `already_succeeded_calls` in the "
    "decision state. Do NOT re-invoke a (tool, input) you already succeeded "
    "with - reuse its prior observation. Re-calling a succeeded tool wastes a "
    "turn and counts toward the waste limit: after 3 total wasted turns "
    "(duplicates, blocked searches, or failed calls) the step is handed to "
    "the user. "
    "When context supplies candidate_urls, treat them as a finite candidate set: "
    "prefer fetch-public-job-pages to capture the set in one bounded observation; "
    "otherwise fetch each unique URL at most once, then use the observed artifact IDs to "
    "extract structured details and move to the next requested Skill. Never "
    "re-fetch a URL that is already represented by a successful observation. "
    "Once all supplied candidates have been observed, choose extraction, matching, "
    "tailoring, planning, verification, or a truthful limitation; do not keep "
    "fetching pages. Structured extraction is an enhancement for human-readable "
    "JD normalization, not a prerequisite: match-observed-jobs, "
    "build-resume-tailoring-brief, and build-preparation-plan operate on the "
    "observed page text itself and accept any observed artifact as target. When "
    "extraction returns no structured candidates (for example a card-list page "
    "whose entries are not normalized), still proceed with matching, tailoring, "
    "or planning on the raw observed text and note the extraction limitation in "
    "your summary. Only when the observed page text itself contains no job "
    "information at all (empty, blocked, or irrelevant page) may you state the "
    "limitation and ask the user for a more specific job-page URL. "
    "When candidate_urls is non-empty, do not call public search: the candidate "
    "set is already user-provided evidence to process. "
    "When multiple observed public-page artifacts need detailed JD normalization, "
    "prefer extract-observed-job-details-batch so one evidence-bound tool result "
    "covers the finite set. "
    "\n## 流程\n"
    "When a job-discovery task has no supplied URL, or the goal refers to the "
    "recruitment data source by name (校招内推汇总表/内推台账/招聘数据源/就业信息网), "
    "first call query-career-sheet-records with the recency window stated in "
    "the goal and at most location or company keywords. Do NOT pass role or "
    "position keywords: the sheet stores companies, not roles, so role terms "
    "would filter out every company. When it returns records, fetch each "
    "company's apply_url with fetch-public-job-pages (each unique URL at most "
    "once) and look for matching roles inside the fetched pages; if a page "
    "yields no usable job text, note it and move on - never re-fetch a URL "
    "and never issue a fetch with an empty URL list. "
    "Only when the sheet query returns no matching records may you use the "
    "public-job search tool; then independently select a returned direct URL "
    "for evidence capture. Use the user's language and role terms when forming "
    "a search query (Chinese goals need Chinese recruitment terms). After one "
    "search observation, prefer fetching a plausible returned result; retry a "
    "search at most once only when no plausible public career URL was returned. "
    "Do not loop through search-provider or job-board domain variations without "
    "capturing evidence. A search observation with an empty results list is a "
    "verified provider limitation: do not search again; when the goal names a "
    "company with a known official careers site, construct and fetch that site's "
    "listing or search URL directly (for example careers.tencent.com/search.html?keyword=<role terms>), "
    "because fetch renders JavaScript while search engines often omit such "
    "listing pages; only when no official careers URL can be formed, or it yields "
    "no usable JD, ask the user for an official careers URL or relax the source "
    "constraint. If a fetched "
    "page does not contain a usable JD, state that evidence limitation and choose "
    "a different returned direct URL at most once before asking the user. "
    "When verifier_feedback is present, the Verifier found a tool-backed "
    "deliverable missing from the prior attempt for this same step. The "
    "missing deliverable is named in feedback. Call that named tool next, "
    "reusing the observed public evidence that prior_observations already "
    "captured; do not repeat a discovery tool (fetch/extract/search) whose "
    "result already appears in prior_observations, and do not re-fetch a URL "
    "that prior_observations already observed. "
    "A duplicate_tool_call observation means you just re-issued an identical "
    "tool call that already succeeded: that result is already in observations. "
    "Move to the next distinct action (extract, match, tailor, plan, verify, or "
    "complete) instead of repeating the same call. A repeated fetch or extract "
    "cannot improve the evidence: if the observed page text itself contains no "
    "usable job information, ask the user instead of re-running capture tools; "
    "when the page text is usable but structured extraction produced nothing, "
    "proceed with matching, tailoring, or planning on the raw observed text "
    "instead of extracting again. If verifier_feedback "
    "names a deliverable that this step's permitted Skills cannot produce, "
    "state that limitation and ask the user for the specific missing input "
    "rather than re-running capture tools. "
    "\n## 输出契约\n"
    "The 'complete' decision ends the step as succeeded. Use it only when "
    "(1) the step's success criteria are met with tool-backed results, or "
    "(2) you exhausted every allowed path and deliver an honest, "
    "evidence-grounded negative finding as the final result (for example, "
    "every permitted source was searched and none matches the goal). When the "
    "path is blocked (login/captcha/anti-bot, which must never be "
    "circumvented) or you need something from the user (a specific URL, a JD, "
    "permission to relax a constraint) to proceed, decide 'needs_user' with a "
    "clear question. Never decide 'complete' while your own summary asks the "
    "user a question. If your final summary asks the user to provide "
    "anything, to paste a link, or to relax a constraint, the decision must be "
    "'needs_user', never 'complete'. 'complete' is reserved for delivering the "
    "step's final result, not for reporting a handoff. A summary that reports "
    "tool failure, a blocked page (login/captcha/anti-bot), or that the "
    "evidence could not be captured is not a completed negative finding: when "
    "evidence acquisition failed or was blocked, decide 'needs_user' and "
    "state what input would unblock it. 'complete' with a negative finding is "
    "allowed only when the searches and queries themselves executed "
    "successfully and returned no matching results (an empty-result finding), "
    "never when the tools failed or the pages were blocked. "
    "A deliverable produced by its tool is tool-backed even when the "
    "underlying evidence is imperfect (for example a card-list page without "
    "full JD 职责/要求 bodies). After the requested deliverable(s) have been "
    "produced by their tools, do not ask the user for a fuller JD, a "
    "detail-page URL, or any other extra input: deliver the result, note the "
    "evidence limitation inside the summary, and decide 'complete'. needs_user "
    "is for missing inputs that block a deliverable from being produced at "
    "all, not for improving already-produced evidence. "
    "\n## 禁止项\n"
    "A discovery step must not issue more than 3 search observations in "
    "total, no matter how the query differs. The fourth search call is "
    "forbidden: after the third search observation, either fetch one returned "
    "URL or decide 'needs_user'/'complete'; never search again. "
    "A fetch counts as no-progress when its page text contains no job "
    "information (only navigation, footer, or a shell), even though the tool "
    "returned success: treat it exactly like a failed fetch for the hard-stop "
    "rule, and after 3 consecutive no-progress fetches stop fetching "
    "entirely. Many real career pages (SPA login walls, JS-loaded lists) "
    "return 'success' with no usable content; re-fetching or searching "
    "further cannot change that. "
    "Never loop over pages one fetch at a time: when several URLs need "
    "capture, use fetch-public-job-pages or extract-observed-job-details-batch "
    "so one bounded observation covers the set. A page that yields no usable "
    "JD (insufficient content, blocked, or irrelevant) is a dead end: do not "
    "re-fetch it, and do not keep fetching further speculative URLs after the "
    "plausible candidates have been observed. If all plausible candidates were "
    "observed without capturing valid evidence, decide 'needs_user' with an "
    "honest summary instead of continuing to fetch. "
    "Hard stop: after 3 consecutive fetch attempts that fail or yield no "
    "usable job text, stop fetching entirely - no further speculative fetches "
    "and no retrying a failed URL, regardless of how many candidates remain. "
    "Complete the step with an honest negative finding (if the searches and "
    "queries themselves succeeded but returned nothing) or decide 'needs_user' "
    "with the evidence limitation stated. A query-career-sheet-records call "
    "that returned 0 records will not change when re-issued with the same "
    "parameters: never retry a sheet query identically; switch to the "
    "public search tool instead. "
    "A tool_skill_forbidden observation means the current step's Skill scope "
    "permanently excludes that tool; retrying it cannot succeed. Do not retry "
    "the forbidden tool. Produce the deliverable with a tool allowed in this "
    "step, or decide 'needs_user' and explain what input would unblock the "
    "step."
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
# step's skill permanently excludes the tool (tool_skill_forbidden), or the
# tool does not exist (unknown_tool). An identical re-issue of such a call is
# rejected as duplicate_tool_call WITHOUT incrementing total_wasted_turns and
# WITHOUT consuming budget, mirroring the succeeded-call dedup: an external
# rate limit must never be mislabeled as model waste. Transient failures
# (tool_execution_failed) and blocked codes (login_required etc.) are NOT
# recorded, so a legitimate retry and a blocked-flow handoff keep today's
# behavior.
_STABLE_FAILURE_ERROR_CODES = frozenset(
    {
        "sheet_rate_limited",
        "sheet_call_failed",
        "tool_skill_forbidden",
        "unknown_tool",
    }
)

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


def _snapshot_execution_state(
    *,
    succeeded_calls: list[tuple[str, dict[str, Any]]],
    prior_succeeded_calls: list[dict[str, str]],
    consecutive_stalls: int,
    total_wasted_turns: int,
    stable_failed_calls: list[tuple[str, dict[str, Any]]] | None = None,
    prior_stable_failed_calls: list[dict[str, str]] | None = None,
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
    }


class ExecutorAgent:
    """Bounded perceive–decide–act–observe loop for a single plan step."""

    def __init__(self, *, gateway: AgentModelGateway, tools: ToolRegistry) -> None:
        self._gateway = gateway
        self._tools = tools

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
        deadline: float | None = None,
        prior_observations: list[ToolObservation] | None = None,
    ) -> ExecutorResult:
        """Execute a step without precomputing its tool sequence in the harness."""
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

        def current_state() -> dict[str, Any]:
            """Snapshot this invocation's state for the runtime to carry on RETRY."""
            return _snapshot_execution_state(
                succeeded_calls=succeeded_calls,
                prior_succeeded_calls=prior_succeeded_calls,
                stable_failed_calls=stable_failed_calls,
                prior_stable_failed_calls=prior_stable_failed_calls,
                consecutive_stalls=consecutive_stalls,
                total_wasted_turns=total_wasted_turns,
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
            decision = self._gateway.decide(
                role=AgentRole.executor,
                instruction=_EXECUTOR_INSTRUCTION,
                state={
                    "goal": task.goal,
                    "context": task.context,
                    "private_context": task.private_context,
                    "remaining_tool_calls": (
                        tool_budget.remaining if tool_budget is not None
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
                    "verifier_feedback": task.context.get("verifier_feedback", []),
                    "already_succeeded_calls": [
                        *[
                            {
                                "tool": entry["tool"],
                                "input_summary": entry["input_summary"],
                            }
                            for entry in prior_succeeded_calls[
                                -_MAX_PROJECTED_SUCCEEDED_CALLS:
                            ]
                        ],
                        *[
                            _summarize_succeeded_call(name, payload)
                            for name, payload in succeeded_calls[
                                -_MAX_PROJECTED_SUCCEEDED_CALLS:
                            ]
                        ],
                    ],
                },
                response_model=ExecutorDecision,
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
                    and _has_candidate_urls(task)
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
                if observation.status == "succeeded":
                    succeeded_calls.append((decision.tool_name, decision.tool_input))
                else:
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
        evidence.append(
            {
                "artifact_id": page["artifact_id"],
                "source_url": page["source_url"],
                "content_hash": page["content_hash"],
                "visible_text": page["visible_text"],
                "title": page.get("title"),
            }
        )
        seen_artifact_ids.add(page["artifact_id"])
    if not evidence:
        return context
    metadata = dict(context.metadata)
    metadata["observed_public_evidence"] = evidence
    return ToolContext(user_id=context.user_id, run_id=context.run_id, metadata=metadata)


def _has_candidate_urls(task: AgentTaskRequest) -> bool:
    """Avoid redundant public search when the user already bounded the evidence set."""
    candidate_urls = task.context.get("candidate_urls")
    return isinstance(candidate_urls, list) and any(
        isinstance(url, str) and url.strip() for url in candidate_urls
    )

