# Agent Runtime Optimization Plan (P0 + P1)

> Source: [docs/agent-runtime-industry-evaluation.md](../../agent-runtime-industry-evaluation.md) §四 P0(1-3) + P1(4-7).
> Scope confirmed with user: implement P0 items 1-3 and P1 items 4-7. P2 (8-10) excluded (needs product decision).
> Method: Subagent-Driven Development — 7 sequential tasks, each implemented by a fresh implementer subagent + task review, then a final whole-branch review, then behavioral validation on the affected 83-doc subset.
> Branch: `optimize/agent-runtime-p0p1` (branched from master at d4ac2ad).
> Authoritative baseline: 83-doc final 70 succeeded / 12 waiting_user / 1 failed (84.3%).

## Global Constraints (bind every task)

- **100% branch coverage gate**: `.\.venv\Scripts\python.exe -m pytest tests/unit/ -q` must stay green at `fail_under=100`; `.\.venv\Scripts\python.exe -m ruff check backend tests scripts` must pass. Every new/changed code path must be covered.
- **Seven security hard gates** (CLAUDE.md): never auto-click submit (no submit tool/scope); never bypass login/captcha/anti-bot (mark needs_manual_review); student API only returns `verified` jobs; never write secrets/passwords/tokens/raw payloads to repo/logs/argv; Redis is never authority (MySQL single source of truth); never trust device token alone; job review requires review_version optimistic locking.
- **Three-layer separation**: API→Service→Repository; no raw SQL in services; no HTTP in repositories.
- **`ToolObservation` is `extra="forbid"`** (schemas.py:177): any new field MUST be declared on the model.
- **Tool exceptions never leak raw payloads**: `ToolRegistry.invoke` converts every exception to a `ToolObservation(status=failed)`; any `error_message` MUST be a short structured string, never a raw payload/token/credential-bearing URL.
- **No harness control-flow behavior change for P0**: P0 tasks (1-3) are additive. T3 surfaces more specific error info to the LLM (model-input change — validate). P1 tasks (4-7) change model input — validate on affected 83-doc subset.
- **DeepSeek capability**: supports `response_format: json_object` (JSON mode); does NOT support `json_schema` strict. Never attempt json_schema strict on DeepSeek.
- **Migration convention**: `alembic/versions/YYYYMMDD_NNNN_description.py`; current head `0019`; next is `0020` (check the exact existing filename pattern in `alembic/versions/` before naming).
- **Don't globally touch `enforce_result_invariants`** (per prior eval).
- **Behavioral validation env**: affected 83-doc subset needs `DEEPSEEK_API_KEY` env + public network (eval_runner under tests/question).

## Cross-task Interface Contract (introduced by T1, used by T2/T5)

T1 expands the trace callback and gateway usage contract. T2/T5 build on it. **All three tasks must agree on this shape:**

- `DecisionTrace` (tracing.py:10) changes from `Callable[[AgentRole, dict[str, str]], None]` to `Callable[[AgentRole, dict[str, str], dict[str, Any] | None], None]`. The 3rd arg `turn_metadata` is a dict (or None) carrying per-decision observability data.
- T1 populates `turn_metadata` with `{"model_name": str, "input_tokens": int, "output_tokens": int}` (from gateway usage).
- T2 adds a `"context_manifest"` sub-dict to the same `turn_metadata`.
- `AgentModelGateway` Protocol gains a `last_usage` property returning `dict[str, Any] | None` (`{"model_name","input_tokens","output_tokens"}`) or None. Concrete `LangChainModelGateway` captures it; test fakes return None or a fixture dict.

## Pre-flight note (factual correction to the source report)

The report P1-4 describes a "当前 10 条硬截断" (10-observation hard truncation). A code survey found **no such 10-observation cap exists**: `_MAX_PROJECTED_ITEMS=10` (observation_projection.py:20) caps **per-observation pages/details arrays**, not the observation-list length (which grows O(turns)). The actual truncation mechanisms T4 targets are: (a) the run-level 48,000-char evidence hard-cut (runtime.py:546-572) that drops early-link evidence, and (b) the unbounded `observations_for_decision` list growth. T4 is specified against these real mechanisms, using the report's intended strategy (old→summary, recent→full, keep total budget). This serves the user's intent (preserve early-link evidence in long chains) — only the report's mechanism description was imprecise.

---

## Task 1: Token usage metering (P0-1)

**Objective:** Capture per-decision token usage from the model provider and persist it to `AgentTurn.input_tokens/output_tokens/model_name` (columns already exist since migration 0017; no migration needed).

**Source:** report §四 P0-1; evidence at model_gateway.py:52,96-98; runtime.py:515-538; repositories/agent_runtime.py:294-313; models.py:1718-1720.

**Files:**
- `backend/app/services/agent_runtime/model_gateway.py` — capture usage in both decide paths; expose `last_usage`.
- `backend/app/services/agent_runtime/tracing.py` — expand `DecisionTrace` to carry `turn_metadata`; keep `decision_summary` whitelist unchanged (privacy-safe).
- `backend/app/services/agent_runtime/runtime.py` — `trace` closure (515-538) passes `model_name/input_tokens/output_tokens` to `create_turn`.
- `backend/app/services/agent_runtime/planner_agent.py`, `executor_agent.py`, `verifier_agent.py` — after `gateway.decide(...)`, read `self._gateway.last_usage` and pass it to `trace(...)`.
- `backend/app/repositories/agent_runtime.py` — `create_turn` already accepts `model_name/input_tokens/output_tokens` (294-313); the runtime just passes them.

**Exact changes:**
1. `AgentModelGateway` Protocol: add `@property last_usage(self) -> dict[str, Any] | None`.
2. `LangChainModelGateway`:
   - **Local-json path** (`_decide_with_local_json_validation`, model_gateway.py:201): `raw_result = self._model.invoke(...)` is an `AIMessage`; after extracting `.content`, read `raw_result.usage_metadata` (langchain) or `raw_result.response_metadata.get("token_usage")` (provider). Normalize to `{"model_name": <self._model.model>, "input_tokens": int, "output_tokens": int}`. Stash on `self._last_usage`.
   - **Structured path** (model_gateway.py:97-98): switch to `with_structured_output(response_model, include_raw=True)`; unpack `{"raw": AIMessage, "parsed": model, "parsing_error": ...}`. Return `parsed`. Read usage off `raw` the same way. **Preserve the existing fallback chain** (response_format_unavailable → `_decide_with_local_json_validation`; validation failure → retry). Keep recovery codes (`model_request_failed`, `invalid_model_response`).
   - Initialize `self._last_usage = None` in `__init__`; reset to None at the start of each `decide()` call; set after a successful invoke.
3. `tracing.py`: `DecisionTrace` → `Callable[[AgentRole, dict[str, str], dict[str, Any] | None], None]`. `decision_summary` unchanged.
4. `runtime.py` `trace` closure: accept `turn_metadata`; extract `model_name/input_tokens/output_tokens`; pass to `create_turn`.
5. Each agent's `trace(...)` call site (planner:166-172, executor:265-271, verifier:116-128): pass `self._gateway.last_usage` as the 3rd arg.

**Interfaces this introduces (for later tasks):** the `turn_metadata` 3rd arg on `DecisionTrace`; the `last_usage` property on the gateway. T2/T5 depend on these.

**Acceptance:**
- `pytest tests/unit/test_agent_model_gateway.py tests/unit/test_agent_runtime.py tests/unit/test_agent_runtime_repository.py tests/unit/test_planner_agent.py tests/unit/test_executor_agent.py tests/unit/test_verifier_agent.py -q` green at 100% branch coverage.
- Assertions: a fake gateway returning `last_usage={"model_name":"x","input_tokens":10,"output_tokens":5}` → runtime's `create_turn` called with those values; gateway with `last_usage=None` → `create_turn` called with `None` (no crash); structured path with `include_raw=True` unpacks `parsed` correctly and preserves fallback on `parsing_error`.
- `ruff check` clean. No behavior change to decision routing.

**Validation:** P0 — no eval re-run needed (pure observability).

**Risks:** `include_raw=True` may change structured-path exception shapes; preserve recovery codes. Some providers return `usage_metadata=None` — handle gracefully (write None, no crash).

---

## Task 2: Context Manifest (P0-2)

**Objective:** Per decision, record a context manifest (system-prompt char count, tool-catalog count + total chars, observation count + total chars, evidence total chars, model name) for debugging "how much context did this decision consume".

**Source:** report §四 P0-2; builds on T1's `turn_metadata`/trace expansion.

**Depends on T1:** the `DecisionTrace` callback now takes a 3rd `turn_metadata` arg (introduced by T1) and `AgentModelGateway.last_usage` exists. Extend `turn_metadata` with a `context_manifest` sub-dict.

**Files:**
- `backend/app/db/models.py` — add `context_manifest: Mapped[dict | None] = mapped_column(JSON, nullable=True)` to `AgentTurn` (after `decision_json`, ~line 1717).
- `alembic/versions/<20260805_0020_agent_turn_context_manifest>.py` — add the nullable JSON column; include downgrade (drop column). Match the exact filename pattern of existing migrations in `alembic/versions/`.
- `backend/app/repositories/agent_runtime.py` — `create_turn` gains `context_manifest: dict[str, Any] | None = None` param; persisted.
- `backend/app/services/agent_runtime/tracing.py` (or a new `context_manifest.py` if tracing.py should stay decision-summary-only — implementer's call, but keep it pure and tested) — add `build_context_manifest(*, instruction: str, available_tools: list | None, observations_for_decision: list, evidence_chars: int | None, model_name: str | None) -> dict[str, Any]`. Returns `{"system_prompt_chars": int, "tool_catalog_count": int, "tool_catalog_chars": int, "observation_count": int, "observation_chars": int, "evidence_chars": int|None, "model_name": str|None}`. Pure function, no PII (counts/chars only).
- `backend/app/services/agent_runtime/runtime.py` — `trace` closure: persist `context_manifest` via `create_turn`.
- `backend/app/services/agent_runtime/planner_agent.py`, `executor_agent.py`, `verifier_agent.py` — at the trace call site, build the manifest (instruction constant, available_tools local, observations_for_decision local are all in scope) and include it in `turn_metadata["context_manifest"]`. Executor has `available_tools`; planner/verifier pass `None`/`[]`.

**Exact changes:**
1. New `context_manifest` JSON nullable column + migration 0020.
2. `create_turn(..., context_manifest: dict | None = None)`.
3. `build_context_manifest(...)` pure helper.
4. At each agent's trace call site, build the manifest and put it in `turn_metadata`; runtime's `trace` closure extracts it and passes to `create_turn`.

**Interfaces:** reuses T1's `turn_metadata`. `build_context_manifest` is reused by T5 (prompt section stats).

**Acceptance:**
- `pytest tests/unit/test_agent_runtime.py tests/unit/test_agent_runtime_repository.py tests/unit/test_planner_agent.py tests/unit/test_executor_agent.py tests/unit/test_verifier_agent.py -q` green at 100%.
- Migration 0020 applies + downgrades cleanly (`alembic upgrade head` then `alembic downgrade -1` then `alembic upgrade head`).
- Assertions: manifest counts match constructed inputs; `create_turn` receives `context_manifest`; manifest contains only ints/strings/None (no payload content, no PII).

**Validation:** P0 — no eval re-run.

**Risks:** migration on existing DBs (nullable, safe). Building manifest at 3 call sites — keep DRY via the helper.

---

## Task 3: Tool error_message granularity (P0-3)

**Objective:** Add optional `error_message` to `ToolObservation`; make `ToolRegistry.invoke` surface specific error codes + a short message from typed exceptions; upgrade bare `ValueError` in resume_tailoring/career_planning to typed exceptions with `code` (mirroring `PublicJobFetchError`), so the LLM can distinguish "evidence missing" vs "evidence incomplete".

**Source:** report §四 P0-3; schemas.py:174-192; tool_registry.py:100-156; resume_tailoring.py:62-66; career_planning.py:65-69; job_discovery.py:68-73.

**Files:**
- `backend/app/services/agent_runtime/schemas.py` — add `error_message: str | None = Field(default=None, max_length=500)` to `ToolObservation` (extra="forbid" requires declaration).
- `backend/app/services/agent_runtime/tool_registry.py` — `invoke` except block (146-151): read `.code` and a short message off the exception via `getattr(exc, "code", None)` and `str(exc)`; construct `ToolObservation(error_code=code or "tool_execution_failed", error_message=<short sanitized message>)`. **Honor existing typed exceptions** (`PublicJobFetchError.code`, `SheetQueryError.code`) — they now surface their specific codes instead of `tool_execution_failed`.
- `backend/app/services/career_skills/resume_tailoring.py` — replace `raise ValueError("target_evidence_not_found")` (62) and `raise ValueError("target_evidence_incomplete")` (66) with a typed `ResumeTailoringError(RuntimeError)` with `.code` (mirror PublicJobFetchError at job_discovery.py:68-73).
- `backend/app/services/career_skills/career_planning.py` — same for lines 65/69 with `CareerPlanningError`.
- Per report "仿 PublicJobFetchError", per-skill exceptions are acceptable. A shared `CareerSkillError` base is optional — implementer's call, but keep DRY and tested.

**Exact changes:**
1. `ToolObservation.error_message: str | None = Field(default=None, max_length=500)`. Validator unchanged (failed still requires `error_code`; `error_message` is supplementary).
2. `invoke`: in the `except Exception` block, `code = getattr(exc, "code", None) or "tool_execution_failed"`; `message = _sanitize_error_message(str(exc))` (truncate to ≤500, strip any URL userinfo/tokens — cap length, no raw payload). Return `ToolObservation(error_code=code, error_message=message)`. Optionally pass `error_message` in other failed branches (`invalid_tool_input` etc.) where a short message helps — min requirement is the `tool_execution_failed` branch.
3. New typed exceptions `ResumeTailoringError`/`CareerPlanningError` with `code` attr, replacing the bare ValueErrors.

**Interfaces:** `ToolObservation.error_message` is additive (optional, default None) — existing constructors/serializers unaffected. `observation_for_decision` (observation_projection.py) already does `model_dump(mode="json")` so `error_message` flows to the model automatically.

**Acceptance:**
- `pytest tests/unit/test_agent_tool_registry.py tests/unit/test_resume_tailoring_pev_skill.py tests/unit/test_career_planning_pev_skill.py tests/unit/test_pev_job_discovery_skill.py tests/unit/test_career_sheets_skill.py tests/unit/test_agent_runtime_branches.py -q` green at 100%.
- Assertions: a tool raising `ResumeTailoringError("target_evidence_not_found")` → `invoke` returns `ToolObservation(status="failed", error_code="target_evidence_not_found", error_message=<non-empty>)`; a tool raising a bare `RuntimeError("x")` → `error_code="tool_execution_failed"`, `error_message=<short>`; `PublicJobFetchError("unsafe_public_url")` now surfaces `error_code="unsafe_public_url"` (previously `tool_execution_failed`) — update affected tests.
- `error_message` never contains raw payloads/tokens/credentials (assert in tests).

**Validation:** model-input change (LLM now sees specific codes + messages) — recommend running affected 83-doc subset (questions hitting tool errors) post-implementation.

**Risks:** surfacing specific codes for `PublicJobFetchError`/`SheetQueryError` changes model input for job-discovery/career-sheets failures (previously generic). This is intended (report P0-3) but is a behavior change — update existing tests that assert `tool_execution_failed` for these.

---

## Task 4: Observation context layered compression (P1-4)

**Objective:** Replace the run-level 48,000-char evidence hard-cut (which drops early-link evidence in long chains) with a layered strategy: older evidence → single summary line; recent evidence → full projection; keep the 48,000-char total budget. Also bound the `observations_for_decision` list (currently unbounded O(turns)) with the same old→summary/recent→full strategy.

**Source:** report §四 P1-4. NOTE: the report's "10 条硬截断" framing is imprecise (see Pre-flight note) — real mechanisms are the 48,000 evidence cap (runtime.py:546-572) + unbounded observation list. Strategy is the report's: old→summary, recent→full, keep total budget.

**Files:**
- `backend/app/services/agent_runtime/observation_projection.py` — add `summarize_observations(observations_for_decision: list, *, keep_recent: int, budget_chars: int) -> list` that keeps the `keep_recent` most-recent full and collapses older ones to single summary lines (`{tool_name, status, source_url, content_hash}` only). Keep `_VISIBLE_TEXT_EXCERPT`/`_MAX_PROJECTED_ITEMS` per-observation caps.
- `backend/app/services/agent_runtime/runtime.py` — the 48,000-char evidence assembly (546-572): instead of hard-cutting when `remaining_characters <= 0`, summarize older evidence into single lines once over budget. Keep total ≤ 48,000.
- `backend/app/services/agent_runtime/executor_agent.py` (and planner/verifier where observations are passed) — apply `summarize_observations` to `observations_for_decision` before putting it in `state` (215-264): recent full + older summarized, total chars within budget.

**Exact changes:**
1. New `summarize_observations(...)` pure function with `keep_recent` default (e.g., 5) and `budget_chars`.
2. Evidence assembly (runtime.py:546-572): when budget exceeded, emit summary lines for older artifacts instead of dropping them silently.
3. Observation list: apply summarization so the list passed to the model stays bounded (recent full + older summarized), total chars within budget.

**Interfaces:** none new externally; internal helper. Observation summary lines reuse existing fields (tool_name/status/source_url/content_hash) — no schema change.

**Acceptance:**
- `pytest tests/unit/test_agent_runtime.py tests/unit/test_agent_runtime_branches.py tests/unit/test_executor_agent.py tests/unit/test_planner_agent.py tests/unit/test_verifier_agent.py -q` green at 100%.
- Assertions: with >keep_recent observations, older ones become summary dicts (no `visible_text`/`pages`), recent stay full; total chars ≤ budget; a simulated 20+ observation chain does not exceed the char budget; evidence over 48,000 chars produces summary lines, not silent drops.

**Validation:** P1 — model-input change. Run affected 83-doc subset (the 15 multi-link chains) to confirm no regression; expect improvement (early-link evidence preserved as summaries).

**Risks:** summarization changes what the model sees in long chains — could change LLM behavior. `keep_recent`/budget params are the levers. Conservative defaults. Validate.

---

## Task 5: System prompt structured sectioning (P1-5)

**Objective:** Restructure the executor's ~139-line `_EXECUTOR_INSTRUCTION` (and planner/verifier) into named sections (角色/行为规则/流程/输出契约/禁止项) WITHOUT changing semantic content; emit per-section char stats via T2's manifest helper.

**Source:** report §四 P1-5; executor_agent.py:28-166; planner_agent.py:23-96; verifier_agent.py:25-43.

**Depends on T2:** `build_context_manifest` exists; extend it (or add `prompt_section_stats`) for per-section stats.

**Files:**
- `backend/app/services/agent_runtime/executor_agent.py` — restructure `_EXECUTOR_INSTRUCTION` into sections (preserve exact rules/wording; move text into section headers). **No rewording of behavioral rules.**
- `backend/app/services/agent_runtime/planner_agent.py`, `verifier_agent.py` — same (smaller).
- `backend/app/services/agent_runtime/tracing.py` (or `context_manifest.py` from T2) — extend `build_context_manifest` to compute per-section char counts when the instruction is sectioned (or add `prompt_section_stats(instruction: str) -> dict`); wire into T2's manifest.

**Exact changes:**
1. Section the three instruction constants with clear `## 角色`/`## 行为规则`/`## 流程`/`## 输出契约`/`## 禁止项` (or role-appropriate) headers, moving existing text verbatim into sections. **Do not rewrite rules** — the report warns "确定性 > 提示词"; restructuring only.
2. Add per-section char stats to the context manifest (T2).

**Interfaces:** uses T2's manifest. Instruction still passed as `instruction=` to `gateway.decide` (unchanged contract).

**Acceptance:**
- `pytest tests/unit/test_executor_agent.py tests/unit/test_planner_agent.py tests/unit/test_verifier_agent.py tests/unit/test_agent_runtime.py -q` green at 100%.
- Assertions: sectioned instruction still contains all original rule phrases (assert key phrases present); prompt stats helper returns per-section char counts; manifest includes section stats.

**Validation:** P1 — model-input change (prompt restructured). Run affected 83-doc subset; expect neutral (content preserved). If regression, the restructure silently changed semantics — bisect.

**Risks:** any rewording risks eval regression. Constraint: move-only, preserve wording. Reviewer checks no rule text was altered.

---

## Task 6: Tool catalog incremental reuse (P1-6)

**Objective:** Enable provider prompt caching for the (step-constant) tool catalog by moving it into the SystemMessage stable prefix, behind a config flag (default off = current behavior); measure before/after token cost via T1.

**Source:** report §四 P1-6; survey found catalog is in HumanMessage JSON (not cacheable); moving to SystemMessage prefix is the viable path.

**Depends on T1:** token metering provides the before/after measurement.

**Files:**
- `backend/app/config.py` — add `agent_harness_catalog_in_system_prompt: bool = False`.
- `backend/app/main.py` — wire the setting into the gateway/runtime if needed.
- `backend/app/services/agent_runtime/model_gateway.py` — when the flag is on, prepend the tool catalog to the SystemMessage content (stable prefix) instead of (or in addition to) the state JSON. When off, current behavior.
- `backend/app/services/agent_runtime/executor_agent.py` — pass the catalog to the gateway in a way that supports both placements (or the gateway reads it from state and moves it to SystemMessage when flagged).

**Exact changes:**
1. New setting `agent_harness_catalog_in_system_prompt` (default False).
2. Gateway: if flag on, build SystemMessage as `f"Role: {role}. {instruction}\n\nAvailable tools:\n{catalog_json}\n..."` (catalog in stable prefix); HumanMessage state carries observations (changing) but NOT the catalog. If off, current (catalog in state JSON).
3. Measure: with T1's token metering, compare `input_tokens` for turn 2+ of a step with flag on vs off (catalog prefix cached → lower `input_tokens` on repeat).

**Interfaces:** uses T1's usage metering for measurement. Config flag is additive (default off).

**Acceptance:**
- `pytest tests/unit/test_agent_model_gateway.py tests/unit/test_executor_agent.py tests/unit/test_agent_runtime.py tests/unit/test_career_skill_registry.py -q` green at 100% (both flag branches).
- Assertions: flag off → catalog in HumanMessage (current); flag on → catalog in SystemMessage, HumanMessage excludes catalog; both produce a valid decision.

**Validation:** P1 — with flag on, run affected 83-doc subset; confirm no regression. Use T1 token data to assess cache benefit. If regression or no benefit, leave flag off (default) and document.

**Risks:** moving catalog to SystemMessage changes prompt structure the model has seen — could regress. Flag-default-off contains the risk. DeepSeek prompt-caching support varies — measure, don't assume.

---

## Task 7: DeepSeek official JSON mode (P1-7)

**Objective:** Switch deepseek-v4 from plain invoke + local JSON validation (`prefer_local_json_validation=True`) to the structured path with `method="json_mode"` (sends `response_format={"type":"json_object"}`), guiding the provider to emit valid JSON at the protocol layer. Preserve the full degradation chain.

**Source:** report §四 P1-7 + §二 DeepSeek schema investigation; model_gateway.py:90-98,250-275; survey confirmed langchain_openai 1.1.11 supports `method="json_mode"`.

**Depends on T1:** structured path uses `include_raw=True` (from T1) for usage capture — confirm it composes with `method="json_mode"`.

**Files:**
- `backend/app/services/agent_runtime/model_gateway.py` — for deepseek-v4, set `prefer_local_json_validation=False` and use `with_structured_output(response_model, method="json_mode")` in the structured path. Keep `_decide_with_local_json_validation` as fallback. Keep `_strip_json_fence` as a defensive layer.
- `backend/app/services/agent_runtime/model_gateway.py` `build_agent_model_gateway` (250-275) — change the deepseek-v4 branch: instead of `prefer_local_json_validation=True`, construct the gateway with `structured_method="json_mode"` (new ctor param) and `prefer_local_json_validation=False`. Keep `extra_body={"thinking":{"type":"disabled"}}`.
- Ensure the prompt contains the word "json" (DeepSeek json_mode requirement) — add an explicit "Return one JSON object" line to the SystemMessage if not already present (without changing the action contract).

**Exact changes:**
1. `LangChainModelGateway.__init__` gains `structured_method: str = "json_schema"` (default preserves non-DeepSeek behavior).
2. Structured path: `self._model.with_structured_output(response_model, method=self._structured_method)`. For deepseek-v4 → `"json_mode"`.
3. `build_agent_model_gateway`: deepseek-v4 branch → `LangChainModelGateway(ChatOpenAI(**kwargs), prefer_local_json_validation=False, structured_method="json_mode")`. Keep `extra_body={"thinking":{"type":"disabled"}}`.
4. Preserve fallback: if `method="json_mode"` raises response_format_unavailable → `_decide_with_local_json_validation` (existing). If validation fails → existing retry chain → `invalid_model_response` → safe `waiting_user`.
5. Confirm `include_raw=True` (T1) works with `method="json_mode"`; if not, capture usage another way and note it.

**Interfaces:** uses T1's `include_raw=True`. `structured_method` ctor param.

**Acceptance:**
- `pytest tests/unit/test_agent_model_gateway.py tests/unit/test_agent_runtime.py -q` green at 100%.
- Assertions: deepseek-v4 gateway constructed with `structured_method="json_mode"`, `prefer_local_json_validation=False`; non-deepseek gateway still uses `structured_method="json_schema"` default; fallback chain intact (json_mode failure → local-json → invalid_model_response); `include_raw=True` works with json_mode (or usage captured otherwise).

**Validation:** P1 — provider-protocol change affecting ALL decisions. Run affected 83-doc subset (broad; prioritize questions that previously hit `invalid_model_response`/JSON retries). Expect reduction in retries. If regression, revert to `prefer_local_json_validation=True` and document.

**Risks:** `include_raw=True` + `method="json_mode"` composition. DeepSeek json_mode occasional empty content — fallback handles. The "json" word in prompt requirement. High-impact — validate thoroughly.

---

## Post-implementation: behavioral validation (affected 83-doc subset)

After all 7 tasks pass their SDD gate (100% coverage + spec + quality) and the final whole-branch review is clean, run the affected 83-doc subset for the model-input-changing tasks (T3, T4, T5, T6, T7) to confirm no behavioral regression vs the authoritative baseline (70/12/1, 84.3%). Requires `DEEPSEEK_API_KEY` + public network.

```
.\.venv\Scripts\python.exe -m tests.question.eval_runner --ids <affected subset> --out-dir tests/question/eval_results/round_p0p1
```

If regression: bisect by task (revert per-task), identify the culprit, feed back as a fix. If clean: merge to master via finishing-a-development-branch.
