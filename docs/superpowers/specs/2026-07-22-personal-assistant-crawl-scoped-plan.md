# 个人求职助手 · 全量抓取与筛选（scoped）实施计划

> 本文件是对 `2026-07-22-job-discovery-complete-crawl-refactor.md` 的**范围裁剪 + 修正版**，针对当前 active `/goal`（用 6 个 URL + 用户简历产出匹配岗位）。它不是替代，而是把那份大重构里**与当前目标相关、且不冲突**的部分挑出来，并落实用户确认的 5 条修正。
>
> **与 complete-crawl-refactor 的关系**：那份文档的架构方向（确定性全量抓取 + CoverageVerifier + 三阶段去重 + 硬门槛 + JD 主体去重）被采纳为长期目标；本文件**推迟**其中与当前 6 URL 无关的通用框架（CrawlPlan schema、6 类分页状态机、PATH C Planning Agent、checkpoint 续跑），并**修正**其中与现有约束/已有组件冲突的 5 处。

**Goal：** 在不破坏现有 PATH A/B/C 兜底、不动冻结工具、不重复造 ranker 的前提下，搭出"确定性全量抓取 -> 硬门槛 -> JD 主体去重 -> 复用已有 LLM RelevanceRanker 打分 -> 全量返回（不截断）"的 V2 流水线骨架，并用真实 URL 基线测试量化当前 supervisor 路径与真实岗位数的差距。

**Tech Stack：** Python 3.12、Pydantic/dataclass、Playwright、LangChain/DeepAgent、DeepSeek LLM、MySQL、pytest、SHA-256、标准库文本规范化。

---

## 1. 背景与实测基线

当前 supervisor（PATH C）路径在 3 个职业站上的实测（DOM ground truth 对照）：

| 公司 | 正确 URL | 真实岗位数 | 页数 | supervisor 实测 | 病根 |
|---|---|---|---|---|---|
| 元戎启行 | `https://app.mokahr.com/campus-recruitment/deeproute/145894#/home` | 21 | 1 | 重复+漏（5 个×2） | 不进 `#/jobs/` 列表 + 重复 |
| 小米 | `https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY` | 151 | 16 | ~21（`_MAX_LIST_PAGES=5` 截断） | 不翻完 16 页 |
| 拼多多 | `https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x` | 22 | 3 | 47（>22，重复+幻觉） | 重复 + 幻觉编岗位 + apply_url 全错 |

> 注：记忆中"元戎 68 / 拼多多 47、问题已 CLOSED"的旧结论是错的——只看候选数自报成功，未与 ground truth 对照。本计划 Phase 6 用真实数重新建立可信基线。

四个病根的修复归属：

| 病根 | V2 机制 | 阶段 |
|---|---|---|
| 不翻页 | `CrawlExecutor` + 分页迭代器 + `require_all_pages` + 页面指纹防环 | Phase 2 框架 / 站点 adapter 推迟 |
| 重复 | source/detail/canonical 三阶段去重 + `canonical_job_key`（不含地点/URL） | Phase 2 + Phase 4 |
| 幻觉编岗位 | 岗位由确定性 DOM/API 产出；LLM 不产岗位 | 站点 adapter 推迟 |
| apply_url 全错 | 每 listing 带 `detail_url`/`apply_urls` + 跟踪参数归一化 | Phase 2 + 站点 adapter 推迟 |

---

## 2. 架构（灰度切换，保留旧 PATH C）

```text
DiscoveryTaskInput
    ↓
StrategyRouter.match(url)
    ├─ 命中 CompleteCrawlAdapter（本轮仅 mokahr/mioffice/pdd，推迟实现）
    │     -> adapter.execute_crawl() -> CrawlExecutionResult
    │     -> run_post_crawl_pipeline(task, crawl_result) -> DiscoveryRunResult
    │
    ├─ 命中 legacy Adapter（如 Alibaba）   -> adapter.execute()            （不变）
    ├─ 命中 SnapshotPlan（如 微信）       -> SnapshotExecutor            （不变）
    └─ 无匹配 / 失败接管                   -> PATH C Supervisor           （不变，仍直接产出岗位）

run_post_crawl_pipeline:
    verify_coverage  ── 不完整 ──> partial_success / needs_manual_review（保存中间数据，不伪装成功）
        │ 完整
        ↓
    per-detail: classify_recruitment_scope + normalize_jd + evaluate_education + evaluate_eligibility
        ↓
    deduplicate_canonical_jobs（仅 normalized_company + exact core_hash 自动合并；相似仅打标）
        ↓
    PASS 岗位 ──> 复用现有 LLM RelevanceRanker 打分
        ↓
    filter: eligibility PASS AND score >= minimum_match_score  ──> 全量返回，按分数降序（不截断）
        ↓
    DiscoveryRunResult(succeeded, coverage, counts, candidates, review_candidates)
```

---

## 3. 决策记录（5 条修正 + 2 条既有约束）

### D1 — 不删除现有 PATH C 主路径（灰度切换）
本轮推迟通用 `CrawlPlanExecutor` 与 PATH C 规划化改造，因此**不能**删除旧 PATH C，否则未知网站、未迁移站点、Adapter 失效场景失去兜底。

```text
mokahr / mioffice / pdd  -> CompleteCrawlAdapter V2（站点实现推迟）
其他已有网站              -> 继续沿用 legacy PATH A/B/C
```

待通用 CrawlPlan 与新 PATH C Planning Agent 完成后，再废弃"PATH C 直接产出岗位"的旧模式。当前 PATH C 仍承担未知站点与失败接管。**`result_contract.enforce_result_invariants` 不做全局改造**（否则 PATH C 无 `coverage` 会被打成 needs_manual_review）；coverage 不变量只在新的 `run_post_crawl_pipeline` 内部强制。

### D2 — 不保留任何 Top-N 截断
```text
硬条件通过 AND score >= minimum_match_score -> 全部返回，仅按分数降序排列
```
需审计 `RelevanceRanker`、`MatchService` 及调用方是否存在 `top_k` / `limit` / `max_candidates` / `[:N]`，V2 流程中一律不得截断。

### D3 — 第一版不采用 Jaccard ≥ 0.94 自动合并
仅自动合并 `normalized_company + exact core_hash` 相同者（`core_hash` 基于规范化后的岗位职责 + 任职要求，不含地点/岗位编号/发布时间）。相似度 ≥ 0.94 的岗位只标记 `duplicate_review_group`，**暂不自动合并**（阈值未经真实标注集验证，误合并会直接丢失真实岗位，风险高于保留少量重复）。

### D4 — 不破坏性修改 `DomainAdapter.execute()` 返回类型
保留 `execute(...) -> DiscoveryRunResult`（Alibaba、路由、测试、失败接管均依赖）。新增版本化接口：

```python
class CompleteCrawlAdapter(DomainAdapter):
    def execute_crawl(self, task, strategy, trajectory) -> CrawlExecutionResult: ...
```

路由按 `isinstance` 分发：

```python
if isinstance(adapter, CompleteCrawlAdapter):
    crawl_result = adapter.execute_crawl(...)
    return run_post_crawl_pipeline(task, crawl_result)
return adapter.execute(...)
```

等所有 Adapter 完成迁移后，再统一基类接口。

### D5 — 禾赛不静默 skip 后仍宣称 6 URL
本轮验收范围明确为 **5 URL**（3 职业站 + 2 微信）。禾赛二选一：返回 `needs_manual_review`（说明无法验证完整性），或本轮验收范围不含禾赛并显式声明。**禁止**内部跳过禾赛却把任务标记为"6 URL 已完成"。

### D6（既有约束）— 不动冻结工具
`tools/jd_extraction.py`、`tools/evidence_verifier.py` 本轮**不得修改**。"招聘范围判定"与"JD 规范化"作为**新纯模块**消费现有工具输出；3 个职业站走确定性 adapter、不经过 `jd_extraction` 的多岗位切分主路径（自然绕开"2 段上限"）。

### D7（既有约束）— 复用已有 RelevanceRanker，不建确定性 scorer
已有 `backend/app/services/relevance/relevance_ranker.py`（LLM 批量打分 0-100，UPSTREAM of MatchService，已 smoke-tested）。本轮**不建** complete-crawl-refactor 的 `matching/scorer.py`；只新建它缺失的**确定性硬门槛**（招聘类型/届别/学历），在 ranker 之前跑。

---

## 4. Global Constraints

- 单任务单一 `recruitment_type`：`campus`（默认）/`internship`/`social`；`campus`/`internship` 用 `graduation_year`（默认 2027），`social` 强制归一化为 `None`。
- 必须先完整抓取目标招聘范围内全部岗位，再筛选；**不使用 Top-K 截断**（D2）。
- 招聘类型、届别、学历为硬门槛，不允许被技术方向/关键词/专业/地点分数补偿。
- 学历通过规则：用户学历等级 ≥ 岗位最低学历等级；硕士可投本科/硕士要求岗位，不可投博士最低要求。"博士优先"是偏好不是硬要求。
- 专业、地点不作硬淘汰；不匹配只扣分。
- 列表记录、详情资源、业务岗位三阶段去重相互独立。
- 只有 `coverage_complete=True` 且所有唯一详情处理成功，任务才可 `succeeded`。
- 完整抓取失败保存中间数据与续跑游标，状态 `partial_success`/`needs_manual_review`，不得伪装为完整结果。
- 完整抓取后匹配数为 0 仍可 `succeeded`。
- 所有判定/过滤/合并/评分结论携带 `evidence_refs`。
- 不绕过登录/验证码/滑块/扫码/反爬；命中安全门禁进人工复核。
- 不向仓库/日志/参数写机密；不以 Redis 为权威；学生 API 仅返回 `verified` 岗位。
- 每任务 TDD：先写失败测试，再最小实现，再回归。
- 每任务独立提交，Conventional Commits。

---

## 5. 文件结构变更

### 新建
```text
job_discovery/
├── crawling/
│   ├── __init__.py
│   ├── coverage.py                    # verify_coverage -> CoverageDecision
│   ├── crawl_executor.py              # CrawlExecutor + CrawlExecutionResult + 去重键
│   └── pagination.py                  # page_fingerprint + 所需分页迭代器（推迟全 6 类）
├── scope/
│   ├── __init__.py
│   ├── recruitment_classifier.py      # campus/internship/social + 届别
│   └── education_gate.py             # 学历解析 + 硬门槛
├── normalization/
│   ├── __init__.py
│   ├── jd_parser.py                  # 职责/要求段解析
│   └── jd_normalizer.py              # 去元数据 + core_hash
├── deduplication/
│   ├── __init__.py
│   └── canonical_job_deduplicator.py # exact core_hash 合并 + 相似打标（D3）
├── matching/
│   ├── __init__.py
│   └── eligibility.py                # 招聘类型/届别/学历硬门槛聚合
├── adapters/
│   └── complete_crawl_base.py        # CompleteCrawlAdapter(DomainAdapter) + execute_crawl
└── post_crawl_pipeline.py            # run_post_crawl_pipeline
```

### 修改（仅允许清单内 + 新模块）
```text
job_discovery/schemas.py              # 扩契约（D6 允许：非冻结工具）
job_discovery/deepagents_runner.py    # 灰度 isinstance 分发（D1/D4）
job_discovery/worker.py               # partial/needs_manual_review 落库
# 注意：result_contract.py 不做全局不变量改造（D1，保 PATH C）
# 注意：tools/jd_extraction.py、tools/evidence_verifier.py 不得修改（D6）
# 注意：adapters/base.py、alibaba_spa.py 不破坏 execute() 签名（D4）
```

### 新建测试
```text
tests/unit/job_discovery/test_crawl_schemas.py
tests/unit/job_discovery/test_coverage.py
tests/unit/job_discovery/test_crawl_executor.py
tests/unit/job_discovery/test_complete_crawl_base.py
tests/unit/job_discovery/test_recruitment_classifier.py
tests/unit/job_discovery/test_education_gate.py
tests/unit/job_discovery/test_eligibility.py
tests/unit/job_discovery/test_jd_normalizer.py
tests/unit/job_discovery/test_canonical_job_deduplicator.py
tests/unit/job_discovery/test_path_routing_v2.py
tests/integration/job_discovery/test_complete_crawl_pipeline.py
tests/integration/job_discovery/test_supervisor_baseline_real_urls.py   # Phase 6
```

---

## 6. 实施阶段

### Phase 0 · Ground truth 与范围（无生产代码）

- [ ] 落 `tests/manual/_ground_truth.json`：

```json
{
  "元戎启行": {"url": "https://app.mokahr.com/campus-recruitment/deeproute/145894#/home", "real_count": 21, "pages": 1, "pagination_source": "single_page_mokahr_hash_jobs"},
  "小米":     {"url": "https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY", "real_count": 151, "pages": 16, "pagination_source": "toptalent_position_list_api_total"},
  "拼多多":   {"url": "https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x", "real_count": 22, "pages": 3, "pagination_source": "page_count_text_共N个岗位"},
  "柏楚电子": {"url": "https://mp.weixin.qq.com/s/F_ehY3q8Zi3-QV-AwoOF5g", "type": "wechat"},
  "华金证券": {"url": "https://mp.weixin.qq.com/s/rjuqB1qQnl9sy5qX9-Xs3w", "type": "wechat"},
  "禾赛科技": {"url": "https://kwh0jtf778.jobs.feishu.cn/229043/m/", "scope": "needs_manual_review", "reason": "飞书 m 页渲染飘 + 抽取器出 0，冻结工具不可改，无法验证完整性"}
}
```
- [ ] 用 DOM 探针复核 3 个职业站的分页来源与每岗位 `detail_url`/`apply_url` 口径（确认 `expected_listing_count` 来源）。
- [ ] 显式声明本轮验收范围 = 5 URL（D5）。

---

### Phase 1 · 契约 + 覆盖验证（不动冻结工具，不改全局不变量）

**Files:** `schemas.py`、`crawling/coverage.py`；Test: `test_crawl_schemas.py`、`test_coverage.py`

- [ ] **T1.1** 扩 `schemas.py`：新增 `RecruitmentScope`（campus 默认 2027；social 强制 graduation_year=None）、`PaginationType`、`CrawlCoverage`、`RawJobListing`、`RawJobDetail`、`RecruitmentScopeDecision`、`NormalizedJD`、`CanonicalJob`；扩 `DiscoveryTaskInput`（+recruitment_scope/preferences/user_education/minimum_match_score）、`DiscoveryRunResult`（+coverage/计数/阶段标志/review_candidates）。字段定义沿用 complete-crawl-refactor Task 1。
- [ ] **T1.2** 新建 `crawling/coverage.py`：`verify_coverage(coverage)->CoverageDecision`（failed_detail>0 / 页数不符 / 列表数不足 / UNKNOWN / 缺正向终止证据 -> 不完整；否则 succeeded）。实现同 complete-crawl-refactor Task 2 Step 4。
- [ ] **T1.3（关键修正）**：**不修改 `result_contract.enforce_result_invariants` 的全局行为**（保 PATH C，D1）。coverage 不变量仅在 Phase 5 的 `run_post_crawl_pipeline` 内部强制。
- [ ] TDD：`test_crawl_schemas.py`（默认值/social 归一化/internship 需年份/resume_cursor 序列化）、`test_coverage.py`（有岗位但分页不全不得 complete、零匹配可 succeeded）。
- [ ] 回归：`pytest tests/unit/ -k "job_discovery or result_contract"`。

```powershell
git add backend/app/services/job_discovery/schemas.py `
  backend/app/services/job_discovery/crawling/coverage.py `
  tests/unit/job_discovery/test_crawl_schemas.py `
  tests/unit/job_discovery/test_coverage.py
git commit -m "feat(job-discovery): add crawl domain contracts and coverage verifier"
```

---

### Phase 2 · CompleteCrawlAdapter V2 框架（⚠️ 本阶段不实现 3 个站点 adapter）

> **本阶段约束（用户明确）**：不生成 mokahr/mioffice/pdd 的确定性 adapter。用户要继续用这 3 个 URL 测 supervisor 路径（见 Phase 6）。本阶段只搭 V2 框架与灰度分发，全部用 `FakeDriver` TDD，不触碰真实站点。

**Files:** `adapters/complete_crawl_base.py`、`crawling/crawl_executor.py`、`crawling/pagination.py`、`deepagents_runner.py`；Test: `test_complete_crawl_base.py`、`test_crawl_executor.py`、`test_path_routing_v2.py`

- [ ] **T2.1** 新建 `adapters/complete_crawl_base.py`（版本化接口，D4）：

```python
from abc import abstractmethod
from backend.app.services.job_discovery.adapters.base import DomainAdapter
from backend.app.services.job_discovery.crawling.crawl_executor import CrawlExecutionResult
from backend.app.services.job_discovery.crawling.coverage import verify_coverage


class IncompleteCrawlError(RuntimeError):
    pass


class CompleteCrawlAdapter(DomainAdapter):
    """V2 adapter: produces a provably-complete crawl, not a final job set."""

    @abstractmethod
    def execute_crawl(self, task, strategy, trajectory) -> CrawlExecutionResult:
        raise NotImplementedError

    # 保留旧 execute() 以满足 DomainAdapter ABC；V2 路径不调用它。
    def execute(self, task, strategy, trajectory):
        raise NotImplementedError("CompleteCrawlAdapter uses execute_crawl(); route via isinstance dispatch")

    def validate(self, crawl_result: CrawlExecutionResult) -> None:
        if crawl_result.coverage is None:
            raise ValueError("adapter result missing coverage")
        decision = verify_coverage(crawl_result.coverage)
        if not decision.complete:
            raise IncompleteCrawlError(decision.reason)
```

- [ ] **T2.2** 新建 `crawling/crawl_executor.py`：`CrawlExecutionResult`（raw_listings/raw_details/coverage/checkpoint/error）、`CrawlExecutor`（driver 抽象 `extract_listings`/`fetch_detail`/`expected_page_count`/`expected_listing_count`）、`normalize_detail_url`（剥 utm/source/channel/ref/timestamp/session）、`make_source_record_key`、`make_detail_resource_key`。实现同 complete-crawl-refactor Task 5 Step 5/6。
- [ ] **T2.3** 新建 `crawling/pagination.py`（最小集）：`page_fingerprint` + `PaginationLoopError` + `iterate_pages`（仅 `page_number`、`api_cursor` 两个迭代器 + `single_page`，覆盖 3 站所需；其余 4 类推迟）。私有迭代器须：记录指纹到 `visited_page_keys`、指纹重复且无合法终止信号时抛 `PaginationLoopError`、只接受正向终止证据、每页更新可序列化 checkpoint、`trajectory.record_step(tool="crawl_page", ...)`。
- [ ] **T2.4** `deepagents_runner.py` 灰度分发（D1/D4）：在 PATH A 分支内：

```python
if isinstance(adapter, CompleteCrawlAdapter):
    crawl_result = adapter.execute_crawl(task, strategy_record, trajectory)
    adapter.validate(crawl_result)
    return run_post_crawl_pipeline(task, crawl_result)
return adapter.execute(task, strategy_record, trajectory)  # legacy（Alibaba 等）不变
```

- [ ] **T2.5（不做的项，显式声明）**：不实现 `adapters/mokahr_spa.py`/`mioffice_spa.py`/`pdd_spa.py`；不种对应 `JobDiscoveryStrategy`。3 个 URL 本轮继续走 PATH C supervisor（供 Phase 6 基线测试）。
- [ ] TDD：`test_complete_crawl_base.py`（无 coverage 不得成功 / validate 拒不完整）、`test_crawl_executor.py`（跟踪参数归一化 / 同一详情只抓一次但合并地区 / 详情失败产可续跑 partial）、`test_path_routing_v2.py`（CompleteCrawlAdapter 走 execute_crawl+pipeline；legacy adapter 走 execute 不变；无匹配走 PATH C 不变）。
- [ ] 回归：`pytest tests/unit/ -k "job_discovery or alibaba_spa"`。

```powershell
git add backend/app/services/job_discovery/adapters/complete_crawl_base.py `
  backend/app/services/job_discovery/crawling/crawl_executor.py `
  backend/app/services/job_discovery/crawling/pagination.py `
  backend/app/services/job_discovery/deepagents_runner.py `
  tests/unit/job_discovery/test_complete_crawl_base.py `
  tests/unit/job_discovery/test_crawl_executor.py `
  tests/unit/job_discovery/test_path_routing_v2.py
git commit -m "feat(job-discovery): add CompleteCrawlAdapter V2 framework (no site adapters)"
```

---

### Phase 3 · 招聘范围 + 学历硬门槛（新模块，不动冻结工具）

**Files:** `scope/recruitment_classifier.py`、`scope/education_gate.py`、`matching/eligibility.py`；Test: `test_recruitment_classifier.py`、`test_education_gate.py`、`test_eligibility.py`

- [ ] **T3.1** `recruitment_classifier.py`：`classify_recruitment_scope(target, structured_fields, channel_text, detail_text, url, title)->RecruitmentScopeDecision`（structured>text>unknown；年份范围解析覆盖 `2027届`/`2026-2027届`/`2026 至 2027 届`/`2026/2027届`；不能仅凭"应届生"推年份）。实现同 complete-crawl-refactor Task 6。
- [ ] **T3.2** `education_gate.py`：`parse_minimum_education(text)->EducationRequirement` + `evaluate_education(user_education, requirement)->GateDecision`（EDUCATION_RANK：不限0/大专1/本科2/硕士3/博士4；"博士优先"算偏好不算硬要求；未知 REVIEW）。实现同 Task 7。
- [ ] **T3.3** `matching/eligibility.py`：`evaluate_eligibility(scope_decision, education_decision)->EligibilityResult`（任一 FAIL->FAIL，任一 REVIEW->REVIEW，否则 PASS）。
- [ ] **T3.4（修正，D6）**：**不修改 `tools/evidence_verifier.py`**。scope 判定在 Phase 5 pipeline 内对 `RawJobDetail` 调用，证据写入 `CanonicalJob.evidence_refs`。
- [ ] TDD：用 complete-crawl-refactor Task 6/7 的用例。
- [ ] 回归：`pytest tests/unit/ -k "evidence_verifier or job_discovery"`（evidence_verifier 行为不变）。

```powershell
git add backend/app/services/job_discovery/scope `
  backend/app/services/job_discovery/matching/eligibility.py `
  tests/unit/job_discovery/test_recruitment_classifier.py `
  tests/unit/job_discovery/test_education_gate.py `
  tests/unit/job_discovery/test_eligibility.py
git commit -m "feat(job-discovery): add recruitment and education hard gates"
```

---

### Phase 4 · JD 规范化 + 主体去重（仅 exact core_hash 合并）

**Files:** `normalization/jd_parser.py`、`normalization/jd_normalizer.py`、`deduplication/canonical_job_deduplicator.py`；Test: `test_jd_normalizer.py`、`test_canonical_job_deduplicator.py`

- [ ] **T4.1** `jd_parser.py`：`parse_jd_sections(full_text)->ParsedJDSections`（职责/要求段头：岗位职责/工作职责/职位描述/主要职责 与 任职要求/岗位要求/职位要求/任职资格）。
- [ ] **T4.2** `jd_normalizer.py`：`normalize_jd(detail: RawJobDetail)->NormalizedJD`。`core_hash = sha256(normalized_responsibilities + "\n---requirements---\n" + normalized_requirements)`，规范化去空白与标点、剥 `岗位编号/职位编号/工作地点/招聘人数/发布时间/招聘批次` 等非业务行。**必须用 `full_text`，不用截断的 `text_excerpt`**。
- [ ] **T4.3（修正，D3）** `canonical_job_deduplicator.py`：
  - `canonical_job_key(company, jd) = sha256(normalized_company + "\n" + jd.normalized_responsibilities + "\n" + jd.normalized_requirements)`。
  - **仅**按 `normalized_company + exact core_hash` 自动合并（合并 locations/alternative_titles/detail_urls/apply_urls/source_listing_urls/source_record_ids/source_job_codes/evidence_refs，稳定排序去重）。
  - 二级相似度（char 3-gram Jaccard）≥ 0.94 时**只**标记 `duplicate_review_group`（附组 id + 对方 canonical_job_id），**不自动合并**。
  - `has_critical_conflict`（最低学历冲突 / 技术方向词集完全不相交且双方非空 / 职责出现互斥方向）触发时**不**打同组标。
- [ ] **T4.4（修正，D6）**：**不修改 `tools/jd_extraction.py`**。规范化是新下游模块，消费 `RawJobDetail.full_text`。`candidate_packager.py` 保留现有 `idempotency_key`，仅**并列**新增 `canonical_job_key`（不替换、不改语义）。
- [ ] TDD：`test_jd_normalizer.py`（地点/编号不同 core_hash 相同 / 职责不同 core_hash 不同 / 用 full_text 不受截断影响）；`test_canonical_job_deduplicator.py`（相同 JD 不同地点合并 / 标题相同 JD 不同不合并 / 标题不同 JD 相同合并 / **相似≥0.94 只打标不合并** / 临界冲突不打标）。
- [ ] 回归：`pytest tests/unit/ -k "jd_extraction or candidate_packager"`（两者行为不变）。

```powershell
git add backend/app/services/job_discovery/normalization `
  backend/app/services/job_discovery/deduplication/canonical_job_deduplicator.py `
  tests/unit/job_discovery/test_jd_normalizer.py `
  tests/unit/job_discovery/test_canonical_job_deduplicator.py
git commit -m "feat(job-discovery): normalize JD and dedup by exact core_hash (similar only flagged)"
```

---

### Phase 5 · 后处理流水线 + 复用 RelevanceRanker（不截断，保 PATH C）

**Files:** `post_crawl_pipeline.py`、`worker.py`；Test: `test_complete_crawl_pipeline.py`

- [ ] **T5.1** `run_post_crawl_pipeline(task, crawl_result)->DiscoveryRunResult`：

```python
def run_post_crawl_pipeline(task, crawl_result):
    coverage_decision = verify_coverage(crawl_result.coverage)
    if not coverage_decision.complete:
        return DiscoveryRunResult(
            status=coverage_decision.status,
            candidates=[],
            evidence=_collect_evidence(crawl_result),
            coverage=crawl_result.coverage,
            raw_listing_count=len(crawl_result.raw_listings),
            error=coverage_decision.reason,
        )

    classified = []
    for detail in crawl_result.raw_details:
        scope = classify_recruitment_scope(target=task.recruitment_scope,
                                           structured_fields=detail.structured_fields,
                                           channel_text=detail.channel_text,
                                           detail_text=detail.full_text,
                                           url=detail.detail_url, title=detail.title or "")
        njd = normalize_jd(detail)
        edu = evaluate_education(task.user_education,
                                 parse_minimum_education(njd.education_requirement or ""))
        classified.append(ClassifiedJob(detail, scope, njd,
                                        evaluate_eligibility(scope, edu)))

    canonical_jobs = deduplicate_canonical_jobs(classified)
    eligible = [j for j in canonical_jobs if j.eligibility.status == "PASS"]
    review = [j for j in canonical_jobs if j.eligibility.status == "REVIEW"]

    # 复用现有 LLM RelevanceRanker（D7），不建确定性 scorer。
    ranked = RelevanceRanker(llm=_ranker_llm(task)).rank(
        [_to_ranker_view(j) for j in eligible],
        profile_summary=task.profile_summary,
        preferences=task.preferences,
    )
    # D2：不截断。PASS 且 score >= minimum_match_score 的全部返回，按分数降序。
    matched = sorted(
        [ScoredJob(j, r) for j, r in zip(eligible, ranked)
         if r.score >= task.minimum_match_score],
        key=lambda s: (-s.score, s.job.company, s.job.canonical_title),
    )

    return DiscoveryRunResult(
        status="succeeded",
        candidates=[package_scored_job(s) for s in matched],
        review_candidates=[package_review_job(j) for j in review],
        evidence=_collect_evidence(crawl_result),
        coverage=crawl_result.coverage,
        raw_listing_count=len(crawl_result.raw_listings),
        canonical_job_count=len(canonical_jobs),
        eligible_job_count=len(eligible),
        matched_job_count=len(matched),
        scope_filter_completed=True,
        dedup_completed=True,
        scoring_completed=True,
    )
```

- [ ] **T5.2（D2 审计）**：检查 `relevance/relevance_ranker.py`、`recommendation_service.py`、`MatchService` 及调用方是否存在 `top_k`/`limit`/`max_candidates`/`[:N]`；V2 路径（`run_post_crawl_pipeline` 及其下游）一律移除截断。保留旧路径行为不动（灰度）。新增单测 `test_no_truncation_in_v2_path` 断言"返回数 == 满足条件的全部数"。
- [ ] **T5.3（D1，保 PATH C）**：**不删除** `deepagents_runner.py` 中"PATH C 直接产出岗位"的主路径与 `result_contract.parse_agent_result` 的 tool 输出恢复逻辑。PATH C 仍用于无匹配/失败接管。仅 PATH A 命中 `CompleteCrawlAdapter` 时走 V2。
- [ ] **T5.4** `worker.py`：`partial_success` 存 coverage+evidence；`needs_manual_review` 存上下文（coverage/error/trajectory）。沿用现有状态机，不新增状态。
- [ ] TDD：`test_complete_crawl_pipeline.py`
  - 完整端到端：6 条 detail（campus2027北京 / campus2027上海同JD / social / campus2028 / 博士最低 / 销售）-> succeeded，raw=6，canonical=5（同JD两地合并），eligible=2，matched=1，候选 locations={北京,上海}。
  - 分页不全 -> partial/needs_manual_review，`scoring_completed=False`。
  - **零匹配但抓取完整 -> succeeded，matched_job_count=0**。
  - **不截断**：构造 50 条全 PASS 全过阈 -> 返回 50 条（验证无 top_k/[:N]）。
  - **相似≥0.94 不合并只打标**：两条职责 95% 相似但 core_hash 不同 -> canonical=2，其中一条带 `duplicate_review_group`。
- [ ] 回归：`pytest tests/unit/ -k job_discovery` + `tests/integration/job_discovery/`。

```powershell
git add backend/app/services/job_discovery/post_crawl_pipeline.py `
  backend/app/services/job_discovery/worker.py `
  tests/integration/job_discovery/test_complete_crawl_pipeline.py
git commit -m "feat(job-discovery): integrate post-crawl pipeline reusing RelevanceRanker (no truncation)"
```

---

### Phase 6 · Supervisor 基线测试（用 3 个真实 URL 量化当前路径与真实数的差距）

> 用户明确要求：用以下 3 个正确 URL 测"agent 提取出的信息是否和真实信息一致"。本阶段**不改任何生产代码**，只新增一个 gated live 测试，建立可信基线，作为后续是否建站点 adapter 的依据。

**Ground truth：**

| 公司 | URL | 真实岗位数 | 页数 |
|---|---|---|---|
| 元戎启行 | `https://app.mokahr.com/campus-recruitment/deeproute/145894#/home` | 21 | 1 |
| 小米 | `https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY` | 151 | 16 |
| 拼多多 | `https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x` | 22 | 3 |

**Files:** `tests/integration/job_discovery/test_supervisor_baseline_real_urls.py`

- [ ] **T6.1** 测试结构：
  - gated：`@pytest.mark.live` + 环境变量 `RUN_SUPERVISOR_BASELINE=1`（未设置时 skip）。
  - 复用 `tests/manual/test_non_alibaba_urls.py` 的 `_setup_runtime` / `build_discovery_supervisor_agent` / `invoke_supervisor_agent` / `parse_agent_result`，确保跑的就是**当前 PATH C supervisor**。
  - 对每个 URL 跑 supervisor，捕获：`status`、`evidence_count`（≈抓取页数）、`raw_candidate_count`、`unique_candidate_count`（按 `title+company` 规范化去重）、`apply_url_sample`（前 5 个，判别是否全是列表页 URL）、`pages_traversed`（从 trajectory 的 `extract_rendered_job_evidence` 调用数推断）。
  - 输出对照表（actual vs real），并断言以下**一致性维度**：

```python
@pytest.mark.live
@pytest.mark.skipif(not os.environ.get("RUN_SUPERVISOR_BASELINE"), reason="needs live LLM")
@pytest.mark.parametrize(("company","url","real_count","real_pages"), [
    ("元戎启行","https://app.mokahr.com/campus-recruitment/deeproute/145894#/home",21,1),
    ("小米","https://xiaomi.jobs.f.mioffice.cn/s/kJVnd58xtWY",151,16),
    ("拼多多","https://careers.pddglobalhr.com/campus/grad?t=AOT9z6aa0x",22,3),
])
def test_supervisor_extracts_match_real(company,url,real_count,real_pages):
    result = run_supervisor(url)
    record = {
        "company": company,
        "real_count": real_count,
        "real_pages": real_pages,
        "status": result.status,
        "raw_count": len(result.candidates),
        "unique_count": _unique_count(result.candidates),
        "pages_traversed": _pages_traversed(result),
        "apply_url_all_listpage": _all_apply_urls_are_listpage(result, url),
    }
    _dump(record)  # 写 tests/manual/_supervisor_baseline_<company>.json
    # 一致性断言（当前 supervisor 预期 FAIL，文档化基线差距）
    assert record["unique_count"] == real_count, (
        f"{company}: unique {record['unique_count']} != real {real_count}")
    assert record["pages_traversed"] >= real_pages, (
        f"{company}: pages {record['pages_traversed']} < real {real_pages}")
    assert not record["apply_url_all_listpage"], (
        f"{company}: apply_url 全是列表页，非逐岗位")
```

- [ ] **T6.2** 运行（PowerShell UTF-8）：

```powershell
$env:PYTHONUTF8=1; $env:PYTHONIOENCODING='utf-8'
$env:RUN_SUPERVISOR_BASELINE='1'
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/job_discovery/test_supervisor_baseline_real_urls.py -v -s
```

- [ ] **T6.3** 产出基线报告（写入 `tests/manual/_supervisor_baseline_summary.md`）：每站 actual vs real 表 + 病根归因（漏页/重复/幻觉/apply_url 错位）。预期当前 supervisor 在 ≥2 站 FAIL，作为"需建站点 adapter"的依据。

- [ ] **T6.4 决策点**：基线 FAIL 则进入"推迟项：建 3 站 CompleteCrawlAdapter V2 实现"；若某站意外 PASS，则该站维持 supervisor 路径，仅对其余建 adapter。

```powershell
git add tests/integration/job_discovery/test_supervisor_baseline_real_urls.py `
  tests/manual/_supervisor_baseline_*.json `
  tests/manual/_supervisor_baseline_summary.md
git commit -m "test(job-discovery): supervisor baseline against real URL counts (21/151/22)"
```

---

## 7. 推迟项（本轮不做，显式列出）

- **3 站 CompleteCrawlAdapter V2 实现**（`adapters/mokahr_spa.py`/`mioffice_spa.py`/`pdd_spa.py` + 种策略）：待 Phase 6 基线确认后启动。每站：Playwright/XHR 全量翻页（用站点自身 total/hasMore 作 `expected_listing_count`，禁用固定最大页数作正常停止条件）、逐岗位 `detail_url`+`apply_url`、详情去重后抓全部唯一详情、输出 `CrawlCoverage`、`coverage_complete` 需正向终止证据。
- **通用 CrawlPlan schema + 6 类分页状态机**（complete-crawl-refactor Task 3/4/5/12）：泛化用，非本轮 6 URL 所需。
- **PATH C -> CrawlPlan Planning Agent 改造**（Task 13）：完成后才废弃"PATH C 直接产出岗位"（D1）。
- **Checkpoint 续跑基础设施**（Task 15）：规模化用。

---

## 8. 阶段门禁

| 阶段 | 门禁 |
|---|---|
| Phase 0 | ground_truth.json 落盘；验收范围显式声明 5 URL（禾赛 out-of-scope） |
| Phase 1 | 新类型可序列化；`verify_coverage` 可判完整/不完整；`result_contract` 全局行为未变（PATH C 回归通过） |
| Phase 2 | CompleteCrawlAdapter V2 框架 + 灰度分发 TDD 通过（FakeDriver）；**无任何站点 adapter**；legacy Alibaba + PATH C 不回归 |
| Phase 3 | 硕士用户被博士最低要求岗位 FAIL；2027 校招不混社招/2028；`evidence_verifier.py` 未改 |
| Phase 4 | 同 JD 多地点合并；相似 ≥0.94 只打标不合并；`jd_extraction.py`/`candidate_packager` 旧键未改 |
| Phase 5 | 端到端 pipeline 通过；零匹配可 succeeded；**不截断**（50 条全过阈返回 50 条）；PATH C 主路径未删 |
| Phase 6 | 3 站基线报告产出；actual vs real 对照清晰；决策点结论记录 |

---

## 9. 完成定义（本轮）

- Phase 1–6 全部门禁通过；冻结工具 `jd_extraction.py`/`evidence_verifier.py` 零改动。
- V2 流水线骨架可跑通（FakeDriver 端到端集成测试 PASS）。
- 当前 supervisor 在 3 眙的真实基线已量化（Phase 6 报告），并据此给出"是否建站点 adapter"的结论。
- 不截断、仅 exact core_hash 合并、版本化 adapter 接口、PATH C 保留、禾赛显式 out-of-scope——5 条修正全部落实。
- 本轮**不**声称"6 URL 已完成"；验收范围 = 5 URL，禾赛 `needs_manual_review`。

---

## 10. 风险与回滚

- 逐路径灰度切换，不一次性替换：本轮只新增 V2 框架，不迁移任何站点；mokahr/mioffice/pdd 仍走 PATH C。回滚 = 不接入 V2 分发即可，零影响现有站点。
- 若 Phase 6 基线显示 supervisor 意外可用某站，则该站不建 adapter，避免过度工程。
- 任何 V2 路径异常不得伪装 `succeeded`；`coverage_complete=False` 一律降级为 `partial_success`/`needs_manual_review`。
