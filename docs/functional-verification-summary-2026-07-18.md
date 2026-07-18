# 功能验收与运行记录摘要

> 日期：2026-07-18
> 来源：Codex 内置浏览器测试、API 复放、服务层回归测试与构建验证。
> 详细测试用例见 `docs/browser-functional-test-plan-2026-07-18.md`。

## 1. 当前已验证功能

### 1.1 认证与路由权限

- 注册学生账号后会进入默认匹配工作台。
- 登录后侧边栏显示学生可用入口：Match、Jobs、Profile、Snapshots、Devices。
- 学生访问 `/admin/jobs` 会被路由守卫重定向回 `/matching`，不会展示管理员页面。
- 当前源码中点击 Logout 会清理登录态并跳转 `/login`，登录页不再显示工作台导航。

注意：Docker/Nginx 的 `15173` 端口曾服务旧静态包，出现过 Logout 不生效的旧页面现象。当前源码验收使用 Vite 临时端口完成；如果继续用 `15173` 验收，需要先重建前端容器。

### 1.2 学生职位中心

- `/jobs` 可加载已核验岗位列表。
- 测试环境中能看到 fixture 岗位：`Software Engineer Intern (Fixtures) @ Fixture Corp`。
- 学生职位接口只展示 `verified` 岗位，非核验状态岗位不应进入职位中心。
- 职位筛选表单、职位卡片和基础详情入口可加载，无浏览器 console error。

### 1.3 手动职位提交

- `/jobs/submissions` 可加载。
- 页面支持“职位链接”和“JD 文本”两种输入模式。
- 浏览器测试中提交测试职位链接后，页面显示“提交已创建”，列表新增草稿记录。
- 管理员后续可在提交审核队列中处理学生提交的岗位线索。

### 1.4 简历与档案

- `/profile` 可加载上传简历、简历资产、证据校对、确认版本区域。
- 没有简历资产时显示空态。
- 测试中为学生准备了一个已确认档案版本，匹配页可以读取到 `v1` 确认版本。
- 已确认版本会作为岗位匹配输入之一。

### 1.5 岗位匹配

- `/matching` 可加载已核验岗位和已确认简历版本。
- 当缺少已确认档案版本时，“开始匹配”按钮保持禁用。
- 当同时选择已核验岗位和已确认档案版本后，浏览器可以触发 `POST /api/matches`。
- 已修复 fixture 岗位缺少 `JobVerification` 导致的 500。
- 已修复 MatchReport 最终状态只 flush 不 commit 的问题；最终状态现在会持久化，不会长期停在 `running`。
- 真实模型调用仍可能返回失败状态，常见稳定错误码包括：
  - `match_model_validation_failed`：模型返回 JSON 或结构化字段不符合协议。
  - `match_execution_interrupted`：图执行或上游调用发生运行异常。

### 1.6 简历草稿与投递快照

- `/resume-drafts/not-found` 不再 500 或白屏，会显示 `not_found` 错误态。
- `/snapshots` 可加载空态。
- `/snapshots/not-found` 不再 500 或白屏，会显示错误态。
- Snapshot/Draft 相关数据库结构已通过 Alembic `20260718_0011` 补齐，开发库已升级到 head。

### 1.7 设备页面

- `/devices` 可加载。
- 未配对设备时显示空态。
- 本轮没有测试真实本地 GUI Agent 执行器提交网站流程。

## 2. 腾讯智能表岗位同步与解析

系统内置两个腾讯智能表来源：

- `tencent-27-referrals`：27 届内推信息。
- `tencent-intern-referrals`：实习内推汇总。

当前业务规则：

- `tencent-27-referrals` 只有企业名称和内推链接，不包含明确岗位名。系统不会虚构岗位标题，Mapper 会将记录跳过为 `missing_title`，进入后续人工补全/审核语义。
- `tencent-intern-referrals` 字段更完整，完整记录可解析为 `NormalizedJobCandidate`，包含公司、岗位、投递链接、地点、招聘类型、行业、内推码、截止日期等。

本轮真实同步结果：

- 使用带 User-scope `TENCENT_DOCS_TOKEN` 的当前源码后端调用两个同步接口。
- 两个真实来源均返回 `502 tencent_protocol_error`。
- 该错误已稳定返回，不泄露令牌。
- 因真实上游协议错误，本轮没有完成“真实智能表数据同步到本地并进入审核队列”的通过验收。

已有自动化覆盖：

- `tests/unit/test_job_mappers.py` 覆盖两个来源的字段映射、缺失字段跳过、URL 校验和 schema 漂移。
- `tests/integration/test_tencent_smartsheet_live.py` 是 opt-in live 测试，需要有效令牌和专用 `_test` MySQL 库。

## 3. 本轮修复的缺陷

| 缺陷 | 根因 | 修复 |
| --- | --- | --- |
| 已核验 fixture 岗位匹配返回 500 | fixture `JobPosting` 为 `verified`，但没有对应 `JobVerification`；API 未映射 `match_no_job_verification`。 | fixture 脚本补建 `JobVerification`；匹配 API 将该错误映射为 422。 |
| MatchReport 终态未持久化 | `MatchService` 在最终 `finalize()` 后只 `flush()`，调用方回滚会使报告回到 `running`。 | 所有 finalize 路径统一 `finalize + commit`。 |
| 模型结构化输出错误被归为执行中断 | `assess_match` 只捕获 JSON 解析错误，Pydantic 字段校验错误会冒出图执行。 | 捕获 Pydantic `ValidationError`，转换为 `match_model_validation_failed`。 |
| Vite 无法方便指向临时后端 | 代理目标硬编码为 `http://localhost:8000`。 | 支持 `VITE_API_PROXY_TARGET`，默认仍保持 `http://localhost:8000`。 |

## 4. 验证命令

本轮已执行并通过：

```powershell
.\.venv\Scripts\python.exe -m pytest tests\unit\test_evidence_matching_agents.py tests\api\test_matches_api.py tests\integration\test_match_service.py -q
.\.venv\Scripts\python.exe -m ruff check backend\app\services\match_service.py src\evidence_matching\agents.py backend\app\api\routes\matches.py scripts\create_wave2_fixtures.py tests\api\test_matches_api.py tests\unit\test_evidence_matching_agents.py tests\integration\test_match_service.py
npm.cmd --prefix frontend run test
npm.cmd --prefix frontend run typecheck
npm.cmd --prefix frontend run build
git diff --check
```

结果摘要：

- 后端相关测试：23 passed。
- 前端 Vitest：13 个测试文件，79 passed。
- 前端 typecheck：通过。
- 前端 build：通过。
- `git diff --check`：无 whitespace 错误，仅 CRLF warning。

## 5. 后续需要关注

- 重建 Docker 前端容器后，再用 `http://127.0.0.1:15173` 做一次当前源码验收。
- 排查腾讯智能表真实同步的 `tencent_protocol_error`：优先看 MCP 返回协议、令牌权限、表结构字段类型是否漂移。
- 如果要求“岗位匹配成功生成完整报告”，需要稳定模型输出协议，或在测试环境注入 deterministic match graph。
- 当前浏览器测试没有覆盖真实 GUI Agent 自动投递网站流程；设备页只验证了空态。
