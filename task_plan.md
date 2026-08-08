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
