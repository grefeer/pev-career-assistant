# 83 题全量评测报告（2026-08-12）

## 结论

本次测试已完成两轮 83/83 全量评测，均使用 8 个独立 Python 进程。整体成功数明显低于用户提供的历史结果 `71 / 70 / 65 / 65 / 68`。详细检查 waiting_user 链后，确认存在一个与本次改动直接相关的回归：固定 `limit=100` 被实现为严格的 `Literal[100]`，模型生成非 100 的旧格式参数时，匹配工具连续返回 `invalid_tool_input`，最终触发 waiting_user。

除此之外，公网评测环境也不稳定：两轮都出现大量猎聘页面 `public_page_content_insufficient`，而历史成功结果中的 C 链大多依赖猎聘专区页面；另有 1 例 `model_request_failed`（C015-L1）。因此当前回归是“代码问题 + 外部抓取问题 + 少量上下文/工具契约问题”的叠加，不应只归因于其中一项。

## 本次结果

| 运行 | 进程 | 顶层题目 | succeeded | waiting_user | failed | 链 link 数 | link succeeded |
|---|---:|---:|---:|---:|---:|---:|---:|
| 直接并行 | 8 | 83 | 34 | 49 | 0 | 18 | 3 |
| 错峰并行 | 8 | 83 | 30 | 52 | 1 | 17 | 3 |

用户给出的历史成功数如果按顶层 `succeeded` 口径解释，则本次结果分别低：

| 历史值 | 本次直接并行 34 | 本次错峰并行 30 |
|---:|---:|---:|
| 71 | -37 | -41 |
| 70 | -36 | -40 |
| 65 | -31 | -35 |
| 65 | -31 | -35 |
| 68 | -34 | -38 |

## 分组结果

### 直接并行 8 进程

- C：0/15 succeeded，15 waiting_user；15 个 L1 中 12 个出现 `public_page_content_insufficient`。
- Q：7/21 succeeded，14 waiting_user。
- R：27/47 succeeded，20 waiting_user。
- failed：0。

输出目录：`tests/question/eval_results/lazy_jd_full_20260812_8p/`

### 错峰并行 8 进程

- C：1/15 succeeded，13 waiting_user，1 failed；15 个 L1 中 11 个出现 `public_page_content_insufficient`。
- Q：6/21 succeeded，15 waiting_user。
- R：23/47 succeeded，24 waiting_user。
- failed：C015-L1，`model_request_failed`。

输出目录：`tests/question/eval_results/lazy_jd_full_20260812_8p_staggered/`

## 判断

1. 这不是可接受的“无大幅回归”结果：按用户提供的历史成功数，当前低了 31–41 个成功题。
2. 已确认的代码回归：直接并行 run 的 `C002-L2` 在抓取 7 个页面、抽取 7 个结构化 JD 均成功后，`match-observed-jobs` 连续 3 次 `invalid_tool_input`，没有一次成功，随后因无进展进入 waiting_user。历史 baseline 的同一环节虽也出现过 2 次非法输入，但第 3 次修正后成功；当前 `Literal[100]` 把原本可恢复的模型参数错误变成了不可恢复的等待。
3. 已确认的下游证据契约问题：直接并行 run 的 `C015-L3` 已经成功抓取 7 个页面、抽取 7 个 JD，并成功产出 `job_matching_report`；但 `build-preparation-plan` 连续 3 次 `target_evidence_not_found`。当前准备计划工具只按 `observed_public_evidence[*].artifact_id` 查找目标，而匹配结果同时暴露了 `candidate_id`。如果模型把匹配结果中的候选指针传给准备计划，就会出现“上一工具有产出、下一工具找不到目标”的断链。评测结果没有保存原始 tool payload，因此“具体传的是 candidate_id 还是 artifact_id”仍需补充 trace 才能最终定案。
4. 两轮的主要外部失败仍是猎聘等页面的 `public_page_content_insufficient`；这类 waiting_user 发生在匹配前，不能归因于 lazy JD hydration。
5. 第二轮的 `C005-L2` 暴露了另一组问题：`match-observed-jobs` 曾成功 2 次，但随后 `build-resume-tailoring-brief` 出现 `tool_skill_forbidden` / `target_evidence_not_found`，同时还有一次 fetch `invalid_tool_input` 和一次匹配重复调用。该环节累计 `input_tokens=134,398`，说明长链上下文已经明显放大了模型选错工具、重复调用和目标 ID 丢失的概率；但这是跨多次模型调用的累计值，不等于单次请求必然超过上下文窗口，不能仅凭该字段断言模型服务发生了 context overflow。
6. 因此需要分开修复和验证：先修正 `limit` 输入边界与目标证据 ID 解析，再用缓存证据重放评估 lazy context；公网抓取不稳定的问题单独做 live smoke test。

## 评测命令与并发方式

canonical runner：

```powershell
.\.venv\Scripts\python.exe -m tests.question.eval_runner
```

runner 本身串行处理 `--ids`，本次通过 8 个独立进程分片执行，每个进程使用 SQLite `:memory:` 和独立输出目录。第二轮采用两波各 4 个进程、间隔约 60 秒启动，但两波仍允许总计 8 个进程运行。

## 当前限制

- 评测依赖实时 DeepSeek 和公网招聘站，成功数会受页面反爬、登录墙、站点内容变化和模型服务波动影响。
- 本次未覆盖“缓存证据重放”的稳定性验证；因此不能仅凭这两轮 live 结果判定 lazy JD context 的业务回归。
- 评测 JSON 只保存工具成功/失败计数和错误码，没有保存失败调用的原始 payload 或完整 `error_message`；因此 `invalid_tool_input` 的具体字段、以及 `target_evidence_not_found` 的具体目标 ID，需要增加诊断 trace 后才能逐次还原。

## 与历史 70/83 结果的可比性

- `tests/question/eval_results/results/SUMMARY.md` 中的历史 `70/83` 是 `retry_6` 的最终合并结果，不是一次 83 题单跑：先有 `full_run`，再合并 `rerun_27` 和 `rerun_17` 的重点重跑结果。历史报告明确记录了 `full_run=56 succeeded / 26 waiting_user / 1 failed`，最终合并后变为 `70 succeeded / 12 waiting_user / 1 failed`。
- 历史最终结果中 15 条 C 链全部通过；历史并非完全没有公网失败，R005 的部分公司链接有 `public_page_content_insufficient`，R013 也有一个公司页面抓取失败，但这些是局部失败，其他证据仍足以完成任务。
- 当前两轮是一次性 83 题 live run，并且通过 8 个独立进程并发请求实时招聘站；直接并行和错峰并行分别有 12/15、11/15 个 C-L1 出现 `public_page_content_insufficient`。因此当前结果与历史最终合并口径不能直接比较。
- 本次改动涉及 runtime 的证据投影和 tool-side hydration，没有修改 `fetch-public-job-pages` 的网络抓取实现。C-L1 在开始时尚无历史 evidence，抓取工具收到的仍是候选 URL；所以 pointer-only context 不会直接把一个可访问网页变成 `public_page_content_insufficient`。它最多会通过模型选择、重试和请求参数间接影响流程，但当前 C-L1 的主要证据指向实时站点并发/页面状态，而不是上下文 hydration。

## 本轮修复与评测调度

- `MatchObservedJobsInput` 仍把实际结果截断为 100 条；对模型旧格式显式传入的整数 `limit` 在 schema 边界归一化为 100，避免旧参数触发 `invalid_tool_input`。
- 新增统一 JD 目标解析：`artifact_id`、`candidate_id`、`source_artifact_id` 和同一候选的 `source_url` 均可在工具边界解析到已持久化的完整 JD。匹配结果可以直接交给简历定制和面试准备工具。
- Executor 对当前 Skill 永久越权的工具按工具名去重；不同参数不会再次实际调用同一个被禁止工具，避免 C005-L2 的越权/重复调用链继续消耗预算。
- 新增 [4 进程 60 秒错峰评测脚本](../scripts/run_question_eval_4p_staggered.ps1)。本轮 live 评测输出目录为 `tests/question/eval_results/lazy_jd_full_20260812_4p_staggered_run3/`，启动偏移为 0/60/120/180 秒。
