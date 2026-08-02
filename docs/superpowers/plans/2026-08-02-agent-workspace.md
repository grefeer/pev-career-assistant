# Agent Workspace Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give an authenticated user one personal workspace to submit a natural-language PEV task and inspect its plan activity, result artifacts, source evidence, and safe failure state.

**Architecture:** Extend the existing owner-scoped AgentRun service with a list projection; keep all authority checks in the service/repository layers. Add a Vue route that creates a run with an explicit four-Skill allowlist, fetches the safe trace/artifact projections after completion, and renders an evidence-first workspace without raw model messages or private profile facts.

**Tech Stack:** FastAPI, SQLAlchemy, Pydantic, Vue 3, Vue Router, Vitest, pytest.

## Global Constraints

- API routes parse DTOs and call services only; repositories contain SQL only.
- Users can only read their own runs, events, and artifacts.
- Never expose private context, raw prompts, resume bytes, tokens, or chain-of-thought.
- A run is bounded by server-owned AgentBudget; the browser cannot enlarge it.
- UI must display evidence/source links and must not claim an unsupported recommendation.
- Preserve all existing admin and legacy pages until an approved retirement change.

---

### Task 1: Owner-scoped run-list API

**Files:**
- Modify: `backend/app/repositories/agent_runtime.py`
- Modify: `backend/app/services/agent_runtime/service.py`
- Modify: `backend/app/api/agent_runtime_schemas.py`
- Modify: `backend/app/api/routes/agent_runtime.py`
- Test: `tests/unit/test_agent_runtime_repository.py`
- Test: `tests/unit/test_agent_runtime_service.py`
- Test: `tests/unit/test_agent_runtime_routes.py`

**Interfaces:**
- Produces `list_runs_for_owner(db: Session, user_id: str, limit: int) -> list[AgentRun]`, ordered newest first.
- Produces `AgentRunService.list_runs(db, *, user_id: str, limit: int) -> list[AgentRun]`.
- Produces `GET /api/agent-runs?limit=20` with `AgentRunListResponse(items: list[AgentRunResponse])`.

- [ ] **Step 1: Write failing repository and endpoint tests**

```python
def test_repository_lists_only_owner_runs_newest_first(db_session) -> None:
    newest = agent_runtime.create_run(db_session, user_id="owner", goal="new", ...)
    agent_runtime.create_run(db_session, user_id="other", goal="hidden", ...)
    assert [run.id for run in agent_runtime.list_runs_for_owner(db_session, "owner", 20)] == [newest.id]

def test_agent_runs_list_requires_owner(client, auth_headers) -> None:
    response = client.get("/api/agent-runs?limit=20", headers=auth_headers)
    assert response.status_code == 200
    assert set(response.json()) == {"items"}
```

- [ ] **Step 2: Run tests to verify the missing list contract fails**

Run: `python -m pytest tests/unit/test_agent_runtime_repository.py tests/unit/test_agent_runtime_service.py tests/unit/test_agent_runtime_routes.py -q`

Expected: failure because `list_runs_for_owner` and the GET route do not exist.

- [ ] **Step 3: Implement the repository, service and DTO projection**

```python
def list_runs_for_owner(db: Session, user_id: str, limit: int) -> list[AgentRun]:
    return list(db.scalars(
        select(AgentRun).where(AgentRun.user_id == user_id)
        .order_by(AgentRun.created_at.desc(), AgentRun.id.desc()).limit(limit)
    ))

@router.get("", response_model=AgentRunListResponse)
def list_agent_runs(..., limit: Annotated[int, Query(ge=1, le=100)] = 20) -> AgentRunListResponse:
    return AgentRunListResponse(items=[_to_run_response(run) for run in service.list_runs(...)])
```

- [ ] **Step 4: Run the three unit modules and Ruff**

Run: `python -m pytest tests/unit/test_agent_runtime_repository.py tests/unit/test_agent_runtime_service.py tests/unit/test_agent_runtime_routes.py -q; python -m ruff check backend/app/repositories/agent_runtime.py backend/app/services/agent_runtime/service.py backend/app/api/agent_runtime_schemas.py backend/app/api/routes/agent_runtime.py`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add backend/app/repositories/agent_runtime.py backend/app/services/agent_runtime/service.py backend/app/api/agent_runtime_schemas.py backend/app/api/routes/agent_runtime.py tests/unit/test_agent_runtime_repository.py tests/unit/test_agent_runtime_service.py tests/unit/test_agent_runtime_routes.py
git commit -m "feat(agent-runtime): list owner-scoped runs"
```

### Task 2: Typed browser API boundary

**Files:**
- Create: `frontend/src/features/agent-workspace/agentRuntimeApi.ts`
- Create: `frontend/src/features/agent-workspace/agentRuntimeTypes.ts`
- Test: `frontend/src/features/agent-workspace/__tests__/agentRuntimeApi.spec.ts`

**Interfaces:**
- Produces `createAgentRun`, `fetchAgentRuns`, `fetchAgentRunEvents`, and `fetchAgentRunArtifacts`.
- `CreateAgentRunPayload` carries `goal`, `allowed_skills`, and optional public `candidate_urls` only.
- Event and artifact response types mirror public FastAPI DTO fields; no type exposes private context.

- [ ] **Step 1: Write failing API client tests**

```typescript
it('sends only the public task payload and bearer token', async () => {
  await createAgentRun('token', { goal: '找 AI Agent 岗位', allowed_skills: ['job-discovery'], candidate_urls: ['https://jobs.example/1'] })
  expect(fetch).toHaveBeenCalledWith('/api/agent-runs', expect.objectContaining({ method: 'POST' }))
})
```

- [ ] **Step 2: Run the test to verify the module is absent**

Run: `npm.cmd --prefix frontend run test -- agentRuntimeApi.spec.ts`

Expected: failure because the API module does not exist.

- [ ] **Step 3: Implement typed request wrappers**

```typescript
export function createAgentRun(token: string, payload: CreateAgentRunPayload) {
  const context = payload.candidate_urls?.length ? { candidate_urls: payload.candidate_urls } : {}
  return request<AgentRunCreatedResponse>('/agent-runs', { method: 'POST', body: JSON.stringify({ goal: payload.goal, allowed_skills: payload.allowed_skills, context }) }, token)
}
```

- [ ] **Step 4: Run API tests and type checking**

Run: `npm.cmd --prefix frontend run test -- agentRuntimeApi.spec.ts; npm.cmd --prefix frontend run typecheck`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add frontend/src/features/agent-workspace/agentRuntimeApi.ts frontend/src/features/agent-workspace/agentRuntimeTypes.ts frontend/src/features/agent-workspace/__tests__/agentRuntimeApi.spec.ts
git commit -m "feat(frontend): add typed PEV run API client"
```

### Task 3: Personal Agent Workspace route

**Files:**
- Create: `frontend/src/features/agent-workspace/AgentWorkspace.vue`
- Modify: `frontend/src/router/index.ts`
- Modify: `frontend/src/components/AppShell.vue`
- Test: `frontend/src/features/agent-workspace/__tests__/AgentWorkspace.spec.ts`

**Interfaces:**
- Route `/assistant` requires authentication and is the root redirect.
- Workspace accepts a goal and optional newline-separated public career URLs.
- Workspace renders recent runs, safe event timeline, typed artifact cards and evidence source links.

- [ ] **Step 1: Write failing component tests**

```typescript
it('submits a natural-language goal with the selected Skill authority', async () => {
  await wrapper.get('textarea[name="goal"]').setValue('找最近三天的 AI Agent 岗位')
  await wrapper.get('form').trigger('submit.prevent')
  expect(createAgentRun).toHaveBeenCalledWith(expect.any(String), expect.objectContaining({ goal: '找最近三天的 AI Agent 岗位' }))
})

it('renders artifacts as evidence-linked cards without private context', async () => {
  expect(wrapper.text()).toContain('来源证据')
  expect(wrapper.find('a[href="https://jobs.example/1"]').exists()).toBe(true)
  expect(wrapper.text()).not.toContain('private_context')
})
```

- [ ] **Step 2: Run the component test to verify it fails**

Run: `npm.cmd --prefix frontend run test -- AgentWorkspace.spec.ts`

Expected: failure because the workspace component does not exist.

- [ ] **Step 3: Implement the workspace**

```vue
<form @submit.prevent="submit">
  <textarea v-model="goal" name="goal" required />
  <label v-for="skill in skills" :key="skill.name"><input v-model="selectedSkills" type="checkbox" :value="skill.name" />{{ skill.label }}</label>
  <textarea v-model="candidateUrlsText" name="candidate-urls" />
  <button :disabled="submitting || !goal.trim() || !selectedSkills.length">开始任务</button>
</form>
```

Render event labels with a local allowlist (`plan_created`, `executor_tool_observation`, `verification_passed`, failures) and render artifact content only through the typed public API response. Add an accessible loading status and an error banner for disabled/unavailable runs.

- [ ] **Step 4: Register the route and navigation**

```typescript
{ path: '/', redirect: '/assistant' },
{ path: '/assistant', name: 'assistant', component: () => import('../features/agent-workspace/AgentWorkspace.vue'), meta: { requiresAuth: true } },
```

Add a first navigation item labelled `Assistant`; retain all legacy links for this non-destructive transition.

- [ ] **Step 5: Run the component test, full frontend tests, typecheck and build**

Run: `npm.cmd --prefix frontend run test; npm.cmd --prefix frontend run typecheck; npm.cmd --prefix frontend run build`

Expected: all pass.

- [ ] **Step 6: Commit**

```powershell
git add frontend/src/features/agent-workspace/AgentWorkspace.vue frontend/src/features/agent-workspace/__tests__/AgentWorkspace.spec.ts frontend/src/router/index.ts frontend/src/components/AppShell.vue
git commit -m "feat(frontend): add personal PEV agent workspace"
```

### Task 4: Safe integration smoke

**Files:**
- Test: `tests/unit/test_agent_runtime_routes.py`
- Test: `frontend/src/features/agent-workspace/__tests__/AgentWorkspace.spec.ts`

**Interfaces:**
- The browser only sees owner-safe run, event and artifact DTOs.
- A missing runtime returns the existing stable `agent_harness_disabled` or `agent_harness_unavailable` error state.

- [ ] **Step 1: Write failing response-whitelist and UI-error tests**

```python
assert "context" not in client.get(f"/api/agent-runs/{run_id}", headers=headers).json()
```

```typescript
mockCreateAgentRun.mockRejectedValue(new ApiError(503, { code: 'agent_harness_unavailable' }, 'agent_harness_unavailable'))
expect(wrapper.text()).toContain('暂不可用')
```

- [ ] **Step 2: Run tests to verify the relevant missing behavior fails**

Run: `python -m pytest tests/unit/test_agent_runtime_routes.py -q; npm.cmd --prefix frontend run test -- AgentWorkspace.spec.ts`

Expected: failure until the UI maps the stable error code.

- [ ] **Step 3: Add only public-field assertions and user-readable stable error copy**

```typescript
if (error instanceof ApiError && (error.message === 'agent_harness_unavailable' || error.message === 'agent_harness_disabled')) {
  errorMessage.value = '智能求职助手暂不可用，请稍后重试。'
}
```

- [ ] **Step 4: Execute API and frontend regression suites**

Run: `python -m pytest tests/unit/test_agent_runtime_routes.py tests/unit/test_agent_runtime_service.py -q; npm.cmd --prefix frontend run test; npm.cmd --prefix frontend run typecheck; npm.cmd --prefix frontend run build`

Expected: all pass.

- [ ] **Step 5: Commit**

```powershell
git add tests/unit/test_agent_runtime_routes.py frontend/src/features/agent-workspace/__tests__/AgentWorkspace.spec.ts frontend/src/features/agent-workspace/AgentWorkspace.vue
git commit -m "test(agent-workspace): verify safe run presentation"
```

## Plan self-review

- The plan covers only the approved agent-workspace slice; it leaves asynchronous SSE, run resume, and legacy retirement to the next independent subprojects.
- Every endpoint remains owner-scoped, and UI types intentionally omit private context and raw model messages.
- The workspace cannot increase Agent budgets and uses only existing public `candidate_urls` context for optional source hints.
