# full83_single_20260814_215826 28 个非成功用例画像

## C002  [waiting_user] err= rc=adapter:empty_result role=executor/execution resumable=True
Q: null
seeds: 1 | 
wall=198.7s turns=1 tools=1 artifacts=1 (x1)
steps: ::
tools: =0/0
verifier(last3): 
summary: 连续或累计多次工具调用未取得进展，请人工确认岗位证据后重试。

## C007  [waiting_user] err= rc=need_user role=executor/execution resumable=True
Q: null
seeds: 1 | 
wall=125.9s turns=1 tools=1 artifacts=1 (x1)
steps: ::
tools: =0/0
verifier(last3): 
summary: 工具未产生可核验的交付物，当前总结不能视为完成。请提供可公开访问的岗位页面或补充必要信息后重试。

## C008  [waiting_user] err= rc=need_user role=executor/execution resumable=True
Q: null
seeds: 1 | 
wall=219.7s turns=1 tools=1 artifacts=1 (x1)
steps: ::
tools: =0/0
verifier(last3): 
summary: 匹配报告未使用已确认的简历事实（confirmed_profile_facts 缺失），无法验证匹配的透明性和完整性。需要用户提供已确认的简历事实或授权访问简历，才能完成基于简历的匹配排序。

## C010  [waiting_user] err= rc=need_user role=executor/execution resumable=True
Q: null
seeds: 1 | 
wall=127.1s turns=1 tools=1 artifacts=1 (x1)
steps: ::
tools: =0/0
verifier(last3): 
summary: 结构化提取未满足 contract：extract-observed-job-details-batch 仅返回占位标题，未产出含 title/company_name/locations/requirements 的 candidate；唯一有效 candi...

## C011  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: null
seeds: 1 | 
wall=118.8s turns=1 tools=1 artifacts=1 (x1)
steps: ::
tools: =0/0
verifier(last3): 
summary: 猎聘网（liepin.com）前端开发工程师专区页面 https://www.liepin.com/zpqiandongruanjiankaifagongchengshi/ 被站点反爬验证码（anti-bot captcha）阻断，无法抓取页面证据。由于您明确...

## C015  [waiting_user] err= rc=adapter:empty_result role=executor/execution resumable=True
Q: null
seeds: 1 | 
wall=136.7s turns=1 tools=1 artifacts=1 (x1)
steps: ::
tools: =0/0
verifier(last3): 
summary: 连续或累计多次工具调用未取得进展，请人工确认岗位证据后重试。

## Q011  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 针对岗位“前端开发工程师”帮我定制简历，突出Vue3、TypeScript、Vite相关经历。
seeds: 1 | liepin 前端 role landing page (5523ch, real JDs)
wall=100.7s turns=2 tools=3 artifacts=2 (job_search_resultsx1, public_job_pagex1)
steps: 1:job-discovery:failed | 2:resume-tailoring:not_started
tools: fetch-public-job-page=1/0 fetch-public-job-pages=2/0 query-career-sheet-records=0/0
verifier(last3): 
summary: 模型未返回可解析的终态，但当前步骤的交付证据尚未满足完成契约。请补充岗位正文或重试。

## Q013  [waiting_user] err= rc=need_user role=executor/execution resumable=True
Q: 针对LLM 应用工程师岗位，给出简历修改建议（改动点+原因）。
seeds: 1 | liepin LLM-dev landing page (sgcc seeds removed)
wall=85.1s turns=4 tools=5 artifacts=22 (public_job_pagex16, structured_job_detailsx4, job_search_resultsx2)
steps: 1:job-discovery:succeeded | 2:job-discovery:succeeded | 3:resume-tailoring:failed
tools: extract-observed-job-details-batch=6/0 fetch-public-job-page=0/1 fetch-public-job-pages=24/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 目标岗位是「LLM 应用工程师」，但当前可用的 JD 证据与目标岗位不匹配：现有抓取到的职位详情分别是苏宁的「工程经理」（土木工程/施工管理方向）和 CVTE 的「SU-技术支持工程师（镭晨）」，均非 LLM 应用工程师岗位。请提供目标 LLM 应用工程师岗位...

## Q028  [waiting_user] err= rc=need_user role=executor/execution resumable=True
Q: 最近3天学校就业指导中心官网/就业信息网适合我的AIGC 产品经理（应届生）岗位有哪些？为最匹配的岗位定制简历。
seeds: 2 | campus job pages (probe-verified)
wall=119s turns=7 tools=5 artifacts=95 (public_job_pagex59, structured_job_detailsx34, job_matching_reportx1, job_search_resultsx1)
steps: 1:job-discovery:succeeded | 2:job-matching:succeeded | 3:resume-tailoring:failed
tools: extract-observed-job-details-batch=39/0 fetch-public-job-page=1/0 fetch-public-job-pages=62/0 match-observed-jobs=1/0 query-career-sheet-records=0/0
verifier(last3): ,
summary: 模型未返回可解析的终态，但当前步骤的交付证据尚未满足完成契约。请补充岗位正文或重试。

## Q046  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 收集猎聘（中高端招聘平台）最近7天岗位→按匹配度筛选→为胜出岗位给面试建议。
seeds: 1 | liepin 后端 role landing page
wall=44.6s turns=2 tools=3 artifacts=4 (job_search_resultsx4)
steps: 1:job-discovery:failed | 2:job-matching:not_started | 3:career-planning:not_started
tools: fetch-public-job-page=0/1 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 猎聘公开页面被站点反爬/验证码（anti_bot_challenge）阻断，且该域名已被暂停访问；招聘 smartsheet 中也没有匹配的猎聘 Java 后端岗位记录（近7天）。我无法绕过验证码或反爬机制。请提供以下任一替代方案：
1) 一个可直接访问的猎聘...

## Q055  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 针对“前端开发工程师”岗位，给我一份面试准备计划。
seeds: 1 | liepin 前端 role landing page (aggregator)
wall=117.8s turns=2 tools=3 artifacts=2 (job_search_resultsx1, public_job_pagex1)
steps: 1:job-discovery:failed | 2:job-discovery:not_started | 3:career-planning:not_started
tools: fetch-public-job-page=1/2 fetch-public-job-pages=2/0 query-career-sheet-records=0/0
verifier(last3): 
summary: 猎聘网的目标岗位页面（https://www.liepin.com/zpqiandongruanjiankaifagongchengshi/）被站点反爬验证码阻断，无法抓取到完整 JD。smartsheet 台账返回了匹配记录（中国电信天翼云 27 届秋招），...

## Q057  [waiting_user] err=wall_clock_budget_exhausted rc=wall_clock_budget_exhausted role=executor/execution resumable=True
Q: LLM 应用工程师面试前我该如何准备？输出详细计划。
seeds: 0 | no seeds (search/degrade under test)
wall=305.1s turns=3 tools=6 artifacts=30 (public_job_pagex20, job_search_resultsx5, structured_job_detailsx5)
steps: 1:job-discovery:succeeded | 2:job-discovery:succeeded | 3:career-planning:failed
tools: extract-observed-job-details=1/0 extract-observed-job-details-batch=4/0 fetch-public-job-page=0/2 fetch-public-job-pages=40/0 query-career-sheet-records=0/0 search-public-job-pages=0/2
verifier(last3): 
summary: 运行时间预算耗尽（模型响应偏慢），该步骤尚未完成。恢复运行将获得新的时间窗口继续。

## Q071  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 我的简历如何调整才能匹配“前端开发工程师”这个岗位？
seeds: 0 | no seeds (search/degrade under test)
wall=112.5s turns=2 tools=4 artifacts=5 (job_search_resultsx4, public_job_pagex1)
steps: 1:job-discovery:failed | 2:resume-tailoring:not_started
tools: fetch-public-job-page=1/0 fetch-public-job-pages=2/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 模型未返回可解析的终态，但当前步骤的交付证据尚未满足完成契约。请补充岗位正文或重试。

## Q115  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 前端开发工程师面试前我该如何准备？输出详细计划（先找到一份该岗位的公开 JD 作为依据）。
seeds: 0 | no seeds (search/degrade under test)
wall=133.8s turns=2 tools=4 artifacts=5 (job_search_resultsx4, public_job_pagex1)
steps: 1:job-discovery:failed | 2:job-discovery:not_started | 3:career-planning:not_started
tools: fetch-public-job-page=1/3 fetch-public-job-pages=2/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 连续或累计多次工具调用未取得进展，请人工确认岗位证据后重试。

## Q134  [waiting_user] err= rc=access_denied role=executor/execution resumable=True
Q: 我的简历如何调整才能匹配“Java 后端开发工程师”这个岗位？请先找到一份该岗位的公开 JD 再给出定制建议。
seeds: 0 | no seeds (search/degrade under test)
wall=84.9s turns=3 tools=4 artifacts=5 (job_search_resultsx4, public_job_pagex1)
steps: 1:job-discovery:failed | 2:job-matching:not_started | 3:resume-tailoring:not_started
tools: fetch-public-job-page=1/2 fetch-public-job-pages=2/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 连续或累计多次工具调用未取得进展，请人工确认岗位证据后重试。

## R013  [waiting_user] err=replan_budget_exhausted rc=replan_budget_exhausted role=runtime/planning resumable=True
Q: 百度、美团、小米哪个大厂最近有适合我的 AIGC 产品经理（应届生）校招岗位？请逐一核实岗位并给出投递建议。
seeds: 0 | no seeds (search/degrade under test)
wall=189.6s turns=9 tools=4 artifacts=11 (public_job_pagex6, job_search_resultsx3, structured_job_detailsx2)
steps: 1:job-discovery:skipped | 2:job-discovery:skipped
tools: extract-observed-job-details-batch=3/0 fetch-public-job-pages=16/0 query-career-sheet-records=0/0 search-public-job-pages=0/2
verifier(last3): ,
summary: step discover_meituan_jd requires context input '美团校园招聘页面 URL'

## R021  [waiting_user] err= rc=need_user role=executor/execution resumable=True
Q: 中国移动、中国联通有没有适合我的 Java 后端开发工程师（社招）岗位？请通过国聘网/官网等公开渠道核实。
seeds: 1 | iguopin Java 后端搜索页 (render-verified job cards)
wall=181.2s turns=8 tools=3 artifacts=26 (public_job_pagex14, structured_job_detailsx10, job_search_resultsx2)
steps: 1:job-discovery:failed | 2:job-discovery:not_started
tools: extract-observed-job-details-batch=10/0 fetch-public-job-pages=24/0 query-career-sheet-records=0/2
verifier(last3): ,,
summary: 当前步骤 discover-jobs 的 success_criteria 要求至少获取一个中国移动或中国联通岗位的 jd_complete 页面工件，带 source_url 和 content_hash。已获取多个 jd_complete 页面（如 211...

## R024  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 猎聘网上找最近7天发布的 Java 后端开发工程师（3 年经验）岗位，筛选出最适合我的一个。
seeds: 1 | liepin 后端 role landing page
wall=119.2s turns=2 tools=3 artifacts=4 (job_search_resultsx4)
steps: 1:job-discovery:failed | 2:job-matching:not_started
tools: fetch-public-job-page=0/1 query-career-sheet-records=0/0 search-public-job-pages=0/1
verifier(last3): 
summary: 猎聘网（liepin.com）的公开招聘页面被站点的反爬验证码（anti-bot captcha）阻断，且该域名在当前运行中已被临时暂停访问，无法获取猎聘网上的 Java 后端开发工程师岗位 JD。公开搜索也未返回猎聘网的直接招聘链接（仅返回了智联招聘和 BO...

## R025  [waiting_user] err= rc=need_user role=planner/planning resumable=True
Q: 猎聘网上找广州/深圳的前端开发工程师（2 年经验）岗位，并为最匹配的岗位定制针对性简历。
seeds: 1 | liepin 前端 role landing page
wall=110.1s turns=10 tools=4 artifacts=15 (structured_job_detailsx6, public_job_pagex5, job_search_resultsx4)
steps: 1:job-discovery:succeeded | 2:job-discovery:skipped | 3:job-matching:not_started | 4:resume-tailoring:not_started
tools: extract-observed-job-details-batch=6/0 fetch-public-job-pages=5/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): ,
summary: 猎聘网直接抓取被访问控制阻断，且已尝试的第三方来源（gobasearcher.com、sunsharer.cn、haolietou.com）均非猎聘网且未产出可用 JD。请提供一个可访问的猎聘网前端开发工程师（广州/深圳，2年经验）岗位详情页 URL，以便继续...

## R027  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 猎聘网后端开发工程师专区找上海的 Java 后端开发工程师（3 年经验）岗位并汇总。
seeds: 1 | liepin 后端 role landing page
wall=68.4s turns=3 tools=3 artifacts=6 (job_search_resultsx4, public_job_pagex2)
steps: 1:job-discovery:succeeded | 2:job-discovery:failed
tools: fetch-public-job-pages=2/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 猎聘网候选 URL（https://www.liepin.com/zphouduanjavakaifagongchengshi/）被站点反爬验证码（anti_bot_challenge）阻断，属于公开证据访问限制，无法绕过。当前唯一可用的页面证据是来自中国电信...

## R038  [waiting_user] err=verification_failed rc=verification_failed role=verifier/verification resumable=True
Q: 在腾讯招聘官网搜索 Web 前端工程师岗位（校招/社招）并汇总。
seeds: 0 | no seeds (search/degrade under test)
wall=225.5s turns=9 tools=5 artifacts=31 (public_job_pagex17, structured_job_detailsx11, job_search_resultsx3)
steps: 1:job-discovery:succeeded | 2:job-discovery:failed
tools: extract-observed-job-details=3/0 extract-observed-job-details-batch=14/0 fetch-public-job-pages=19/0 query-career-sheet-records=0/0 search-public-job-pages=0/1
verifier(last3): ,,
summary: Verifier 判定当前产出不满足任务要求：当前步骤 normalize_job_details 的 success_criteria 要求对每个 quality=jd_complete 的页面 artifact 调用 extract-observed-jo...

## R040  [waiting_user] err= rc=adapter:empty_result role=executor/execution resumable=True
Q: 在百度招聘（talent.baidu.com）搜索 AI 算法工程师（应届生）岗位并汇总。
seeds: 7 | baidu talent role-matched JDs
wall=100.5s turns=3 tools=5 artifacts=13 (public_job_pagex9, job_search_resultsx3, structured_job_detailsx1)
steps: 1:job-discovery:succeeded | 2:job-discovery:failed | 3:job-discovery:not_started
tools: fetch-public-job-page=0/2 fetch-public-job-pages=11/0 fetch-wechat-article=2/0 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): 
summary: 百度招聘（talent.baidu.com）的 GRADUATE 岗位详情页无法抓取：4 个目标 URL（4f1cbc80、74d83772、3287bb6a、6f9c3a86）均返回 adapter:empty_result（JS 渲染页面无可见正文），无法...

## R041  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 字节跳动 2026 秋招的 AIGC 产品经理（应届生）岗位，招聘数据源中最近7天更新过相关信息吗？请核实并验证投递链接。
seeds: 0 | no seeds (search/degrade under test)
wall=181.1s turns=14 tools=5 artifacts=52 (public_job_pagex34, structured_job_detailsx13, job_search_resultsx5)
steps: 1:job-discovery:succeeded | 2:job-discovery:succeeded | 3:job-discovery:failed | 4:job-discovery:not_started
tools: extract-observed-job-details-batch=19/0 fetch-public-job-page=1/0 fetch-public-job-pages=37/0 query-career-sheet-records=0/0 search-public-job-pages=0/1
verifier(last3): ,
summary: 在招聘数据源中未找到字节跳动「AIGC 产品经理（应届生）」岗位的精确匹配。已核实的情况如下：

1. 招聘 smartsheet（内推/招聘台账）按「字节跳动 + AIGC 产品经理 + 最近7天」过滤返回 0 条记录。
2. 字节跳动官方校招官网（jobs...

## R042  [waiting_user] err=wall_clock_budget_exhausted rc=wall_clock_budget_exhausted role=planner/planning resumable=True
Q: 招聘数据源中最近1天更新的公司里，腾讯发布的校招信息有适合我的AI 算法岗位吗？请核实岗位与投递链接。
seeds: 0 | no seeds (search/degrade under test)
wall=300.1s turns=15 tools=4 artifacts=89 (structured_job_detailsx45, public_job_pagex41, job_search_resultsx3)
steps: 1:job-discovery:succeeded | 2:job-discovery:succeeded | 3:job-discovery:skipped
tools: extract-observed-job-details-batch=58/0 fetch-public-job-pages=43/1 query-career-sheet-records=0/0 search-public-job-pages=0/0
verifier(last3): ,,
summary: 运行时间预算耗尽（模型响应偏慢），无法生成执行计划。恢复运行将获得新的时间窗口重试。

## R043  [waiting_user] err= rc=anti_bot_challenge role=executor/execution resumable=True
Q: 猎聘网找上海 Java 后端开发工程师（3 年经验）岗位，为最匹配的岗位输出面试准备计划。
seeds: 1 | liepin 后端 role landing page
wall=125.6s turns=7 tools=6 artifacts=98 (public_job_pagex59, structured_job_detailsx34, job_search_resultsx5)
steps: 1:job-discovery:failed | 2:job-matching:not_started | 3:career-planning:not_started
tools: extract-observed-job-details=1/0 extract-observed-job-details-batch=34/0 fetch-public-job-page=0/1 fetch-public-job-pages=61/0 query-career-sheet-records=0/1 search-public-job-pages=0/1
verifier(last3): ,
summary: 连续或累计多次工具调用未取得进展，请人工确认岗位证据后重试。

## R045  [failed] err=tool_budget_exhausted rc=tool_budget_exhausted role=runtime/execution resumable=True
Q: 针对猎聘网上的产品经理（AIGC 方向）岗位，给出简历修改建议（改动点+原因）。
seeds: 1 | liepin 产品经理专区 incl. AIGC 专场
wall=284.8s turns=12 tools=6 artifacts=72 (structured_job_detailsx35, public_job_pagex30, job_search_resultsx7)
steps: 1:job-discovery:succeeded | 2:job-discovery:succeeded | 3:resume-tailoring:failed
tools: build-resume-tailoring-brief=0/5 extract-observed-job-details-batch=40/0 fetch-public-job-page=0/3 fetch-public-job-pages=30/0 query-career-sheet-records=0/0 search-public-job-pages=0/3
verifier(last3): 
summary: 

## R046  [waiting_user] err=verification_failed rc=verification_failed role=verifier/verification resumable=True
Q: 我的简历如何调整才能匹配“大模型应用开发工程师”岗位？请先找到一份该岗位的公开 JD 再给出定制建议。
seeds: 1 | liepin 大模型应用开发 role landing page
wall=46.2s turns=4 tools=5 artifacts=4 (job_matching_reportx1, job_search_resultsx1, public_job_pagex1, structured_job_detailsx1)
steps: 1:job-discovery:succeeded | 2:job-matching:failed | 3:resume-tailoring:not_started
tools: extract-observed-job-details-batch=1/0 fetch-public-job-page=0/1 fetch-public-job-pages=1/0 match-observed-jobs=1/0 query-career-sheet-records=0/0
verifier(last3): 
summary: Verifier 判定当前产出不满足任务要求：match_job 步骤的 success_criteria 要求 match-observed-jobs 返回包含目标 JD 的评分与 matched_keywords，但实际匹配到的岗位标题为“技术服务工程师”...

## R047  [waiting_user] err= rc=adapter:empty_result role=executor/execution resumable=True
Q: 针对“AI 算法工程师”岗位，给我一份面试准备计划（先找到一份该岗位的公开 JD 作为依据）。
seeds: 7 | baidu talent role-matched JDs
wall=131.2s turns=2 tools=3 artifacts=4 (public_job_pagex3, job_search_resultsx1)
steps: 1:job-discovery:failed | 2:career-planning:not_started
tools: fetch-public-job-page=0/2 fetch-public-job-pages=6/0 query-career-sheet-records=0/0
verifier(last3): 
summary: 模型未返回可解析的终态，但当前步骤的交付证据尚未满足完成契约。请补充岗位正文或重试。


## 迭代 1 修复记录 (2026-08-14/15)

**根因归类**：external_blocked 15（反爬 10/适配器空 4/access_denied 1）· need_user 7 · budget 4（墙钟 2/replan 1/tool 1）· verification_failed 2。

**迭代 1 改动**（commit 883d806）：
1. eval DEFAULT_BUDGET 墙钟 300s→600s（Q057/R042 类超时恢复窗口）。
2. 种子多源化：liepin 前端/Java/LLM/PM 单种子题 + iguopin 搜索页 fallback；baidu GRADUATE 不稳定题 + iguopin AI/campus fallback（Q011 Q013 Q046 Q055 R024 R025 R027 R040 R043 R045 R046 R047 C002/C010/C011/C015-L1）。
3. target_role_mismatch/target_source_mismatch 加入双 executor 稳定失败集（同 payload 重发即去重）。
4. Executor 规则新增：目标对象与用户要求不符时换语义匹配目标重试，连续两次同类失败即收尾（修复 R045 的 5 次 brief 失败预算燃烧）。

**预期**：Q057/R042/C002/C015/R040/R047 及 liepin 反爬组（Q011 Q046 Q055 Q071 Q115 R024 R027 R043 C010 C011）大概率转成功；R045 部分改善；Q028/R021/R038/R013/Q134/C007-L2/C008-L2 留待迭代 2。

## 迭代 2/3 修复记录

**关键发现**：评估实际使用 Classic Planner + Deep Executor + Classic Verifier；Deep Executor 的英文
_EXECUTOR_OPERATING_PROCEDURE 不包含 prompt_rules.py 的中文运行时规则；_bounded_context_metadata
刻意不投影 confirmed_profile_facts（有单测锁定），事实经 private_context 送达模型。

**迭代 2（ac5a43e）**：
1. 完成门禁拒绝（执行器宣告成功但契约未满足、非核验步骤）→ 有界 REPLAN 一次（shared RETRY_CONTRACT_EXHAUSTED marker）。
2. Planner 规则：禁止规划任务上下文与上游都没有的 context 输入（R013）。
3. Executor 规则：下游工具必须传入已确认事实。

**迭代 3（5866fb0）**：
1. Deep Executor 流程新增：target_role/target_source mismatch → 换语义匹配候选，同类失败两次即收手（R045）；
   声明 confirmed-facts 输入端口的工具必须从 private_context 原样传入事实（C008-L2）。
2. 运行时：Deep Executor 终态不可解析且契约未满足 → 有界 REPLAN 一次（Q011/Q028/Q071/R047 类）。

**迭代 4 候选（待 iter2 结果确认）**：R021 加中国移动/中国联通关键词 iguopin(国聘) 种子；R038 补"contract 要求全量规范化时批处理所有 jd_complete 页面"流程点；Q134/R047 视结果再定。

