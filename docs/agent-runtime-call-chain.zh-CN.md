# Agent Runtime 完整调用链（时序图）

> 本文用 Mermaid 时序图绘制 `backend/app/services/agent_runtime/`（**自研 PEV Harness + DeepAgents Executor**）的完整调用链：API 请求 → Service → Runtime → 三个 Agent → 工具 → MySQL / SSE。
>
> - 架构总述见 [pev-agent-architecture.zh-CN.md](pev-agent-architecture.zh-CN.md)
> - 所有行号以 `master` 当前代码为准

## 0. 总体结构

```
浏览器(Vue)
   │  REST / SSE
   ▼
api/routes/agent_runtime.py        ← 解析请求、返回 JSON、开 SSE 流（不写 SQL、无业务逻辑）
   │
   ▼
services/agent_runtime/service.py  ← feature gate、所有权、注入已确认画像事实
   │
   ▼
services/agent_runtime/runtime.py  ← 主循环：planner → 逐步执行 → verifier → 路由（唯一调度者）
   │              │              │
   ▼              ▼              ▼
PlannerAgent   ExecutorAgent   VerifierAgent     ← 各自的 turn 循环（自主决策，语义都在这里）
   │              │              │
   └──────────────┼──────────────┘
                  ▼
      AgentModelGateway (model_gateway.py)        ← LLM 边界：schema-first + json_mode + 重试
                  │
                  ▼
        ToolRegistry (tool_registry.py)           ← role+skill 双授权、异常不泄漏
                  │
                  ▼
     career_skills/* 工具 handler（fetch/extract/match/…）
                  │
                  ▼
   repositories/agent_runtime.py ──► MySQL（Run/Plan/Step/Turn/Event/Artifact 唯一权威）
```

**三条硬规则贯穿全程**：
1. Agent 只做**决策**，harness（runtime）只做**编排**——工具序列从不被预计算；
2. 每个模型决策 = 一个 MySQL 检查点（`db.commit()`），崩溃后可恢复；
3. 只有工具产物（带 `source_url` + `content_hash`）能成为证据落库，模型提议的 URI 永不信任。

---

## 1. 图 A — 请求入口与后台调度（同步部分）

```mermaid
sequenceDiagram
    autonumber
    participant B as 浏览器
    participant API as api/routes/agent_runtime.py
    participant SVC as services/agent_runtime/service.py
    participant RT as runtime.py
    participant DB as MySQL

    B->>API: "POST /agent-runs {goal, allowed_skills, context}"
    Note over API: 组装 AgentTaskRequest 与 build_adaptive_agent_budget<br/>routes/agent_runtime.py:166-171
    API->>SVC: queue_run(user_id, task)
    Note over SVC: gate 检查 (agent_harness_enabled / available)<br/>service.py:83-89
    SVC->>RT: create_queued_run(user_id, task)
    RT->>DB: INSERT agent_runs (status=queued)
    DB-->>RT: run_id
    SVC-->>API: AgentRunResult(run_id, queued)
    API-->>B: 201 + run_id
    Note over API: background_tasks.add_task(execute_queued_run)<br/>routes/agent_runtime.py:184-190 —— 请求立即返回，run 进后台

    rect rgb(235, 245, 255)
    Note over API,SVC: 【后台线程】执行开始
    SVC->>SVC: execute_queued_run (service.py:94-125)
    Note over SVC: 独立 session_factory()，请求无关<br/>从 DB 重建 task (service.py:102-107)<br/>注入 confirmed_profile_facts (service.py:112,127-151)
    SVC->>RT: run(db, user_id, task, existing_run)
    end
```

**要点**：run 记录先落库（`queued`）再返回 201，SSE 立刻有持久化目标；真正执行在 `BackgroundTasks` 的独立 session 里，不占 HTTP 线程。

---

## 2. 图 B — 主循环：Planner 阶段（runtime.py:132-169）

```mermaid
sequenceDiagram
    autonumber
    participant RT as runtime.py (run 主循环)
    participant P as PlannerAgent
    participant GW as model_gateway.py
    participant DB as MySQL

    Note over RT: 恢复分支：count_plans/turns/tool_decisions 复用已耗预算<br/>runtime.py:104-118；deadline = now 加 wall_clock (119)<br/>replans = max(0, revision-1) (126)<br/>_build_decision_trace (127-131)
    RT->>P: planner.run(task, context, 预算, deadline)
    loop 每轮决策 for turn < max_agent_turns (planner:141)
        P->>P: 检查 wall-clock (142-147) / turn 预算 (148-153)
        P->>GW: decide(role=planner, instruction, state, response_model=PlannerDecision)
        GW-->>P: PlannerDecision（schema 校验失败自动重试）
        P->>DB: trace() → INSERT agent_turns 并 commit<br/>（每个模型决策 = 恢复检查点，planner:180-198 / runtime:558-599）
        alt action=call_tool（planner 需要补充信息时）
            P->>P: tool 预算扣减 (planner:200-205)
            P->>ToolRegistry: invoke(role=planner, name, payload)
            ToolRegistry-->>P: ToolObservation
            Note over P: record_observation → 下一轮继续
        else action=plan
            P->>P: 构造 ExecutionPlan（每步恰好 1 个 skill）
        end
    end
    P-->>RT: PlannerResult(planned, plan)
    RT->>DB: create_plan、append_event(plan_created)、checkpoint<br/>runtime.py:150-169
    Note over RT: 进入 step 循环（见图 C/D）
```

---

## 3. 图 C — Step 阶段：Executor 内部循环 + 工具调用

```mermaid
sequenceDiagram
    autonumber
    participant RT as runtime.py (_run_step)
    participant EX as ExecutorAgent
    participant GW as model_gateway.py
    participant TR as ToolRegistry
    participant SK as career_skills handler
    participant DB as MySQL

    RT->>DB: create_step 并 checkpoint (runtime.py:173-181)
    RT->>EX: executor.run(task, plan, step, 预算, prior_observations)
    loop 每轮决策 for turn < max_agent_turns (executor:300)
        EX->>EX: wall-clock (301-307) / turn 预算 (308-314)
        EX->>GW: decide(role=executor, state=目标+观测投影+already_succeeded_calls, response_model=ExecutorDecision)
        GW-->>EX: ExecutorDecision
        EX->>DB: trace() → INSERT agent_turns 并 commit
        alt action=call_tool
            Note over EX: 空转防护：candidate_urls 时禁搜索 (376-403)<br/>已成功调用去重 (404-407) → duplicate 观测
            EX->>TR: invoke(role=executor, name, payload, allowed_skills)
            TR->>TR: role 授权 (tool_registry:121-133) 与 skill 授权 (134-139)
            TR->>TR: input/output schema 校验 (140-156)
            TR->>SK: handler(context, input)
            SK-->>TR: 结构化输出 或 抛异常
            Note over TR: 异常 → failed observation，绝不泄漏给 Agent (157-165)
            TR-->>EX: ToolObservation(succeeded/failed/duplicate_tool_call)
            EX->>EX: record_observation（投影封顶 1200 字符/10 条）
        else action=complete
            EX-->>RT: ExecutorResult(status=succeeded)
        else action=needs_user
            EX-->>RT: ExecutorResult(status=needs_user)
        end
    end
    EX-->>RT: ExecutorResult（预算耗尽等终态）

    rect rgb(240, 245, 235)
    Note over RT,DB: 证据持久化（runtime.py:347-361, 704-888）
    RT->>DB: _record_failed_executor_observations → 失败事件
    RT->>DB: _persist_observed_evidence → INSERT agent_artifacts<br/>只收 source_url+content_hash 的工具产物 + executor_*_observation 事件
    Note over RT: 合并 prior_observations（RETRY 场景，runtime.py:356-361）
    end
```

---

## 4. 图 D — Verifier 与五路路由（runtime.py:392-552）

```mermaid
sequenceDiagram
    autonumber
    participant RT as runtime.py (_run_step)
    participant V as VerifierAgent
    participant GW as model_gateway.py
    participant DB as MySQL

    alt 不需要核验 (requires_verification=False 且复杂度 L1/L2)
        RT->>DB: finish_step(succeeded) 并 step_succeeded 事件 (runtime.py:392-405)
    else 需要核验（L3/L4 或 requires_verification，runtime.py:554-556）
        RT->>V: verifier.run(plan, step, execution, 预算)
        loop 每轮决策 (verifier:88-178)
            V->>GW: decide(role=verifier, state=独立检视 evidence, response_model=VerifierDecision)
            GW-->>V: VerifierDecision(PASS/RETRY_EXECUTOR/REPLAN/NEED_USER/FAIL)
            V->>DB: trace() → INSERT agent_turns
            alt 需要工具核验
                V->>ToolRegistry: invoke（同 Executor 授权路径）
                ToolRegistry-->>V: ToolObservation
            else 输出决策
                V-->>RT: VerifierResult(decision, feedback)
            end
        end
    end

    alt PASS
        RT->>DB: finish_step(succeeded) 并 verification_passed 事件 (457-476)
    else RETRY_EXECUTOR
        Note over RT: retries 加 1；≤max_replans → 注入 verifier_feedback 回 Executor (477-510)<br/>超过 → _wait_for_user (516-522)
    else NEED_USER
        RT->>RT: _wait_for_user → run 转 waiting_user，可 resume (523-530, 1002-1042)
    else REPLAN
        RT->>DB: finish_step(skipped) 并 verification_replan 事件 (531-552)
        Note over RT: 主循环 replans 加 1：超 max_replans → failed (207-224)<br/>否则带 feedback 回 Planner 重新计划 (225-230)
    else FAIL / 异常
        RT->>RT: _fail_step / _fail_run (1044-1077)
    end

    Note over RT: 全部 step 通过 → finish_run(succeeded) + run_succeeded (231-240)
```

**降级语义**（模型输出异常 ≠ 业务失败）：
| 触发点 | 处理 |
|---|---|
| Planner 输出非法 | `invalid_model_response` → `waiting_user`（[runtime.py:142-147](backend/app/services/agent_runtime/runtime.py#L142-L147)） |
| Executor 输出非法 | → `waiting_user`（[runtime.py:336-345](backend/app/services/agent_runtime/runtime.py#L336-L345)） |
| Verifier 输出非法 | 降级为 NEED_USER（[runtime.py:419-425](backend/app/services/agent_runtime/runtime.py#L419-L425)） |
| wall-clock 耗尽（任一 Agent 边界） | → `waiting_user`，resume 刷新时间窗（[runtime.py:370-383](backend/app/services/agent_runtime/runtime.py#L370-L383)） |
| 预算语义 | turn/tool/replan **不**重置，只有 wall-clock 窗口刷新（[runtime.py:104-126](backend/app/services/agent_runtime/runtime.py#L104-L126)） |

---

## 5. 图 E — SSE 消费（routes/agent_runtime.py:279-328）

```mermaid
sequenceDiagram
    autonumber
    participant B as 浏览器 (EventSource)
    participant API as api/routes/agent_runtime.py
    participant SVC as services/agent_runtime/service.py
    participant DB as MySQL

    B->>API: "GET /{run_id}/events/stream?after_sequence=N"
    Note over API: _effective_event_cursor 兼容 Last-Event-ID (routes:133-140)
    API->>SVC: list_events(run_id, after_sequence=cursor)
    SVC->>DB: SELECT agent_events WHERE sequence > cursor
    DB-->>SVC: 已持久化事件
    SVC-->>API: 事件列表
    loop 初次回放
        API-->>B: SSE 事件（_sse_event，routes:115-130）
    end
    loop 之后每 1s 轮询 (routes:304-322)
        API->>SVC: list_events(run_id, after_sequence=cursor)
        SVC->>DB: SELECT ...
        DB-->>SVC: 新事件
        API-->>B: 逐条 SSE + keep-alive
    end
    Note over B,DB: 断线重连：浏览器带 Last-Event-ID 头 → 从上次 sequence 续读<br/>MySQL 是事件唯一权威，Redis 仅为加速器（不参与权威判定）
```

---

## 6. 行号速查表

| 层 | 关键点 | 位置 |
|---|---|---|
| API 入口 | `create_agent_run`（组装 task+budget → queue → 后台执行） | [routes/agent_runtime.py:155-196](backend/app/api/routes/agent_runtime.py#L155-L196) |
| API 恢复 | `resume` / `recover`（waiting_user / running 中断恢复） | [routes/agent_runtime.py:199-276](backend/app/api/routes/agent_runtime.py#L199-L276) |
| API SSE | 轮询流 + 断线续传（1s 间隔） | [routes/agent_runtime.py:279-328](backend/app/api/routes/agent_runtime.py#L279-L328) |
| Service | `queue_run` gate + 建 run | [service.py:84-92](backend/app/services/agent_runtime/service.py#L84-L92) |
| Service | `execute_queued_run`（后台线程入口，独立 session） | [service.py:94-125](backend/app/services/agent_runtime/service.py#L94-L125) |
| Service | 注入已确认画像事实（private_context，不落库） | [service.py:127-151](backend/app/services/agent_runtime/service.py#L127-L151) |
| Runtime 主循环 | planner → plan 落库 → step 循环 → replan 预算 → succeeded | [runtime.py:132-240](backend/app/services/agent_runtime/runtime.py#L132-L240) |
| Runtime step | executor → 证据持久化 → verifier → 五路路由 | [runtime.py:302-552](backend/app/services/agent_runtime/runtime.py#L302-L552) |
| Runtime 证据 | 只持久化带 `source_url`+`content_hash` 的工具产物 | [runtime.py:704-888](backend/app/services/agent_runtime/runtime.py#L704-L888) |
| Runtime 上下文 | 证据预算 48k 字符（最新保持全文，旧证据折叠为指针） | [runtime.py:606-675](backend/app/services/agent_runtime/runtime.py#L606-L675) |
| Runtime 降级 | `_wait_for_user` / `_finish_planner_waiting` / `_fail_run` | [runtime.py:966-1077](backend/app/services/agent_runtime/runtime.py#L966-L1077) |
| Planner 循环 | turn 循环 → decide → trace →（可选工具）→ plan | [planner_agent.py:141-216](backend/app/services/agent_runtime/planner_agent.py#L141-L216) |
| Executor 循环 | turn 循环 → decide → trace → 去重/空转计数 → 工具 | [executor_agent.py:300-407](backend/app/services/agent_runtime/executor_agent.py#L300-L407) |
| Executor 空转上限 | `_MAX_CONSECUTIVE_STALLS=3` / `_MAX_TOTAL_WASTED_TURNS=3` | [executor_agent.py:189-224](backend/app/services/agent_runtime/executor_agent.py#L189-L224) |
| Verifier 循环 | turn 循环 → decide → trace →（可选工具）→ 决策 | [verifier_agent.py:88-178](backend/app/services/agent_runtime/verifier_agent.py#L88-L178) |
| 工具授权 | role/skill 双授权 + schema 校验 + 异常不泄漏 | [tool_registry.py:111-170](backend/app/services/agent_runtime/tool_registry.py#L111-L170) |
| LLM 边界 | schema-first + json_mode + 2 次重试 + 本地校验 | [model_gateway.py:38-68](backend/app/services/agent_runtime/model_gateway.py#L38-L68) |

---

## 7. 阅读建议

1. **入口**：先看 [图 A](#1-图-a--请求入口与后台调度同步部分) 掌握「请求立即返回、run 进后台」的模型，再看 [routes/agent_runtime.py:155-196](backend/app/api/routes/agent_runtime.py#L155-L196)；
2. **主循环**：[图 B](#2-图-b--主循环planner-阶段runtimepy132-169) + [runtime.py:132-240](backend/app/services/agent_runtime/runtime.py#L132-L240)——全部调度逻辑都在这个 while 循环里；
3. **最复杂的决策树**：[图 D](#4-图-d--verifier-与五路路由runtimepy392-552) + [runtime.py:302-552](backend/app/services/agent_runtime/runtime.py#L302-L552)——Executor 产出怎么被验证、怎么路由到 5 个终点；
4. **自主性所在**：三个 Agent 的 turn 循环结构完全同构（decide → trace → act），从 [executor_agent.py:300](backend/app/services/agent_runtime/executor_agent.py#L300) 读一遍即可类推其余两个；
5. **安全边界**：[tool_registry.py:111-170](backend/app/services/agent_runtime/tool_registry.py#L111-L170) 是任何工具调用的必经之路——授权、校验、异常隔离都在这里。
