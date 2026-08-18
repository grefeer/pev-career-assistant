# CLAUDE.md - Career Assistant Platform

## Project Overview

A multi-agent personal career assistant. The default runtime is a self-built **adaptive Planner–Executor–Verifier (PEV) agent runtime** (plain Python + Pydantic + langchain-openai). The harness (planning, budgets, verification routing, persistence) is self-built; the production Executor uses a DeepAgents tool-calling loop (`agent_runtime/executor/deep_executor.py`) inside that self-built harness. It drives three career skills: job discovery, job matching, and resume tailoring. Supporting capabilities include a WP2 manual-import → admin review → verified student job-center flow, structured talent profiles with evidence-based matching, and a Windows executor skeleton/simulator for human-reviewed form filling. Real-site GUI adapters and production deployment remain incomplete; final submission is always human-controlled.

> The previous LangGraph/Deep-Agents job-discovery pipeline (Supervisor / Web Navigation Agent / Strategy Router / skill runtime / worker) has been retired and removed. `docs/job-discovery-legacy-architecture-summary.md` archives that design. The current `job-discovery` is a career skill inside the PEV runtime, not a standalone Tencent→Smartsheet pipeline.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12+, FastAPI, SQLAlchemy, Pydantic / pydantic-settings |
| Agent runtime | Adaptive PEV harness (plain Python) + langchain-openai (ChatOpenAI, OpenAI-compatible) |
| Frontend | Vue 3, Vite, TypeScript, Nginx |
| Database | MySQL 8.4 (authoritative), Redis 8 (SSE/cache), MinIO (encrypted objects) |
| LLM | DeepSeek API (OpenAI-compatible via langchain-openai) |
| Browser | Playwright (headless Chromium, used by the job-discovery skill) |
| Infra | Docker Compose, Alembic migrations |

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan (assembles PEV runtime)
│   │   ├── config.py            # Typed Settings (pydantic-settings)
│   │   ├── api/
│   │   │   ├── dependencies.py  # Auth dependency injection
│   │   │   ├── router.py        # Route aggregation
│   │   │   └── routes/          # agent_runtime, auth, health, metrics, profiles
│   │   ├── db/
│   │   │   ├── base.py          # UUID + timestamp mixins
│   │   │   └── models.py        # All ORM models
│   │   ├── domain/              # Pure domain rules (enums, allowlists, contracts)
│   │   │   └── agent_runtime.py # PEV lifecycle contracts (roles, states, transitions)
│   │   ├── repositories/        # Data access layer (SQL only, no business logic)
│   │   └── services/
│   │       ├── agent_runtime/   # ★ PEV harness: runtime + 2 LLM agents (Planner/Executor) + deterministic CompletionGate, gateway, tools, budgets
│   │       ├── career_skills/   # ★ 3 career skills + tool registry + manifest
│   │       ├── job_discovery/   # Retained JD extraction helpers (schemas, tools/jd_extraction)
│   │       ├── common/          # Shared service helpers
│   │       ├── auth.py          # Registration, login, JWT, Argon2
│   │       ├── profiles.py      # Profile lifecycle
│   │       ├── profile_parser.py
│   │       ├── storage.py       # MinIO encrypted object storage
│   │       └── rate_limit.py
│   └── entrypoint.py            # Container entrypoint
├── skill/                       # Self-contained skill packages (PRESERVE — do not delete)
│   └── <skill-name>/            # Hyphenated skill dir, e.g. job-discovery/
│       ├── SKILL.md             # Skill contract (purpose, tools, safety)
│       ├── scripts/             # Playwright / browse / validate / dedup / state scripts
│       ├── references/          # Skill-local reference material
│       └── runtime/             # Underscored Python module package (skill.<name>.runtime.*) — single source of truth for skill business logic
├── frontend/src/features/
│   ├── agent-workspace/         # PEV natural-language task, evidence, artifacts, human recovery
│   └── profile/                 # Profile workspace
├── executor/                    # Windows executor skeleton/simulator (assisted form filling)
├── docs/                        # Design specs, plans, runbooks, tech docs
├── tests/
│   ├── unit/                    # Primary deterministic/unit suite (key-point coverage, see Coverage Policy)
│   ├── integration/             # Integration + live smoke tests (gated: RUN_LIVE_PEV_E2E, ALLOW_DESTRUCTIVE_MYSQL_TESTS)
│   ├── e2e/                     # E2E Playwright + fixture tests
│   ├── question/                # 83-question full83 PEV eval harness (eval_runner, compare_full83/merge/compare rounds)
│   └── manual/                  # Standalone smoke/diagnostic scripts (excluded from pytest)
├── alembic/versions/            # Database migrations (0001 -> 0024; head: 20260812_0024_retire_legacy_tables.py)
├── scripts/                     # Admin/dev scripts (create_admin, seed_strategies, fixtures, etc.)
└── docker-compose.yml
```

## Key Commands

### Setup & Environment

```powershell
# Activate virtual environment
.\.venv\Scripts\activate

# Check required env vars (must exist in User scope)
$required = @('DB_PASSWORD','REDIS_PASSWORD','MINIO_ROOT_USER','MINIO_ROOT_PASSWORD','APP_AUTH_SECRET','OBJECT_ENCRYPTION_KEY')
foreach ($n in $required) { if (-not [Environment]::GetEnvironmentVariable($n,'User')) { throw "Missing: $n" } }

# Docker Compose (current dev machine uses non-default ports)
$env:MYSQL_HOST_PORT='3307'; $env:REDIS_HOST_PORT='6380'; $env:MINIO_HOST_PORT='19000'; $env:MINIO_CONSOLE_HOST_PORT='19001'; $env:BACKEND_HOST_PORT='18000'; $env:FRONTEND_HOST_PORT='15173'
docker compose -p platform-foundation up -d --build
```

### Testing

```powershell
# All unit tests (key-point coverage; per-change targeted run, not the full 100% gate — see Coverage Policy)
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q

# PEV + career-skills targeted regression
.\.venv\Scripts\python.exe -m pytest tests/unit/test_agent_runtime*.py tests/unit/test_planner_agent.py tests/unit/test_executor_agent.py tests/unit/test_*pev_skill.py tests/unit/test_job_matching_skill.py -q

# Lint
.\.venv\Scripts\python.exe -m ruff check backend tests scripts

# Frontend
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build

# Health checks
Invoke-RestMethod http://127.0.0.1:18000/api/health/ready

# Live PEV end-to-end (requires env vars + LLM; resume PDF read in-memory only)
$env:RUN_LIVE_PEV_E2E='1'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_pev_live_end_to_end.py -v

# Full 83-question eval loop (real DeepSeek + public fetch; round set: tests/question/redesign, manifest.json = 83 ids)
#   single-process launcher: .\scripts\launch_full83_gate_eval.ps1 -RunName gate83_round_<N>
#   4-process staggered launcher: .\scripts\run_full_83_staggered.ps1 -WorkerCount 4 -StaggerSeconds 90 -RunName full83_round_<N>
# Single-question dry run (per-question JSON under --out-dir):
.\.venv\Scripts\python.exe -m tests.question.eval_runner --ids Q011 --question-dir tests/question/redesign --out-dir tests/question/eval_results/round_1
```

### Database

```powershell
# Run migrations
.\.venv\Scripts\alembic.exe upgrade head

# Check current migration
.\.venv\Scripts\alembic.exe current
```

## Architecture Rules

### Three-Layer Separation (STRICT)

The target architecture for new and changed business flows is **API -> Service -> Repository**:

- **API layer** (`routes/`): Parse request -> call service -> return JSON. NEVER write SQL or business logic.
- **Service layer** (`services/`): All business logic. NEVER write raw SQL or touch HTTP.
- **Repository layer** (`repositories/`): Data access only. NEVER make business decisions.

Health-check SQL is an intentional infrastructure probe rather than business data access.

### Agent vs Tool vs Skill

Industry framing: **agent = LLM + harness** — an LLM alone is not an agent; the harness (loop, tools, budgets, guardrails, state) is what turns it into one.

- **Agent**: LLM-in-the-loop with autonomous tool selection and plan/verify/replan behavior. The current runtime has **two LLM agents**: **Planner** and **Executor**, each an LLM wrapped in its own decision loop/guardrails (see `backend/app/services/agent_runtime/`). The retired LLM **Verifier** role is replaced by the deterministic **CompletionGate** (`completion_gate.py`) — verification is harness code, not a model call.
- **Harness**: the deterministic scaffolding that turns an LLM into an agent — control flow, budgets, permissions/tool grants, persistence, event routing, and the CompletionGate. Two levels: per-role harness (Planner's loop + retries/fallbacks; Executor's middleware + ledger) and the run-level harness (`runtime.py`).
- **Tool**: Agent-callable deterministic Python function with fixed input/output, registered in `ToolRegistry` with role + skill scoping. Examples: `fetch-public-job-pages`, `extract-observed-job-details-batch`, `match-observed-jobs`, `build-resume-tailoring-brief`. Tools are part of the harness side: the harness grants them to an agent.
- **Skill**: A coherent tool bundle exposed to the PEV runtime via `career_skills/registry.py` + `manifest.py`. Three skills: `job-discovery`, `job-matching`, `resume-tailoring`. Each `PlanStep` allows exactly ONE skill; the Executor only sees that skill's tools.
- **Catalog ↔ invoke consistency**: a scoped `tool_catalog` (one skill per step) omits tools with no `skill_name`, matching `invoke`'s `tool_skill_forbidden` rejection - the Executor is never advertised a tool it cannot call.

### Security Hard Gates

These are code-level restrictions, NOT conventions:

1. **Never auto-click final submit**: No `task:submit` scope exists. The GUI executor stops at `READY_FOR_REVIEW`. Human must click submit.
2. **Never bypass login/captcha/anti-bot**: If blocked, mark `needs_manual_review` - never attempt to circumvent.
3. **Student API only returns `verified` jobs**: Filter at SQL level. Other statuses must never leak.
4. **Never write secrets to repo/logs/argv**: Passwords, tokens, API keys, raw payloads - all rejected.
5. **Never use Redis as authority**: MySQL is the single source of truth for business state.
6. **Never trust device token alone**: Task actions require task lease with scope validation.
7. **Job review requires version check**: JobPosting completion/review/decision writes validate `review_version` (optimistic locking). Concurrent edits get 409.

### Configuration

- All settings live in `backend/app/config.py` -> `Settings` (pydantic-settings).
- `agent_harness_enabled` gates the PEV runtime; a missing model key logs a warning and the API safely returns `agent_harness_unavailable` / `agent_harness_disabled` rather than crashing.
- Rejects: demo keys, template credentials, and SQLite `database_url` in production (enforced in `validate_production_settings`).
- `OBJECT_ENCRYPTION_KEY` must be Base64-encoded 32 bytes.
- `.env` file at project root for local overrides; never committed.

## Personal Career Assistant (PEV Runtime)

> Full architecture, sequence diagrams, and module map: [docs/pev-agent-architecture.zh-CN.md](docs/pev-agent-architecture.zh-CN.md). Design spec: [docs/superpowers/specs/2026-08-01-personal-career-agent-adaptive-pev-design.md](docs/superpowers/specs/2026-08-01-personal-career-agent-adaptive-pev-design.md).

Two autonomous LLM agents collaborate around a deterministic lifecycle harness and a deterministic completion gate:

| Role | Responsibility |
|------|----------------|
| **Planner** | Reads goal + confirmed profile facts + observed evidence; produces an `ExecutionPlan` with one skill per step |
| **Executor** | Perceives-decides-acts-observes within a single step's skill scope; calls tools to gather evidence |
| **CompletionGate** (deterministic) | Derives PASS / RETRY_EXECUTOR / NEED_USER / FAIL from the Skill registry completion contract plus budget state (replaces the retired LLM Verifier) |

Key invariants enforced by the harness (not the agents):

- **Skill-authority scoping**: each `PlanStep` allows exactly one skill; `ExecutionPlan.validate_plan_authority` ensures step skills ⊆ task allowed_skills.
- **Evidence-bound tools**: only tool-produced public evidence (with `source_url` + `content_hash`) is persisted; model-proposed URIs are never trusted.
- **Budgets**: `AgentBudget` (turns / tool_calls / replans / wall-clock), `ToolCallBudget`, `AgentTurnBudget` are hard ceilings enforced by the harness. `build_adaptive_agent_budget` scales turns by skill count.
- **Tool exceptions never leak**: `ToolRegistry.invoke` converts any failure into a `ToolObservation(status=failed, error_code=...)`.
- **Duplicate-call dedup + stall breaker**: a consecutive identical tool call after a success returns `duplicate_tool_call` without consuming budget (prevents executor thrash); after 3 consecutive no-progress decisions (deduped re-calls or a blocked public search) the Executor hands the step to the human (`needs_user`) instead of burning turns on a stuck loop.
- **Safe degradation to `waiting_user`**: an `invalid_model_response` from an agent, a gate `RETRY_EXECUTOR` loop past `max_replans`, or wall-clock budget exhaustion (`wall_clock_budget_exhausted`) at an agent boundary, ends the run as recoverable `waiting_user` with a human-readable question (never a crash or hard failure); `resume()`/`recover()` continue in the remaining budget. Wall-clock exhaustion is a transport/resource pause (the clock window refreshes on resume), not a business failure; turn/tool budgets remain the latency-independent work authority and are NOT reset on resume (only the clock window refreshes).
- **Bounded automatic recovery (auto-resume)**: a `waiting_user` pause whose terminal contract is a verifier/model decision (`need_user`, `verification_failed`, `no_progress_duplicate`, `invalid_model_response`, `wall_clock_budget_exhausted`, or the executor hand-off codes `route_already_consumed` / `candidate_urls_already_supplied` / `target_evidence_not_found` / `target_role_mismatch` / `target_source_mismatch`; the contract is read from the latest `run_needs_user` / `planner_needs_user` / `planner_budget_exhausted` event) is resumed by the harness itself up to `AgentBudget.max_auto_recoveries` times (default 1 = 2 attempts total, schema cap 5). Each attempt steps the `AgentBudget` ceilings up (1.5x then 2x, clamped at schema maxima) and relaxes the consecutive-stall breaker via `context.max_consecutive_stalls` (4 then 5). Source-access blocks (login/captcha/anti-bot) and repeated-plan oscillation guards never auto-recover — they remain human hand-offs.
- **MySQL authority**: Run/Plan/Step/Turn/Event/Artifact persist to MySQL; SSE polls MySQL every 1s; Redis is non-authoritative.
- **Replan budget survives recovery**: `recover()`/`resume()` resume `replans` from the persisted plan count (`max(0, revision - 1)`), so a crashed run cannot re-spend budget already consumed on replanning.
- **Incremental decision-state projection**: each agent appends a bounded projection of a tool observation once per call (visible_text excerpted to 1,200 chars; pages/details capped at 10) and reuses the accumulated list per turn, so decision context grows O(turns) not O(turns²) (shared logic in `observation_projection.py`).
- **Bounded event payloads**: `append_event` caps serialized payload size at `agent_harness_max_event_payload_bytes` (wired in the lifespan); an oversize payload is replaced with a `{"_payload_truncated": True, "original_bytes": ...}` stub so a runaway observation can't grow the event table / SSE stream.

### Three Career Skills

| Skill | Produces | Boundary |
|-------|----------|----------|
| `job-discovery` | Public job-page evidence, structured JDs | Public HTTP(S) pages only; rejects intranet/login/captcha/anti-bot bypass |
| `job-matching` | Sourced job-match ranking | Compares only JDs already captured in the same Run |
| `resume-tailoring` | Auditable resume edit ops | Each op cites a confirmed fact field + target JD; cannot fabricate |

Tool registry: [backend/app/services/career_skills/registry.py](backend/app/services/career_skills/registry.py). **12 tools** are registered across the three skills (job-discovery 10, job-matching 1, resume-tailoring 1); `search-public-job-pages` is executor-only.

## Executor Implementation

The self-built `agent_runtime` owns the PEV lifecycle (planning, budgets,
verification routing, persistence).  The production Executor path
(`agent_runtime/executor_agent.py`) delegates to `agent_runtime/executor/deep_executor.py`,
a DeepAgents tool-calling loop.  The experimental `deepagents_runtime`
package was removed: the self-built harness + one DeepAgents-based Executor is
the single production path.

Skill business logic lives under `skill/<name>/runtime/` (Python packages
with underscored module paths, e.g. `skill.job_discovery.runtime`) and is the
source of truth for the PEV tools.  `backend/app/services/career_skills/`
keeps `registry.py` / `manifest.py` (the PEV tool host) plus thin aliases to
the skill runtime modules for backward-compatible imports.

## State Machines

### AgentRun States (PEV)

```
queued -> running -> {waiting_user, succeeded, failed, cancelled}
waiting_user -> running (resume) | failed
```

StepStatus: `planned -> running -> succeeded | failed | skipped`. VerificationDecision: `PASS | RETRY_EXECUTOR | REPLAN | NEED_USER | FAIL`. Recovery from a crashed `running` run uses `replan_from_durable_evidence` and only trusts MySQL-persisted state.

### ApplicationTask States (executor subsystem, 12 states, 3 actors)

```
CREATED -> WAITING_FOR_DEVICE -> DISPATCHED -> RUNNING -> READY_FOR_REVIEW
  -> OBSERVING_USER_SUBMISSION -> SUBMITTED_SUCCESS / SUBMITTED_FAILED / RESULT_UNKNOWN
DISPATCHED or RUNNING -> WAITING_FOR_HUMAN -> RUNNING or READY_FOR_REVIEW
DISPATCHED / RUNNING / WAITING_FOR_HUMAN -> FAILED
Non-terminal pre-submission states -> CANCELLED (human only)
```

**Key**: Only HUMAN can transition `READY_FOR_REVIEW` -> `OBSERVING_USER_SUBMISSION`. SYSTEM never. EXECUTOR can only observe after that point.

### JobPosting States (5 states)

```
pending_completion -> pending_review -> verified -> expired
pending_completion / pending_review -> rejected -> pending_review (after correction)
```

Students ONLY see `verified`. Admin review uses `review_version` optimistic locking.

## Key Conventions

### Naming & Code Style
- Python: ruff linter, docstrings for public functions
- Frontend: TypeScript strict, Vue 3 Composition API, Vitest
- Commit messages: `type: description` (feat:, fix:, chore:, docs:, test:)
- File organization: One class/concept per file in `domain/`; related operations in `services/`

### Idempotency
- Candidate idempotency key: SHA-256 of normalized `company + title + location + apply_url + evidence_hash`
- Feedback idempotency: `Idempotency-Key` header -> SHA-256 request fingerprint comparison

### Error Handling
- Blocked errors (`login_required`, `captcha`, `anti_bot`) -> `needs_manual_review`
- Transient failures -> retry within budget
- `partial_success` when some candidates found but budget exhausted
- Never expose raw payloads, tokens, or OCR full text in logs/audit

### WeChat / Public-Page Fetching
- Public-page fetching lives in the `job-discovery` career skill (`career_skills/job_discovery.py`), driven by Playwright via the `skill/job-discovery/scripts/browse.py` runtime (invoked with `cwd=skill_dir`).
- Blocked pages (login/captcha/anti-bot) are surfaced as failures -> `needs_manual_review`; the system never attempts to circumvent them.
- HTTP redirects are followed manually (`_fetch_validated`), re-running `_assert_public_url` (scheme, no userinfo, global IP) on every `Location` hop (max 5); a public page that redirects to a private or cloud-metadata address is rejected as `unsafe_public_url` rather than followed.

## Database Migrations

Alembic migrations numbered `YYYYMMDD_NNNN_description.py`. Current head is `0024`. Sequence:

- `0001`: Platform foundation (users, sessions, devices, tasks, audit)
- `0002`: Device credentials
- `0003`: Real job sync (sources, raw records, sync runs, job postings)
- `0004`: Job completion/review lifecycle
- `0005`–`0008`: Profiles, resumes, manual JD import, feedback, matching, snapshots
- `0009`–`0011`: Site adapters, multi-site extension, Wave 2 schema alignment
- `0012`: Personal mode memory
- `0013`: Personalized job discovery v1
- `0014`–`0016`: Company research reports, interview-prep kits, application-tracking records
- `0017`–`0019`: Agent runtime runs, artifacts, artifact-type dedup
- `0020`: Agent turn-context manifest
- `0021`: Profile active version
- `0022`: DeepAgents runtime runs + artifacts (tables retained for database continuity; the experimental runtime package was removed)
- `0023`: SeenJob cross-run dedup ledger (job-discovery dedup, TTL-pruned)
- `0024`: Retire 14 legacy tables (job-discovery / site-adapter / personalized-discovery / analysis-session schemas)
- Hotfixes (renamed to timestamp convention): `20260718_0008a` (MatchReport CHECK constraint), `20260718_0011a` (job-discovery tables), `20260718_0011b` (strategy/trajectory tables)

## Coverage Policy

`[tool.coverage.run]` in `pyproject.toml` measures **retained production packages** at 100% branch coverage (`fail_under = 100`). The PEV runtime, career skills, skill runtime packages, repositories, API, config, and `main` remain measured and must stay at 100%.

**Active development gate (since 2026-08-09):** the user relaxed the 100% branch gate for day-to-day development — "现在不要求100%覆盖了，只要关键点覆盖就行，加快开发效率". `fail_under = 100` stays in `pyproject.toml` as an opt-in check, but per-change verification is now: targeted unit tests on the changed module + a fast smoke + a flip-matrix / error-code check on critical mechanisms. Security invariants remain non-negotiable and must keep full coverage:

- `task:submit` scope never granted
- No secrets in code, logs, error messages, or audit records
- Blocked sources (login / captcha / anti-bot) always surface as `needs_manual_review`; no auto-bypass
- Student API returns only `verified` jobs
- `JobPosting` review writes use `review_version` optimistic locking
- Device actions validate task lease, not just device token
- Public-page redirect hops re-validate `_assert_public_url` (no jump to private / cloud-metadata addresses)

Revisit this gate with the user before re-enabling `fail_under = 100` as a hard pre-merge check.

## Important Docs

| Document | Content |
|----------|---------|
| [PEV Architecture](docs/pev-agent-architecture.zh-CN.md) | Current 2-agent (Planner/Executor) + deterministic CompletionGate PEV runtime: structure, sequence diagrams, modules, constraints |
| [Study Walkthrough v4](docs/study/v4/00-教学总览与参考.md) | Per-file interview-oriented code walkthrough of the 6 core py files + job-discovery skill; includes the "agent = LLM + harness" terminology mapping |
| [PEV Design Spec](docs/superpowers/specs/2026-08-01-personal-career-agent-adaptive-pev-design.md) | Adaptive PEV design, agent definitions, security boundaries, acceptance |
| [WP1 Tech Doc](docs/WP1-平台基础与权威数据-技术文档.md) | Platform foundation (auth, state machine, encryption, layers) |
| [WP2 Tech Doc](docs/WP2-真实职位同步与核验-技术文档.md) | Job sync, review pipeline, student submissions |
| [Job Discovery (legacy summary)](docs/job-discovery-legacy-architecture-summary.md) | Retired Supervisor/Web-Nav/Strategy-Router/skill-runtime architecture archive |
| [Job Discovery Workflow](docs/job-discovery-agent-workflow.md) | Legacy agent workflow reference |
| [Job Discovery Operations](docs/job-discovery-agent-operations.md) | Legacy startup, config, API reference, troubleshooting |
| [Platform Runbook](docs/runbooks/platform-foundation.md) | Full environment setup, backup, recovery |
| [Handover Summary](docs/platform-foundation-handover-summary.md) | Platform-foundation baseline; supplement with the latest functional-verification summary |
