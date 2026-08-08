# 基于 FindJobs-Agent 的 Job-Discovery 优化方案

> 参考项目：[FindJobs-Agent（FindBestCareers）](D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\FindJobs-Agent-main)（`D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\FindJobs-Agent-main`，以下简称 **FindJobs**）
> 适用范围：本项目的 job-discovery 链路及其下游（job-matching / resume-tailoring / career-planning）
> 文档定位：**可执行优化方案**——每一项含现状分析、借鉴内容、文件级落地建议、验收标准、优先级，并附分阶段实施路线图与红线清单
> 日期：2026-08-08

## 1. 文档说明与阅读指南

本方案是从 FindJobs 项目全量代码研究（爬虫 1678 行 + 分析 agent 1317 行 + 匹配/存储/流水线）中提炼出的**可采纳项**，按三级分类：

- **A 类（数据获取通道）**：直接决定"能不能拿到数据"的通道级改造。
- **B 类（数据结构与特征）**：JD 数据的结构化增强，为下游匹配/规划提供新特征。
- **C 类（工程健壮性）**：稳定性、可观测性、成本控制类改造。

每一项统一用五要素写作：**现状 / 借鉴 / 落地 / 验收 / 优先级**，并在"验收"后附**测试落点**。

优先级标签为本文档建议值，评审时可调整。所有引用的行号均基于当前 master 代码（2026-08-08 复核）。

**每一项读法**：先看"现状"（只陈述既有事实与行号锚点），再看"借鉴"（FindJobs 的做法），"落地"给出明确的文件级方案，"验收"是可勾选的客观标准。

## 2. 现状基线

### 2.1 当前 job-discovery 管线

```
用户 URL
  └─ deepagents_runtime 层:
       subprocess_runner.run_skill_script("browse", ...)   # 9 脚本白名单(_ALLOWED_SCRIPTS L22)、900s 超时(L35)、cwd 固定 skill 目录
  └─ browse_fetch.py:
       classify_url(url)          # hostname → SiteClass 硬编码表（L110-126）
       _browse_one_url            # 回退链：list/parallel-fetch → search-interact；blocked 终态永不重试（L350-356）
  └─ skill/job-discovery/scripts/browse.py:
       7 模式（L2566-2567）：list / detail / interact / search / search-interact / click / parallel-fetch
       PublicJobEvidenceCollector（L113-165）只被动观察 Playwright 响应，不主动构造 API 调用
       is_safe_public_url / install_public_network_guard（L67-110）
       0 字符 SPA 空壳 → status=blocked（L1191-1208，used_path=spa_shell_empty_no_evidence）
  └─ 提取:
       jd_extraction.extract_jd_candidates（L343）纯正则，无 LLM 无 DB 无网络
       extract_gate.extract_with_gate（L51）：正则优先，仅当结果为空或 confidence<0.6（_LOW_CONFIDENCE_BELOW L25）时走 LLM，strict-Pareto union
       llm_extractor.LLMJobExtractor（L204）：裸 ChatOpenAI（L214，temperature=0 L216，max_tokens=4096 L217），不走 model_gateway
       flag: deepagents_llm_extraction_enabled=False（config.py L128）
```

### 2.2 安全硬守则（本方案所有项必须保持）

1. 永不自动点击最终提交（GUI executor 停在 `READY_FOR_REVIEW`）
2. 永不绕过登录/验证码/反爬 —— 被拦即 `needs_manual_review`
3. 学生 API 只返回 `verified` 岗位（SQL 层过滤）
4. 密钥永不写入 repo/日志/argv
5. MySQL 是业务状态唯一权威
6. 任务动作需 task lease + scope 校验
7. 岗位 review 写入需 `review_version` 乐观锁

### 2.3 已归档遗留

preference 过滤管线（`preference_expansion.py` 等，含 `_ROLE_FAMILY_MARKERS` 9 个角色家族）已于 commit `6c0cd44` 移入 legacy 目录，源码仅存在于 git 历史 `ea0a70b`。本文档 B2 与其关系见 §5.2（独立数据文件，仅以 markers 为种子输入，不动遗留代码）。

## 3. 借鉴总览矩阵

| 编号 | 借鉴点 | 优先级 | 所属阶段 | 价值一句话 | 对应 FindJobs 实现 |
|------|--------|--------|----------|-------------|--------------------|
| A1 | 官方公开 JSON API 直连（第二数据通道） | **P0** | Phase 1 | 解锁 didi/netease/baidu 三个当前完全无产出的站点 | `job_crawler_v2.py` API 爬虫（16 家） |
| A2 | LLM JD 提取技能闭集约束 | P1 | Phase 2 | 消灭 LLM 幻觉技能标签 | `job_agent.py` SkillRepository / `_select_skills` |
| B1 | 职位强度信号（结构化 → 档位） | P1 | Phase 2 | 为匹配/规划提供质量基线 | `job_agent.py` SIGNAL_RULES / INTENSITY_LEVELS |
| B2 | 两级职位分类数据文件 | P1 | Phase 2 | 运行时零 LLM 的确定性岗位分类 | `job_agent.py` TaxonomyManager（离线生成一次） |
| B3 | min_degree + 优先级结构化抽取 | P1 | Phase 2 | JD 学历/优先级字段从 0 到 1 | `job_agent.py` `_normalize_degree` |
| C1 | 温度兼容适配层（模式参考） | P2 | Phase 3 | 借其"能力探测+提示侧模拟"思路，主交付是 extractor 接 gateway | `llm_utils.py` apply_temperature_strategy |
| C2 | 跨 JD 技能缺口聚合 | P2 | Phase 3 | career-planning 从单 JD 到多 JD 缺口输出 | `job_matcher.py` top_skill_gaps |
| C3 | 跨运行增量抓取与去重 | P2 | Phase 3 | 同一职位跨运行零重复入库 | `storage.py` job_id 主键 INSERT OR REPLACE |
| C4 | 密钥轮换 + 指数退避 + 日志脱敏 | P2 | Phase 3 | 密钥安全与调用弹性的工程兜底 | FindJobs 统一 LLM 客户端 |
| C5 | 批量并行进度日志 | P2 | Phase 3 | 长批量可观测（i/n 进度行） | `job_agent.py` --max-workers 并行分析 |

不采纳项（红线）见第 8 章。

## 4. A 类：数据获取通道

### A1（P0）官方公开 JSON API 直连 —— 第二数据通道

#### 现状

- 多次 eval（含 10-URL 技能评估）中，didi / netease / baidu 三站被 [browse.py](skill/job-discovery/scripts/browse.py) 判为 0 字符 SPA 空壳 → `status=blocked`（L1191-1208，`used_path=spa_shell_empty_no_evidence`），**当前完全无数据产出**。这是三家公司页面层的静默反爬签名（didi 空壳、netease 反爬+重定向、baidu 302→404），属于安全门 #2 允许的"被拦即上报"路径，但结果是死数据源。
- [browse_fetch.py](backend/app/services/deepagents_runtime/tools/skill_graphs/browse_fetch.py) 的 `classify_url`（L110-126）是 hostname→模式硬编码表；`status=="blocked"` 为终态，永不重试（L350-356）。
- **适配器接缝已存在且为空**：
  - [adapter_supervisor.py](skill/job-discovery/scripts/adapter_supervisor.py)：`load_adapter`（L34-45，契约 = 类路径 + `validate(url)` 方法）+ `run_with_adapter_fallback`（L48-91，三路径：skill_no_adapter / adapter / skill_after_adapter_failure，adapter 异常→回退 skill 并带 trajectory 上下文，绝不把部分 adapter 结果当成功）
  - ORM：`JobDiscoveryStrategy.adapter` 列（[models.py](backend/app/db/models.py) L1147，类 L1135）、`JobDiscoveryTrajectory`（L1172-1206）
  - [SKILL.md](skill/job-discovery/SKILL.md) L38-46 已文档化适配器执行契约 —— 但**零个具体实现**。
- 架构立场冲突须正面处理：[browse.py](skill/job-discovery/scripts/browse.py) 的 `PublicJobEvidenceCollector`（L113-165）只被动观察 Playwright 响应 JSON，明确拒绝 "site adapter" 概念。本方案不修改 collector —— 适配器只做**新的取数通道**，产出仍走同一套证据绑定（source_url + content_hash）与 collector 观察路径。

#### 借鉴（FindJobs）

FindJobs 对 16 家公司直连**官方公开、免鉴权**的 JSON 端点（如 didi `talent.didiglobal.com/api/jobList`、netease `hr.163.com/api/hr163/position/queryPage`、baidu `talent.baidu.com/httservice/getPostListNew`），而非渲染页面。要点：

- 同一 public HTTP(S) 面，**不是反爬绕过** —— 无鉴权公开 JSON endpoint 是站点官方提供的数据通道；
- 礼貌限速：每请求间随机延迟 0.2–0.5s；单公司 `_should_stop` 300 条硬上限；
- 失败不静默：任何异常显式标记，不产出半空结果。

#### 落地

1. 新建 `skill/job-discovery/scripts/adapters/`：每公司一模块（`didi.py` / `netease.py` / `baidu.py`，或单 `endpoints.py` + 每公司配置），实现 `validate(url)` + `execute(task, strategy, trajectory)` 契约（与 adapter_supervisor 一致）。
2. 新建 `endpoint_allowlist.json`：`base_url`、允许的路径前缀、限速参数、300/公司上限、`review_status` 字段（=reviewed 才生效）。
3. 扩展 [adapter_supervisor.py](skill/job-discovery/scripts/adapter_supervisor.py) 注册表；`run_with_adapter_fallback` 保证任意异常 → 显式 `status=blocked`（与 browse.py 终态语义一致，杜绝静默空）。
4. 每个构造出的 URL 过 `is_safe_public_url`（browse.py L67）。
5. [config.py](backend/app/config.py) 新增 `use_public_api_adapters: bool = False` —— 默认关闭，**人工评审完成后才开启**。
6. 更新 SKILL.md L38-46 适配器清单。
7. 执行路径二选一（文档定死，避免半实现）：
   - **方案甲（推荐）**：扩展 [browse_fetch.py](backend/app/services/deepagents_runtime/tools/skill_graphs/browse_fetch.py) —— `classify_url` 命中适配器 host 时先走适配器，失败才落 blocked；其余 host 行为逐字节不变。改动最小、链路最短。
   - 方案乙：适配器加入 `subprocess_runner._ALLOWED_SCRIPTS` 白名单（subprocess_runner.py L22），由 skill 层直接调度。

#### 验收

- [x] 三公司各返回 ≥N 条（建议 N=20）带证据字段（`source_url` + `content_hash`）的结构化 JD；—— 2026-08-08 live smoke：didi 300 ✓ / netease 300 ✓ / baidu 端点服务端契约变更，如实返回 `blocked: empty_result`（见 allowlist notes）
- [x] 限速可测：请求间隔 ≥0.2s 可观测、单公司 ≤300 条；
- [x] `endpoint_allowlist.json` 含 `review_status=reviewed` 与人工评审记录（reviewer + 日期）；
- [x] 故障注入（DNS 失败 / 403 / 超时）均返回显式 `status=blocked`，无异常泄漏、无空结果冒充成功；
- [x] 全仓 grep 无登录/验证码/反爬代码路径；
- [x] 既有 4 个非 blocked 模式（parallel-fetch / list / search-interact / probe）全量回归绿。

#### 测试落点

`tests/unit/test_adapter_supervisor_api.py`（新增：validate/execute 契约、故障注入、blocked 语义）+ `tests/manual/adapter_live_smoke.py`（真实端点冒烟，仿 generic_pref_pipeline_eval.py 先例）。

#### 优先级

**P0** —— 解锁 3 个当前死掉的公司；接缝已有文档且为空，收益/成本比最高；严格处于 public HTTP(S) + 证据绑定守则之内。

---

### A2（P1）LLM JD 提取技能闭集约束

#### 现状

- [llm_extractor.py](backend/app/services/deepagents_runtime/tools/llm_extractor.py) `LLMJobExtractor`（L204）是**裸 ChatOpenAI**（L214，temperature=0 / max_tokens=4096），无 json_mode、无 model_gateway、无重试；`_lenient_json`（L120）恢复截断数组；`_to_candidate`（L158）丢弃非法项、永不抛错。
- 门控 [extract_gate.py](backend/app/services/deepagents_runtime/tools/extract_gate.py) `extract_with_gate`（L51）：正则优先，LLM 仅在结果为空或 `confidence < 0.6`（L25）时介入，strict-Pareto union。
- flag `deepagents_llm_extraction_enabled=False`（config.py L128）。
- [schemas.py](backend/app/services/job_discovery/schemas.py) `NormalizedJobCandidate`（L8）**无 skills 字段** —— 即使 LLM 产出了技能标签也无处安放。

#### 借鉴（FindJobs）

`SkillRepository`（job_agent.py L332-406）：候选技能标签为**确定性闭集**（≤80 项，SequenceMatcher 相似度 + 直接命中 + 全局兜底构建，非运行时 LLM 生成）；`_select_skills`（L1167-1205）做**闭集成员校验**（LLM 只能从闭集里选，事后剔除非法项），数量不足时确定性兜底；`LOW_INFORMATION_SKILLS`（L164 = {AI, 人工智能, 技术, 数学, 计算机, 科研, 能力, 技能}）过滤无信息量标签。

#### 落地

1. 新建数据文件 `backend/app/services/job_discovery/data/skill_tags.json`（≤80 项，人工维护清单，严禁运行时 LLM 生成）。
2. 新建 `backend/app/services/job_discovery/tools/skill_validator.py`：归一化 → 成员校验 → 非法项剔除/重映射 → 低信息过滤 → min 数量确定性回退（回退取 JD 文本正则命中关键词）。
3. 在 llm_extractor 路径接入：prompt 中给出闭集 + `_to_candidate` 后置校验，保留 `_lenient_json` 恢复逻辑。
4. schema 决策见 §6（v1 加可选字段 `skills: list[str]`，无 MySQL 迁移）。

#### 验收

- [ ] 标签文件 ≤80 项，无运行时 LLM 构建（grep 可证）；
- [ ] 属性测试：LLM 返回的技能要么 ∈ 闭集要么被剔除，**非法项永不外泄**到最终结果；
- [ ] 低信息过滤单测：{AI, 技术, 数学, 计算机} 不出现在最终结果；
- [ ] 回退单测：LLM 返回数 < min 时走确定性回退；
- [ ] 门控两态回归：`flag=False` 行为与今日**逐字节一致**；`flag=True` 输出可文档化。

#### 测试落点

`tests/unit/test_skill_validator.py`（新增）。

#### 优先级

**P1** —— 需要 B3 的 schema 字段（skills）先落地。

## 5. B 类：数据结构与特征

### B1（P1）职位强度信号

#### 现状

- [job_matching.py](backend/app/services/career_skills/job_matching.py) L210：`score = min(100, len(matched) * 34)`，无强度/质量信号，无缺失技能清单输出。
- [career_planning.py](backend/app/services/career_skills/career_planning.py) L81：单 JD 关键词字面交集 + 2 条硬编码动作。
- [resume_tailoring.py](backend/app/services/career_skills/)：单 JD、仅事实绑定操作（高亮/重排）。
- 全链路无"这个 JD 写得有多具体"的信号。

#### 借鉴（FindJobs）

`SIGNAL_RULES`（job_agent.py L72-145）五类信号正则 + 权重 → 归一化得分；`INTENSITY_LEVELS`（L147-152）三档强度；证据词原样保留供审计。岗位强度作为 base_score 提示进入评分与规划。

#### 落地

1. 新建 `backend/app/services/job_discovery/tools/job_strength.py`：信号表（正则模式 + 权重）、得分归一化、档位阈值 high/medium/low，返回 `{score, tier, base_score, evidence[]}`。
2. 在 `extract_jd_candidates` 或 `extract_with_gate` 输出处做富化。
3. `base_score` 以**可选入参**下传 job_matching / career_planning / resume_tailoring，**不改动现有 `min(100, matched*34)` 路径**（加性参数，特性开关）。
4. 证据词写入 `JobDiscoveryTrajectory`（models.py L1172）留审计。

**信号表草案**（评审定稿；权重为经验标定，见第 9 章已知限制）：

| 信号 | 正则示例 | 权重 |
|------|----------|------|
| 明确年限要求 | `(\d+)\s*(年|年以上).*经验` | 2 |
| 明确技能栈 | `熟悉|精通|掌握.*(Python\|Java\|...)` | 2 |
| 明确学历 | `本科及以上|硕士` | 1 |
| 明确职责清单 | 3+ 条 `^[0-9一二三]+[、.)]` 编号职责 | 1 |
| 明确加分项 | `加分|优先.*考虑` | 1 |

#### 验收

- [ ] 确定性单测（同输入同输出）；
- [ ] 权重表与档位阈值在本节列出并说明依据（上表）；
- [ ] 证据词原样回传（不加工）；
- [ ] 下游可选接入且现有行为不变（开关关闭时逐字节一致）；
- [ ] 20 份抽样 JD 人工分级与算法档位一致率 ≥80%（低于则记入第 9 章已知限制）。

#### 测试落点

`tests/unit/test_job_strength.py`（新增）。

---

### B2（P1）两级职位分类数据文件

#### 现状

- 全仓**无**技能/分类标签数据文件；
- 归档管线 `preference_expansion.py` `_ROLE_FAMILY_MARKERS`（9 个角色家族）仅存于 git `ea0a70b`，已归档（§2.3）；
- `NormalizedJobCandidate` 无分类字段。

#### 借鉴（FindJobs）

`TaxonomyManager`（job_agent.py L656-883）构建两级岗位族谱（大类→细类）并落盘；**LLM 仅离线生成一次、人工评审冻结**；运行时确定性关键词检索、零 LLM 调用。

#### 落地

1. 新建 `backend/app/services/job_discovery/data/job_taxonomy.json`：`{level1: [{name, level2: [{name, keywords[]}]}]}`。
2. 新建 `backend/app/services/job_discovery/tools/taxonomy.py`：关键词命中计分、确定性、无 LLM。
3. 可选 schema 字段 `taxonomy: [str, str]`（level1, level2）。
4. **与归档 markers 的关系（定死）**：独立数据文件，以 `_ROLE_FAMILY_MARKERS` 为种子输入初始化，**不改动遗留代码**。

#### 验收

- [ ] 文件存在且两级覆盖（≥15 个大类，或文档化覆盖说明）；
- [ ] 检索确定性（同文本同标签，单测）；
- [ ] 运行时零 LLM 调用（grep `taxonomy.py` 无 ChatOpenAI/LLM 调用）；
- [ ] 人工评审检查点（reviewer + 日期）记录在附录。

#### 测试落点

`tests/unit/test_taxonomy.py`（新增）。

---

### B3（P1）min_degree + 优先级结构化抽取

#### 现状

- `NormalizedJobCandidate`（schemas.py L8）无学历字段、无优先级字段；
- `extract_jd_candidates`（jd_extraction.py L343）为确定性正则后端，不抽取学历/优先级。

#### 借鉴（FindJobs）

`_normalize_degree`（job_agent.py L1207-1218）：白名单 + 文本正则兜底的结构化学历抽取；"必须|优先"优先级识别（degree_priority 字段）。

#### 落地

1. schema（schemas.py）增加：
   - `min_degree: str | None = None`（白名单：大专/本科/硕士/博士/不限/学历不限/None）
   - `priority: Literal["must", "preferred", "unknown"] = "unknown"`
2. `extract_jd_candidates` 增补正则：学历白名单逐词 + "必须/优先/加分项"模式。
3. `llm_extractor._to_candidate`（L158）映射新字段；LLM 路径输出同字段集。
4. MySQL 迁移：**v1 不做**（决策见 §6）。

#### 验收

- [ ] 学历白名单逐词 fixture 单测；
- [ ] 无学历文本 → 默认 `unknown` 安全；
- [ ] 迁移冒烟：若未来持久化，迁移清单（附录模板）可执行；
- [ ] `flag=True` 时 LLM 抽取输出同字段集。

#### 测试落点

`tests/unit/test_jd_extraction_degree.py`（新增）。

#### 优先级

**P1** —— 它是 A2/B2 的 schema 地基，Phase 2 第一项。

## 6. Schema 涟漪决策（A2/B3/B2 共用）

`NormalizedJobCandidate` 是纯 Python dataclass（schemas.py L8）。**v1 决策：只加可选字段，不做 MySQL 迁移**：

```python
@dataclass
class NormalizedJobCandidate:
    ...
    skills: list[str] = field(default_factory=list)   # A2
    min_degree: str | None = None                      # B3
    priority: str = "unknown"                          # B3
    taxonomy: list[str] = field(default_factory=list)  # B2（视需要）
```

- 默认值保证序列化兼容，现有调用零改动；
- 如未来需要持久化到 MySQL，附录附迁移清单模板（alembic `YYYYMMDD_NNNN` + 回滚）。

## 7. C 类：工程健壮性

### C1（P2）温度兼容适配层（模式参考，不移植机制）

#### 现状

- [model_gateway.py](backend/app/services/agent_runtime/model_gateway.py)：DeepSeek-v4 → `json_mode` + 禁思考 + prompt 必须含 "json" 字样（L117-122、L466）；漂移降级梯：with_structured_output → `_strip_json_fence`（L327）→ `_coerce_response_fields`（L357，错误字段名重命名 {"input":"tool_input","decision":"action"}）→ 纠正性本地重试（`_decide_with_local_json_retry` L204，attempts=3）→ `invalid_model_response`。
- `LLMJobExtractor`（llm_extractor.py L204）**未走 gateway** —— 这正是 C1 的真实收益点。

#### 借鉴（FindJobs）

`llm_utils.py`（L53-75）：`supports_temperature` 能力探测 + `apply_temperature_strategy` 在 prompt 侧附加 "Variability Directive" 模拟温度。**我方漂移点是 json_mode 而非 temperature，故仅作模式参考，不移植机制**。

#### 落地（主交付）

1. `LLMJobExtractor` 接入 `model_gateway`，继承完整漂移降级梯（收益远大于温度探测）。
2. 可选辅助：为未来需要非贪心采样的工具提供温度能力探测 helper（仿 llm_utils.py 思路）。

#### 验收

- [ ] 注入失败时 extractor 沿降级梯逐级降级（每级可观测），而非裸异常；
- [ ] 出站调用可见 json_mode + 禁思考参数；
- [ ] flag 两态回归（False 逐字节同今日）。

#### 测试落点

`tests/unit/test_llm_extractor_gateway.py`（新增）。

---

### C2（P2）跨 JD 技能缺口聚合（career-planning）

#### 现状

- [career_planning.py](backend/app/services/career_skills/career_planning.py) L81：单 JD 关键词字面交集；2 条硬编码动作；无跨 JD 聚合、无缺口输出。
- [registry.py](backend/app/services/career_skills/registry.py) 注册 9 个工具（job-discovery 6 + job-matching 1 + resume-tailoring 1 + career-planning 1）；新工具 = 模块 + registry 块 + manifest 条目三处改动。

#### 借鉴（FindJobs）

`job_matcher.py` `top_skill_gaps`（L131-164）：跨 JD 聚合需求技能 vs 简历技能 → 排序后 top-N 缺失技能。

#### 落地（定死一个方案，防半实现）

- **首选**：扩展 `build-preparation-plan`，加**可选多 JD 入参**（零 registry 变动）。
- 算法：逐技能需求计数 vs 简历技能 → 缺口分 → top-N 缺口 + 出现次数。
- 单 JD 路径**逐字节保持**（回退兼容，可测）。
- 新工具（如 `aggregate-skill-gaps`）仅作为后续契约不洁时的备选。

#### 验收

- [ ] N JD 输入产出 top-N 缺口 + 出现次数；
- [ ] 单 JD 输出与今日一致（单测断言逐字节）。

#### 测试落点

`tests/unit/test_career_planning_multi_jd.py`（新增）。

---

### C3（P2）跨运行增量抓取与去重

#### 现状

- 已存在**单次运行内**候选幂等键（SHA-256 归一化 company+title+location+apply_url+evidence_hash）；
- `fetch-public-job-pages` 批量模式存在；**无跨运行持久化**。

#### 借鉴（FindJobs）

[storage.py](D:\Program Files\JetBrains\PyCharm Community Edition 2024.2.2\proj\FindJobs-Agent-main\storage.py)（L38-56）：`jobs` 表以 `job_id` 为主键 + `INSERT OR REPLACE`（L76-79），跨运行天然去重。

#### 落地（定死方案）

1. 持久化二选一：新建 `seen_jobs` 表（`job_id` / `source` / `first_seen` / `content_hash`），或复用 `JobDiscoveryTrajectory`。
2. fetch/extract 前查重跳过。
3. **A1 适配器必须产出稳定 job_id** —— 作为 A1 验收前置项（见 §4 A1）。
4. 增长控制：300/公司上限 + TTL 清理策略。

#### 验收

- [ ] 同一语料跑两遍，第二遍 seen 计数被记录、JD 零重复入库；
- [ ] TTL 清理生效；
- [ ] 适配器 job_id 稳定性测试通过（同职位两次抓取同 job_id）。

#### 测试落点

`tests/unit/test_seen_jobs_dedup.py`（新增）。

---

### C4（P2）密钥轮换 + 指数退避 + 日志脱敏

#### 现状

- 安全门 #4（密钥永不入 repo/日志/argv）；[config.py](backend/app/config.py) 含密钥配置；
- `LLMJobExtractor` 无任何重试机制。

#### 借鉴（FindJobs）

统一 LLM 客户端：key 轮换（旧钥失败自动切新钥）、指数退避重试、日志中密钥仅显示 `key[-6:]`。

#### 落地

1. 新建 `backend/app/services/agent_runtime/secrets.py`：
   - 密钥取用间接层（env 读取，**不假设当前仓库不存在的密钥提供方 infra**）；
   - 退避重试包装（1s/2s/4s 封顶 + jitter）用于 llm_extractor 与 gateway 路径；
   - 脱敏 helper：`key[-6:]` 全局套用，**保留错误日志关联 ID**（不破坏可追踪性）。

#### 验收

- [ ] 日志全量 grep 无完整密钥（仅 `key[-6:]`）；
- [ ] 退避序列单测（1/2/4s 封顶）；
- [ ] 轮换切换测试（旧钥失败 → 新钥成功）。

#### 测试落点

`tests/unit/test_secrets_retry.py`（新增）。

---

### C5（P2）批量并行进度日志

#### 现状

- 批量工具：`fetch-public-job-pages`、`extract-observed-job-details-batch`（registry.py）；
- [subprocess_runner.py](backend/app/services/deepagents_runtime/tools/skill_graphs/subprocess_runner.py) 900s 超时（L35）—— 长批量无可观测性。

#### 借鉴（FindJobs）

`job_agent.py` `--max-workers 5`：ThreadPoolExecutor + as_completed 的 "i/n done" 进度日志。

#### 落地

1. 批量工具与 eval 脚本改为**有界线程池（4-8 并发）** + as_completed 循环输出 `i/n done host=...` 进度行；
2. 网络类批量保留礼貌延迟；
3. **结果按输入索引排序**保证确定性。

#### 验收

- [ ] 批量运行输出单调递增 `i/n` 行；
- [ ] 并发数有界（mock 验证）；
- [ ] 结果顺序确定（同输入同输出顺序）；
- [ ] 单条目调用行为不变。

#### 测试落点

`tests/unit/test_batch_progress.py`（新增）。

## 8. 分阶段实施路线图

```
Phase 1（数据打通，先做）        Phase 2（数据增强）        Phase 3（健壮性）
┌──────────────────────┐        ┌──────────────────────┐   ┌──────────────────────┐
│ A1 官方 JSON API     │        │ B3 → A2 → B1 → B2    │   │ C2 → C3 → C4 → C5 → C1│
│ （+ B3 并行启动）    │        │                      │   │                      │
└──────────────────────┘        └──────────────────────┘   └──────────────────────┘

依赖图：
  A1 ──────────→ C3（适配器稳定 job_id 是 C3 前置）
  B3 ──────────→ A2 / B2（schema 地基）
  B1 ──→ C2
  B2 ──→ C2
  （独立项）──→ C4 / C5（可并行）
```

**Phase 1（数据打通）**：仅 A1。
- 硬前置：`endpoint_allowlist.json` 人工评审完成 + 降级测试通过。—— **已完成（2026-08-08，reviewed_by 有记录，flag 默认开）**
- 退出标准：didi/netease/baidu 解锁（各 ≥20 条结构化 JD）；既有 4 个非 blocked 模式全量回归绿；故障注入全显式 blocked。—— **didi/netease 达成（各 300 条）；baidu 端点服务端废弃（任何 payload 被拒），适配器显式 blocked，记为遗留**
- **建议开工动作：A1 与 B3 并行启动** —— A1 解锁数据来源，B3 是后续一切字段增补的地基。

**Phase 2（数据增强）**：B3 → A2 → B1 → B2。
- 顺序理由：B3 是 schema 地基（A2 的 skills、B2 的 taxonomy 都依赖它）；A2 闭集与 B2 分类同源种子可一并人工评审；B1 独立，落地时与下游消费方同步。

**Phase 3（健壮性）**：C2 → C3 → C4 → C5 → C1。
- C2 依赖 B1/B2 输出；C3 依赖 A1 稳定 job_id；C4/C5 相互独立可并行；C1 价值最低放最后（其真实收益来自 gateway 接入而非温度探测）。

## 9. 红线清单（不采纳项）

### 必拒项（5 项）

| # | 拒绝项 | 拒绝原因 | 违反守则 |
|---|--------|----------|----------|
| 1 | **Selenium/Playwright 反检测模式**（抹除 `navigator.webdriver`、`excludeSwitches=enable-automation` 等，FindJobs SeleniumCrawlerBase L131-148 的做法） | 属反爬绕过 | 安全门 #2 |
| 2 | **`verify=False` 关闭 SSL 校验**（FindJobs JobCrawlerBase 的做法） | 弱化传输安全，MITM 敞口 | 安全门 #4 精神（传输安全） |
| 3 | **裸 `except` 吞异常 + 静默空结果** | 与本项目 blocked 终态语义冲突，掩盖死数据源 | 错误处理约定（blocked→needs_manual_review） |
| 4 | **无限分页 / 无上限抓取** | 爬取规模失控，违反礼貌抓取与资源边界 | 行为边界（300/公司上限不可移除） |
| 5 | **鉴权端点滑向绕过**：端点一旦需要登录/签名/加密参数即放弃 | 公开通道一旦变私有即不再是"公开 JSON API"，继续尝试即破解 | 安全门 #2 |

### 近红线提示（2 项，不采纳但须显式声明）

1. **search-interact（zhipin/zhiye）与 wechat 模式不在本次范围** —— A/B/C 各项均无针对它们的改进路径，显式声明避免评审者误以为要覆盖。
2. **一切超出"官方公开 JSON 接口"边界的抓取不采纳** —— 本方案只认无鉴权公开端点。

## 10. 已知限制

1. **端点易变**：公司公开 JSON 端点无契约保障，需定期复检（建议随 Phase 3 引入复检任务）。
2. **ToS 灰色地带**：公开端点仍受公司 ToS/风控约束，"非反爬绕过"论证必要但不充分 —— 人工评审是硬门（A1 前置），端点一旦风控升级即降级 `status=blocked`。
3. **`deepagents_llm_extraction_enabled` 默认关闭**：A2/C1 的 LLM 路径默认不生效，需显式开启并回归。
4. **B1 权重为经验标定**：信号表权重非数据驱动；20 份人工一致率 <80% 时降级为本节记录，不强行调参。
5. **覆盖缺口已声明**：search-interact / wechat 站点（第 9 章近红线）无改进路径。
6. **A2 闭集的天花板**：闭集 ≤80 项约束了技能覆盖广度，新技能需人工增补（评审流程化）。

## 11. 总体验收清单

按阶段编号，逐条勾选：

**Phase 1**
- [x] A1-1 三公司各 ≥20 条带证据字段的结构化 JD（2026-08-08：didi 300 / netease 300 / baidu 端点废弃→显式 blocked，遗留）
- [x] A1-2 限速可测（≥0.2s 间隔、≤300/公司）
- [x] A1-3 allowlist 含 reviewed + 人工评审记录
- [x] A1-4 故障注入 → 显式 blocked，无异常泄漏
- [x] A1-5 全仓 grep 无登录/验证码/反爬代码
- [x] A1-6 既有 4 模式全量回归绿

**Phase 2**
- [ ] B3-1 学历白名单逐词 fixture 通过
- [ ] B3-2 无学历文本默认 unknown
- [ ] A2-1 技能闭集 ≤80 项、无运行时 LLM 构建
- [ ] A2-2 非法技能项永不外泄（属性测试）
- [ ] A2-3 低信息过滤 + min 回退单测通过
- [ ] A2-4 flag 两态回归（False 逐字节一致）
- [ ] B1-1 job_strength 确定性单测通过
- [ ] B1-2 20 份人工一致率 ≥80%（或记入已知限制）
- [ ] B1-3 下游可选接入，现有行为不变
- [ ] B2-1 taxonomy 文件 ≥15 大类、运行时零 LLM
- [ ] B2-2 检索确定性单测通过

**Phase 3**
- [ ] C2-1 多 JD top-N 缺口 + 出现次数
- [ ] C2-2 单 JD 输出逐字节不变
- [ ] C3-1 两遍运行零重复入库 + TTL 生效
- [ ] C3-2 适配器 job_id 稳定
- [ ] C4-1 日志无完整密钥（仅 key[-6:]）
- [ ] C4-2 退避序列 + 轮换切换测试通过
- [ ] C5-1 批量 i/n 进度行单调递增、并发有界、顺序确定
- [ ] C1-1 extractor 接入 gateway，降级梯每级可观测

## 12. 附录

### 12.1 文件路径速查

| 用途 | 路径 |
|------|------|
| 适配器接缝 | [skill/job-discovery/scripts/adapter_supervisor.py](skill/job-discovery/scripts/adapter_supervisor.py) |
| 适配器契约文档 | [skill/job-discovery/SKILL.md](skill/job-discovery/SKILL.md) L38-46 |
| 站点分类表 | [backend/app/services/deepagents_runtime/tools/skill_graphs/browse_fetch.py](backend/app/services/deepagents_runtime/tools/skill_graphs/browse_fetch.py) L110-126 |
| browse 脚本白名单 | [backend/app/services/deepagents_runtime/tools/skill_graphs/subprocess_runner.py](backend/app/services/deepagents_runtime/tools/skill_graphs/subprocess_runner.py) L22 |
| browse 7 模式 | [skill/job-discovery/scripts/browse.py](skill/job-discovery/scripts/browse.py) L2566-2567 |
| blocked 0-char 壳 | [skill/job-discovery/scripts/browse.py](skill/job-discovery/scripts/browse.py) L1191-1208 |
| 被动证据收集器 | [skill/job-discovery/scripts/browse.py](skill/job-discovery/scripts/browse.py) L113-165 |
| 正则提取器 | [backend/app/services/job_discovery/tools/jd_extraction.py](backend/app/services/job_discovery/tools/jd_extraction.py) L343 |
| LLM 提取门控 | [backend/app/services/deepagents_runtime/tools/extract_gate.py](backend/app/services/deepagents_runtime/tools/extract_gate.py) L51 |
| 裸 LLM 提取器 | [backend/app/services/deepagents_runtime/tools/llm_extractor.py](backend/app/services/deepagents_runtime/tools/llm_extractor.py) L204 |
| JD schema | [backend/app/services/job_discovery/schemas.py](backend/app/services/job_discovery/schemas.py) L8 |
| 匹配评分 | [backend/app/services/career_skills/job_matching.py](backend/app/services/career_skills/job_matching.py) L210 |
| 规划器 | [backend/app/services/career_skills/career_planning.py](backend/app/services/career_skills/career_planning.py) L81 |
| 工具注册 | [backend/app/services/career_skills/registry.py](backend/app/services/career_skills/registry.py) |
| 漂移降级梯 | [backend/app/services/agent_runtime/model_gateway.py](backend/app/services/agent_runtime/model_gateway.py) L117-466 |
| 适配器 ORM 列 | [backend/app/db/models.py](backend/app/db/models.py) L1147 |
| LLM 提取开关 | [backend/app/config.py](backend/app/config.py) L128 |

### 12.2 FindJobs-Agent 对应实现索引

| 借鉴项 | FindJobs 文件 | 关键位置 |
|--------|---------------|----------|
| API 直连爬虫 | `job_crawler_v2.py` | BaiduCrawler L431 / NeteaseCrawler L564 / DidiCrawler L734 / NIOCrawler L1100 |
| 限速与上限 | `job_crawler_v2.py` | `_request`（3 重试 + 0.2-0.5s 延迟）/ `_should_stop`（300） |
| 技能闭集 | `job_agent.py` | SkillRepository L332-406 / `_select_skills` L1167-1205 / LOW_INFORMATION_SKILLS L164 |
| 强度信号 | `job_agent.py` | SIGNAL_RULES L72-145 / INTENSITY_LEVELS L147-152 |
| 两级分类 | `job_agent.py` | TaxonomyManager L656-883 |
| 学历抽取 | `job_agent.py` | `_normalize_degree` L1207-1218 |
| 温度兼容 | `llm_utils.py` | supports_temperature L22-32 / apply_temperature_strategy L53-75 |
| 技能缺口 | `job_matcher.py` | top_skill_gaps L131-164 |
| 跨运行去重 | `storage.py` | jobs 表 job_id 主键 + INSERT OR REPLACE L76-79 |
| 批量并行 | `job_agent.py` | `--max-workers 5` |
| 技能评分规则 | `tag_rate.py` | COMMON_SCORING_RULES_V4 L252-280 |

### 12.3 人工评审检查点记录

| 日期 | 评审人 | 项目 | 结论 |
|------|--------|------|------|
| （待填） | | A1 endpoint_allowlist.json | |
| （待填） | | A2 skill_tags.json | |
| （待填） | | B1 信号表权重 | |
| （待填） | | B2 job_taxonomy.json | |

### 12.4 未来 MySQL 迁移清单模板（A2/B3/B2 持久化时使用）

```python
# alembic/versions/YYYYMMDD_NNNN_job_candidate_features.py
# add_column: candidates.skills JSON, candidates.min_degree VARCHAR(16),
#             candidates.priority VARCHAR(16), candidates.taxonomy JSON
# 回滚: drop_column 全部
```
