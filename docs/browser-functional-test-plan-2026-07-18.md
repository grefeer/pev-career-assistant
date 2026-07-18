# 浏览器功能测试文档

> 日期：2026-07-18
> 目标：基于当前本地项目，使用 Codex 内置浏览器验证 Web 工作台核心功能，并记录发现的缺陷与修复结果。

## 1. 测试依据

- `docs/platform-foundation-handover-summary.md`
- `docs/superpowers/specs/2026-07-16-mvp-parallel-delivery-design.md`
- `docs/superpowers/specs/2026-07-17-wave2-evidence-matching-implementation-plan.md`

## 2. 测试环境

| 项目 | 值 |
| --- | --- |
| Backend | `http://127.0.0.1:18000` |
| Frontend | `http://127.0.0.1:15173` 或 Vite 本地端口 |
| MySQL | `127.0.0.1:3307` |
| Redis | `127.0.0.1:6380` |
| MinIO | `127.0.0.1:19000` |
| 浏览器 | Codex 内置浏览器 |

## 3. 核心业务范围

1. 认证：注册、登录、退出、受保护路由跳转。
2. 学生工作台：匹配、职位中心、手动提交、个人资料、快照、设备。
3. 管理员工作台：职位审核、提交审核、反馈处置入口与权限可见性。
4. Wave 2 链路：匹配报告、简历草稿、投递快照页面的空态、错误态和可访问性。
5. 基础健康：后端 live/ready、前端页面加载、浏览器控制台错误。

## 4. 测试用例

| ID | 场景 | 步骤 | 预期 |
| --- | --- | --- | --- |
| BFT-001 | 未登录访问根路由 | 打开 `/` | 跳转 `/login`，显示登录/注册表单 |
| BFT-002 | 注册学生账号 | 切换注册，填写唯一账号、昵称、8 位以上密码并提交 | 注册成功，进入默认工作台 |
| BFT-003 | 登录状态导航 | 依次进入匹配、职位、提交、个人资料、快照、设备 | 页面均可加载，无前端崩溃 |
| BFT-004 | 学生权限 | 学生访问 `/admin/jobs` | 被 guard 拦截，不展示管理员页面 |
| BFT-005 | 职位中心空态/列表态 | 打开 `/jobs` | 请求成功时展示职位或空态，失败时展示错误 |
| BFT-006 | 手动职位提交 | 打开 `/jobs/submissions`，提交 URL/JD 文本 | 前端完成表单校验并展示提交结果或稳定错误 |
| BFT-007 | 个人资料 | 打开 `/profile`，检查上传/文本导入入口 | 页面可操作，错误态可读 |
| BFT-008 | 匹配工作台 | 打开 `/matching` | 无数据时给出明确空态，操作按钮不会造成崩溃 |
| BFT-009 | 简历草稿详情 | 访问不存在的 `/resume-drafts/not-found` | 展示错误态，不白屏 |
| BFT-010 | 快照列表与详情 | 打开 `/snapshots`，访问不存在详情 | 列表可加载，详情错误态不白屏 |
| BFT-011 | 设备页面 | 打开 `/devices` | 设备配对说明/占位页面可加载 |
| BFT-012 | 控制台错误 | 浏览器测试过程中采集 console error | 无未处理运行时异常 |
| BFT-013 | 腾讯智能表同步解析 | 管理员调用两个内置来源同步；核对 27 届表与实习表行为 | 27 届表不虚构岗位名；实习表完整记录可解析为岗位候选；真实上游错误需返回稳定错误码 |
| BFT-014 | 完整岗位匹配触发 | 准备已核验岗位和已确认档案，在 `/matching` 选择两项并点击“开始匹配” | 不返回 500；MatchReport 终态被持久化；模型输出异常返回稳定错误码 |
| BFT-015 | 当前源码 Logout | 点击侧边栏 Logout | 清理登录态并跳转 `/login`，受保护导航消失 |

## 5. 执行记录

| ID | 结果 | 证据/备注 |
| --- | --- | --- |
| BFT-001 | 通过 | 打开 `/` 跳转 `/login`；修复后未登录页不再显示 Match/Jobs/Profile/Snapshots/Devices/Logout。 |
| BFT-002 | 通过 | 注册 `bft_student_1784354262154` 成功，进入 `/matching`。 |
| BFT-003 | 通过 | `/matching`、`/jobs`、`/jobs/submissions`、`/profile`、`/snapshots`、`/devices` 均可加载。 |
| BFT-004 | 通过 | 学生访问 `/admin/jobs` 被重定向回 `/matching`，不显示管理员入口。 |
| BFT-005 | 通过 | `/jobs` 显示 fixture verified 职位，详情/官方入口按钮可见。 |
| BFT-006 | 通过 | 提交测试职位链接后显示“提交已创建”，列表新增草稿记录。 |
| BFT-007 | 通过 | `/profile` 显示上传简历、资产、证据校对和确认版本空态。 |
| BFT-008 | 通过 | `/matching` 显示已核验岗位；无已确认简历版本时“开始匹配”禁用。 |
| BFT-009 | 通过 | 修复后 `/resume-drafts/not-found` 显示 `not_found` 错误态，不再 500/白屏。 |
| BFT-010 | 通过 | 修复后 `/snapshots` 显示空态；`/snapshots/not-found` 显示错误态，不再 500/白屏。 |
| BFT-011 | 通过 | `/devices` 显示未配对设备空态。 |
| BFT-012 | 通过 | 浏览器测试过程中未采集到 console error。 |
| BFT-013 | 阻塞/部分通过 | 当前源码后端显式加载 User-scope `TENCENT_DOCS_TOKEN` 后，两个真实来源均返回 `502 tencent_protocol_error`；本地 Mapper 单元测试覆盖 `tencent-27-referrals -> missing_title` 和 `tencent-intern-referrals -> NormalizedJobCandidate`。 |
| BFT-014 | 通过/模型失败可诊断 | 通过浏览器注册 `bft_full_1784355914280`，准备确认档案后点击“开始匹配”；修复后不再因缺少 `JobVerification` 返回 500，且最新 MatchReport 终态可持久化。真实模型调用仍可能返回 `match_execution_interrupted` 或 `match_model_validation_failed`。 |
| BFT-015 | 通过 | 在当前源码 Vite 端口点击 Logout 后跳转 `/login`，导航消失；旧 Docker/Nginx 15173 曾服务旧静态包，需重建容器后再用作当前源码验收。 |

## 6. 缺陷记录

| 缺陷 | 根因 | 修复 | 验证 |
| --- | --- | --- | --- |
| 未登录时仍显示工作台导航和 Logout | `AppShell` 无条件渲染导航，登录页也被 shell 包裹。 | `AppShell` 仅在 `isAuthenticated` 时显示导航；Logout 后主动跳转 `/login`；补充 App 测试断言。 | 浏览器退出后 `/login` 无导航；`npm --prefix frontend run test -- App.spec.ts` 通过。 |
| Snapshot/Draft API 在开发库返回 500 | Alembic head 表结构与 ORM 分叉，缺少 `user_id`、幂等字段、`attachment_ids` 等列；测试库未覆盖真实迁移后的 schema。 | 新增 `20260718_0011_wave2_schema_alignment.py` 补齐列、索引、约束和状态枚举；迁移测试增加 Wave 2 列断言。 | 开发库 `alembic upgrade head` 到 `20260718_0011`；API 重放：snapshot list 200 空列表，missing snapshot/draft 404。 |
| 匹配已核验 fixture 岗位返回 500 | fixture `JobPosting` 为 `verified`，但缺少对应 `JobVerification`，`build_verified_job_snapshot` 抛 `match_no_job_verification`；API 未映射该稳定错误码。 | `scripts/create_wave2_fixtures.py` 为 fixture 岗位创建 `JobVerification`；`POST /api/matches` 将 `match_no_job_verification` 映射为 422。 | 新增 API 回归测试；本地 fixture 脚本补齐验证记录后，浏览器触发匹配不再出现前置 500。 |
| MatchReport 终态未持久化 | `MatchService` 在 pending/running 阶段 commit，但最终 `finalize()` 只 flush，调用结束后事务回滚会把报告留在 `running`。 | `MatchService` 所有 finalize 路径统一 `finalize + commit`。 | 新增 `test_final_status_is_committed`，回滚调用方事务后新查询仍为 `failed match_model_validation_failed`。 |
| 模型结构化输出校验错误被归类为执行中断 | `assess_match` 只捕获 JSON 解析错误，Pydantic 结构错误会冒出图执行。 | 捕获 Pydantic `ValidationError` 并返回 `match_model_validation_failed` fail state。 | 新增 `test_assess_match_converts_structured_validation_errors_to_fail_state`；直接服务调用返回并持久化 `failed match_model_validation_failed`。 |
| 真实腾讯智能表同步失败 | 当前真实上游调用两个内置来源均返回 `tencent_protocol_error`。 | 未改代码；该错误已稳定映射为 502，需后续检查腾讯 MCP 协议/令牌/表结构漂移。 | API 记录：`tencent-27-referrals` 与 `tencent-intern-referrals` 均为 `502`，不泄露令牌。 |
