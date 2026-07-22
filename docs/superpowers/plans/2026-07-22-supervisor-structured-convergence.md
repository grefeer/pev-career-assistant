# Plan: Supervisor 收敛到结构化输出

## 根因
supervisor 配置了 `response_format=_DiscoveryRunResultPydantic`（已确认 `create_deep_agent` 接受该参数，TypeError fallback 不触发）。但 Moka/禾赛/拼多多的 result summary 是 "Recovered incomplete discovery output from tool results" —— supervisor LLM 调用了 `run_web_navigation`（拿到已 verify+package 的权威 candidates），但没在最后产出 `structured_response`（DiscoveryRunResult），导致 `parse_agent_result` 走恢复路径（`_collect_tool_outputs` 从 tool payload 恢复 candidates）→ `partial_success`（有候选但 LLM 没产出结构化壳）。小米是 LLM 产出结构化 → succeeded。

## 方案：确定性 finalizer（核心）+ prompt 微调（辅助）

LLM 是否产出 `structured_response` 是非确定行为（prompt 已强，line 33 的 3-step 收敛规则），难以可靠保证。因此核心是用确定性 finalizer 兜底：恢复路径里有有效候选时，直接标记 `succeeded`。

### 改动 1（核心）— `result_contract.py` `parse_agent_result` 恢复路径
位置：`backend/app/services/job_discovery/result_contract.py` line 38-45。

当前：有 `tool_candidates` → `partial_success`（`block_reason="parse_failed"`）。
改为：有 `tool_candidates` → `succeeded`（`block_reason=None`）。

理由：`tool_candidates` 来自 `run_web_navigation` 的 `_extract_and_verify_candidates_from_evidence`（已 verify+package 的权威候选），候选有效；LLM 只是没输出结构化壳。把"发现有效候选"标记为 `succeeded` 更准确反映结果。
- 无候选 → 保持 `needs_manual_review`（`block_reason="parse_failed"`）
- `summary` 明确 "candidates recovered from tool outputs"
- `enforce_result_invariants` 已保证 `succeeded`+0 候选 → `failed`，不受影响

边界（不破坏现有语义）：
- 只影响"恢复路径"（LLM 没产出结构化 + 有 tool outputs，即 recovered）。
- LLM 产出结构化 result（小米 `succeeded`、或 budget-exhausted 的 `partial_success`）走 `_iter_result_candidates` 找到，**不进**恢复路径，不受影响。
- 柏楚（needs_manual_review/0，WeChat 验证墙）、拼多多 login 墙（无 detail body 候选）不受影响 —— 守界保持。

### 改动 2（辅助）— `prompts/supervisor_base.txt` 微调
强化"调 `run_web_navigation` 后立即输出 `structured_response`，禁止再调 `verify_evidence`/`package_candidates` 等工具（其结果已包含）"。prompt 已较强，微调收益有限，主要靠改动 1 兜底，但能让 LLM 在能收敛时也产出结构化（减少 recovered 比例）。

## 测试
1. job_discovery 单测：确认 `parse_agent_result`/`enforce_result_invariants` 变更不破坏；若有用例断言 recovered→`partial_success`，更新为 `succeeded`。
2. 6-URL smoke：验证 Moka/禾赛/拼多多 → `succeeded`，候选数不降，守界保持（柏楚 needs_manual_review、拼多多 login 墙、Feishu）。

## 约束
- 只改 supervisor 子系统（`result_contract.py`、`prompts/supervisor_base.txt` 可改）
- 不改 `tools/jd_extraction.py` / `tools/evidence_verifier.py`
- API 反复报错时停止开发测试
