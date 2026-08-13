# Agent 3 独立方案：证据获取与反爬/来源降级优先

日期：2026-08-12  
范围：只读方案，不修改代码。

## 结论

当前主要瓶颈不是“模型不会匹配”，而是“可访问页面没有被转换成满足下游要求的 JD 证据”。最新 4 进程错峰评测为 `58/83 succeeded、24/83 waiting_user、1/83 failed`；展开后的 100 个链路单元中，`query-career-sheet-records` 累计 28 次调用但 0 次成功，`search-public-job-pages` 16 个单元中 0 次成功、3 次被拒/去重。报告将 24 个 `waiting_user` 中 19 个归为证据或数据源不可用。

我建议采用“来源路由 + 证据质量状态机 + 一次性有界批量降级”。它与当前偏 runtime/Skill 解耦、匹配输入契约修复的方向不同：Agent 3 的主指标是**从一次来源失败到下一条合法公开来源的转移是否发生**，而不是继续增加重试或降低安全门槛。

## 一、当前失败结构

### 1. 反爬/登录墙：必须终止原站访问

`job_discovery.py` 已在重定向链上重新校验公网 URL，并优先识别 `anti_bot_challenge`、`access_denied`、`rate_limited`；猎聘安全页不会再进入 Playwright。当前 C008-L3 明确是猎聘登录墙，不能通过无状态重试解决。

方案判断：反爬不是普通网络失败，不应加入“再试一次”队列；但“原站终止”不等于“整个岗位目标终止”。可以转向同一公司的其他公开招聘域名或其他公开来源，但不得再请求被阻断 host、不得使用代理/镜像绕过验证、不得把搜索摘要当 JD。

### 2. JS shell / 列表页：抓取成功不等于证据成功

当前已有 requests → Playwright 回退，以及同域职位详情链接最多展开 5 页的机制。问题在于：页面可能能渲染、能返回列表或总览，但没有岗位职责/任职要求正文。Q040 就是典型：猎聘专区页技术上成功，却只有社招列表，目标“北京 AIGC 产品经理（应届生）”没有完整 JD。

应将以下状态分开：

- `jd_complete`：有岗位名、职责/要求等可供下游使用的正文。
- `list_only`：有职位卡片/总览，但无目标详情正文。
- `js_shell`：渲染后仍只有启动壳、导航或极短文本。
- `wrong_population`：页面有 JD，但城市、校招/社招、岗位方向不满足目标。
- `empty_public_page` / `dead_link`：空页或软 404。

只有 `jd_complete` 能满足 `job-matching`、`resume-tailoring`、`career-planning` 的证据输入；`list_only` 和 `wrong_population` 应触发来源降级，而不是继续重复提取。

### 3. 空结果：必须区分“没有结果”和“没有继续路线”

Q115、Q144 同时出现微信 OCR 失败和公开搜索三次空结果；R042 是 sheet 最近 1 天无记录、官方页只有社招；Q113/R034 等则进入下游时发现 `observed_public_evidence` 为空。当前空结果虽被如实反馈，但没有一个结构化的“已尝试来源矩阵”，Executor 容易重复同一查询，最后以通用 `waiting_user` 结束。

空结果应产生结构化结论：查询条件、已尝试来源、每个来源的结果数、是否属于目标人群、下一条允许路线、是否已达到终止条件。

### 4. `candidate_urls` 门禁：安全正确，但把“语义无效”当成“仍可用”

当前搜索授权条件是：所有用户候选 URL 都出现 `public_fetch_failed`、`empty_public_page`、`public_page_content_insufficient` 或 `dead_link`。`anti_bot`/登录墙不会授权搜索，这是为了不把阻断页伪装成可搜索条件。

漏洞在另一侧：一个候选 URL 如果成功抓到列表页，但没有目标 JD，它既不是失败 URL，也不是可用 JD，于是永久阻止 `search-public-job-pages`。Q040 的种子 URL 就触发了 `candidate_urls_already_supplied`，重规划后仍无法走独立公开来源。

建议把门禁拆为两个判断：

1. `candidate_fetch_complete`：候选是否已经被抓取或明确阻断；避免重复抓取。
2. `candidate_evidence_usable`：是否形成满足目标条件的 JD；决定是否还需要来源降级。

安全不变量保持不变：阻断 host 进入 circuit breaker，原 host 不再访问；搜索只能走“其他公开来源”策略，且搜索结果必须重新经过公网 URL 校验和正文证据校验。这样是来源降级，不是绕过反爬。

## 二、Agent 3 目标架构：来源路由状态机

### 1. 来源优先级

对每个公司/岗位目标建立独立 `SourceAttempt` 记录，按以下顺序一次性尝试：

1. 用户给出的精确 JD URL。
2. Tencent Smartsheet 的记录：只作为候选 URL 和先验元数据，不把表格行直接当完整 JD。
3. 同一公司的官方公开招聘入口：`careers`、`campus`、`jobs` 等已知公开域名；必要时使用已人工审核的公开 JSON adapter。
4. 受限范围的公开搜索：优先官方域名，其次公开招聘平台/社区；只把结果 URL 当候选，必须再次抓取正文。
5. 用户提供的 JD 文本或新 URL。

搜索路由必须带 `fallback_scope`：

- `official_other_host`：原 host 被阻断时，仅搜索同一公司其他公开 host。
- `public_alternate_sources`：列表页/错误人群/空结果后，搜索其他公开来源。
- `same_host_retry`：默认禁止，只有 transient transport 才允许一次有界重试。

### 2. 失败分类契约

建议所有来源尝试统一输出以下字段，而不是只返回字符串错误码：

```text
source_url              # 工具实际尝试的 URL；不接受模型自报 URL 作为证据
source_kind             # sheet / official / public_search / job_board / wechat
stage                   # query / fetch / render / extract / qualify
status                  # succeeded / terminal_failure / retryable_failure
error_code              # 稳定分类码
retryable               # 是否允许同一路线再次尝试
search_authorized       # 是否允许切换到另一公开来源
next_route              # official_other_host / public_alternate_sources / user
evidence_refs           # 仅包含工具产出的 artifact_id/source_url/content_hash
quality                 # jd_complete / list_only / js_shell / wrong_population / empty
```

错误码分组建议：

| 分组 | 码 | 处理 |
|---|---|---|
| 访问阻断 | `anti_bot_challenge`、`access_denied`、`login_required`、`captcha`、`rate_limited` | 原 host 终止；不得重试；可按策略转其他公开 host |
| 传输暂态 | `public_fetch_failed`、`timeout`、`dns_error`、`redirect_loop` | 同 URL 最多一次重试，然后进入来源降级 |
| 内容质量 | `empty_public_page`、`js_shell`、`list_only`、`public_page_content_insufficient`、`dead_link` | 不作 JD 证据；允许其他来源 |
| 语义不匹配 | `wrong_population`、`no_matching_role`、`stale_evidence` | 不作目标岗位证据；允许放宽条件或切换来源 |
| Sheet | `sheet_empty`、`sheet_rate_limited`、`sheet_call_failed`、`sheet_bridge_unavailable` | 不重复 sheet；转公开来源 |
| 搜索 | `search_empty`、`search_provider_failed`、`unsafe_search_result` | 更换一次查询/提供方；达到上限后请求用户 |
| 证据链 | `target_evidence_not_found`、`evidence_not_found` | 停止下游调用，回到 discovery 或请求明确 JD |

### 3. 有界批量策略

- Sheet 每个查询最多读取当前既定扫描上限；`matched_count=0` 是有效的 `sheet_empty`，不是异常，也不应重复调用同一查询。
- 对返回的 URL 先做 host/来源分类和去重，再批量抓取；同一 host 限制并发，建议每 host 1–2 个、全局 4 个，并保留输入顺序。
- 20 条微信记录不要连续 OCR 同一失败路线。先按公司去重，每家公司最多选择 1–2 条文章；同一公司 OCR 连续失败后，进入该公司的官方公开入口路由。
- 列表页渲染后，优先同域详情链接展开；详情页仍无 JD 时立刻标记 `list_only`，不重复 extract。
- 搜索最多两个提供方/三个查询变体；结果 URL 仍需重新抓取，搜索摘要永远不能直接持久化为 JD。
- 对成功 evidence 以 `content_hash` 去重；对 terminal failure 以 `(canonical_url, error_code, stage)` 去重，避免 verifier retry 再次请求。

## 三、Skill 契约建议

### `job-discovery` 输出契约

`job-discovery` 不应只承诺“抓取工具成功”，而应交付 `DiscoveryReport`：

- `target`: 公司、岗位、城市、校招/社招、时间窗。
- `candidates[]`: `candidate_id`、标题、公司、地点、招聘类型、质量状态。
- `evidence_refs[]`: 工具产生的 `artifact_id`、`source_url`、`content_hash`。
- `attempts[]`: 每个来源的 stage、稳定错误码、重试次数、下一路线。
- `coverage`: 已尝试公司数、来源数、目标条件覆盖情况。
- `terminal_reason`: `jd_found`、`no_public_match`、`blocked_source_with_alternatives_exhausted` 或 `user_input_required`。

完成条件：

- 需要下游 JD 时，至少一个 `jd_complete` 候选且证据指针可解析；
- 允许输出否定结论时，必须有明确的来源覆盖和 `no_public_match`，不能用单个空页推出“没有岗位”；
- 只有 `list_only`、`wrong_population`、`search_empty` 或 sheet 空结果时，不得进入匹配/定制/准备工具；应先执行下一 `next_route` 或如实请求用户。

### 与下游 Skill 的指针契约

`candidate_id`、`artifact_id`、`source_artifact_id`、`source_url` 必须在工具边界解析到同一持久化 JD。下游只接受 `jd_complete` 候选；如果目标指针无法解析，返回 `target_evidence_not_found` 并阻止后续 tool call，不让 Executor 在错误 Skill 中继续找 JD。

## 四、测试与验收

### P0：门禁和失败分类单测

- 候选 URL 抓取成功但 `list_only`：不重复 fetch，允许 `public_alternate_sources` 搜索。
- 候选 URL 是 `wrong_population`：允许切换来源，但不能把该列表页当目标 JD。
- 候选 URL 是 `anti_bot`/登录墙：原 host 被 circuit-breaker；同 host 搜索和重试均拒绝；若策略允许，只能搜索其他公开 host。
- 候选集合中一条 `jd_complete`、一条 `dead_link`：不触发无意义搜索，下游只消费完整证据。
- 候选集合中存在未处理 URL：搜索仍禁止，直到每个候选有 `succeeded`、质量终态或阻断终态。

### P0：来源路由单测

- `sheet_empty`、`sheet_rate_limited`、`sheet_call_failed` 各只调用 sheet 一次，并最多产生一次公开搜索降级。
- 20 条微信 URL 中 19 条 OCR 失败、1 条可用：保留可用 evidence，不因失败项丢弃整批。
- Bing/360 均空：输出 `search_empty`、已尝试提供方和用户下一步，不再循环。
- JS 列表页有同域职位详情链接：最多展开上限，详情失败逐 URL 记录，不把列表页升级成 JD。
- 详情页有访问控制标记：不进入 Playwright fallback，不被分类为普通短页。

### P1：链路回放

固定重放最新 run3 中的 `Q040、C008-L3、R010、R003、R004、Q115、Q144、R042、Q113、R013`，以及成功样本 `C010-L2、R005`。验收不以“所有题目 succeeded”为唯一标准，而看：

- Q040 能从猎聘列表页的 `list_only/wrong_population` 转到其他公开来源，且不再次访问同一猎聘 host；
- C008-L3 保持登录墙安全终止，不出现 bypass 或同 host 重试；
- R010/R003/R004 对微信失败生成按公司聚合的官方来源降级，不重复 OCR 同一 URL；
- R042 的 sheet 空结果最多一次查询，并给出官方/公开搜索覆盖记录；
- Q113/Q034 在搜索确实为空时输出结构化 `no_public_match`，而不是三个相同搜索调用后才泛化等待；
- 所有进入下游的候选都能由 `candidate_id → artifact_id → source_url/content_hash` 回溯到同一 JD。

### P1：低并发 live smoke

公网 live 测试单独运行，使用 1–4 个进程、同 host 冷却和独立结果目录；不把猎聘、微信、DeepSeek 波动混入 runtime 契约回归。至少记录：每 host 请求数、`jd_complete/list_only/blocked` 数量、fallback 转移次数、重复调用次数和 wall-clock。

## 五、明确不做的事

- 不降低 `_MIN_USABLE_TEXT_CHARS`，不把列表/总览页冒充 JD。
- 不对 CAPTCHA、登录墙、anti-bot 做自动点击、指纹伪装、代理绕过或无限重试。
- 不把搜索 snippet、sheet 文案或模型生成的岗位正文作为公开 JD 证据。
- 不自动扩展未经人工审核的公开 API adapter allowlist。
- 不用“所有候选都被阻断”作为同 host 搜索授权；只允许有明确 `fallback_scope` 的其他公开来源路由。

## 最小实施顺序

1. 先实现并验证 `SourceAttempt/quality/next_route` 的失败分类和审计输出。
2. 再修正 `candidate_urls` 门禁：把 `list_only/wrong_population` 与“仍待抓取”分开；保留 blocked host 硬阻断。
3. 接入 sheet 空结果、sheet 失败、微信批量失败的统一降级路由。
4. 最后做 Q040/R010/Q115/Q144/R042 的固定夹具回放，再做低并发 live smoke。

