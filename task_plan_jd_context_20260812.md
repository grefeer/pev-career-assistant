# Task Plan: JD Context and Runtime Follow-up Optimization

## Goal

保留当前猎聘反爬修复，在现有 PEV runtime 上降低 JD 上下文膨胀并修复历史评测暴露的匹配、目标指针和前置步骤门控问题。

## Phases

- [x] Phase 1: 保存当前猎聘反爬修复
- [x] Phase 2: 检查匹配契约、目标证据解析和步骤门控
- [x] Phase 3: 实现最小兼容修复与上下文压缩
- [x] Phase 4: 运行单元回归、定向链路测试和静态检查

## Acceptance Criteria

1. `match-observed-jobs` 的历史非 canonical 输入仍能被归一化，业务 limit 最终固定为 100。
2. `candidate_id`、`artifact_id`、`source_artifact_id` 和 `source_url` 能解析到同一持久化 JD。
3. 前置步骤失败或缺少交付物时，不进入后续匹配/简历/面试工具。
4. 模型决策上下文优先携带 JD 指针，只有工具真正需要时才 hydration 完整 JD。
5. 猎聘 `anti_bot_challenge`、redirect trace 和 circuit breaker 行为保持不变。

## Errors Encountered

- 旧测试仍断言跨步骤上下文携带完整 visible_text，已按 pointer-first 契约更新。
- `unknown_tool` 继续保留原有稳定失败去重；只有 `tool_skill_forbidden` 立即停止并重规划，避免扩大行为变化。
- 全量单元测试最终通过 `1514 passed`。

## Status

**Completed** - 已完成代码修改、回归测试和编译检查；未启动 83 题全量评测。
