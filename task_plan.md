# Task Plan: 本轮83案例全量在线评测与自适应优化

## 2026-08-13 P0/P1 收敛优化

### Goal
在不绕过公网反爬和不放宽评测契约的前提下，消除 DeepExecutor 证据已满足后的空转/终态硬失败，并修复 planner 非法计划与 list-only 详情页流程。

### Phases
- [x] P0-A：锁定 `jd_complete` 评测契约与运行时完成门禁测试
- [x] P0-B：工具调用后确定性收敛，补齐终态解析失败降级与 trace
- [x] P1-A：补强 completion contract 提示词与结构化 JD 完成规则
- [x] P1-B：planner 非法计划有界重试
- [x] P1-C：list-only 详情页展开与来源一致性校验
- [x] Verification：定向单测、ruff、10 题公网回归（代码门禁完成；公网回归待单独启动）

### Invariants
- 不自动绕过 Liepin 验证码、登录、反爬或安全页。
- `list_only`、无正文、无 hash、无 source_url 的页面不得作为成功 JD。
- 替代来源必须通过职位/公司/来源一致性校验，不能用无关职位百科冒充目标 JD。
- 现有未提交修改全部保留，测试结果写入新目录。

### Status
**Currently complete for code phase** - P0/P1 代码、定向测试、全量单元测试与代码质量检查完成；公网 10 题未启动。

# Task Plan: 10轮提示词快速迭代（前10个非链式案例）

## 本轮目标
仅通过修改 Planner、Executor、Verifier 的业务无关运行规则提示词，循环测试前10个非链式案例；每轮保存结果和工具调用归因，最多10轮，达到7/10 succeeded立即停止。

## 固定测试集
`Q011 Q013 Q017 Q028 Q034 Q040 Q045 Q046 Q055 Q057`

## 迭代阶段
- [ ] 第1轮：当前提示词基线
- [ ] 第2轮：失败分流与 Planner 提问门槛
- [ ] 第3轮：Executor 证据复用与工具选择
- [ ] 第4轮：Verifier 交付契约与 PASS/RETRY
- [ ] 第5轮：三角色协同状态读取
- [ ] 第6轮：格式修复与行动一致性
- [ ] 第7轮：预算/重复调用收敛
- [ ] 第8轮：反爬边界下的最小交接
- [ ] 第9轮：综合规则压缩与冲突消除
- [ ] 第10轮：最终提示词验证

## 停止条件
- 任一轮直接成功数 >= 7：提前停止。
- 完成第10轮：停止并报告最终成功数及不可通过的外部限制。

## 非作弊边界
- 不在提示词中写入岗位、公司、URL、岗位要求、答案或测试集专属事实。
- 只增加角色决策规则、证据/工具使用注意事项、失败分流和交接边界。

## 当前状态
已完成测试集确认，准备运行第1轮。

## 本轮目标
运行83个顶层案例，每180秒统计状态；全量结束后按工具调用/失败轨迹评估，必要时修改提示词、Harness或工具层并完成回归验证。

## 本轮阶段
- [x] 启动83案例与180秒监控
- [x] 完成全量状态统计和严格成功审计
- [x] 按根因决定提示词/Harness/工具层改动
- [x] 运行定向与全量回归
- [x] 记录结果并交付结论

## 当前状态
已完成本轮全量评测、定向回归、提示词与 Harness 优化；所有新评测均写入独立目录，未覆盖历史结果。

## 结果摘要
- 全量目录：`tests/question/eval_results/round6_live_20260812_prompt_harness_adaptive/`
- 直接按 83 个 manifest 顶层 ID 统计：1 succeeded、82 waiting_user、0 failed、0 unknown。
- 定向目录：`tests/question/eval_results/round6_targeted_after_prompt_harness/`，16/16 waiting_user；主要由 Smartsheet 限流和公开来源证据不足造成。
- R020 路由保护复测：fetch 48→12 次，wall-clock 328.3s→94.2s，终态从 `wall_clock_budget_exhausted` 改为可解释的 `no_progress_duplicate`。
- 回归：`tests/unit` 1547 passed、1 warning；Ruff 通过。全仓 `pytest -q` 未采用，因历史 `temp/round5_worktrees` 被 pytest 自动收集导致 import mismatch。

# Task Plan: waiting_user 优化执行（借鉴 job-board-aggregator + P0/P1/P2）

## Goal
1. 借鉴 job-board-aggregator 的 SPA 内嵌 JSON/数据 API 提取方法，解决 42 例合理 waiting_user 中的 iguopin/大厂官网部分（11–19 例）
2. 执行 P0（12 例 match/extract 工具链岗位级拆分 + invalid_tool_input 错误信息）
3. 执行 P1（11 例 iguopin 种子 URL 换详情/搜索页 + 预检，测试层）
4. 执行 P2（4 例 search-public-job-pages site: 限定 + 招聘域名白名单）
5. **终验**：自研架构全量 83 题 eval，success 数 > 基线（17 单题 / 26 链接），且无回归（0 failed、unit 100% branch）

## Phases
- [x] Phase A（P0，12 例）：match/extract 工具链修复 —— 列表页按岗位条目拆分输出 + invalid_tool_input 给出具体 schema 失败字段
- [x] Phase B（借鉴项目）：SPA 可行性验证 → 结论：**不需要 embedded-JSON 提取**，Playwright 渲染管线已覆盖；实际增量为 Pattern 5（iguopin 「」卡片拆分）
- [x] Phase C（P1，测试层）：iguopin 类种子 URL 从首页换成具体岗位/搜索页 + 预检脚本
- [x] Phase D（P2，4 例）：search-public-job-pages 加 site: 限定 + 招聘域名白名单过滤
- [x] Phase E：全量回归 —— tests/unit 100% branch + ruff + 83 题 eval 对比 v2 基线

## Key Questions
1. iguopin 内嵌 JSON/数据 API → **无**（CRA 空壳 + 所有 API 请求 nginx 405 需浏览器会话）；但 Playwright 渲染成功：首页（公告流 1600+ 字符）、搜索页 `https://www.iguopin.com/job/list?keyword=<kw>`（2342 字符岗位卡）均产出可用文本。bytedance `https://jobs.bytedance.com/experienced/position` 渲染 4431 字符 Feishu 式岗位卡（含 JD 正文）
2. match-observed-jobs 的 invalid_tool_input → 已修复：tool_registry.py 把 pydantic `exc.errors()` 的 loc/type/msg 写入 error_message（脱敏，不含提交值）
3. extract-observed-job-details-batch 页面级聚合根因 → 已修复：jd_extraction.py 新增 Pattern 4（猎聘 `【】`，真实页 5032→40 段→31 候选）+ Pattern 5（iguopin `「」`，真实页 2342→18 段→18 候选）；均要求 ≥2 卡片
4. search-public-job-pages 过滤逻辑 → Phase D 处理

## Decisions Made
- 借鉴边界：只提取**页面本身公开可达的数据**，不复制 job-board-aggregator 的 UA 轮换/Origin-Referer 伪造（违反安全红线 never bypass anti-bot）
- 执行顺序按 ROI：P0 工具链（纯内部、确定收益）→ B 可行性验证（不确定，先探）→ P1 种子（测试资产）→ P2 搜索 → E 全量终验
- **Phase B 决策**：SPA 内嵌 JSON/数据 API 提取**不实施**——iguopin API 需浏览器会话（405），bytedance HTML 无 embedded JSON；现有 Playwright 渲染管线（含 public-URL 路由守卫 + 16.5s 稳定等待）已产出全部可用证据。借鉴项目的方法论（列表页=卡片流 → 按卡拆分）以 Pattern 4/5 落地
- Phase A/B 拆分门禁：括号卡片（猎聘/iguopin）要求 **≥2 张** 才拆分（单张 title+城市块与普通 JD 页不可区分，保持原路径防回归）

## Errors Encountered
- pytest-cov 未安装，`--cov-report` 参数不可用；canonical 门禁命令为 `coverage run --source=backend -m pytest tests/unit/` + `coverage report --fail-under=100`（7695 stmts 100% branch，1205 passed）
- iguopin `POST /api/jobs/v3/list` 裸请求一律 nginx 405（含 Accept/X-Requested-With/Referer 变体）——API 有会话/指纹校验，判定为反爬边界，不碰

## Status
**Phase A+B 完成** - 1205 tests passed（+10）、backend 100% branch、ruff clean
**Phase C 完成** - 11 个 iguopin 种子全部换为 keyword 搜索页（Java/前端/AI/产品经理），
  预检脚本 tests/manual/iguopin_seed_precheck.py（复用 eval 同款管线）全量 --render PASS：
  Java 18 卡 / 前端 20 卡 / AI 算法 19 卡 / 产品经理 13 卡；eval_runner import 干净，
  compare_runner 单测 31 passed，ruff clean
**Phase D 完成** - search-public-job-pages 加 site: 限定（10 个招聘域名 OR 操作符，已有 site: 时不覆盖）
  + 招聘域名白名单两档过滤（白名单域名保留宽松检查；未知域名必须有 job 形态 URL 路径，
  拒绝教程/百科纯文本命中）—— B4 噪声收敛；56 tests passed、ruff clean
**Phase E 进行中** - 全量回归
- unit 门禁已全过：1210 passed（基线 1195，+15）、100% branch（7712 stmts / 2048 branches，0 miss）、ruff backend tests scripts clean
- 83 题全量 eval 已后台启动（2026-08-08）：`--out-dir tests/question/eval_results/phase_e_round_1`，串行、真实 DeepSeek + Playwright 渲染回退，预计数小时
- 对比工具：tests/question/eval_results/compare_rounds_full.py（覆盖 Q/C/R 全 id + 链链接级对比；compare_rounds.py 只 glob Q*.json）
- 早期信号：C001-L1 猎聘列表页经 Pattern 4 拆出 13 张岗位卡（title/company/city/salary/exp/degree 全字段）
- **第一轮 eval 结果**：单题 17→23（+6）、链接 26→31（+5）、0 failed；15 题提升
- **发现系统性回归（3 题）**：Q143/R032/R033（稀土掘金社区招聘帖）succeeded→waiting_user。
  根因 = Phase D 白名单与 site: 操作符不含 juejin.cn（so.com 回退返回的 juejin 招聘 pin 全被过滤 → 搜索 0 结果）
- **修复**：juejin.cn 加入 _JOB_SEARCH_ALLOWED_HOST_PATTERNS + _JOB_SEARCH_SITE_OPERATORS；
  新增回归单测 test_search_keeps_juejin_pins_and_drops_non_job_posts（pin 文本信号保留、无招聘词 post 丢弃）
  + site: 测试断言 site:juejin.cn；实时复测 7 条 juejin 招聘结果；1211 passed（+1）、100% branch、ruff clean
- **C005 澄清**：两轮 doc status 均 waiting_user（无 doc 级回归）；v2=L1 succeeded+L2 waiting_user，
  新轮=L1 waiting_user 链终止。L1 失败机制（微信/mokahr JS 渲染失败 + verifier 重试标准）与 Phase A-D 无关，
  判断为 LLM 轨迹方差，已加入重跑验证（phase_e_retry_juejin 轮次）
- **重跑验证完成**：Q143/R032/R033 修复后全 succeeded（抓取 juejin.cn/pin/ 招聘帖，Q143 首条与 v2 相同 pin）
- **C005 定论**：3 次重跑全部 waiting_user（确定性失败，非方差）；v2 的 L1 succeeded 属 LLM 方差。
  三次均未调用 search 工具（只用 query-career-sheet-records + fetch），与 Phase A-D 无因果；
  根因 = 微信/mokahr 渲染不可靠 + verifier 严格标准（公司真实岗位+投递链接）。doc 级两轮一致 waiting_user。
  C005 属"公司官网微信文章"类，不在 P0/P1/P2 覆盖范围，记为遗留项

## 最终结果（Phase E 完成，2026-08-08）
- **单题：17 → 26 succeeded（+9）**，waiting_user 51 → 42
- **链：7 → 13 succeeded（+6）**，waiting_user 8 → 2
- **链接：26 → 31 succeeded（+5）**，waiting_user 8 → 2
- **0 failed**（83/83 JSON 可解析）
- **doc 级 0 回归**（worsened docs: 0；Q143/R032/R033 修复后回到 succeeded）
- 15 题提升：C001/C002/C003/C004/C010/C015/Q046/Q114/Q134/Q148/R024/R028/R034/R043/R045
- tests/unit：**1211 passed**（基线 1195，+16）、**100% branch**（7712 stmts / 2048 branches，0 miss）、ruff clean
- 轮次：phase_e_round_1（83 题全量）→ phase_e_retry_juejin（Q143/R032/R033/C005）→ phase_e_retry_c005（C005）→ phase_e_merged（最终对比目录）
- 对比工具：tests/question/eval_results/compare_rounds_full.py（覆盖 Q/C/R 全 id + 链链接级）

## 基线（v2，不可回归）
- 83/83 JSON 可解析、0 failed
- 单题：17 succeeded / 51 waiting_user
- 链接：26 succeeded / 8 waiting_user（34 链接）
- tests/unit：1195 全绿、branch coverage 100%、ruff 通过

## 2026-08-12 四轮闭环续跑

### 方案融合
- [x] 5 个子 agent 分别提出不同方案：运行时 DAG、canonical Skill 迁移、证据/来源路由、PEV policy compiler、可观测性闭环。
- [x] 独立评估 agent 完成融合：先做 Artifact Port/工作流依赖，再做来源质量路由，再逐步编译 Skill 契约，最后低并发评测。

### 第 1 轮：跨步骤 Artifact Port 与依赖门控
- [x] `StepInputRef.artifact_type` 与输出类型标签接入运行时。
- [x] 缺失/类型不匹配时记录 `step_dependency_gate_failed`，不再静默进入后续模型循环。
- [x] 56 个 runtime/contract/skill tests 通过，Ruff 通过。

### 第 2 轮：证据质量与链式继承
- [x] 页面增加 `jd_complete/list_only/js_shell/empty` 质量信号。
- [x] 链式评测传递 `artifact_id/content_hash/visible_text/quality`，不再要求下一环节重复抓取同一来源。
- [x] discovery/runtime 回归集合 141 tests 通过，Ruff 通过。

### 第 3 轮：Skill Artifact Port 编译
- [x] `SkillDefinition` 增加 input/output `ArtifactPort`。
- [x] career manifest 编译四个 PEV Skill 的 artifact 类型契约。
- [x] 60 个 Skill/runtime 契约测试通过，Ruff 通过。

### 第 4 轮：全量 live 评测与按阈值停止
- [x] 4 个 worker 覆盖 83 题；首次启动的参数编排错误未计入业务结果，修正后重新启动。
- [x] 3 分钟监视触发停止条件时为 47/83 完成：4 succeeded、40 waiting_user、3 failed。
- [x] 因 `waiting_user > 30` 停止全部 8 个相关进程；无残留评测进程。
- [x] 失败/等待聚类：28 个合规反爬或访问控制人工交接，8 个来源不足，2 个重复调用停滞；`sheet_rate_limited` 17 次，`duplicate_tool_call` 5 次。
- [x] 修复招聘站根首页搜索误收、长文本首页误判为 JD、结构化 JD 丢失来源质量；新增监控脚本 `scripts/monitor_question_eval.ps1`。
- [x] 修复后针对性集合 154 tests 通过，Ruff 通过。

### 当前结论
- 四轮优化和测试闭环已完成，但本轮只完成 47/83，成功数 4，未达到 65 success；不能声称达到用户设定的成功阈值。
- 合规反爬/登录/验证码仍必须人工交接，不通过绕过安全限制提升成功数。

## 用户追加要求：再做三轮 5+1 闭环

目标：上一轮只算第 1 轮。本轮起再执行 3 轮，每轮固定顺序为：

`5 个方向不同的独立方案 -> 1 个独立评估 -> 实现 -> 评测 -> 失败轨迹分析`

停止条件：单轮评测累计超过 30 个非 `succeeded` 案例（`failed` 或
`waiting_user`）立即停止，并保存该轮工具调用轨迹与原因聚类。

- [x] 第 2 轮：来源配额/降级、重复调用、artifact 选择、Planner 契约、观测分析五方向方案
- [x] 第 2 轮：独立评估、实现、测试与停止条件评测
- [x] 第 3 轮：基于第 2 轮轨迹重新提出五个不同方向方案并评估
- [x] 第 3 轮：实现、测试与停止条件评测
- [x] 第 4 轮：基于第 3 轮轨迹重新提出五个不同方向方案并评估
- [x] 第 4 轮：实现、离线验证与最终失败原因报告；在线评测按用户要求停止
# Task Plan: PEV 通用规则提示词与 Harness 收敛

## Goal
在不把 career skill 业务流程硬编码进提示词的前提下，补强 Planner/Executor/Verifier 的通用行为规则，并修复仍能由 harness 防住的失败路径。

## Phases
- [x] Phase 1: 读取架构、提示词、决策 schema 与第5轮失败证据
- [x] Phase 2: 设计通用规则边界，区分提示词职责与 harness 职责
- [x] Phase 3: 实现提示词和必要 harness 修复
- [x] Phase 4: 添加回归测试并运行定向/全量单元验证
- [x] Phase 5: 总结改动、未解决问题与下一轮 live 验证口径

## Key Questions
1. 哪些失败是模型没有遵守通用执行规则，适合通过提示词改善？
2. 哪些失败必须由 harness 强制拦截，不能依赖模型自律？
3. 如何验证提示词改动没有把 skill 业务规则重新塞回 runtime？

## Decisions Made
- 提示词只包含角色职责、状态/证据/工具调用纪律和终止规则，不写招聘网站、搜索关键词或具体 skill 流程。
- harness 继续拥有预算、权限、重复调用、证据契约、状态迁移和最终成功审计的决定权。

## Errors Encountered
- 待记录。

## Status
**Complete** - 提示词与 harness 补强完成；完整单元集、Ruff、compileall 均通过，下一步是重新进行 live A/B 评测。

# Task Plan: Executor Skill/Tool P0-P2 capability remediation

## Goal
基于 run6 证据，修复确定性完成闸门的误拒与不可诊断问题，增强公开职位来源降级和列表页详情展开，并保留反爬安全边界与现有成功契约。

## Phases
- [ ] P0：复现 Q057 gate mismatch，补充门禁诊断、证据与 artifact 一致性校验及回归测试
- [ ] P1-A：评测开启 public API adapters，补充低反爬来源路由和有限搜索降级
- [ ] P1-B：增强 list-only SPA 的公开详情路由发现，不绕过登录/验证码/反爬
- [ ] P2：补充 partial-evidence 可观测字段，不引入未经验证的 partial_success 评分状态
- [ ] Verification：定向测试、tests/unit、Ruff/compileall、串行 10 题公网回归

## Invariants
- 不绕过 Liepin 或任何站点的验证码、登录、反爬、安全页。
- `jd_complete` 仍是 job-discovery 成功证据门槛；`list_only` 不能冒充 JD。
- 仅使用工具产出和已持久化的证据；模型提出的 URL 不具备证据权威。
- P2 只增加诊断，不改变当前 RunStatus 和 eval 成功评分口径。

## Status
**Currently in P0 investigation** - 已确认 Q057 有 2 个 `jd_complete` artifact，但尚未完成 observation/gate 的运行时级复现。

# 2026-08-13 run8 收敛修复

## Goal
复测固定 10 题，达到至少 8/10 `succeeded`；若剩余非成功样例均为公开站点反爬、登录或验证码阻断，则按外部阻断交付证据。

## Current evidence
- run8 目录：`tests/question/eval_results/deep_executor_nonchain_20260813_run8_live/`
- 当前结果：10/10 `waiting_user`；7/10 `external_blocked`，Q017/Q028 为 `model_or_verifier_decision`，Q034 为 `no_progress_duplicate`。
- Q017/Q028 已有 `jd_complete` 页面和结构化详情 artifact，但终态在步骤完成门禁前被暂停。
- Q034 只有 `list_only` 页面；不能把列表页直接判为 JD 成功，需先验证公开详情路由是否可发现。

## Phases
- [ ] 修复 runtime completion gate 合并 trusted artifact refs
- [ ] 为证据门禁和 needs_user rescue 补回归测试
- [ ] 验证国聘列表页详情展开，不绕过反爬/登录
- [ ] 运行定向测试、ruff、compileall
- [ ] 串行重跑 10 题并审计 success/root cause

## Status
当前在第一阶段：修改 runtime 的 completion evidence 路径。
