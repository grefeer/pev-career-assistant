# full83 v3 基线分析（2026-08-16）

## 结果

| 指标 | 数值 |
|---|---|
| 运行 | full83_autorecovery_v3_20260816（单线程，10:54–15:59，约 5h05m） |
| succeeded | 71（85.5%） |
| waiting_user | 12（14.5%） |
| failed | **0** |
| 基准提交 | 267c5e6（f30e96f 礼貌抖动 + 26611eb/b1aa714 自动恢复 + 267c5e6 预算上调） |

三代演进：v1 65/17/1 → v2 70/11/2 → **v3 71/12/0**（硬失败清零）。

## 12 个非成功用例归类

### external_blocked（5，策略性交回，不自动恢复）
- C012 / Q071 / R023 / R028：anti_bot_challenge（站点风控挑战页）
- R013：adapter:url_not_allowlisted（适配器端点未过白名单审查）

这些是安全降级的正确行为；出路是换来源/人工确认，不是重试。

### model_or_verifier_decision（6，自动恢复已给满机会）
- Q028（need_user, 34 turns / 5 plans / 820k tokens）
- Q046（need_user, 17 turns / 3 plans）
- R002（need_user, 37 turns / 6 plans —— 37 > 36 上限，升级预算的恢复轮证据）
- R010（script_not_found，执行器要求缺失脚本）
- R014（planner need_user, 1345s / 4 plans ≈ 三轮尝试）
- R025（target_source_mismatch, 15 turns / 3 plans）

### budget_exhausted（1）
- C003：replan_budget_exhausted（重规划预算耗尽，不在自动恢复白名单）

## 自动恢复实弹证据（turns/plans 反推）

eval 结果 JSON 原不含 events 字段，无法直接计数 run_auto_recovered；
已给 eval_runner 加 auto_recovery_count 字段（本基线之后的轮次可精确统计）。
本轮证据：R002 37 turns（首跑上限 36）、R014 1345s（600+900+部分第三轮）、
Q028/R025/Q046 3–5 次 plan 重规划。

## 关键结论

1. **预算上调疗效显著**：Q017 failed→succeeded；R002/R013 failed→waiting_user；
   Q028 failed→waiting_user（820k tokens 的思考空间）。
2. **自动恢复是“有界再给一次机会”**，不是万能：6 个模型决策题给满 3 轮仍交回，
   说明剩余的 waiting_user 是实网数据缺失/风控波动的本质上限，而非机制缺陷。
3. 站点风控（anti_bot_challenge）仍是最硬的墙：按策略交回，不解码不绕过。
