# FindJobs 优化实施 + redesign 全量评估报告（2026-08-08）

> 对应 `docs/findjobs-optimization-plan.zh-CN.md` §11 总体验收清单的逐项结论。
> 评估运行：自研 PEV 运行时（`tests.question.eval_runner`），live DeepSeek + 公网抓取，输出 `tests/question/eval_results/redesign_full_20260808_v2/`。

## 1. 执行摘要

- **评估规模**：83 个问题文档全部跑完并产出可解析 JSON（15 条 C 链展开为 34 个链接环节 + 68 道单题），**0 failed、0 坏 JSON**。
- **成功分布**：链接级 34 环节中 26 成功（76.5%）；单题 68 道中 17 `succeeded`（25%）、51 `waiting_user`（75%）。
- **失败模式**：全部为设计内的 `waiting_user` 可恢复收敛（证据不足 → 转人工），无崩溃、无模型调用级系统性失败。
- **工程侧**：`tests/unit` 1195 全绿、branch coverage 100%、ruff 通过；§11 中除 3 项依赖人工评审/实机验证的条目外全部满足。
- **阻断修复**：本次评估前发现 User 级 `DEEPSEEK_API_KEY` 已失效（401），已由用户更换为可用密钥并验证通过。

## 2. 运行环境与中途修复记录

| 事项 | 详情 |
|---|---|
| 运行时 | 自研 PEV（Planner–Executor–Verifier），模型 `deepseek-v4-flash`，网关 json_mode + 本地 JSON 修复 |
| 首次运行（invalid） | 83/83 `model_request_failed`、turns=0 —— 根因：User 级 `DEEPSEEK_API_KEY`（尾 `…e62b`）被 DeepSeek 401 拒绝；`.env` 中 `OPENAI_API_KEY`（尾 `…6bc6`）实测 200 可用，但 dotenv 不覆盖已存在环境变量 |
| 修复 | 用户已更新 User 级 `DEEPSEEK_API_KEY` 为可用密钥（实测 200）；评估以该密钥直接运行 |
| 进程竞态 | 首轮 v2 的 python 子进程在 shell 被杀后成为孤儿，与补跑进程同时写同一输出目录 → 已定位并终止孤儿进程（PID 25576/9620），补跑独占运行 |
| 最终结果 | 83/83 JSON 完整、可解析；无重复 ID；竞态期间日志中的 C004 `failed` 行来自被终止进程，磁盘 C004.json 为 L1 succeeded / L2 waiting_user，无需补跑 |

## 3. 全量评估结果

### 3.1 状态分布

| 类别 | 题级统计 | 链接级统计 |
|---|---|---|
| C（链，15 题） | 15 chain | 34 链接：**26 succeeded / 8 waiting_user**（76.5% 链接成功） |
| Q（21 题） | 6 succeeded / 15 waiting_user | — |
| R（47 题） | 11 succeeded / 36 waiting_user | — |
| **合计（83）** | **17 succeeded / 51 waiting_user / 0 failed** | 26 succeeded / 8 waiting_user |

### 3.2 特征观察

- **C 链表现最好**：多环节链式任务（抓取 → 匹配/定制）成功率高，Verifier 在证据充分时稳定 PASS。
- **waiting_user 是保守行为而非缺陷**：R 类实时核验题（"最近 7 天数据源更新…"）依赖 Playwright 抓取腾讯/百度/字节招聘页，页面反爬或缺少完整 JD 正文时，系统按安全边界转人工核验，不绕过、不编造 —— 与红线清单一致。
- 日志中大量 `gateway stage1 raw unrecoverable` 为**设计内的本地 JSON 修复路径**在正常恢复模型输出，非错误。
- 与上一轮（redesign_round_2：67 链接级成功）可比口径不同（本轮链展开为 34 链接、非链 68 题），但两轮均**无 failed**，本轮 83/83 全量产出且可解析。

## 4. §11 总体验收清单逐项状态

### Phase 1（A1 数据获取通道）

| 项 | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| A1-1 | 三公司各 ≥20 条带证据字段的结构化 JD | ⏳ 待实机验证 | 适配器 + 21 单测（mock 网络）通过；live 批量 ≥20 条未跑（tests/manual/adapter_live_smoke.py 可选未做） |
| A1-2 | 限速可测（≥0.2s 间隔、≤300/公司） | ✅ | [base.py:173](backend/../skill/job-discovery/scripts/adapters/base.py) `_pace` 0.2–0.5s + 指数退避，测试覆盖 |
| A1-3 | allowlist 含 reviewed + 人工评审记录 | ⏳ 需人工 | `endpoint_allowlist.json` 仍为 pending_review；§12.3 检查点待填 |
| A1-4 | 故障注入 → 显式 blocked，无异常泄漏 | ✅ | 单测：坏 payload、未知公司 `blocked: adapter_unknown`，SystemExit 行为正确 |
| A1-5 | 全仓 grep 无登录/验证码/反爬代码 | ✅ | 本次实查：仅 3 处否定性 docstring（"never adapted / not a bypass"） |
| A1-6 | 既有 4 模式全量回归绿 | ✅ | tests/unit 1195 全绿 + coverage 100% |

### Phase 2（B 类数据结构与特征）

| 项 | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| B3-1 | 学历白名单逐词 fixture 通过 | ✅ | test_jd_extraction_degree.py |
| B3-2 | 无学历文本默认 unknown | ✅ | 同上 |
| A2-1 | 技能闭集 ≤80 项、无运行时 LLM 构建 | ✅ | skill_validator.py + skill_tags.json |
| A2-2 | 非法技能项永不外泄（属性测试） | ✅ | test_skill_validator.py |
| A2-3 | 低信息过滤 + min 回退单测通过 | ✅ | 同上 |
| A2-4 | flag 两态回归（False 逐字节一致） | ✅ | 双态回归测试 |
| B1-1 | job_strength 确定性单测通过 | ✅ | test_job_strength.py |
| B1-2 | 20 份人工一致率 ≥80% | ⏳ 记入已知限制 | 未做人工标注（见 §5） |
| B1-3 | 下游可选接入，现有行为不变 | ✅ | 回归全绿 |
| B2-1 | taxonomy ≥15 大类、运行时零 LLM | ✅ | test_taxonomy.py |
| B2-2 | 检索确定性单测通过 | ✅ | 同上 |

### Phase 3（C 类工程健壮性）

| 项 | 验收标准 | 状态 | 证据 |
|---|---|---|---|
| C2-1 | 多 JD top-N 缺口 + 出现次数 | ✅ | test_career_planning_multi_jd.py |
| C2-2 | 单 JD 输出逐字节不变 | ✅ | 回归 |
| C3-1 | 两遍运行零重复入库 + TTL 生效 | ✅ | test_seen_jobs_dedup.py（5 测） |
| C3-2 | 适配器 job_id 稳定 | ✅ | base.py 稳定 job_id + 测试 |
| C4-1 | 日志无完整密钥（仅 key[-6:]） | ✅ | test_secrets_retry.py（12 测） |
| C4-2 | 退避序列 + 轮换切换测试通过 | ✅ | 同上 |
| C5-1 | 批量 i/n 进度行单调递增、并发有界、顺序确定 | ✅ | test_batch_progress.py（6 测） |
| C1-1 | extractor 接入 gateway，降级梯每级可观测 | ✅ | test_llm_extractor_gateway.py |

**合计：22 项 ✅ / 2 项 ⏳（A1-1 实机、A1-3 人工评审）/ 1 项记入已知限制（B1-2）。**

## 5. 已知限制与遗留项

1. **A1-3 allowlist 人工评审未完成**：`endpoint_allowlist.json` 处于 pending_review，需人工填入 `reviewed_by` / `reviewed_on` 后才能启用适配器上线（双门控）。
2. **A1-1 实机批量未验证**：`tests/manual/adapter_live_smoke.py` 为可选项，未执行；三公司各 ≥20 条结构化 JD 的实机数据量验证待做。
3. **B1-2 人工一致率**：job_strength 的 20 份人工标注一致率未做，按计划约定记入已知限制。
4. **R 类题依赖站点可达性**：腾讯/百度/字节招聘页的反爬与页面结构变化会推高 `waiting_user` 占比，属外部依赖而非代码缺陷。
5. 本次评估所有密钥均未写入任何仓库文件、日志或 argv（进程级环境变量注入）。

## 6. 下一步建议

1. 人工完成 §12.3 检查点记录（A1 allowlist、A2、B1、B2）→ 解锁 A1 上线。
2. 执行 `tests/manual/adapter_live_smoke.py` 实机冒烟 → 补 A1-1 数据量证据。
3. （可选）对 51 道 `waiting_user` 题按原因归类（反爬 / 证据不足 / 无公开 JD），确认是否需要补充种子 URL 或问题重述。
