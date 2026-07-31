# 职位发现旧版架构汇总（已过时 · 存档）

> ⚠ **本文档为历史存档**。当前默认运行时为 **Skill Discovery Runtime**，
> 权威描述见：
> - `backend/app/services/job_discovery/ARCHITECTURE.zh-CN.md`（agent 架构与 URL→JDs 全过程）
> - `docs/superpowers/plans/2026-07-29-skill-runtime-job-discovery.md`
> - `docs/superpowers/plans/2026-07-29-skill-runtime-hardening.md`
>
> 本文把已被取代的旧架构（Discovery Supervisor / Strategy Router / PEV / Generic Supervisor）
> 汇总于一处，便于回溯设计演进。原始 spec/plan 已从仓库删除，全文可经 git 历史恢复
> （见文末「已删除文档清单」）。

---

## 0. 当前默认：Skill Discovery Runtime（2026-07-29）

`JOB_DISCOVERY_SKILL_RUNTIME_ENABLED=true`（`backend/app/config.py` 默认）时，
`worker.py` 在任何 URL 策略匹配、Adapter 或旧 Supervisor **之前**调用 Skill runtime：

- `create_deep_agent` + 作业发现 Skill + 受限 `run_skill_script` 工具 + 每个证据页的
  `jd_extractor` 子 Agent。
- 受限脚本白名单：`browse / validate / normalize / deduplicate / ocr_image / state /
  read_evidence / write_candidates / coverage_gate`。
- `SkillToolPolicy` 预算：`max_browse_calls=2`、`max_coverage_gate_calls=1`、
  `max_pages=20`、`max_candidates=10`。
- 每任务隔离工件目录 `JOB_DISCOVERY_SKILL_ARTIFACT_ROOT/<task_id>/skill/job-discovery/`。
- 持久化：`result_summary_json`（`execution_path=skill_agent`，不存原始模型消息）、
  `discovered_job_candidates`、`job_discovery_evidence`（`storage_uri` 指向隔离工件）、
  `job_discovery_trajectories`（经安全截断的工具轨迹）。
- 旧路径（Strategy Router / PATH A/B/C / Supervisor）仅在显式关闭该开关时作为回滚代码保留，
  **不参与默认任务**。

> 注意：Skill runtime 使用**自己的浏览/覆盖计数器**（`SkillToolPolicy`）作为完成门，
> 而非旧 PEV 的 `verify_coverage`。

**发现送达方式**：候选不再经管理员审核晋升 `JobPosting(verified)`，而是经
**个性化发现 v1**（预审核、owner-scoped 推荐直达用户，卡片标注「自动发现，建议自行确认」）。
verified-only 的 `/api/jobs` 职位中心由 WP2 手动导入/补全流程喂养，与发现候选解耦。
详见 `docs/superpowers/specs/2026-07-25-personalized-job-discovery-v1-design.md`。

---

## 1. 演进时间线

| 阶段 | 日期 | 默认架构 | 完成权威 |
|---|---|---|---|
| ① 单 Supervisor | 07-18 | Discovery Supervisor（deepagents，LLM-in-loop） | 无覆盖证明 |
| ② Strategy Router | 07-20 | Router + PATH A/B/C | adapter/plan 命中即完成 |
| ③ PEV | 07-21 / 07-22 | Planner-Executor-Verifier | `CoverageVerifier` |
| ④ complete-crawl 重构 | 07-22 | PEV + 完整抓取 adapter | `verify_coverage` |
| ⑤ LLM JD 抽取端口 | 07-25 | PATH C behind flag + skill v1.6 | 同上 |
| ⑥ Generic Supervisor → Skill | 07-28 → 07-29 | Skill Discovery Runtime | `SkillToolPolicy` |

---

## 2. ① Discovery Supervisor（07-18，Legacy PATH C）

- **构建**：`deepagents.create_deep_agent` 编译的 LangGraph 图。
- **角色**：LLM-in-the-loop，plan → triage → 委派 → 收尾；自主选择工具，循环到产出结果。
- **工具**：`triage_link`、`run_web_navigation`、`parse_wechat_article`、`run_ocr`、
  `extract_jd_candidates`、`standardize_from_record_fields`、`verify_evidence`、
  `package_candidates`、`finish_with_manual_review`。
- **Web Navigation Agent**：嵌套在 `run_web_navigation` 工具内部的子 Agent（非 deepagents
  `subagents=[...]` 委派），7 工具：`open_url`、`open_rendered_url`、
  `extract_rendered_job_evidence`、`read_dom`、`extract_links`、`click_link`、`go_back`。
- **ReadGZH**：`open_url()` 自动把 `mp.weixin.qq.com` 经 ReadGZH 代理，回退链
  ReadGZH → direct HTTP → Playwright → error。
- **递归上限**：Supervisor 50（无 snapshot）/ 30（带 snapshot）；Web Nav 30。
- **缺陷**：无覆盖证明；非收敛降级（`GraphRecursionError` 时保留 partial state，
  从工具输出恢复已抓候选，仍判 `succeeded`）；提示级收敛（3 步）无程序化工具调用计数器。

---

## 3. ② Strategy Router + 三路径（07-20）

`StrategyRouter` 按 `url_pattern`（fnmatch）匹配活跃 `JobDiscoveryStrategy`：

| 路径 | 模块 | 含义 |
|---|---|---|
| **PATH A** | `DomainAdapter` | 认证站点驱动 / 站点适配器（Moka、飞书、汇川、小红书、阿里 SPA） |
| **PATH B** | `SnapshotExecutor` | 确定性执行器回放 `SnapshotPlan` + `CrawlPlan` |
| **PATH C** | Planner | `CrawlPlan` 生成 / 修复 Agent |
| **Legacy PATH C** | Supervisor | 兜底（PEV 关 / planner 不可用 / adapter 失败） |

命中 adapter 或 plan 即视为完成；未命中或失败回退 Supervisor。

---

## 4. ③ PEV：Planner-Executor-Verifier（07-21 / 07-22）

- **CoverageVerifier 是唯一的完成权威**：只有 `verify_coverage` 给出正向终止证据
  （`completion_evidence`）才算完成。Legacy Supervisor 无覆盖率，始终 `coverage-unverified`。
- **PEV PASS 定义**（全部成立）：
  ```
  coverage_verified   = true
  coverage_complete   = true
  failed_detail_count = 0
  candidate_count     == unique_listing_count   # canonical 多地区合并单列，不计 dup
  count_apply_url_is_listpage = 0
  body 覆盖率 = 100%   # 合法鉴权墙除外
  ```
- **结果隔离**：worker summary 写 `execution_path`
  （`path_a_adapter` / `path_b_crawl_plan` / `legacy_path_c`）、`coverage_verified`、
  `coverage`、`legacy_fallback_reason`。Legacy 结果单列，**不计入 PEV pass rate**。
- **全局 invariant 不变**：`enforce_result_invariants` 仍只把 `succeeded`（无候选）转
  `failed`、`partial_success`（无候选）转 `needs_manual_review`；PEV 完整性由
  `run_post_crawl_pipeline → verify_coverage` 强制，不靠全局 invariant。

### 灰度发布（PEV gray migration）

- 四站点 adapter 在 `scripts/seed_strategies.py` 以 `enabled=False` 发布。
- 按 `GRAY_ROLLOUT_ORDER` 逐站启用：Moka → 飞书 → 汇川 → 小红书，
  每次只切该站 `JobDiscoveryStrategy.enabled=True`，需 **3 次连续 coverage-verified live smoke**。
- 回滚触发（`GRAY_ROLLBACK_TRIGGERS`）：计数漂移 / 正向终止字段消失 /
  `failed_detail_count>0` / `count_apply_url_is_listpage>0` / 新 blocked marker /
  三次计数不一致。回滚只禁该站策略，不删契约、不改全局 invariant、不影响已稳定站点。

---

## 5. ④ complete-crawl 重构（07-22）与 ⑤ LLM JD 抽取端口（07-25）

- **complete-crawl**：PEV + 完整抓取 adapter，`extract_rendered_job_evidence`
  （Playwright + 分页 + 详情下钻）承担重抓取（xiaomi 16 页 / 151 岗位即靠它）。
- **LLM JD 抽取端口**：把 LLM JD 抽取移植进 PATH C，behind flag
  `job_discovery_llm_extraction_enabled`，与确定性抽取做 strict-Pareto union（v2 merge）。
  10-URL eval（07-25）：bytedance 201/201 匹配 A，feishu-xiaopeng 85%，deeproute 超 A；
  v2 body 不变 → 差距在 CRAWL 不在 EXTRACTION。
- **skill v1.6 并行分页**：per-page `jd_extractor` 子 Agent + verify-retry +
  确定性 deduplicate filter + 并行 fetch + load-more 盲点修复；
  xiaomi 15→165clean/0garb→v1.6 151/151real/0drift/215s（-74%）；deeproute 5→25/21。

---

## 6. ⑥ Generic Supervisor → Skill 迁移（07-28 → 07-29）

- **Generic Supervisor**（07-28）：把站点特异性收敛进通用 Supervisor + skill 机制。
- **Skill 迁移**（07-29）：用 Skill Discovery Runtime 取代旧 Supervisor/Strategy/PEV 默认路径：
  - `create_deep_agent` + 作业发现 Skill + 受限 `run_skill_script` 工具 + 每页 `jd_extractor` 子 Agent。
  - retry-safe、有界、覆盖可验证、独立于遗留 Supervisor。
  - 系统提示明确：「不要绕过登录/验证码/反爬；不要使用 URL 适配器或策略匹配。」

---

## 7. 沿用的不变量（跨所有版本）

### 幂等
- **任务键**：`SHA-256(source_id + external_record_id + url_hash + payload_hash + agent_version)`。
- **候选键**：`SHA-256(company + title + location + apply_url + evidence_hash)`。
- **canonical 去重**：full-JD 身份键**含** location（同岗不同城 = 独立 listing）；
  title-only 键**不含** company/location（同岗多城 = 一个岗位，合并）；
  `canonical_job_key` 排除 location（相同 JD 跨城合并）。

### 安全硬门
- 从不绕过登录 / 验证码 / 反爬 / 鉴权墙 → `needs_manual_review`。
- 从不自动点提交；最终提交始终人工控制（GUI Agent 止于 `READY_FOR_REVIEW`）。
- 学生 API（`/api/jobs`、`/jobs/{id}`）仅返回 `verified`；其它状态不泄漏。
- 从不在仓库 / 日志 / argv 写机密、token、原始 payload。
- MySQL 为业务状态唯一权威；Redis 仅 checkpoint/缓存。
- 任务动作需 task lease + scope 校验，不信裸 device token。
- JobPosting 完成/审核写校验 `review_version`（乐观锁，并发冲突 409）。

### 后处理（确定性，无 LLM）
`extract_jd_candidates` → `_is_plausible_job_title`（3 规则：结构分隔符 / 裸泛化后缀 / 侧栏 tab）
→ `verify_evidence`（冻结）→ `deduplicate_candidates`（canonical）→ 跨类型子串归并
→ `package_candidates`（幂等 / 相似度键）。

---

## 8. 个性化发现 v1（当前发现送达方式，2026-07-25）

取代发现候选的「管理员审核 → JobPosting(verified)」终点：

- 把已完成的共享 `JobDiscoveryTask` 中「证据核验 + 覆盖完整 + URL 安全 + 去重 + 相关性达标」
  的候选，以 **owner-scoped 推荐**直达单个用户，**跳过管理员审核**（预审核通道）。
- 卡片固定标注「自动发现，建议自行确认」。
- **独立于** verified-only 的 `/api/jobs` 路径；绝不修改 `JobPosting`、`JobRelevanceScore`、
  `review_version`，绝不把预审核候选写入 `/jobs`。
- 完整性证明（任一）：`coverage_verified` 或注册的 `single_source_complete` 契约。
- 候选级三道门：JD body 非空 + 证据存在 + apply URL 安全校验。
- 初始覆盖：仅 Moka / Feishu / Inovance / Xiaohongshu 四个已迁移完整抓取 adapter；
  WeChat / PDD / SnapshotExecutor / Alibaba SPA / legacy / PATH C 结果只产出 owner-scoped 状态（不推荐）。
- 每用户每日 5 次 run（超限 429）；`SourceStatusReason` 闭合枚举，不存原始 wall 文本。
- 删数据顺序：先 `personalized_discovery_recommendations`，再 `discovered_job_candidates`，
  再 `job_discovery_tasks`（RESTRICT）。

详见 `docs/superpowers/specs/2026-07-25-personalized-job-discovery-v1-design.md`
与 `docs/superpowers/plans/2026-07-25-personalized-job-discovery-v1.md`。

---

## 9. 已删除文档清单（git 历史可恢复）

以下描述旧架构的 spec/plan 已从仓库删除，内容已汇总于本文：

**specs/**
- `2026-07-18-job-discovery-agent-design.md`
- `2026-07-20-discovery-supervisor-agent-architecture.md`
- `2026-07-20-discovery-strategy-router-design.md`
- `2026-07-21-supervisor-site-wide-jd-discovery-design.md`
- `2026-07-22-job-discovery-complete-crawl-refactor.md`
- `2026-07-28-generic-supervisor-skill-migration-design.md`

**plans/**
- `2026-07-18-job-discovery-agent.md`
- `2026-07-20-discovery-strategy-router-implementation.md`
- `2026-07-21-supervisor-site-wide-jd-discovery.md`
- `2026-07-22-supervisor-structured-convergence.md`
- `2026-07-28-generic-supervisor-skill-migration.md`

> `2026-07-25-llm-jd-extractor-port.md`（旧 PATH C 抽取 lineage）保留未删，可按需另行处置。
