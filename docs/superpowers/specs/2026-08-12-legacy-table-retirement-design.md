# 遗留表退役设计（sub-project 4 · DB 部分）

日期：2026-08-12
状态：已批准（用户确认：范围 = 物理清理遗留表；数据策略 = 先备份再删；路线 = 一次性完整清理）

## 1. 背景与目标

当前 `db/models.py` 定义 53 张表，其中 14 张属于已退役的求职发现/个性化发现/分析会话 schema：
- 这些表的 worker、service、repository 已被移除（CLAUDE.md 记载 "The `JobDiscoveryTask` model and table persist, but the worker that processed them has been retired"）；
- 全仓库（backend、tests、scripts，排除 .venv/node_modules）除 `db/models.py`、`db/__init__.py`、alembic 历史、`tests/manual` 两个已死脚本外，**无任何活跃代码引用**这些表或其模型；
- pyproject.toml 覆盖配置注释已写明：遗留 domain 模块 "slated for retirement in sub-project 4 (Legacy Retirement and Portfolio)"。

目标：物理删除 14 张遗留表及其全部代码残留，将表数从 53 收敛到 39，且不触碰任何活跃路径（PEV 双 runtime、WP2 职位链路、档案简历、执行器子系统）。

## 2. 退役表清单与依据

| 表 | 原属域 | 退役依据 |
|---|---|---|
| `analysis_sessions` | 平台基础（supervisor 时代分析会话） | 无活跃写入；`thread_id` 唯一约束是 LangGraph 线程时代的残留；唯一子表引用是 `match_reports` FK（本设计一并摘除） |
| `job_discovery_tasks` | 求职发现 worker | worker 已退役（CLAUDE.md）；被 4 张子表引用，簇内闭环 |
| `job_discovery_evidence` | 同上 | 只引用 `job_discovery_tasks`（CASCADE） |
| `discovered_job_candidates` | 同上 | 只引用 `job_discovery_tasks`（CASCADE） |
| `job_discovery_strategies` | 同上 | 被 `job_discovery_trajectories` 引用（SET NULL） |
| `job_discovery_trajectories` | 同上 | 只引用 `job_discovery_strategies` |
| `site_adapters` | 多站点扩展 | 无活跃代码引用 |
| `observed_sites` | 同上 | 无活跃代码引用 |
| `user_preferences` | 个性化发现 v1 | 无活跃代码引用 |
| `user_job_interactions` | 同上 | 无活跃代码引用 |
| `job_relevance_scores` | 同上 | 无活跃代码引用 |
| `personalized_discovery_runs` | 同上 | 被 2 张子表引用，簇内闭环 |
| `personalized_discovery_recommendations` | 同上 | 引用 `personalized_discovery_runs`（CASCADE）+ `job_discovery_tasks`（RESTRICT） |
| `user_discovery_source_statuses` | 同上 | 引用 `personalized_discovery_runs` + `job_discovery_tasks`（均 CASCADE） |

**依赖分析（已核对 models.py 的 ForeignKey 声明）**：14 张表构成完全自洽的遗留簇；唯一跨簇边是 `match_reports.analysis_session_id → analysis_sessions.id`（migration 0008 创建，`ondelete=RESTRICT`，**匿名约束**）。当前 backend 代码无任何 `MatchReport(...)` 构造点，`analysis_session_id` 列已无写入路径。

## 3. 设计

### 3.1 数据备份（drop 之前）

新增可复用脚本 `scripts/dump_legacy_tables.py`：
- 从 `backend.app.config.Settings` 读取数据库连接；
- 对 14 张表逐一导出（优先 mysqldump 单表模式；不可用时退化为 SELECT + INSERT 语句文件）；
- 输出 `backups/legacy_tables_<YYYYMMDD>.sql`（`backups/` 加入 .gitignore）；
- 导出后打印每张表的行数，供与线上 `COUNT(*)` 核对。

### 3.2 迁移 `20260812_0024_retire_legacy_tables.py`

**upgrade**（顺序即安全顺序）：
1. `match_reports`：先查 `information_schema.KEY_COLUMN_USAGE`（`table_name='match_reports' AND referenced_table_name='analysis_sessions'`）取得匿名 FK 的实际约束名 → `DROP FOREIGN KEY <name>` → `DROP INDEX ix_match_reports_analysis_session_id` → `DROP COLUMN analysis_session_id`。
2. 按 FK 依赖序 drop 14 张表（子先父后）：
   - `personalized_discovery_recommendations`、`user_discovery_source_statuses`（子）
   - `personalized_discovery_runs`、`job_discovery_evidence`、`discovered_job_candidates`（子）
   - `job_discovery_trajectories`（子）
   - `job_discovery_tasks`、`job_discovery_strategies`（父）
   - `observed_sites`、`site_adapters`、`user_preferences`、`user_job_interactions`、`job_relevance_scores`（独立）
   - `analysis_sessions`（最后）

**downgrade**：恢复 `match_reports` 列/索引/FK，并重建 14 张空表（create 逻辑从 0001、0008、7e8f22313271、ffc4f5917966 复制，含约束与索引）。

### 3.3 代码清理

- `backend/app/db/models.py`：删除 14 个模型类、簇内枚举（如 `JobDiscoveryTaskStatus`、`DiscoveredJobCandidateStatus` 等）、`User.sessions` relationship、`MatchReport.analysis_session_id` 列与 `ix_match_reports_analysis_session_id` 索引。
- `backend/app/db/__init__.py`：删除对应导出。
- `backend/app/domain/`：删除清理后无消费者的孤儿模块（预期 `personalized_discovery.py`、`preferences.py`；删除前逐个 trace 确认）。`job_feedback`、`job_submissions`、`company_research`、`interview_prep`、`application_tracking`、`profiles`、`agent_runtime` 保留（仍被活跃模型消费）。
- `pyproject.toml`：omit 列表同步移除已删 domain 模块；保留注释准确。
- `tests/manual/run_personalized_discovery_e2e.py`、`tests/manual/run_worker_ten_url_eval.py`：删除（引用已不存在的 `backend.app.services.personalized_discovery`，本已死代码）。
- `tests/integration/test_mysql_migration.py`：期望表集合移除 14 个表名。

### 3.4 文档

- `CLAUDE.md`：migration 序列追加 0024；删除 "JobDiscoveryTask States" 状态机段落；更新遗留 schema 相关措辞。
- 历史文档（WP1 技术文档、旧 spec/plan）不逐一改写。

## 4. 验证与验收标准

1. 备份文件存在，各表行数与 `COUNT(*)` 一致。
2. 干净库：`alembic upgrade head` 成功；`alembic downgrade -1` 成功（重建空表 + match_reports 列恢复）。
3. 开发库：`alembic upgrade head` 成功。
4. `pytest tests/unit -q` 全绿（含更新后的迁移回放测试）。
5. `ruff check backend tests scripts` 通过。
6. 表数 53 → 39（`SHOW TABLES` 核对）。
7. 覆盖门：只删代码不加分支，保留包的 100% branch 覆盖不破裂（`pytest --cov` 验证）。

## 5. 明确不做（后续事项）

- 合并双 runtime 表（`agent_runs` ↔ `deepagents_runs` 等）——另立 spec。
- `match_reports` 本身是否退役（当前无写入点，但与 WP2 匹配链路的现状绑定）——另立 spec。
- 陈旧 `__pycache__/*.pyc` 清理（git 不跟踪，无价值）。
- 历史 docs（WP1 技术文档、旧 spec/plan）改写。

## 6. 风险与对策

| 风险 | 对策 |
|---|---|
| `match_reports` 的 FK 是匿名约束，约束名未知 | upgrade 中查 `information_schema` 动态取得约束名后再 DROP |
| 覆盖门 100% branch 被破坏 | 清理只删代码不加分支；domain omit 列表同步；跑 `pytest --cov` 验证 |
| 迁移回放测试 `test_mysql_migration.py` 变红 | 同步更新期望表集合，随本变更集一起提交 |
| 误删仍有写入的表 | 备份先行 + 本 spec 的清单来自全仓库引用核对（无活跃引用为删除前提） |
