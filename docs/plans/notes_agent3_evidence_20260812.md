# Agent 3 证据获取审计笔记（2026-08-12）

## 证据路径

- 最新评测汇总：`docs/83-question-comparison-report-2026-08-12.md`
- 两轮全量边界：`docs/83-question-full-eval-report-2026-08-12.md`
- 最新结果目录：`tests/question/eval_results/lazy_jd_full_20260812_4p_staggered_run3/`
- 抓取实现：`backend/app/services/career_skills/job_discovery.py`
- Sheet 实现：`backend/app/services/career_skills/career_sheets.py`
- Executor 门禁：`backend/app/services/agent_runtime/executor_agent.py`
- Skill 工具契约：`backend/app/services/career_skills/registry.py`、`skill/job-discovery/SKILL.md`

## 已核对事实

1. 报告口径：4 进程、每 60 秒错峰的顶层结果为 `58/83 succeeded、24/83 waiting_user、1/83 failed`；前一轮 8 进程错峰为 `30/83、52/83、1/83`。
2. 逐条展开最新 run3 的 83 个顶层题目后得到 100 个链路单元：`75 succeeded、24 waiting_user、1 failed`。
3. 链路层工具累计统计：
   - `query-career-sheet-records`：28 个单元调用，成功 0、失败 0（结果统计不把空返回作为失败）。
   - `search-public-job-pages`：16 个单元调用，成功 0、失败 3；错误为 `candidate_urls_already_supplied` 2、`duplicate_tool_call` 1。
   - `fetch-public-job-pages`：成功 742、失败 7；错误为 `duplicate_tool_call` 3、`tool_skill_forbidden` 1、`invalid_tool_input` 1。
   - `fetch-public-job-page`：成功 4、失败 1；`public_page_content_insufficient` 1。
   - `match-observed-jobs`：成功 40、失败 19；`invalid_tool_input` 4、`tool_skill_forbidden` 2、`duplicate_tool_call` 3。
4. 最新 run3 的 24 个 waiting_user 中，报告人工归类为证据不可用/数据源不满足的有 19 个；其余主要是工具契约、Skill 越权/重复、`target_evidence_not_found` 和 wall-clock。
5. `job_discovery.py` 当前抓取顺序为：微信 OCR 路由 -> 已认证公开 API adapter -> requests + 手动公网 URL 重定向校验 -> 可选 Playwright；JS card-list 可按同域职位详情链接最多展开 5 个详情页。
6. `_build_evidence_page` 把访问控制先分类为 `anti_bot_challenge`/`access_denied`/`rate_limited`，再分类 `empty_public_page`、`dead_link`、`public_page_content_insufficient`；低于 160 个可见字符拒绝成为证据。
7. `candidate_urls` 门禁只在每个候选都属于 `public_fetch_failed`、`empty_public_page`、`public_page_content_insufficient` 或 `dead_link` 时授权公开搜索；登录墙、CAPTCHA、anti-bot 不会授权搜索。成功抓到“列表/总览页但没有目标 JD”的候选不会进入失败集合，因此仍会阻止搜索。
8. 最新 Q040 明确复现了第 7 点：种子 URL 是猎聘产品经理专区，页面抓取成功但只得到社招列表/无目标 JD；重规划后的公开搜索被 `candidate_urls_already_supplied` 拒绝，最终 waiting_user。
9. 最新 R010、R003、R004、Q115、Q144 的摘要显示 sheet 返回的 20 条候选大多为微信文章，`wechat_ocr_failed` 或无正文；Q115/Q144 同时出现公开搜索三次空结果。
10. 最新 R042 显示 sheet 查询 0 条（扫描 721 条），官方页仅社招；这属于“权威台账无匹配 + 官方来源不满足目标条件”，不是反爬成功。
11. Sheet 实现对 `sheet_rate_limited` 不重试，对其他 bridge/解析/超时问题最多重试 1 次；工具描述声明 sheet 失败后允许 `search-public-job-pages`。

## 关键推断

- 公开搜索当前成功率为 0，不能只归因于搜索质量：`candidate_urls` 仍把语义不足但技术抓取成功的候选视为“尚未失败”，安全门禁因此会压制合法的来源降级。
- “抓到了页面”与“拿到了可用于下游的 JD”是两个不同证据状态。门禁应继续禁止盲目绕过，但需要承认 `content_insufficient`、`list_only`、`wrong_population` 等确定性质量失败。
- Sheet 的空匹配不是异常；当前统计没有将空结果作为结构化观察，导致 Executor 只能从文本摘要推断是否应该降级，容易重复查询或错误等待。
- Agent 3 的差异化重点不是放松安全边界，而是建立“来源路由 + 证据质量状态机”：同一目标可从台账、官方公开页、官方公开搜索结果页、公开职位聚合页和用户提供 URL 依次取得可核验候选，但每次跨来源必须留下失败原因和证据等级。
