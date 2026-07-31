# CLAUDE.md — Career Assistant Platform

## Project Overview

A multi-agent campus-recruitment career assistant built with LangGraph, FastAPI, Vue 3, and Docker Compose. Core capabilities include an opt-in Tencent Smartsheet → Skill Discovery Runtime → structured JD extraction → personalized discovery v1 (pre-review, owner-scoped) pipeline, plus a WP2 manual-import → admin review → verified student job-center flow, structured talent profiles with evidence-based matching, and a Windows executor skeleton/simulator for assisted form filling. Real-site GUI adapters and production deployment remain incomplete; final submission is always human-controlled.

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python 3.12+, FastAPI, LangGraph, deepagents, LangChain |
| Frontend | Vue 3, Vite, TypeScript, Nginx |
| Database | MySQL 8.4 (authoritative), Redis 8 (checkpoints/cache), MinIO (encrypted objects) |
| LLM | DeepSeek API (OpenAI-compatible via langchain-openai), ChatOpenAI |
| Browser | Playwright (headless Chromium) |
| Infra | Docker Compose (6 services), Alembic migrations |

## Project Structure

```
.
├── backend/
│   ├── app/
│   │   ├── main.py              # FastAPI app + lifespan
│   │   ├── config.py            # Typed Settings (pydantic-settings)
│   │   ├── api/
│   │   │   ├── dependencies.py  # Auth dependency injection
│   │   │   ├── routes/          # API route modules
│   │   │   └── job_schemas.py   # DTOs with field whitelists
│   │   ├── db/
│   │   │   ├── base.py          # UUID + timestamp mixins
│   │   │   └── models.py        # All ORM models
│   │   ├── domain/              # Pure domain rules (enums, allowlists, contracts)
│   │   ├── repositories/        # Data access layer (SQL only, no business logic)
│   │   └── services/            # Business logic layer
│   │       ├── job_discovery/   # ★ Job Discovery Agent system
│   │       │   ├── deepagents_runner.py  # Supervisor + Web Nav Agent + tools
│   │       │   ├── tools/       # Deterministic tools (triage, wechat, OCR, JD extract, verify, package)
│   │       │   ├── worker.py    # Polling worker loop
│   │       │   └── schemas.py   # Pydantic models
│   │       ├── job_mappers.py   # Tencent → NormalizedJobCandidate mapping
│   │       ├── job_sync.py      # Sync orchestration
│   │       └── auth.py          # Registration, login, JWT, Argon2
│   └── entrypoint.py            # Container entrypoint
├── frontend/src/features/jobs/  # Vue job center + admin panels
├── src/                         # Legacy LangGraph agents (CLI demo)
│   ├── agents.py                # Supervisor, Job Analyst, Resume Reviewer, etc.
│   ├── graph.py                 # Main state graph with Send, Command, subgraphs
│   └── prompts.py               # Agent system prompts
├── docs/                        # Design specs, plans, runbooks, tech docs
├── tests/
│   ├── unit/                    # Primary deterministic/unit suite
│   ├── integration/             # Integration + live smoke tests
│   ├── e2e/                     # E2E Playwright + fixture tests
│   ├── fixtures/job_discovery/  # HTML fixture pages for agent testing
│   └── manual/                  # Standalone smoke test scripts
├── alembic/versions/            # Database migrations (0001 → 0010+)
├── scripts/                     # Admin scripts, worker runner
└── docker-compose.yml           # 6-service orchestration
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
# All unit tests (count changes as the suite evolves)
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q

# Job discovery unit suite (PowerShell does not expand pytest path globs)
.\.venv\Scripts\python.exe -m pytest tests/unit/ -k job_discovery -v

# Lint
.\.venv\Scripts\python.exe -m ruff check backend src tests scripts

# Mocked Deep Agents integration (no live LLM required)
.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_deepagents.py -v

# E2E fixtures only (no backend needed)
.\.venv\Scripts\python.exe -m pytest tests/e2e/test_job_discovery_e2e.py -x -v -k "TestFixturePages"

# Live smoke tests (requires env vars + LLM)
$env:RUN_LIVE_TENCENT_DISCOVERY='1'; .\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_readgzh_smoke.py -v

# Frontend
npm.cmd --prefix frontend ci
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build

# Health checks
Invoke-RestMethod http://127.0.0.1:18000/api/health/ready
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

The target architecture for new and changed business flows is **API → Service → Repository**:

- **API layer** (`routes/`): Parse request → call service → return JSON. NEVER write SQL or business logic.
- **Service layer** (`services/`): All business logic. NEVER write raw SQL or touch HTTP.
- **Repository layer** (`repositories/`): Data access only. NEVER make business decisions.

Known debt: `backend/app/api/routes/job_discovery.py` currently performs candidate approval/rejection queries and writes directly. Treat this as an exception to refactor when that flow is changed, not as a pattern for new endpoints. Health-check SQL is also an intentional infrastructure probe rather than business data access.

### Agent vs Tool vs Skill

- **Agent**: LLM-in-the-loop, autonomous tool selection, plan-verify-replan. Within the job-discovery subsystem these are the Discovery Supervisor and Web Navigation Agent; `src/` also contains the legacy CLI-demo graph and agents.
- **Tool**: Agent-callable deterministic Python function with fixed input/output. Examples include `triage_link`, `extract_jd_candidates`, `verify_evidence`, and `package_candidates`.
- **Helper/pipeline module**: Non-agent modules under `job_discovery/tools/` implement triage, WeChat parsing, OCR inspection, JD extraction, evidence verification, and candidate key generation. “Skill” is a design term here, not a separate runtime registry in this repository.

### Security Hard Gates

These are code-level restrictions, NOT conventions:

1. **Never auto-click final submit**: No `task:submit` scope exists. GUI Agent stops at `READY_FOR_REVIEW`. Human must click submit.
2. **Never bypass login/captcha/anti-bot**: If blocked, mark `needs_manual_review` — never attempt to circumvent.
3. **Student API only returns `verified` jobs**: Filter at SQL level. Other statuses must never leak.
4. **Never write secrets to repo/logs/argv**: Passwords, tokens, API keys, raw payloads — all rejected.
5. **Never use Redis as authority**: MySQL is the single source of truth for business state.
6. **Never trust device token alone**: Task actions require task lease with scope validation.
7. **Job review requires version check**: JobPosting completion/review/decision writes validate `review_version` (optimistic locking). Concurrent edits get 409. Discovery-candidate approve/reject is a separate pre-review flow and currently does not use this version field.

### Configuration

- Backend platform and job-discovery settings live in `backend/app/config.py` → `Settings` (pydantic-settings); the legacy `src/` demo still has its own environment helpers
- Rejects: demo keys, template credentials, SQLite in production
- `OBJECT_ENCRYPTION_KEY` must be Base64-encoded 32 bytes
- `.env` file at project root for local overrides; never committed

## Job Discovery Agent System

> **Default runtime (2026-07-29): Skill Discovery Runtime** (`JOB_DISCOVERY_SKILL_RUNTIME_ENABLED=true`). Discovery candidates are delivered via **personalized discovery v1** (pre-review, owner-scoped, card labelled 「自动发现，建议自行确认」), **not** via admin review -> JobPosting(verified). The verified-only `/api/jobs` job center is fed by the WP2 manual-import/completion workflow and is decoupled from discovery candidates. The Supervisor / Web Navigation Agent / Strategy Router / PEV paths below are **legacy fallback**, retained only when the skill runtime flag is off. Legacy architecture summary: [docs/job-discovery-legacy-architecture-summary.md](docs/job-discovery-legacy-architecture-summary.md). (Code-side discovery candidate admin approve/reject -> JobPosting promotion still exists; migration tracked separately.)

### Architecture

```
Tencent Smartsheet → RawJobRecord → JobDiscoveryTask (queued)
  → Worker (claim + lease) → Skill Discovery Runtime (DEFAULT; create_deep_agent
        + job-discovery Skill + restricted run_skill_script + per-page jd_extractor subagent)
    → browse → per-page JD extraction → deduplicate → coverage gate
  → DiscoveredJobCandidate → Personalized Discovery v1 (pre-review, owner-scoped)
    → 用户 (卡片: 自动发现, 建议自行确认)   [skip admin review]

Legacy fallback (JOB_DISCOVERY_SKILL_RUNTIME_ENABLED=false only):
  → DiscoverySupervisorAgent → link_triage → WebNavigationAgent | wechat_parser
    → jd_extraction → evidence_verifier → candidate_packager
```

### Web Navigation Agent (deepagents_runner.py)

Standalone DeepAgent with 7 tools: `open_url`, `open_rendered_url`, `extract_rendered_job_evidence`, `read_dom`, `extract_links`, `click_link`, `go_back`.

**ReadGZH Integration**: `open_url()` automatically routes WeChat (`mp.weixin.qq.com`) URLs through ReadGZH proxy first, with automatic fallback: ReadGZH → direct HTTP → Playwright browser → error. Set `READGZH_API_KEY` explicitly in the process environment for authenticated live runs.

### Tool Chain

| Tool | Function | Signature |
|------|----------|-----------|
| `triage_link` | Classify URL type | `(url) → dict` |
| `run_web_navigation` | DeepAgent web nav | `(start_url, settings=None, subagent=None, model=None) → dict` |
| `parse_wechat_article` | Extract WeChat content | `(html, url) → dict` |
| `run_ocr` | Decode/inspect one base64 image; OCR extraction is still a placeholder | `(image_base64, settings=None) → dict` |
| `extract_jd_candidates` | Deterministic wrapper around JD extraction | `(page_text, url) → str (JSON)` |
| `verify_evidence` | Validate candidate fields against evidence | `(candidates_json, evidence_json) → str (JSON)` |
| `package_candidates` | Add candidate idempotency and similarity keys | `(candidates_json, evidence_hash, source_key) → str (JSON)` |
| `standardize_from_record_fields` | Fallback: record fields + evidence → candidate | `(record_fields_json, evidence_json, source_url) → str (JSON)` |
| `finish_with_manual_review` | Block → manual review result | `(reason) → dict` |

## State Machines

### ApplicationTask States (12 states, 3 actors)

```
CREATED → WAITING_FOR_DEVICE → DISPATCHED → RUNNING → READY_FOR_REVIEW
  → OBSERVING_USER_SUBMISSION → SUBMITTED_SUCCESS / SUBMITTED_FAILED / RESULT_UNKNOWN
DISPATCHED or RUNNING → WAITING_FOR_HUMAN → RUNNING or READY_FOR_REVIEW
DISPATCHED / RUNNING / WAITING_FOR_HUMAN → FAILED
Non-terminal pre-submission states → CANCELLED (human only)
```

**Key**: Only HUMAN can transition `READY_FOR_REVIEW` → `OBSERVING_USER_SUBMISSION`. SYSTEM never. EXECUTOR can only observe after that point.

### JobPosting States (5 states)

```
pending_completion → pending_review → verified → expired
pending_completion / pending_review → rejected → pending_review (after correction)
```

Students ONLY see `verified`. Admin review uses `review_version` optimistic locking.

### Job Discovery Task States

```
queued → running → succeeded / partial_success / needs_manual_review / failed / cancelled
```

Worker claims tasks with lease timeout. Expired leases allow re-claim.

## Key Conventions

### Naming & Code Style
- Python: ruff linter, docstrings for public functions
- Frontend: TypeScript strict, Vue 3 Composition API, Vitest
- Commit messages: `type: description` (feat:, fix:, chore:, docs:, test:)
- File organization: One class/concept per file in `domain/`; related operations in `services/`

### Idempotency
- Task idempotency key: SHA-256 of `source_id + external_record_id + url_hash + payload_hash + agent_version`
- Candidate idempotency key: SHA-256 of normalized `company + title + location + apply_url + evidence_hash`
- Feedback idempotency: `Idempotency-Key` header → SHA-256 request fingerprint comparison

### WeChat Article Handling
- All WeChat URLs auto-routed through ReadGZH proxy in `open_url()`
- Fallback chain: ReadGZH API → direct requests.get → Playwright browser → error
- Verification wall detection: checks for `环境异常` + `完成验证后即可继续访问`
- Image/video-heavy articles may require OCR or manual review; the current OCR tool only inspects inputs and returns a placeholder result

### Error Handling
- Blocked errors (`login_required`, `captcha`, `anti_bot`) → `needs_manual_review`
- Transient failures → retry with lease
- `partial_success` when some candidates found but budget exhausted
- Never expose raw payloads, tokens, or OCR full text in logs/audit

## Database Migrations

Alembic migrations numbered `YYYYMMDD_NNNN_description.py`. Current head includes:
- `0001`: Platform foundation (users, sessions, devices, tasks, audit)
- `0003`: Job sources, raw records, sync runs, job postings
- `0004`: Job review lifecycle
- `0005`–`0008`: Profiles, resumes, manual JD import, feedback, matching, snapshots
- `0009`–`0010`: Site adapters, rollout, observation sites
- `0011`: Wave 2 schema alignment
- Hotfix `7b757ef17d3f`: MatchReport CHECK constraint fix
- Current head `7e8f22313271`: Job-discovery tasks, evidence, and candidates

## Important Docs

| Document | Content |
|----------|---------|
| [WP1 Tech Doc](docs/WP1-平台基础与权威数据-技术文档.md) | Platform foundation explained (auth, state machine, encryption, layers) |
| [WP2 Tech Doc](docs/WP2-真实职位同步与核验-技术文档.md) | Job sync, review pipeline, student submissions explained |
| [Job Discovery (legacy summary)](docs/job-discovery-legacy-architecture-summary.md) | Superseded Supervisor/PEV/Strategy-Router architecture archive |
| [Job Discovery Architecture (current)](backend/app/services/job_discovery/ARCHITECTURE.zh-CN.md) | Current skill-runtime agent architecture & URL->JDs flow |
| [Job Discovery Workflow](docs/job-discovery-agent-workflow.md) | Agent workflow + architecture reference |
| [Job Discovery Operations](docs/job-discovery-agent-operations.md) | Startup, config, API reference, troubleshooting |
| [Platform Runbook](docs/runbooks/platform-foundation.md) | Full environment setup, backup, recovery |
| [Handover Summary](docs/platform-foundation-handover-summary.md) | Platform-foundation baseline through 2026-07-17; supplement with the latest functional-verification summary for job discovery |
