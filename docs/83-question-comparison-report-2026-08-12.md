# 83 题评测对比与 waiting_user 审计（2026-08-12）

## 结论

本轮 4 进程、每 60 秒错峰的全量评测为 **58/83 succeeded、24/83 waiting_user、1/83 failed**；上一轮 8 进程错峰为 **30/83、52/83、1/83**。success 净增加 28 个，成功率从 36.1% 提升到 69.9%。

主要提升来自降低并发后公网招聘页面更稳定；但本轮仍明确暴露两个代码层问题：`match-observed-jobs` 的输入契约仍会在真实模型调用中触发 `invalid_tool_input`，以及在步骤进入 `need_user` 或 Skill 越权后，Executor/Planner 仍可能继续尝试后续工具。JD 目标证据断链没有再以 C 链直接失败的形式出现，但仍需专门的下游成功链重放才能宣称完全解决。

## 本轮成功题目

### C 链：9/15

`C001 C006 C007 C009 C010 C011 C012 C013 C014`

覆盖猎聘大模型、Java 后端、前端、AIGC 产品经理等专区的岗位抓取，部分链还完成了匹配和简历定制。

### Q 链：12/21

`Q011 Q013 Q017 Q028 Q034 Q045 Q046 Q055 Q057 Q081 Q143 Q148`

覆盖前端/LLM/AI 应用/AI 产品经理的简历定制、面试准备、国聘/猎聘/字节/掘金等岗位搜索与匹配。

### R 链：37/47

`R001 R002 R005 R006 R007 R011 R014 R015 R016 R017 R018 R019 R020 R021 R022 R023 R024 R025 R026 R027 R028 R029 R030 R031 R032 R033 R035 R036 R037 R038 R040 R041 R043 R044 R045 R046 R047`

覆盖招聘数据源、国聘、猎聘、掘金、腾讯、字节、百度等来源的岗位检索、匹配、投递链接核验和面试/简历建议。

## 与上一轮的状态变化

### 新增成功：36 题

`C001 C006 C007 C009 C010 C011 C012 C013 C014`；
`Q011 Q013 Q028 Q045 Q046 Q055 Q057 Q148`；
`R001 R002 R005 R006 R014 R023 R024 R025 R026 R027 R028 R029 R033 R035 R038 R043 R044 R045 R046`。

这些变化说明低并发对实时页面抓取和后续链路完成有显著帮助。

### 上一轮成功、本轮未成功：8 题

| 题目 | 本轮状态 | 主要原因 |
|---|---|---|
| C002 | waiting_user | 匹配工具 3 次 `invalid_tool_input` |
| Q113 | waiting_user | 没有可用 AI 应用开发实习生 JD 证据 |
| Q144 | waiting_user | 腾讯产品经理微信 OCR 失败，搜索为空 |
| R008 | waiting_user | 工具未产生可核验交付物 |
| R009 | waiting_user | `wall_clock_budget_exhausted` |
| R034 | waiting_user | 掘金 AIGC 产品经理 JD 证据为空 |
| R039 | waiting_user | 工具未产生可核验交付物 |
| R042 | waiting_user | 腾讯最近 1 天数据源无记录，官网仅社招 |

这些回归中，只有 C002 直接指向当前匹配输入契约；其余主要是公网/数据源波动或运行预算问题。

## 当前 waiting_user 的工具调用审计

### P0：匹配输入契约仍未解决

以下三个链路均已成功抓取和结构化提取 JD，但匹配连续失败：

| 链路 | 前置结果 | 工具过程 | 结果 |
|---|---|---|---|
| C002-L2 | fetch 7/7、extract 7/7 | `match-observed-jobs` ×3 | 0 success，3 `invalid_tool_input` |
| C004-L2 | fetch 19/19、extract 4/4 | `match-observed-jobs` ×3 | 0 success，3 `invalid_tool_input` |
| C015-L2 | fetch 7/7、extract 7/7 | `match-observed-jobs` ×3 | 0 success，3 `invalid_tool_input` |

因此，“把 limit 归一化到 100”没有覆盖真实模型请求的全部非法形状。当前结果 JSON 没有保存被拒绝的原始 payload，下一步必须增加 tool-input 诊断 trace，记录字段级 Pydantic 错误和脱敏后的 payload，才能判断是 `limit` 类型、`ranking_criteria` 形状、关键词/地点数量上限，还是其他字段导致失败。

### P0：步骤失败后仍继续调用越权工具

C005-L2 的计划显示第一步属于 `job-discovery`，且因微信 URL 抓取失败进入 `need_user`；但随后仍调用了 `match-observed-jobs`。该工具属于 `job-matching`，所以得到 `tool_skill_forbidden`，不同 payload 又被去重为 `duplicate_tool_call`。

C003-L2 也表现出类似模式：多轮 `fetch`、`extract` 后调用匹配工具，连续得到 `tool_skill_forbidden`/`duplicate_tool_call`，最终 `replan_budget_exhausted`。

当前已有“同一越权工具不同 payload 去重”，但这只是抑制预算消耗，不能替代流程修复。需要保证：

1. 当前 PlanStep 返回 `need_user`、永久失败或 Skill 不可用时，Executor 立即结束该 step；
2. 后续 step 不得在前置交付物不存在时启动；
3. replan 必须重新生成合法的单 Skill step，而不是继续沿用失败 step 的工具候选。

### P1：公网/数据源不可用仍是主要 waiting_user 来源

当前 24 个 waiting_user 中，19 个属于证据不可用或数据源不满足：

- 缺少/空的观察 JD：Q071、Q113、Q114、Q134、R034、R039、Q103、Q133、R008、R012；
- 微信 OCR 失败或搜索为空：R003、R004、R010、Q115、Q144；
- 数据源没有目标记录或只有不符合条件的岗位：R013、R042；
- 猎聘登录墙/内容不足：C008。

这部分不能通过上下文压缩直接解决。需要单独处理来源策略：官方招聘页 fallback、微信 OCR 可用性、搜索返回空时的明确终止、以及列表页只能拿到摘要时的降级交付标准。

### P1：时间预算与长链上下文

R009 直接因 `wall_clock_budget_exhausted` 等待用户。C008-L3 的累计 input tokens 为 147,143；其他当前等待的匹配链约 70k-100k。这个字段是累计链路消耗，不能证明单次模型请求发生 context overflow，但它说明失败重试和重复规划正在放大上下文与时间成本。应优先减少失败后的重复抓取/重复匹配，并对链路级输入做上限和压缩审计。

## 哪些之前的问题已解决或明显改善

1. **并发导致的公网抓取不稳定明显改善。** 从 8 进程错峰的 30 success 提升到 4 进程错峰的 58 success；C001、C006、C007、C009-C014 等之前等待的抓取链本轮成功。
2. **目标 JD 指针断链未在本轮 C 链直接复现。** 本轮成功的 C010、C014 等已经能从抓取/结构化证据进入匹配或简历定制；但 Q134 仍保留此前 `target_evidence_not_found` 的用户等待结果，且三个匹配失败链没有机会验证下游 tailoring/planning。因此只能判定“有改善、未完成闭环证明”。
3. **越权工具的重复调用已被抑制。** C005/C003 的第二次不同 payload 不再被当成新的有效工作，而是落为 `duplicate_tool_call`；但流程仍然错误地触发了越权调用，所以问题尚未完全解决。
4. **固定 100 的 limit 兼容修复未达到验收标准。** 真实全量评测中 C002/C004/C015 仍有 3 次连续 `invalid_tool_input`，因此不能宣称已解决。

## 建议的修复优先级

### P0

1. 增加 `invalid_tool_input` 的字段级 trace，并用 C002/C004/C015 的真实 payload 重放；扩展模型输入归一化覆盖非 canonical 的 `ranking_criteria`、字符串/数字 limit、缺省字段和列表上限，但最终业务截断仍固定为 100。
2. 修复 `need_user`/前置 step 失败后的执行门控：没有前置 artifact/report 时禁止进入匹配、简历定制或准备计划；Skill scope 失败应结束当前 step，而不是继续尝试工具。

### P1

3. 对 `target_evidence_not_found` 做端到端链路重放：job-discovery → job-matching → resume-tailoring/preparation，验证 candidate_id、artifact_id、source_artifact_id 和 source_url 四种指针都能落到同一持久化 JD。
4. 对公网抓取单独做低并发 live smoke test，不把公网登录墙/OCR 失败混入 runtime 契约回归判定。
5. 为失败重试增加链路级 token/turn 预算观测，避免在相同证据上重复 fetch、extract、match。

## 证据路径

- 上一轮结果：[lazy_jd_full_20260812_8p_staggered](../tests/question/eval_results/lazy_jd_full_20260812_8p_staggered/)
- 本轮结果：[lazy_jd_full_20260812_4p_staggered_run3](../tests/question/eval_results/lazy_jd_full_20260812_4p_staggered_run3/)
- 现有背景报告：[83-question-full-eval-report-2026-08-12.md](83-question-full-eval-report-2026-08-12.md)
