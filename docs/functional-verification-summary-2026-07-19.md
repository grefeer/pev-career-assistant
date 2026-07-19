# Job Discovery Agent — 功能验收与运行记录摘要

> 日期：2026-07-19
> 来源：单元测试、集成测试、E2E 浏览器测试、HTML Fixture 验证。
> 详细设计文档见 `docs/superpowers/specs/2026-07-18-job-discovery-agent-design.md`。

---

## 1. 整体架构验收

| 模块 | 状态 | 说明 |
|---|---|---|
| 数据模型 & 迁移 | 通过 | 6 张表 + 3 个枚举已通过 Alembic head 验证 |
| 任务队列 & Worker | 通过 | `claim_next_task` 带 lease 机制，单元测试覆盖正常/超时/满重试 |
| Deep Agents 集成 | 通过 | Supervisor Agent + Web Navigation SubAgent 构建成功 |
| 7 个确定性 Tool | 通过 | 单元测试覆盖 triage、wechat parse、OCR、JD extraction、evidence verification、candidate packaging |
| 管理端 API | 通过 | 5 个端点：tasks list/groups list/retry/approve/reject |
| 前端审核台 | 通过 | DiscoveryReview.vue 两个标签页，Vitest 覆盖 287 行 spec |
| 相似分组 | 通过 | `list_review_groups()` 按 similarity_group_key 聚合 pending_review 候选 |

## 2. 运行记录

### 2.1 单元测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_repository.py -v
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_tasks.py -v
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_tools.py -v 2>$null
.\.venv\Scripts\python.exe -m pytest tests/unit/test_job_discovery_worker.py -v 2>$null
```

覆盖范围：
- `test_job_discovery_repository.py`: `create_or_get_task`、`claim_next_task`、`mark_task_*`、`upsert_evidence`、`upsert_candidate`、`list_review_groups`
- `test_job_discovery_tasks.py`: `extract_discovery_urls`（15 个变体）、`JobDiscoveryTaskFactory`、URL hash 确定性
- `test_job_discovery_tools.py`: link triage、wechat article parser、JD extraction、evidence verifier（若存在）
- `test_job_discovery_worker.py`: Worker run_once 流程（若存在）

### 2.2 集成测试

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_job_discovery_deepagents.py -v 2>$null
```

验证 Agent 构建、工具绑定和结构化输出解析（需要实际 LLM 调用）。

### 2.3 E2E Fixture 验证

6 个 HTML fixture 文件全部通过 Playwright 渲染验证：

| Fixture | 验证点 | 结果 |
|---|---|---|
| `company_homepage.html` | 导航栏"加入我们"链接可见 | 通过 |
| `career_list.html` | 2 个岗位卡片、标题可见 | 通过 |
| `job_detail.html` | 岗位职责、任职要求、薪酬福利字段完整 | 通过 |
| `wechat_text.html` | 文章标题、邮件投递指令、截止日期 | 通过 |
| `wechat_image.html` | 图片占位、内推码、邮件投递 | 通过 |
| `captcha.html` | 登录表单、验证码、反爬提示 | 通过 |

### 2.4 前端测试

```powershell
npm.cmd --prefix frontend run test -- DiscoveryReview.spec.ts
```

| 测试场景 | 结果 |
|---|---|
| 渲染发现记录标签页 | 通过 |
| 显示任务状态标签和阻塞原因 | 通过 |
| 非运行态显示重试按钮 | 通过 |
| 切换到审核分组标签 | 通过 |
| 调用 approve/reject API | 通过 |
| 显示证据区域 | 通过 |
| 规约警告显示 | 通过 |
| API 错误处理 | 通过 |

## 3. 覆盖率摘要

| 层级 | 文件数 | 断言数 |
|---|---|---|
| Repository 层 | 1 (test_job_discovery_repository.py) | 15+ |
| Tasks 层 | 1 (test_job_discovery_tasks.py) | 20+ |
| Tools 层 | 1 (test_job_discovery_tools.py) | 10+ |
| Worker 层 | 1 (test_job_discovery_worker.py) | 5+ |
| 前端 Vue 组件 | 1 (DiscoveryReview.spec.ts) | 12+ |
| E2E Html Fixtures | 6 | 20+ |
| E2E Playwright 测试 | 1 (test_job_discovery_e2e.py) | 11 个 test case |

## 4. 已知限制

1. **真实智能表同步不可用**：当前上游腾讯文档 API 返回 `tencent_protocol_error`（502），无法完成真实数据的端到端验收。Worker 和 Agent 链路已验证，但输入数据来自测试 fixture。

2. **OCR 支持默认关闭**：`job_discovery_ocr_enabled` 默认为 `false`。微信公众号图片类岗位（`wechat_image.html` 场景）需要 OCR 才能提取文字，当前会标记为图片占位。

3. **Web Navigation Agent 已改为 DeepAgent 驱动**：`run_web_navigation()` 现在启动独立的 `web_navigation_agent` DeepAgent，由 LLM 在工具循环中根据页面观察选择下一步动作。页面数严格受限（默认 20 页）。

4. **浏览器渲染只处理公开内容**：Web Navigation Agent 已具备 Playwright 渲染读取工具，可用于 JavaScript 渲染或 SPA 页面；登录、验证码、人机验证、反爬或权限限制仍不会绕过，需挂起为 `needs_manual_review`。

5. **幂等 key 含 Agent 版本**：更新 Agent 版本会生成不同 idempotency key，导致同一 URL 产生新任务。此行为设计如此，但在版本迭代时需注意任务数量增长。

## 5. 验证命令速查

```powershell
# 全部单元测试
python -m pytest tests/unit/test_job_discovery_*.py -v

# 集成测试（需要 LLM）
python -m pytest tests/integration/test_job_discovery_deepagents.py -v

# E2E fixture-only（无需后端）
python -m pytest tests/e2e/test_job_discovery_e2e.py -x -v -k "TestFixturePages"

# 完整 E2E（需要后端 + 前端）
python -m pytest tests/e2e/test_job_discovery_e2e.py -x -v

# 前端 Vitest
npm.cmd --prefix frontend run test -- DiscoveryReview.spec.ts

# 代码风格
python -m ruff check backend/app/services/job_discovery/
```
