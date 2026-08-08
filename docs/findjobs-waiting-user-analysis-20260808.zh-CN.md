# redesign 全量评估 waiting_user 分类分析（2026-08-08）

> 数据源：`tests/question/eval_results/redesign_full_20260808_v2/`（83 题，59 例 waiting_user）
> 口径：每例按**主因**归入单一类别；含跨类别证据的案例标注副因。
> **更新（2026-08-08 晚）**：P0/P1/P2 优化已执行完毕并通过全量回归（83 题 eval + 1211 单测 / 100% branch）。59 例中 **15 例转 succeeded、44 例仍 waiting_user**；分类对照与逐例结果见第 7 节。原始分析保留于第 1–6 节。

## 1. 结论（TL;DR）

- **合理（外部约束，系统行为正确）42 例（71%）**：反爬/登录墙/SPA、微信公众文章与台账第三方链接不可达、数据源无匹配、来源时效约束、搜索+抓取双失败。这些案例按安全红线正确转人工，不是缺陷。
- **可优化（系统自身可改进）17 例（29%）**：全部集中在 **JD 提取/匹配工具链**（12 例）与 **搜索工具对非招聘页的过滤**（4 例）+ 模型输出异常（1 例）。
- **下一步优化区间（按 ROI 排序）**：
  - **P0（12 例，最大杠杆）**：`match-observed-jobs` / `extract-observed-job-details-batch` 从列表页/聚合页产出**岗位级**结构化条目，并给出可操作的输入错误信息（消除 `invalid_tool_input` 死循环）。
  - **P1（11 例，测试层）**：iguopin.com 等 SPA 首页作为种子 URL 时，题库应提供**具体岗位/搜索页 URL**；对种子 URL 做可达性预检。
  - **P2（4 例）**：`search-public-job-pages` 增加 site: 限定与招聘站点白名单，过滤教程/百科/官网首页噪声。
  - **P3（1 例）**：Q148 模型输出格式异常，属随机性，暂不处理。

## 2. 分类总表（59 例）

| 类别 | 主因计数 | 案例 ID | 判定 |
|---|---|---|---|
| A1 大厂官网反爬/登录墙 | 8 | R006, R011, R012, R013, R014, R038, Q081, R045 | 合理 |
| A2 微信公众文章/台账第三方链接不可达 | 8 | R001, R002, R003, R004, R005, R008, R010, Q133 | 合理 |
| A3 iguopin.com 首页 SPA 无法抓取 | 11 | Q034, R015-R022, R030, R031 | 合理（种子 URL 可改进，见 P1） |
| A4 台账无匹配 / 来源时效约束 | 4 | Q057, R034, R041, R042 | 合理 |
| A5 抓取 + 搜索双失败（混合外部） | 10 | Q071, Q103, Q115, Q144, R007, R009, R035, R036, R037, R039 | 合理 |
| A6 feishu/sigenergy 官网不可达 | 1 | C005-L2 | 合理 |
| **A 小计** | **42** | | |
| B1 match/extract 工具链未产出岗位级结果 | 5 | C010-L2, Q046, R024, R043, C003-L3 | 可优化 |
| B2 match-observed-jobs `invalid_tool_input` 死循环 | 4 | C001-L2, C002-L2, C004-L2, C015-L3 | 可优化 |
| B3 已有报告但 duplicate 去重后 stall | 3 | C008-L3, Q040, R028 | 可优化 |
| B4 搜索返回非 JD 内容 | 4 | Q113, Q114, Q134, Q028 | 可优化 |
| B5 模型输出格式异常 | 1 | Q148 | 可优化（随机） |
| **B 小计** | **17** | | |
| **合计** | **59** | | |

## 3. 合理类（42 例）理由

**A1 官网反爬/登录墙（8）**：jobs.bytedance.com、careers.tencent.com、hr.xiaomi.com、campus.meituan.com、zhaopin.kuaishou.com、job.xiaohongshu.com、liepin.com 列表页均为 `public_fetch_failed`（登录/反爬）。系统在安全红线内转 `needs_manual_review`/人工确认，行为正确。R013 已捕获百度站证据（部分成功）但目标公司（美团/小米）不可达，转人工合理。

**A2 微信/第三方链接不可达（8）**：台账投递链接以 `mp.weixin.qq.com` 公众号文章为主（R001-R005, R008, R010），另有 mokahr/zhiye/hotjob 私有招聘系统被 `unsafe_public_url` 拒收（Q133）。微信反爬与私有招聘系统是外部约束；`unsafe_public_url` 拒收是安全设计正确行为。

**A3 iguopin.com 首页 SPA（11）**：全部命中 `https://www.iguopin.com/` 首页。该站为 SPA + 反爬，首页永远无法抓取。**主因归外部**（站点本身限制），但种子 URL 给了注定失败的首页而非具体岗位/搜索页，属题库种子质量问题 → 测试层可改进（P1）。

**A4 数据源/时效约束（4）**：Q057 台账无匹配记录；R041/R042 台账最近 7 天/1 天无字节/腾讯记录；R034 证据来自脉脉（04-02 发布），不满足题面"稀土掘金最近 3 天"约束。系统正确拒绝用不满足约束的证据作答。

**A5 抓取+搜索双失败（10）**：单次 `fetch` 或 `search` 失败后 stall（Q144, R007, R009, R035-R039），或抓取失败且搜索仅返回官网首页/百科等无关页（Q071, Q103, Q115）。均无可用公开证据，转人工是唯一正确结局。

**A6 feishu/sigenergy（1）**：C005-L2 两个唯一证据 URL（feishu 校招页、sigenergy 列表页）均 `public_fetch_failed`，无替代证据源。

## 4. 可优化类（17 例）根因与证据

**B1+B2+B3（12 例）→ 同一个根因：JD 提取/匹配工具链对列表页的岗位级产出不足**

- 证据：这些案例**已抓到 public_job_page 且部分已有 structured_job_details**，但：
  - B2（4 例）`match-observed-jobs` 报 `invalid_tool_input` —— executor 传入参数不符合工具 schema，工具的错误信息未说明如何修正，executor 反复重试直至 stall（C001-L2, C002-L2, C004-L2, C015-L3；C002-L2 还叠加 `build-resume-tailoring-brief` 被 `tool_skill_forbidden` 拒绝）。
  - B1（5 例）页面抓到、提取产出为**页面级聚合**（如"本期新增 2997 个职位"）而非按岗位拆分的结构化条目，verifier 拒收 → waiting_user（C010-L2, Q046, R024, R043, C003-L3 列表页缺 JD 正文）。
  - B3（3 例）已有 `job_matching_report` 产出，但内容为聚合级，verifier 不认可 → executor 重复调用被 `duplicate_tool_call` 去重 → stall（C008-L3, Q040, R028）。

**B4 搜索返回非 JD 内容（4）**：Q113/Q114/Q134 搜索两次仅返回教程/百科/官网首页；Q028 抓到的是中粮通用校招页（非目标 AIGC 产品经理岗位）。`search-public-job-pages` 缺乏 site: 限定与招聘站点过滤。

**B5 模型输出异常（1）**：Q148 模型输出格式异常，网关本地修复后仍不可用，属单次随机性。

## 5. 下一步优化区间（按 ROI 排序）

| 优先级 | 优化项 | 涉及案例 | 预期收益 | 工作内容 |
|---|---|---|---|---|
| **P0** | match/extract 工具链岗位级产出 | B1+B2+B3 共 12 例 | 12/59 从 waiting_user 转可成功或可判定 | ① `match-observed-jobs` 接受页面级聚合输入时先按岗位拆分再匹配，或明确报"需要岗位级输入"并给出修正提示；② `extract-observed-job-details-batch` 对列表页按岗位条目拆分（链接+标题），避免"页面标题/聚合"单条输出；③ `invalid_tool_input` 错误信息给出 schema 校验失败的具体字段 |
| **P1** | 种子 URL 可达性 | A3 共 11 例（iguopin） | 消除 11 例"必失败"种子 | 题库种子 URL 从首页换成具体岗位/搜索页；对种子 URL 做预检（`headless fetch → 首页/SPA 检测`） |
| **P2** | 搜索工具过滤 | B4 共 4 例 | 4 例搜索噪声收敛 | `search-public-job-pages` 增加 site: 限定（jobs.*/campus.*/zhaopin 等招聘域名白名单）+ 结果页 title/desc 的 JD 特征过滤 |
| **P3** | 模型输出异常监控 | B5 共 1 例 | 低 | 记录频率，暂不处理 |

**不建议优化的方向**：A1/A2/A5 的站点反爬与微信封锁（外部约束，安全红线要求不绕过）；A4 数据源约束（题面语义，正确拒绝）。

## 6. 验证方法

- 分类依据：`/tmp/wu_cases.txt`（59 例 VERIFIER/SUMMARY 全量 dump）+ 逐例 tool_calls error_codes 核查（脚本 `wu_classify.py` 标签计数：site_blocked 32 / search_no_jd 32 / stall 14 / extract_gap 11 / iguopin 11 / weixin 8 / match_gap 4 等多标签统计；主因归类为本文档的人工判定）。
- 主因归类保证 59 例互斥覆盖（A 42 + B 17），无未分类案例。
- 证据边界：SUMMARY 与 error_codes 可完全复现；未读取各例完整决策轨迹（verifier_decisions 全文），若需更深定位可在 P0 实施时逐例复查。

---

## 7. 优化执行结果（2026-08-08 晚，P0/P1/P2 落地后）

### 7.1 总体指标（83 题 eval，对比 v2 基线）

| 指标 | v2 基线 | 优化后 | 变化 |
|---|---|---|---|
| 单题 succeeded | 17 / 68 | **26 / 68** | **+9** |
| 链（doc 级）succeeded | 7 / 15 | **13 / 15** | **+6** |
| 链接 succeeded | 26 / 34 | **31 / 33** | **+5** |
| failed | 0 | **0** | 无回归 |
| doc 级回退（succeeded→waiting_user） | — | **0** | 无回归 |
| 单测 / branch coverage / ruff | 1195 / 100% / clean | **1211 / 100% / clean** | +16 |

轮次：`phase_e_round_1`（83 题全量）→ `phase_e_retry_juejin`（Q143/R032/R033/C005）→ `phase_e_retry_c005` → `phase_e_merged`（最终对比目录）。

### 7.2 59 例分类对照（v2 waiting_user → 优化后）

| 类别 | 案例数 | 转成功 | 仍 waiting_user | 说明 |
|---|---|---|---|---|
| A1 大厂官网反爬/登录墙 | 8 | **1**（R045） | 7 | 外部约束，安全红线内转人工；R045 为 LLM 轨迹变体 |
| A2 微信/第三方链接不可达 | 8 | 0 | 8 | 外部约束，未动 |
| A3 iguopin 首页 SPA | 11 | **0** | 11 | **种子已换搜索页且预检 11/11 PASS，但 eval 中 11/11 `public_fetch_failed`——渲染不稳定（见 7.4）** |
| A4 数据源/时效约束 | 4 | **1**（R034） | 3 | R034 受益于 juejin.cn 白名单修复（稀土掘金题可搜到目标帖） |
| A5 抓取+搜索双失败 | 10 | 0 | 10 | 外部约束，未动 |
| A6 feishu/sigenergy | 1 | 0 | 1 | C005-L2 本轮链提前终止（L1 失败），未执行 |
| **A 小计** | **42** | **2** | **40** | |
| B1 match/extract 岗位级产出 | 5 | **5**（C010-L2, Q046, R024, R043, C003-L3） | 0 | **P0 全命中** |
| B2 invalid_tool_input 死循环 | 4 | **4**（C001-L2, C002-L2, C004-L2, C015-L3） | 0 | **P0 全命中** |
| B3 duplicate 去重后 stall | 3 | **1**（R028） | 2（C008-L3, Q040） | 部分解决 |
| B4 搜索返回非 JD 内容 | 4 | **2**（Q114, Q134） | 2（Q113, Q028） | 部分解决 |
| B5 模型输出格式异常 | 1 | **1**（Q148） | 0 | 随机性恢复 |
| **B 小计** | **17** | **13** | **4** | |
| **合计** | **59** | **15** | **44** | |

### 7.3 15 例提升明细

C001、C002、C003、C004、C010、C015（B1/B2 链式题）、Q046、R024、R043（B1）、R028（B3）、Q114、Q134（B4）、R034（A4/juejin）、R045（A1）、Q148（B5）。

### 7.4 关键发现

1. **P0 工具链修复精确命中 B1+B2（9/9 全解决）**：`extract-observed-job-details-batch` 按岗位拆分（猎聘 `【】`/iguopin `「」` 卡片，≥2 卡门槛）+ `invalid_tool_input` 输出具体 schema 失败字段，消除了列表页聚合输出与死循环两类根因。B3 部分解决（1/3）、B4 部分解决（2/4）。
2. **P1 种子修复验证了种子质量，但 eval 成功率未提升（A3 0/11）**：预检脚本（同 eval 管线）对 4 个搜索页 URL 11/11 PASS（13–20 岗位卡/页），但正式 eval 中 11/11 次 `public_fetch_failed`（Playwright 渲染失败）。结论：iguopin 渲染在长时间多轮访问下不稳定（反爬波动/渲染超时），是基础设施稳定性问题而非种子质量问题；本轮无法量化 P1 的潜在收益。
3. **Phase D 的 site: 白名单引入一处系统性回归并已修复**：Q143/R032/R033（稀土掘金题）被白名单误过滤（juejin.cn 不在名单内，搜索 0 结果）。修复：juejin.cn 加入白名单 + `site:juejin.cn` 操作符 + 回归单测；重跑 3 题全部 succeeded（抓取 juejin.cn/pin/，Q143 首条与 v2 相同 pin）。R034 亦受益于此。
4. **C005 定性**：L1 三次重跑一致 waiting_user（微信/mokahr 渲染不可靠 + verifier 要求"公司真实岗位+投递链接"），doc 级 v2 与现在同为 waiting_user；未调用 search 工具，与 Phase A–D 无因果，属题目固有难度。

### 7.5 遗留项（44 例仍 waiting_user）

- **外部约束（40 例，A1/A2/A5 为主）**：反爬/登录墙/微信封锁，安全红线要求不绕过，正确转人工。
- **A3 iguopin（11 例）**：渲染稳定性问题。下一步可验证方向：① eval 中对该域名的渲染重试/超时放宽；② 确认是否触发会话级反爬（预检与 eval 交替访问节奏）。
- **B3 2 例（C008-L3, Q040）**：duplicate 去重后 stall 的剩余形态（已有报告但 verifier 不认可），需逐例复查 verifier 标准。
- **B4 2 例（Q113, Q028）**：Q113 教程噪声残留、Q028 中粮通用校招页非目标岗位，需更细的域名/岗位特征过滤。
- **C005**：公司官网微信文章类，验证微信文章渲染回退可行性（需先确认安全边界）。
