# DeepAgents Executor Migration Plan

## Goal

将生产环境的 PEV Executor 切换为 `create_deep_agent`，保留现有 ExecutorAgent 公共契约、ToolRegistry 证据边界、Runtime/Verifier 接口和猎聘安全约束；不修改 Verifier，不参考已退役实现。

## Steps

- [x] 保存并检查当前工作区基线
- [x] 实现受限的 `run_skill_script`
- [x] 实现 DeepAgents Executor 适配层并保留兼容测试注入路径
- [x] 接入生产 Executor、预算、观察结果和工具边界
- [x] 补充单元测试并运行回归
- [x] 从 83 题集中抽取 10 道非链条题并执行评测
- [x] 根据工具调用日志分析失败并定位 DeepExecutor 回归
- [x] 修复 P0：终态校验、只读 Skill、去重熔断、跨 RETRY 状态、反馈/候选输入
- [x] 修复 P1：观察投影、步内证据、Skill policy、trace、异常与预算语义
- [x] 修复 P2：stdout 脱敏、子进程树、step 校验和权限路径
- [ ] 后续按 prompt/context、ledger、tool adapters 拆分 DeepExecutor 模块

## Latest validation
- `deep_executor_nonchain_20260813_run3`: 10/10 completed, 0 succeeded, 2 waiting_user, 8 failed.
- The model-budget regression is reduced but not eliminated; most failures now occur in Planner/DeepAgents turn convergence before a terminal result.
- No further live rerun should start until the turn/terminal convergence path is instrumented and fixed.
- [x] 补充回归测试并运行完整相关测试
- [x] 按 DeepSeek 根因链修复终态、内部调用预算、预算回滚、历史窗口和 Planner schema/output cap
- [x] 完成本轮定向与相关全量单测验证
- [x] 修复散文包裹终态 JSON，并覆盖异常失败路径 trace 与评测 JSON 序列化
- [x] 完成 `tests/unit` 全量验证：1557 passed、7 skipped；ruff 通过
- [x] 修复 Executor Skill prompt：注入有界 policy/完成契约/单步流程，并过滤无关文件工具
- [x] 修复历史窗口裁剪：tool-call 与 ToolMessage 成组保留，相关回归 293 passed

## Audit disposition: 2026-08-13 findings_report.md

- [x] P1-1 state.json：容错加载、备份回退、临时文件 + fsync + replace
- [x] P1-3 gateway：每个真实 provider 请求独立计费，重试不绕过物理预算
- [x] P1-4 resume/recover：owner 查询支持 `FOR UPDATE`，状态检查与变更同一锁内
- [x] P1-5 POSIX helper：新建进程组并在 killpg 失败时回退杀单进程
- [x] P1-6 profile facts：从 DeepExecutor 模型 metadata 投影中移除，工具侧仍可用
- [x] P1-7 Planner：非法 ExecutionPlan 降级为 `waiting_user/invalid_execution_plan`
- [x] P1-8 skill script：显式脚本白名单、最小环境、stdout/stderr 标签脱敏
- [x] 选定 P2：cache hash 校验、非 Web scheme 拦截、deduplicate output 边界、OCR 联系方式提示、持久化正文上限、敏感目录 ignore
- [ ] 保留为风险/后续：DNS TOCTOU（报告标为 unverified）、大范围性能重构、未证实的 output 误贴、Verifier 停滞镜像、模块拆分

## Invariants

- Verifier 不改动。
- 工具仍必须经过 ToolRegistry；失败永不跨越 Agent 边界。
- `run_skill_script` 只能执行当前 Skill 目录下的 `.py` 文件，不输出 secrets。
- FilesystemBackend 只绑定当前 Skill 目录，禁止跨 Skill / 仓库根目录访问。
- 猎聘登录、验证码、反爬不自动绕过。
- DeepExecutor 的终态和 execution_state 必须在 Agent 边界内校验，不能让 Pydantic 异常逃逸。
- Skill 包默认只读；证据和运行产物必须走 ToolRegistry/持久化边界。
- DeepExecutor 共享 turn 只按 step 消耗一次；内部 model call 必须有独立上限且不污染 PEV turn 统计。
- DeepExecutor 失败终态也必须留下安全 trace，包含错误码和内部调用次数。
- Executor prompt 必须包含当前 Skill 的可执行摘要和 completion contract，不能只依赖空 execution_policy。
- 历史窗口不得产生未配对的 AI tool call；无法安全裁剪时交给 SDK 的 canonical repair。
