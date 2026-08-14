# 83 题 live 评测：waiting_user 原因分析

日期：2026-08-14

## 评测边界

- 固定 10 题结果目录：`tests/question/eval_results/prompt_iter_08/`
- 其余 73 题结果目录：`tests/question/eval_results/all_73_20260814_2p_stagger60_live_retry1/`
- 运行方式：其余 73 题使用 2 个 worker，worker 02 比 worker 01 延迟 60 秒。
- 用户要求停止时已终止本次 73 题 worker；不覆盖历史结果。

固定 10 题全部完成：7 succeeded、3 waiting_user。其余 73 题已完成 56 题：12 succeeded、44 waiting_user；另有 17 题尚未执行：R028、R030、R032、R034、R035、R036、R037、R038、R039、R040、R041、R042、R043、R044、R045、R046、R047。

因此当前可合并分析的是 66/83 题：19 succeeded、47 waiting_user、0 failed。19 个 succeeded 的成功审计全部通过；不能把这组数字当成完整 83 题最终成绩，因为 17 题没有运行。

## waiting_user 互斥分类

| 类别 | 数量 | 题目 | 主要判断 |
|---|---:|---|---|
| 外部来源阻断 | 12 | C002、C015、Q034、Q071、Q115、R006、R013、R015、R018、R019、R020、R031 | 站点反爬、403、临时封禁或适配器空结果；不应绕过 |
| 模型/执行协议异常 | 7 | C001、C003、C004、C008、C012、Q055、Q133 | 计划或终态 JSON 不可解析，部分未产生工具证据 |
| 证据/硬约束不可满足 | 8 | C005、C010、C014、Q045、R005、R008、R012、R014 | 有工具执行，但没有同时满足时间、岗位、地点、经验或可访问正文等硬条件 |
| 可信交付契约失败 | 12 | C006、C007、C009、C013、Q081、Q148、R016、R017、R021、R025、R026、R033 | Verifier 或成功审计正确拒绝字段缺失、来源错位、岗位错配或 list-only 证据 |
| Planner 上下文缺失导致重规划耗尽 | 7 | R001、R002、R003、R004、R007、R009、R010 | 首步需要 `recent_days`、`role_keywords` 或 `company_keywords`，但运行上下文没有注入；没有进入工具执行 |
| 重复调用/无进展 | 1 | C011 | 已有证据集合没有变化，Verifier 重试后被进度保护终止 |

合计：47 题。

### 1. 外部来源阻断：12 题

细分如下：

- `adapter:empty_result`：2 题（C002、C015），百度官方详情 URL 适配器返回空正文。
- `anti_bot_challenge`：4 题（Q071、Q115、R006、R013）。
- `access_denied`：6 题（Q034、R015、R018、R019、R020、R031），其中国聘网出现 403/临时封禁或只有 `list_only` 页面。

工具轨迹中出现了 `access_denied`、`anti_bot_challenge`、`domain_temporarily_blocked`、`adapter:empty_result`。这些不是业务逻辑可以可靠消除的失败，应保留为外部阻断，并尽早停止同一路由的重复尝试。

优化方向：

1. 为每个来源增加一次性可用性探针；同域名连续出现 403、验证码、临时封禁或空正文后，直接标记来源不可用。
2. 使用已验证的其他公开来源或用户提供的具体详情页，不伪造 URL、不绕过登录/验证码/反爬。
3. `list_only` 只能作为搜索线索，不能进入成功交付；如果没有公开详情链接，应直接生成可解释的人工交接。

### 2. 模型/执行协议异常：7 题

- C001、C003、C004、C008、C012：Planner 输出格式异常，未生成可执行计划，工具调用基本为空。
- Q055：Executor 返回 `deep_executor_invalid_response`，已有部分抓取证据但没有可解析终态。
- Q133：Executor 终态不可解析，同时候选链接出现 `dead_link`。

优化方向：

1. 对 Planner/Executor 使用统一的 schema-first 校验、有限本地 JSON 修复和稳定的错误回传；连续解析失败时保留最后一个有效计划，不要直接把“无工具证据”变成泛化的 waiting_user。
2. 对带有可信候选 URL 的题目提供严格的确定性 fallback；fallback 只能使用已有候选 URL 和工具注册表，不能生成测试专属事实。
3. 将 `deep_executor_invalid_response`、`dead_link` 与真正的“需要用户补充信息”分开统计和提示，便于自动重试或人工诊断。

### 3. 证据/硬约束不可满足：8 题

代表性轨迹：

- C005、R005：台账查到公司记录，但 19/20 个微信招聘页无法通过 OCR/正文门控；已有记录不等于已有可核验 JD。
- C014：抓到的岗位均为资深岗且不是 AIGC 产品经理，不能因有岗位字段就 PASS。
- R008：最近 3 天窗口没有符合条件的记录，放宽到 30 天不满足用户原约束。
- R014：快手/小红书没有活跃且符合硬条件的公开岗位，且某来源临时封禁。
- C010、Q045、Q133、R012：没有可用的目标岗位详情或链接已失效，无法形成可信交付。

优化方向：

1. 在 Planner 阶段显式编译硬约束：岗位族、地点、经验、应届/社招、时间窗、公司类型；在抓取和匹配前就生成可检查的约束对象。
2. 对“零匹配”定义稳定的终态：说明已检查的来源、缺失的约束和可放宽选项，不重复抓取同一来源。
3. 对 OCR/死链类来源提供安全替代路径；没有可验证正文时保留 waiting_user，不把搜索摘要或列表卡片升级为 JD。

### 4. 可信交付契约失败：12 题

这是当前最有价值的内部优化类别。具体问题包括：

- C009：候选 `title` 实际是 JD 说明文字，不是职位名称；重复岗位和不完整 excerpt 被正确拒绝。
- Q148、R026：岗位标题关键词命中，但地点、AIGC、应届生和 profile facts 没有满足。
- R017、R021：用户要求央国企官方来源，却使用猎聘来源；或抽取结果只有 `candidate_titles`，没有完整 `structured_job_details`。
- R025：简历定制针对烟台高级岗位，而用户要求广州/深圳、2 年经验前端岗位。
- R033：匹配结果没有继承 job-discovery 的 artifact refs/profile facts，且混入旧帖或不相关标题。
- R016：成功审计发现没有完整 `jd_complete` 公共职位页，因此把模型声称的成功降为 `success_contract_not_satisfied`。

工具轨迹显示，这 12 题并非都“没有抓取”：记录中已有大量页面和结构化尝试，但来源、字段、岗位约束或 artifact 继承不一致。继续增加抓取次数不能解决根因。

优化方向：

1. 给 `structured_job_details` 增加强制字段与来源绑定：`artifact_id`、`source_url`、`title`、`company_name`、`location`、`requirements`、`apply_url`、`content_hash`。
2. 将硬约束过滤前置到匹配工具，先过滤地点/经验/岗位族/应届/公司类型，再排序，禁止仅凭“产品经理”“Java”等宽关键词得分。
3. 在链式任务中强制传递上一步的 `execution.artifact_refs` 和 confirmed profile facts；匹配工具拒绝不属于上游 artifact 的候选。
4. 对 verifier 的可修复反馈增加确定性修复动作，例如重新提取职位头部、重新绑定来源、重新按地点/经验筛选；避免在同一错误 artifact 上重复 RETRY。

### 5. Planner 上下文缺失与重规划耗尽：7 题

R001、R002、R004、R007、R010 缺少 `recent_days`；R003 缺少 `role_keywords`；R009 缺少 `company_keywords`。这些题的计划已经生成，但第一步被依赖门禁跳过，随后消耗 3 次重规划预算，完全没有进入工具执行。

优化方向：

1. 增加 task-context compiler，从题目文本、meta 和 profile 中确定性提取 `recent_days`、`role_keywords`、`company_keywords`、`preferred_locations`、`profile_keywords`。
2. Planner schema 校验时区分“可从用户问题推导的上下文”和“必须用户补充的上下文”；前者由 harness 自动注入，不应消耗 replan budget。
3. 将缺少必需上下文从 `replan_budget_exhausted` 改为一次性、可诊断的 `missing_context`，并记录具体字段和值来源。
4. 为 R001–R010 增加回归测试，断言首个 Smartsheet 查询实际发生，而不是只断言最终状态。

### 6. 重复调用/无进展：1 题

C011 已经拿到 6 个公开页面，但 Verifier 连续要求重试；后续工具调用没有新增 artifact，最终以 `no_progress_duplicate` 结束。相关重复信号还在其他题中出现：`route_already_consumed` 10 次、`duplicate_tool_call` 6 次、`executor_stalled` 12 次。这些计数跨题重叠，不能直接相加。

优化方向：

- 用“新增 artifact/source URL/content hash/质量状态”作为进度判据，而不是只看工具是否返回成功。
- 同一路由和同一输入成功后禁止重复；若页面是 `list_only`，只允许一次详情路由发现，再无新链接就转人工交接。
- Verifier 重试时传入已尝试路线和失败原因，避免重新调用相同工具参数。

## 优先级建议

### P0：先修内部、确定能减少 waiting_user 的部分

1. 修复 7 题上下文编译与 replan budget 消耗。
2. 修复 5 题 Planner 计划格式异常，并覆盖 Q055/Q133 的 Executor 终态解析。
3. 修复 12 题的 artifact/硬约束校验与匹配过滤。

### P1：减少重复与误诊

1. 统一 `route_already_consumed`、`duplicate_tool_call`、`executor_stalled` 的进度状态。
2. 将“用户硬约束没有公开匹配”与“外部站点反爬”分别输出。
3. 增加来源可用性探针和早停机制。

### P2：外部能力边界

继续增加反爬站点抓取强度不建议纳入本轮代码目标；应使用合规替代来源或人工提供详情页正文。

## 结论

在已经完成的 66 题中，47 个 waiting_user 并不都是反爬：12 个属于外部阻断，35 个来自内部协议、证据契约、上下文、预算或无进展问题。优先修复 P0 后，系统才有机会把一批当前 waiting_user 转为 succeeded；外部阻断的 12 题应保持安全交接，不应通过绕过站点安全限制解决。
