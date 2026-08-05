# PEV Agent Runtime 行业标准对照评估与优化建议

> 归档日期:2026-08-05。本报告为**只读评估**(交付形式:仅评估报告,不含代码改动),基于 3 个并行探索代理对代码库的全量盘点 + 对 `model_gateway.py`、`schemas.py` 等关键文件的人工核实,所有判断均附 file:line 证据。

## 背景

对照一份行业共识的「AI Agent 核心部件」清单(10 个核心模块 + 4 个优化方向),评估 `backend/app/services/agent_runtime/` 及 `backend/app/services/career_skills/` 的现状,给出优化建议。

---

## 一、总体结论(执行摘要)

这套 PEV(Planner–Executor–Verifier)运行时是一套**确定性编排 + 受限技能 + 证据绑定**的领域专用 Agent 系统,不是通用对话型 Agent(ReAct 类)。对照行业标准:

| 维度 | 评级 | 一句话 |
|---|---|---|
| 编排与循环控制 | ★★★★★ | 预算、停止条件、持久化、恢复、人机回环全部达标,超过多数框架默认配置 |
| 自主性与策略/安全 | ★★★★★ | 7 条安全硬门、状态机守卫、永不自动提交、SSRF 防护 |
| 工具与技能 | ★★★★☆ | 统一 Pydantic schema + 双重 skill 作用域;错误信息粒度不足 |
| 规划模块 | ★★★★☆ | 计划校验 + Verifier 独立批判 + 自适应预算 |
| 交互模块 | ★★★★☆ | SSE 断线续传 + waiting_user 恢复闭环 |
| 评估体系 | ★★★★☆ | 83-doc 真实运行评估(70/12/1)闭环,但判定靠人工,无自动 judge |
| 核心推理引擎 | ★★★☆☆ | 降级链完备,但 token 用量零采集 |
| **上下文构建器** | **★★☆☆☆** | **无独立模块、无 token 计量、无 context manifest** |
| **记忆系统** | **★★☆☆☆** | **长期记忆为零;0012/0013 迁移建的偏好/交互表是死 schema** |
| **可观测性** | **★★☆☆☆** | **token/延迟/成本列存在但从未写入;metrics 端点无 agent 指标** |

**最大差距集中在三个方向,与标准中「上下文工程 + 可观测性」的判断一致**:① token 级计量缺失;② context manifest 缺失;③ 长期记忆未接线。而标准中最强调的「防止无限循环 / 持久化恢复 / 人机确认」,本系统**已经做得比行业常见做法更好**。

**重要前提**:标准中的部分建议是针对通用对话型 Agent 的,与本系统场景不匹配,不应照搬(详见「五、不推荐项」)。

---

## 二、逐维度对照评估(10 项核心部件)

### 1. 核心推理引擎 (LLM) — ⚠️ 良好但有重大盲区

**现状**:`LangChainModelGateway`([model_gateway.py:52](../backend/app/services/agent_runtime/model_gateway.py#L52))是统一模型边界:`ChatOpenAI` + `temperature=0` + 结构化输出([model_gateway.py:96-98](../backend/app/services/agent_runtime/model_gateway.py#L96-L98)),含三层有界降级:provider 拒绝 `response_format` → 本地 JSON 校验;schema 校验失败 → 带修正提示的重试(最多 3 次);`invalid_model_response` 全部安全降级为 `waiting_user`(planner)/ 转人工(executor)/ `NEED_USER`(verifier),永不硬失败。这是 20 题和 150 题评估收敛期沉淀的核心成果。

**差距**:
- **token 用量零采集**:`AgentTurn` 表有 `model_name/input_tokens/output_tokens` 列([models.py:1718-1720](../backend/app/db/models.py#L1718-L1720)),`create_turn` 也接受这些参数([repositories/agent_runtime.py:294-313](../backend/app/repositories/agent_runtime.py#L294-L313)),但唯一调用点 `runtime.py:526` 从不传值 → **列永远为 NULL,eval 里 `input_tokens` 恒为 0**。
- 未设置 `max_tokens`;`ChatOpenAI` 的 `usage_metadata` 被丢弃。
- 无延迟/成本/请求次数度量(见维度 10)。
- **未启用 DeepSeek 官方 JSON 模式**(见下「DeepSeek schema 解码能力调研」):当前 deepseek-v4 因 `prefer_local_json_validation=True` 走普通对话补全 + 本地校验,连 `response_format={"type":"json_object"}` 都没发,解码协议层无引导。

#### DeepSeek schema 解码能力调研(2026-08 时点)

**结论:DeepSeek 不支持 schema 约束解码,JSON 模式可用。**

| 能力 | DeepSeek | 说明 |
|---|---|---|
| `response_format: json_object`(JSON 模式) | ✅ 支持 | 官方文档推荐;要求 prompt 含 "json" 字样与示例;偶尔返回空内容(需兜底);**仅保证合法 JSON,不保证符合 schema** |
| `response_format: json_schema`(strict 结构化输出) | ❌ 拒绝 | 报 `response_format.type 'json_schema' is unavailable now`(DeepSeek-V3 issue #302 官方确认;Milvus 参考、2026 年 Provider 对比评测仍为 Strict ❌) |
| 本地 schema 校验 | 必须 | 「model 发 JSON + 客户端 Pydantic/JSON Schema 校验」是社区共识的生产模式(LLMR 等框架同此做法) |

**对当前 agent 的意义**:
- 现有降级链(`_response_format_unavailable` 检测 [model_gateway.py:213-217](../backend/app/services/agent_runtime/model_gateway.py#L213-L217) + `prefer_local_json_validation` + 修正提示重试)**是正确且必要的**——json_schema 在 DeepSeek 上必然被拒,150 题评估 failed 清零正是这套降级链的成果。
- **发现一个实际增量**:当前 deepseek-v4 走 plain invoke(普通补全,需 `_strip_json_fence` 剥代码围栏),可切换为 DeepSeek 官方 JSON 模式(`response_format={"type":"json_object"}`,即 langchain `method="json_mode"`),在**解码协议层**引导合法 JSON,预期降低 `invalid_model_response` 频率与重试开销;现有空内容/校验兜底链已覆盖其已知副作用。此改动改变 provider 协议,列为 P1(需评估验证)。
- langchain 侧已内置 `method="json_mode"`(langchain_openai base.py 发 json_object),无需自研。
- 若 DeepSeek 未来支持 json_schema(strict),可切回 `with_structured_output` 默认路径并关闭 `prefer_local_json_validation`,届时需要重新评估验证。

### 2. 编排器 (Orchestrator) — ✅ 本系统最强项

**现状**:`AgentRuntime.run()`([runtime.py:63-228](../backend/app/services/agent_runtime/runtime.py#L63-L228))是确定性生命周期:run → planner → 持久化 plan → 逐 step executor →(需验证时)verifier → PASS/RETRY/REPLAN/NEED_USER 路由。停止条件完备且分层:
- 4 类硬预算:turn 数、tool 调用数、replan 数、墙钟时间(`AgentBudget`, [schemas.py:24-33](../backend/app/services/agent_runtime/schemas.py#L24-L33)),且按技能数自适应缩放([service.py:43-56](../backend/app/services/agent_runtime/service.py#L43-L56));
- 空转断路器:连续 3 次无进展(去重调用/被阻塞搜索)→ 转人工([executor_agent.py:172](../backend/app/services/agent_runtime/executor_agent.py#L172));
- 去重:连续成功调用的同参重复调用返回 `duplicate_tool_call`,不消耗预算。

**持久化与恢复**:决策原子提交——每次模型决策后写 `AgentTurn` 并 commit 作为检查点([runtime.py:515-538](../backend/app/services/agent_runtime/runtime.py#L515-L538));`resume()`(waiting_user 续跑)与 `recover()`(进程中断后 `replan_from_durable_evidence`)均从持久化计数回填已消耗预算,replan 预算不会因恢复被重复消耗。

**差距**:几乎没有。唯一可议点:步骤间为单线程串行(无并行/子图),但对本场景(证据采集链)合理,不建议为并行而并行。

### 3. 工具与技能 (Tools/Skills) — ⚠️ 良好,错误信息粒度不足

**现状**:`ToolRegistry` + `ToolDefinition`([tool_registry.py:18-28](../backend/app/services/agent_runtime/tool_registry.py#L18-L28))统一契约(name/input_model/output_model/allowed_roles/handler/skill_name/description),输入输出均 Pydantic 校验。9 个工具分布在 5 个技能([registry.py:16-118](../backend/app/services/career_skills/registry.py#L16-L118),含新增的 `query-career-sheet-records` 台账桥接);skill 作用域双重强制(目录过滤 + 调用时 `tool_skill_forbidden` 复核);`invoke` 把所有异常转成 `ToolObservation(status=failed, error_code=...)`,**错误永不泄漏**([tool_registry.py:100-156](../backend/app/services/agent_runtime/tool_registry.py#L100-L156))。

**差距**:
- **部分工具的错误信息对 LLM 完全丢失**:`resume_tailoring.py:62-66`、`career_planning.py:65-69` 抛裸 `ValueError("target_evidence_not_found")`,被 `invoke` 折叠成通用 `tool_execution_failed`,LLM 看到的是无差别错误码;而 `job_discovery.py:68-73` 的 `PublicJobFetchError(code=...)` 同样只透出 code。`ToolObservation`([schemas.py:174-192](../backend/app/services/agent_runtime/schemas.py#L174-L192))只有 `error_code`,**没有 error_message 字段**——模型永远不知道工具「为什么」失败。
- 工具 schema 未声明错误类型(无 OpenAPI 式 `error codes` 元数据)。
- 工具目录每次决策随 state 全量重发(见维度 6),未走原生 function calling,token 成本更高(但这是有意的架构选择,详见「五、不推荐项」)。

### 4. 记忆系统 — ❌ 主要差距(长期记忆为零)

**短期记忆**:本系统**没有对话历史**——每轮把完整状态(含累积 observations)作为单个 JSON blob 重发给模型([model_gateway.py:84-88](../backend/app/services/agent_runtime/model_gateway.py#L84-L88)),多轮记忆只体现在累积的 observations 数组里。增长受结构化截断控制:单条可见文本 1200 字符、最多 10 条([observation_projection.py:18-20](../backend/app/services/agent_runtime/observation_projection.py#L18-L20))、run 级证据上限 48000 字符([runtime.py:545-572](../backend/app/services/agent_runtime/runtime.py#L545-L572))、用户回复保留最近 10 条([repositories/agent_runtime.py:103-121](../backend/app/repositories/agent_runtime.py#L103-L121))。**无摘要/无淘汰策略**。

**长期记忆**:**完全没有接线**。migration 0012/0013 已建 `user_preferences`、`user_job_interactions`、`job_relevance_scores`、`PersonalizedDiscovery*` 表([models.py:1214-1371](../backend/app/db/models.py#L1214-L1371)),但除 `models.py` 外**全代码库无任何消费方**——是死 schema。无 vector store(全库零 embedding 代码)。

### 5. 知识库 (Knowledge) — ⚠️ 部分接入

**已接入**:① `confirmed_profile_facts`(用户已确认的档案快照,planner 只见字段名、executor 见值——有意的隐私分层,[planner_agent.py:119-124](../backend/app/services/agent_runtime/planner_agent.py#L119-L124));② run 内采集的 `observed_public_evidence`(证据绑定,工具只能读工具产出的证据)。

**未接入**:`verified` job_postings、`user_preferences` 均存在但 agent 运行时不查询。技能清单元数据 `CareerSkillManifest` 仅被测试消费,运行时不读([manifest.py:8-16](../backend/app/services/career_skills/manifest.py#L8-L16))。

### 6. 上下文构建器 (Context Builder) — ❌ 主要差距

**现状**:**无独立模块**。提示词分两处内联组装:① 每角色散文式指令常量(planner 74 行 [planner_agent.py:23-96](../backend/app/services/agent_runtime/planner_agent.py#L23-L96)、executor ~140 行 [executor_agent.py:28-166](../backend/app/services/agent_runtime/executor_agent.py#L28-L166)、verifier 19 行);② 每轮 state dict(goal/可用工具目录/observations/剩余预算/verifier 反馈等,[executor_agent.py:243-262](../backend/app/services/agent_runtime/executor_agent.py#L243-L262))。gateway 拼成 SystemMessage + HumanMessage(JSON)。

**差距**:
- **无 token 计量/预算分配**:系统提示、工具目录、观测上下文各占多少字符/token,从不统计,也没有按预算分配的策略(标准建议的 200K 窗口按比例分配在此不适用——DeepSeek 上下文小得多,更需要精确计量)。
- **无 context manifest**:不记录「本次决策用了哪些系统提示、哪些工具、多少观测」,排障靠人工翻 JSON trace。
- **无压缩/摘要**:48000 字符证据上限是硬截断——长链任务(如 83-doc 集的 3-link chain)早期证据会被机械切掉,无「旧证据降级为摘要行」的平滑策略。
- 系统提示未结构化分层(角色/规则/流程/输出契约混在长散文里),无 few-shot。

### 7. 规划模块 (Planner) — ✅ 良好

`ExecutionPlan.validate_plan_authority` 校验技能授权([schemas.py:146-171](../backend/app/services/agent_runtime/schemas.py#L146-L171))、already-collected 目标必须有交付步骤、`build_adaptive_agent_budget` 按技能数给 turn 预算。Verifier 的 REPLAN 路由构成闭环批判。差距:无规划后的自我反思(reflection)环节——但 Verifier 已承担独立检查角色,再加 reflection 属过度设计。

### 8. 自主性与策略 (Policy & Autonomy) — ✅ 强项

7 条安全硬门全部落实:永不自动提交(无 submit 工具,executor 指令强制 `needs_user`)、不绕过登录/验证码、学生 API 只出 verified、不泄密、Redis 非权威、任务租约、review_version 乐观锁。SSRF 防护完善:每跳重验公网 URL、全局 IP 路由守卫([job_discovery.py:336-441](../backend/app/services/career_skills/job_discovery.py#L336-L441))。人机回环是运行时一等公民(`waiting_user` 状态 + 可恢复问题)。

### 9. 交互模块 (Interface) — ✅ 良好

前端 agent-workspace:自然语言 goal + 技能勾选 + 候选 URL;SSE(fetch + Bearer 头,1s 轮询 MySQL,`Last-Event-ID` 游标断线续传);`waiting_user` 恢复表单、`running` 崩溃恢复入口;事件中文标签、证据/简历改稿/备面计划结构化预览([AgentWorkspace.vue](../frontend/src/features/agent-workspace/AgentWorkspace.vue))。

### 10. 评估与追踪 (Eval & Trace) — ⚠️ 追踪强、度量弱、判定人工化

**追踪**:`decision_summary` 白名单(仅 action/tool_name/verification_decision,[tracing.py:10-25](../backend/app/services/agent_runtime/tracing.py#L10-L25))+ 决策原子落库——隐私安全且可靠。**但**:
- 无 token/延迟/成本采集(列存在未写入,见维度 1);
- `/metrics` 端点只有 app_info/ready/dependency_up 三个存活探针([metrics.py:13-63](../backend/app/api/routes/metrics.py#L13-L63)),零 agent 业务指标;
- 无 context manifest。

**评估**:83-doc 重构题集是**当前权威基线**([SUMMARY.md](../tests/question/eval_results/results/SUMMARY.md)):真实 DeepSeek + 真实公网抓取 + SQLite :memory:,83 文档 = 68 独立题 + 15 条链(34 个 link),终版 **70 succeeded / 12 waiting_user / 1 failed(84.3%)**,15 条链全部通过;前 150 题集与真实证据可得性脱节(成功率仅 ~11%),**出题不准确,已归档**为 `SUMMARY-150q-legacy-2026-08-05.md`。8 项提示词修复(结构化提取非前置、台账查询禁角色关键词、连续失败硬停止、搜索观察上限 3 次等)均为提示词层、框架零改动。**但**判定仍靠人工语义分类(12 条 waiting_user 由人工归为「确定性证据边界 / 诚实索要」),`reference_answer` 字段全为 null,无自动 judge。

---

## 三、对照四大优化方向的差距确认

| 标准方向 | 本系统状态 | 差距 |
|---|---|---|
| 1. 上下文工程 | 结构性截断有,但无 token 计量、无 manifest、无压缩、提示词未分层 | **最大杠杆,建议重点投入** |
| 2. 编排与循环控制 | 最大迭代数 ✅ / 持久化+检查点恢复 ✅ / 人机确认 ✅ | **几乎无差距,无需动** |
| 3. 工具与技能设计 | 统一 schema ✅ / 错误永不崩溃 ✅ / 动态按技能注册 ✅ | 仅剩「错误信息粒度 + 错误类型入 schema」 |
| 4. 可观测性与评估 | 决策追踪 ✅ + 83-doc 真实 eval ✅;token/延迟零采集、无 manifest、判定人工化 | **第二大投入方向** |

---

## 四、分级优化建议

### P0 — 低成本高回报(不动行为,纯观测/信息增量)

1. **Token 用量计量落地**:gateway 在 `decide` 成功后从响应 `usage_metadata` 读取 `prompt_tokens/completion_tokens`,经 trace 回调写入 `AgentTurn.input_tokens/output_tokens`(列与参数均已存在,只需接线)。连带收益:eval 的 `input_tokens` 从恒 0 恢复,83-doc 评估从此可做成本曲线。改动点:`model_gateway.py`(返回 usage)、`runtime.py:515-538`(透传)。纯增量,不改行为,单测补 100% 覆盖即可,无需重跑评估。
2. **Context Manifest 生成**:每次决策在 `AgentTurn.decision_json` 旁记录上下文清单——系统提示字符数、工具目录数量/总字符、observations 条数/总字符、evidence 总字符、模型名。用一次性摘要函数(可放 `observation_projection.py` 旁或新模块),排障时直接看到「这次决策到底喂了多大上下文」,为后续压缩策略提供数据。同样纯增量。
3. **工具错误信息粒度**:`ToolObservation` 增加可选 `error_message`(结构化短文本,遵循现有「不泄漏原始 payload」红线);`resume_tailoring`/`career_planning` 的裸 `ValueError` 升级为带 `code` 的专用异常(仿 `PublicJobFetchError`),让 LLM 能区分「证据缺失」与「证据不完整」。这是评估中 `E_evidence_no_match` 类失败的可诊断性改进。

### P1 — 中等成本,改变送入模型的上下文,需用 83-doc 题集验证

4. **观测上下文分层压缩**:用「旧 observation → 单行摘要 + 新 observation → 完整投影」替代当前 10 条硬截断(保持 48000 字符总上限)。对 83-doc 集的 15 个 2-3 链式题尤其有价值——早期 link 的证据当前会被机械切掉。**此改动改变模型输入,需在 83-doc 题集上验证无回退**(已确认不强制全量,但建议至少跑受影响子集)。
5. **系统提示结构化分节**:executor 的 ~140 行散文按 角色/行为规则/流程/输出契约/禁止项 分节,并输出每节 token 统计;正面表述。可先量化再改,避免凭感觉改提示词(评估收敛的经验是「确定性 > 提示词」)。
6. **工具目录增量复用**:目录为 run 级常量,评估能否用 prompt caching 或「首次全量、后续仅变化」降低每次决策重发成本(与 4 结合后是上下文预算管理的主体)。
7. **DeepSeek 官方 JSON 模式**:`build_agent_model_gateway`([model_gateway.py:250-275](../backend/app/services/agent_runtime/model_gateway.py#L250-L275))对 deepseek-v4 改为 langchain `method="json_mode"`(发 `response_format={"type":"json_object"}`),在解码协议层引导合法 JSON,预期减少 `invalid_model_response` 与重试;现有修正提示/空内容/本地校验兜底链保持不变。**改变 provider 协议,建议在 83-doc 题集子集上验证无回退**(已确认不强制全量)。

### P2 — 战略性,需产品决策

8. **长期记忆接线(不做 vector DB)**:把 migration 0012 的 `user_preferences`(期望岗位/城市/排除公司等)接入 planner 决策输入——这是本场景最自然、ROI 最高的「长期记忆」,比嵌入向量检索便宜得多且可解释。行为会变(规划会参考偏好),需评估验证。
9. **Eval 自动判定增强**:用 Verifier(或独立 judge 模型)对 eval 结果做自动初判,逐步填充 `reference_answer`,把人工复盘(当前 70 条 succeeded 的抽查判定 + 12 条 waiting_user 分类)从「全人工」降为「抽查」。
10. **Metrics 扩展**:run 计数、状态分布(等待用户占比)、turn/工具调用分布、token 成本曲线等 Prometheus 指标。

### 五、不推荐项(明确说明为何不适用)

- **vector DB / embedding 长期记忆**:本场景是「每 run 独立采集证据」,跨 run 语义检索价值低;偏好表(P2-8)先落地,若未来有「历史投递复盘」需求再考虑。
- **原生 function calling / json_schema strict 迁移**:DeepSeek 不支持 `json_schema` strict 解码(见维度 1 调研),「schema 约束解码」在当前 provider 上不可用;可用的替代是「json_object 模式 + 本地校验」(P1-7),而非迁移原生 function calling——单决策对象架构不需要 tool-calling 协议,迁移只会与现有降级链冲突。
- **对话历史摘要/压缩**:系统无多轮自由对话(每轮全量重发),摘要无适用对象;需要的只是观测层压缩(P1-4)。
- **为「上下文预算比例分配」(200K 窗口 10%/20%/25%)照搬**:DeepSeek 上下文远小于 200K,且本系统是单轮 JSON 决策,该比例表无意义;需要的只是精确计量 + 分层压缩。

---

## 六、实施约束(用户已确认)

- 本报告交付形式:**仅评估报告,不含代码改动**。
- 权威基线:**83-doc 终版(70/12/1,84.3%)**,以 [SUMMARY.md](../tests/question/eval_results/results/SUMMARY.md) 为准;150 题集出题不准确,已归档为 `SUMMARY-150q-legacy-2026-08-05.md`。
- 未来实施前提:可接受行为改动;不强制重跑 83-doc 全量评估,但 P1 级行为改动建议至少跑受影响子集;**单测必须保持 100% 分支覆盖**(`pytest tests/unit/ -q` + ruff)。

## 七、验证方式

- 本报告为只读评估,验证 = 证据链完整(全部结论附 file:line,关键文件已人工复核)。
- 若后续实施 P0:补对应单测(100% 覆盖门禁),跑 `tests/unit/` 全量 + `ruff check`;行为断言「token 列从 NULL 变为非空、error_message 正确透出、context manifest 结构正确」。
