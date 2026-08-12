# 遗留表退役（sub-project 4 · DB 部分）实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 物理删除 14 张遗留 MySQL 表（job-discovery / site-adapter / personalized-discovery / analysis-session schema），表数 53 → 39，并清除全部代码残留，不触碰任何活跃路径。

**Architecture:** 先备份（`scripts/dump_legacy_tables.py` → `backups/legacy_tables_<date>.sql`），再新增 Alembic 迁移 0024 一次性 drop 14 张表（先摘除 `match_reports → analysis_sessions` 匿名 FK），最后清理 ORM 模型、domain 孤儿模块、死脚本与文档。downgrade 按原始迁移 DDL 重建空表，保证迁移可回放。

**Tech Stack:** Python 3.12 / SQLAlchemy 2.0 / Alembic / MySQL 8.4 / pydantic-settings / pytest / ruff

## Global Constraints

- **MySQL 是唯一权威**：业务状态不落 Redis；本任务全部改动只发生在 MySQL schema + 代码残留。
- **先备份再删（用户批准的数据策略）**：任何 `DROP TABLE` 之前必须先运行 dump 脚本并核对行数。
- **只删 14 张批准的表**：`analysis_sessions`、`job_discovery_tasks`、`job_discovery_evidence`、`discovered_job_candidates`、`job_discovery_strategies`、`job_discovery_trajectories`、`site_adapters`、`observed_sites`、`user_preferences`、`user_job_interactions`、`job_relevance_scores`、`personalized_discovery_runs`、`personalized_discovery_recommendations`、`user_discovery_source_statuses`。`match_reports` 只摘除 `analysis_session_id` 列，其余不动。
- **不触碰活跃路径**：PEV 双 runtime、WP2 职位链路、档案/简历、执行器子系统、`seen_jobs`、`job_discovery/tools/*` 全部保持原样。
- **迁移编号**：新文件 `20260812_0024_retire_legacy_tables.py`，`down_revision = "20260808_0023"`，历史迁移（0001、0008、7e8f22313271、ffc4f5917966）保持可回放。
- **覆盖门**：保留生产包 100% branch 不破裂（只删代码不加分支）；`pyproject.toml` omit 列表同步移除已删 domain 模块。
- **明确不做**：双 runtime 表合并、`match_reports` 本身退役、历史文档改写、`__pycache__` 清理。

## 与 spec 的三处事实性偏差（已核对代码，计划按此执行）

1. **死脚本数量**：spec §3.3 只点名 2 个 manual 脚本；实际有 **8 个**（7 个 git 跟踪 + 1 个仅本地磁盘）import 了即将删除的模型或已不存在的服务，全部已死。Task 3 一并删除；`run_skill_ten_url_eval.py` 保留（它 import 的 `deepagents_runner` 已不存在、本已死，但不在本任务范围，留给后续清理）。
2. **test_mysql_migration.py 不需要删期望表名**：`analysis_sessions` 出现在 `BUSINESS_TABLES` 中且被 0004 阶段的**全表等值断言**（第 201 行）使用——0004 时该表仍存在，**必须保留**；其余 13 张表不在任何期望集合里。改为在 head 升级后**新增断言**（14 张表不存在 + `match_reports` 无 `analysis_session_id`）。
3. **downgrade 的 `match_reports.analysis_session_id` 重建为 `nullable=True`**：原列是 `NOT NULL` 且无默认值，对非空表（真实库 downgrade 场景）重加 NOT NULL 列会失败；恢复路径优先保证可执行，schema 差异在 Task 2 注释中说明。

---

### Task 1: 备份脚本 `scripts/dump_legacy_tables.py`

**Files:**
- Create: `scripts/dump_legacy_tables.py`
- Modify: `.gitignore`（追加 `backups/`）
- Test: 对开发库实际运行一次（只读 + 写备份文件，无破坏）

**Interfaces:**
- Produces: 可执行脚本 `scripts/dump_legacy_tables.py`，用法 `python scripts/dump_legacy_tables.py [--out-dir backups]`；输出 `<out-dir>/legacy_tables_<YYYYMMDD>.sql`；每表打印行数。Task 2 的 Step 1 直接运行它。

- [ ] **Step 1: 创建 `backups/` 目录并加入 .gitignore**

```bash
mkdir -p backups
```

在 `.gitignore` 的 "Runtime data" 块（`data/` 行之后）追加：

```gitignore
# Legacy-table retirement backups (sub-project 4)
backups/
```

- [ ] **Step 2: 写备份脚本（完整代码）**

```python
"""Dump the 14 legacy tables before retirement (sub-project 4, DB part).

Usage::

    .\\.venv\\Scripts\\python.exe scripts\\dump_legacy_tables.py
    .\\.venv\\Scripts\\python.exe scripts\\dump_legacy_tables.py --out-dir backups

Reads ``Settings.database_url`` (env ``DATABASE_URL``, or the root ``.env``
file).  For each legacy table, prefers ``mysqldump`` (single-table mode; the
password travels via the ``MYSQL_PWD`` env var so it never appears in argv or
logs) and falls back to pure-SQL SELECT + INSERT statements when ``mysqldump``
is not on PATH.  Writes ``<out-dir>/legacy_tables_<YYYYMMDD>.sql`` and prints
per-table row counts for reconciliation against the live ``COUNT(*)``.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import sqlalchemy as sa  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from backend.app.config import Settings  # noqa: E402

LEGACY_TABLES: tuple[str, ...] = (
    "analysis_sessions",
    "job_discovery_tasks",
    "job_discovery_evidence",
    "discovered_job_candidates",
    "job_discovery_strategies",
    "job_discovery_trajectories",
    "site_adapters",
    "observed_sites",
    "user_preferences",
    "user_job_interactions",
    "job_relevance_scores",
    "personalized_discovery_runs",
    "personalized_discovery_recommendations",
    "user_discovery_source_statuses",
)


def _dump_via_mysqldump(url: sa.engine.URL, table: str, out_path: Path) -> bool:
    """Dump one table with mysqldump; return False when mysqldump is missing."""
    if shutil.which("mysqldump") is None:
        return False
    env = {**os.environ, "MYSQL_PWD": url.password or ""}
    cmd = [
        "mysqldump",
        f"-h{url.host}",
        f"-P{url.port or 3306}",
        f"-u{url.username or 'root'}",
        "--no-tablespaces",
        "--single-transaction",
        "--skip-lock-tables",
        url.database or "",
        table,
    ]
    with out_path.open("a", encoding="utf-8") as out:
        result = subprocess.run(cmd, env=env, stdout=out, stderr=subprocess.PIPE, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"mysqldump failed for {table}: {result.stderr[:500]}")
    return True


def _dump_via_sql(engine: sa.Engine, table: str, out_path: Path) -> None:
    """Fallback: SELECT * and write INSERT statements (param-bound values)."""
    with engine.begin() as conn:
        rows = conn.execute(sa.text(f"SELECT * FROM `{table}`")).mappings().all()
    with out_path.open("a", encoding="utf-8") as out:
        out.write(f"-- {table}: {len(rows)} rows\n")
        for row in rows:
            cols = ", ".join(f"`{k}`" for k in row.keys())
            vals = ", ".join(_py_literal(v) for v in row.values())
            out.write(f"INSERT INTO `{table}` ({cols}) VALUES ({vals});\n")


def _py_literal(value: object) -> str:
    """Serialize a Python value into a MySQL literal (fallback dumper)."""
    import json

    if value is None:
        return "NULL"
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, (bytes, bytearray)):
        return "X'" + value.hex() + "'"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, (list, dict)):
        value = json.dumps(value, ensure_ascii=False)
    text = str(value).replace("\\", "\\\\").replace("'", "''")
    return "'" + text + "'"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Dump the 14 legacy tables before retirement."
    )
    parser.add_argument(
        "--out-dir",
        default=str(PROJECT_ROOT / "backups"),
        help="Directory for the dump file (default: backups/)",
    )
    args = parser.parse_args()

    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"legacy_tables_{date.today():%Y%m%d}.sql"
    out_path.write_text("-- Legacy-table retirement dump (sub-project 4)\n", encoding="utf-8")

    settings = Settings()
    url = make_url(settings.database_url)
    engine = sa.create_engine(url, poolclass=sa.pool.NullPool)
    for table in LEGACY_TABLES:
        with engine.connect() as conn:
            count = conn.scalar(sa.text(f"SELECT COUNT(*) FROM `{table}`"))
        if not _dump_via_mysqldump(url, table, out_path):
            _dump_via_sql(engine, table, out_path)
        print(f"{table}: {count} rows")
    print(f"Backup written to {out_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

> 注意：`_dump_via_sql` 中的 `literal_execute_style` 分支是冗余设计，若在实现时发现 `dialect.literal_execute_style` 属性不可用，直接删除该行判断、恒走 `_py_literal` 即可（两分支产物相同）。**首选路径 mysqldump 始终有效时不会执行此分支。**

- [ ] **Step 3: 运行验证（对开发库只读执行）**

```powershell
.\.venv\Scripts\python.exe scripts\dump_legacy_tables.py
```

Expected: 14 行输出，形如 `analysis_sessions: N rows`……最后 `Backup written to backups\legacy_tables_20260812.sql`；文件存在且非空。

- [ ] **Step 4: 核对备份完整性**

```powershell
$f = Get-ChildItem backups\legacy_tables_*.sql | Sort-Object LastWriteTime | Select-Object -Last 1
(Get-Content $f.FullName | Measure-Object -Line).Lines
```

Expected: 行数 > 0；mysqldump 路径下含 14 个 `CREATE TABLE`（`Select-String "CREATE TABLE" $f` 计数 = 14）。

- [ ] **Step 5: Commit**

```bash
git add scripts/dump_legacy_tables.py .gitignore
git commit -m "chore(scripts): add legacy-table dump script for sub-project 4"
```

---

### Task 2: 迁移 `20260812_0024_retire_legacy_tables.py`

**Files:**
- Create: `alembic/versions/20260812_0024_retire_legacy_tables.py`
- Modify: `CLAUDE.md`（migration 序列追加 0024、"Current head is `0023`" → `0024`）
- Test: 开发库真实 upgrade/downgrade 回放（依赖 Task 1 的备份）

**Interfaces:**
- Consumes: Task 1 的备份文件（Step 1 先跑 dump）。
- Produces: 迁移文件。upgrade 后 14 张表消失、`match_reports` 无 `analysis_session_id`；downgrade 后 14 张空表与 `match_reports` 列恢复。Task 4 的断言依赖它。

- [ ] **Step 1: 先运行备份脚本（强制前置）**

```powershell
.\.venv\Scripts\python.exe scripts\dump_legacy_tables.py
```

Expected: 14 行计数输出 + 备份文件存在。**行数记录在此，与 drop 前的 `COUNT(*)` 对照。**

- [ ] **Step 2: 写迁移文件（完整代码）**

```python
"""retire 14 legacy tables (sub-project 4)

Revision ID: 20260812_0024
Revises: 20260808_0023
Create Date: 2026-08-12 09:00:00.000000

Drops the retired job-discovery / site-adapter / personalized-discovery /
analysis-session schema (14 tables) and detaches the dead
``match_reports.analysis_session_id`` column (anonymous FK, index, column).
Downgrade rebuilds the 14 tables as EMPTY tables from the original migration
DDL (see per-table copy sources below) and restores the match_reports column
as nullable (NOT NULL re-add fails on non-empty tables; recovery path only).

Drop order = children before parents within the legacy cluster:
  recommendations/source_statuses -> runs/candidates/evidence -> trajectories
  -> tasks -> strategies -> independent tables -> analysis_sessions last.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "20260812_0024"
down_revision: Union[str, Sequence[str], None] = "20260808_0023"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

LEGACY_TABLES: tuple[str, ...] = (
    # children first, parents last (verified against models.py FKs)
    "personalized_discovery_recommendations",   # -> discovered_job_candidates, job_discovery_tasks
    "user_discovery_source_statuses",           # -> personalized_discovery_runs, job_discovery_tasks
    "personalized_discovery_runs",              # -> users
    "job_discovery_evidence",                   # -> job_discovery_tasks
    "discovered_job_candidates",                # -> job_discovery_tasks
    "job_discovery_trajectories",               # -> job_discovery_strategies, job_discovery_tasks
    "job_discovery_tasks",                      # -> job_sources, raw_job_records (active parents)
    "job_discovery_strategies",                 # no FKs
    "observed_sites",                           # no FKs
    "site_adapters",                            # no FKs
    "user_preferences",                         # -> users
    "user_job_interactions",                    # -> users, job_postings
    "job_relevance_scores",                     # -> users, job_postings, confirmed_profile_versions
    "analysis_sessions",                        # -> users (last: match_reports FK target)
)


def upgrade() -> None:
    """Detach match_reports, then drop the 14 legacy tables."""
    # --- 1. match_reports: drop the anonymous FK first, then index, then column ---
    # The FK is unnamed (0008 created it without a name), so resolve the actual
    # MySQL-generated constraint name at runtime.
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT constraint_name FROM information_schema.KEY_COLUMN_USAGE "
            "WHERE table_schema = DATABASE() AND table_name = 'match_reports' "
            "AND referenced_table_name = 'analysis_sessions'"
        )
    ).fetchall()
    if rows:
        op.drop_constraint(rows[0][0], "match_reports", type_="foreignkey")
    op.drop_index("ix_match_reports_analysis_session_id", table_name="match_reports")
    op.drop_column("match_reports", "analysis_session_id")

    # --- 2. drop the 14 legacy tables (children before parents) ---
    for table in LEGACY_TABLES:
        op.drop_table(table)


def downgrade() -> None:
    """Rebuild the 14 empty tables and restore the match_reports column.

    Copy each ``op.create_table(...)`` / ``op.create_index(...)`` /
    ``op.add_column(...)`` block VERBATIM from the source migration's
    ``upgrade()``, in the order below (parents before children so intra-cluster
    FKs resolve).  Skip data-migration statements (INSERT/UPDATE) - only DDL.
    """
    _COPY_SOURCES = [
        # (table, source migration file, line range in upgrade(), notes)
        ("analysis_sessions", "20260714_0001_platform_foundation.py", "48-70", "create_table + 2 indexes; unique constraint uq_analysis_sessions_thread_id"),
        ("job_relevance_scores", "20260722_0012_personal_mode_memory.py", "120-165", "create_table + uq + index ix_job_relevance_scores_user_score"),
        ("user_job_interactions", "20260722_0012_personal_mode_memory.py", "75-118", "create_table + uq + index ix_user_job_interactions_user_created"),
        ("user_preferences", "20260722_0012_personal_mode_memory.py", "30-72", "create_table + uq_user_preferences_user + index; PLUS 0013 lines 40-52 add_column role_synonyms/excluded_roles/personalized_discovery_min_score"),
        ("site_adapters", "20260718_0009_site_adapters.py", "21-56", "create_table + uq_site_adapters_adapter_id; PLUS 0010 lines 19-35 add_column rollout_stage/last_success_at/last_readonly_verified_at"),
        ("observed_sites", "20260718_0010_multi_site_extension.py", "49-76", "create_table + uq_observed_sites_site_code"),
        ("job_discovery_strategies", "ffc4f5917966_add_strategy_and_trajectory_tables.py", "21-51", "create_table + 3 indexes"),
        ("job_discovery_tasks", "7e8f22313271_add_job_discovery_tables.py", "22-112", "create_table + 2 indexes + uq; enums job_discovery_task_status/discovery_block_reason"),
        ("job_discovery_trajectories", "ffc4f5917966_add_strategy_and_trajectory_tables.py", "52-76", "create_table incl. FKs strategy_id + task_id + 2 indexes"),
        ("discovered_job_candidates", "7e8f22313271_add_job_discovery_tables.py", "143-226", "create_table + indexes"),
        ("job_discovery_evidence", "7e8f22313271_add_job_discovery_tables.py", "112-143", "create_table + indexes"),
        ("personalized_discovery_runs", "20260725_0013_personalized_job_discovery_v1.py", "56-75", "create_table + index ix_pdr_user_started"),
        ("user_discovery_source_statuses", "20260725_0013_personalized_job_discovery_v1.py", "155-210", "create_table + uq_udss_user_run_task_reason + index"),
        ("personalized_discovery_recommendations", "20260725_0013_personalized_job_discovery_v1.py", "82-155", "create_table incl. FKs + uq + 2 indexes"),
    ]
    for table, source, line_range, notes in _COPY_SOURCES:
        op.execute(f"-- downgrade: rebuilding {table} from {source} lines {line_range} ({notes})")

    # --- restore match_reports column (nullable: recovery-safe on non-empty tables) ---
    op.add_column(
        "match_reports",
        sa.Column("analysis_session_id", sa.String(length=36), nullable=True),
    )
    op.create_index(
        "ix_match_reports_analysis_session_id",
        "match_reports",
        ["analysis_session_id"],
        unique=False,
    )
    # Anonymous FK: MySQL auto-names it (same as 0008) - resolvable by the
    # information_schema lookup in upgrade().
    op.create_foreign_key(
        None,
        "match_reports",
        "analysis_sessions",
        ["analysis_session_id"],
        ["id"],
        ondelete="RESTRICT",
    )
```

> **downgrade 实现说明（不是占位符，是精确复制指令）**：上面 `_COPY_SOURCES` 表的每一行给出（表名、源迁移文件、`upgrade()` 中的行区间、要点）。实现时把该源文件对应行区间的 `op.create_table(...)` 及 `op.create_index(...)` 调用**逐字复制**进 `downgrade()`，按表中顺序放置（父表先建），跳过数据迁移语句。`op.f("ix_...")` 这类索引名包装保持原样。**复制后核对**：每个重建表的列集合、索引、唯一约束与源 create 块一致；`site_adapters` 与 `user_preferences` 必须额外包含后续迁移新增的列（见 notes）。每条复制语句上方保留一行 `op.execute("-- downgrade: rebuilding <table> ...")` 注释（如上代码所示），便于审计。

- [ ] **Step 3: 离线 SQL 冒烟（`--sql` 模式不连库，只走 env.py 配置）**

```powershell
.\.venv\Scripts\alembic.exe upgrade head --sql > $null
```

Expected: 退出码 0，无异常（`--sql` 离线模式在内存中生成全部 DDL 文本而不连接数据库）。失败则说明迁移代码有语法/操作错误。不需要也不能连库——如果 env.py 抛错，说明 `.env` 未配置，先用 CLAUDE.md 的 env var 检查清单补齐再跑。

- [ ] **Step 4: 开发库真实回放（依赖 Step 1 备份）**

```powershell
# 4a. 确认当前在 0023
.\.venv\Scripts\alembic.exe current
# Expected: 20260808_0023

# 4b. upgrade head（会 drop 14 张表；备份已在前置 Step 1 完成）
.\.venv\Scripts\alembic.exe upgrade head
# Expected: 成功；alembic current = 20260812_0024

# 4c. 表数核对（39）
$env:MYSQL_PWD=$env:DB_PASSWORD
mysql -h127.0.0.1 -P3307 -uroot -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='career_assistant'" 
# Expected: 39（53 - 14）

# 4d. downgrade -1（重建空表 + match_reports 列恢复）
.\.venv\Scripts\alembic.exe downgrade -1
# Expected: 成功；alembic current = 20260808_0023
mysql -h127.0.0.1 -P3307 -uroot -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='career_assistant'"
# Expected: 53

# 4e. 再 upgrade head（回到 39）
.\.venv\Scripts\alembic.exe upgrade head
# Expected: 成功；表数回到 39
```

> 若数据库名不是 `career_assistant`，以 `.env` 中 `DATABASE_URL` 为准替换 `table_schema`。若 4c 中途失败，按备份文件恢复后重试；迁移本身不可部分重入（index/column drop 幂等性无保证），**一次跑完 upgrade 或 downgrade**。

- [ ] **Step 5: 更新 CLAUDE.md 迁移序列**

在 `alembic/versions/` 序列（`0023` 行之后）追加：

```markdown
- `0024`: Retire 14 legacy tables (job-discovery / site-adapter / personalized-discovery / analysis-session schemas)
```

并把 "Current head is `0023`" 改为 "Current head is `0024`"。

- [ ] **Step 6: Commit**

```bash
git add alembic/versions/20260812_0024_retire_legacy_tables.py CLAUDE.md
git commit -m "feat(migrations): add 0024 retire legacy tables"
```

---

### Task 3: 代码清理（models / domain / 导出 / pyproject / 死脚本 / CLAUDE.md）

**Files:**
- Modify: `backend/app/db/models.py`（8 处删除）
- Modify: `backend/app/db/__init__.py`（imports + `__all__`）
- Delete: `backend/app/domain/preferences.py`、`backend/app/domain/personalized_discovery.py`
- Modify: `pyproject.toml`（omit 列表 + 注释）
- Delete: 8 个死脚本（7 个 `git rm` + 1 个本地删除）
- Modify: `CLAUDE.md`（删除 JobDiscoveryTask States 段落）

**Interfaces:**
- Consumes: 无（Task 2 的迁移在 DB 侧先行，不影响本任务的代码）。
- Produces: 干净的可 import 代码库——`pytest tests/unit -q` 全绿、`ruff` 通过、无任何对已删模型/模块的残留引用。Task 5 在它之上做全量验证。

- [ ] **Step 1: 删除 models.py 的 8 处残留（先确认行号，再删）**

用 `grep -n` 逐处确认下列锚点行号仍与计划一致，然后删除（删除后锚点消失即成功）：

| # | 删除内容 | 计划行号（删除前） |
|---|---|---|
| 1 | `from backend.app.domain.preferences import (JobInteractionType, WorkModePreference)` 与 `from backend.app.domain.personalized_discovery import (RecommendationPresentationState, SourceStatusReason)` 两个 import 块 | 44-51 |
| 2 | `User.sessions` relationship（3 行） | 124-126 |
| 3 | `AnalysisSession` 类 | 129-142 |
| 4 | `MatchReport.analysis_session_id` 列（3 行）与 `__table_args__` 中 `Index("ix_match_reports_analysis_session_id", ...)`（1 行） | 184-186、219 |
| 5 | Site Adapter 段：`SiteAdapterStatus`、`SiteAdapter`、`ObservedSite`（含段注释） | 906-971 |
| 6 | Job Discovery Agent 段：`JobDiscoveryTaskStatus`、`DiscoveredJobCandidateStatus`、`DiscoveryBlockReason`、`JobDiscoveryTask`、`JobDiscoveryEvidence`、`DiscoveredJobCandidate`（含段注释） | 974-1127 |
| 7 | Strategy Router 段：`JobDiscoveryStrategy`、`JobDiscoveryTrajectory`（含段注释） | 1130-1206 |
| 8 | Personal memory 段（注释 + `UserPreference` + `UserJobInteraction` + `JobRelevanceScore`）与 Personalized discovery v1 段（注释 + `PersonalizedDiscoveryRun` + `PersonalizedDiscoveryRecommendation` + `UserDiscoverySourceStatus`） | 1209-1404 |

删除前逐处验证锚点：

```bash
grep -n "^class AnalysisSession\|^class UserPreference\|^class JobDiscoveryTask\|^class UserDiscoverySourceStatus\|^from backend.app.domain.preferences\|^from backend.app.domain.personalized_discovery\|sessions: Mapped\[list\[\"AnalysisSession\"\]\]\|analysis_session_id" backend/app/db/models.py
```

Expected: 每处锚点唯一命中，行号与上表一致。删除后重跑同一 grep：Expected: 0 命中。

- [ ] **Step 2: 清理 `db/__init__.py` 导出**

从 `from .models import (...)` 与 `__all__` 中删除以下 9 个名字（其余保持字母序不动）：

```text
AnalysisSession, DiscoveredJobCandidate, DiscoveredJobCandidateStatus,
JobDiscoveryEvidence, JobDiscoveryTask, JobDiscoveryTaskStatus,
ObservedSite, SiteAdapter, SiteAdapterStatus
```

（`UserPreference`/`UserJobInteraction`/`JobRelevanceScore`/`PersonalizedDiscoveryRun`/`PersonalizedDiscoveryRecommendation`/`UserDiscoverySourceStatus` 本就不在 `db/__init__.py` 导出中，无需处理。）

- [ ] **Step 3: 删除 domain 孤儿模块**

```bash
git rm backend/app/domain/preferences.py backend/app/domain/personalized_discovery.py
```

删除前确认全仓库只剩这两处引用（Task 2 已删的 models.py import 除外）：

```bash
grep -rn "domain.preferences\|domain.personalized_discovery\|WorkModePreference\|JobInteractionType\|RecommendationPresentationState\|SourceStatusReason\|normalize_role_terms\|title_matches_role_recall\|validate_application_url\|source_status_copy" backend/ tests/unit/ scripts/ 
```

Expected: 0 命中（`tests/manual/` 下命中将在 Step 5 一并删除，允许此时仍存在）。

- [ ] **Step 4: 同步 pyproject.toml omit 列表**

删除 omit 列表中这两行（第 32、37 行）：

```text
  "backend/app/domain/personalized_discovery.py",
  "backend/app/domain/preferences.py",
```

并把第 18-24 行注释中 "pre-PEV paths retained solely for StrEnum imports ... slated for retirement in sub-project 4" 改为：

```text
# The ``domain/*`` modules omitted below are pre-PEV paths retained solely
# for ``StrEnum`` imports consumed by ``db/models.py``; their transition/
# helper functions are vestigial.  The personalized-discovery/preferences
# pair was retired in sub-project 4 (legacy table retirement, migration 0024).
# Retained production packages (api, config, db.session/base,
# domain.agent_runtime, domain.profiles, middleware, main, repositories,
# services.agent_runtime, services.career_skills, services.{auth,profiles,
# rate_limit,storage,profile_parser},
# services.job_discovery.{schemas,tools.jd_extraction}) remain measured and
# must stay at 100%.
```

- [ ] **Step 5: 删除 8 个死脚本**

7 个 git 跟踪的（全部 import 已删除模型或已不存在服务，删除是唯一出路）：

```bash
git rm tests/manual/run_personal_assistant_e2e.py \
       tests/manual/run_worker_ten_url_eval.py \
       tests/manual/test_adapter_failure_takeover.py \
       tests/manual/test_non_alibaba_urls.py \
       tests/manual/test_pev_live_smoke.py \
       tests/manual/test_strategy_router_live_smoke.py \
       tests/manual/test_strategy_router_smoke.py
```

1 个仅本地磁盘（git 未跟踪，.gitignore 规则 `tests/manual/*` 覆盖）：

```powershell
Remove-Item tests\manual\run_personalized_discovery_e2e.py
```

删除后验证残留引用归零：

```bash
grep -rln "JobDiscoveryStrategy\|JobDiscoveryTask\|JobDiscoveryTrajectory\|DiscoveredJobCandidate\|UserPreference\|UserJobInteraction\|JobRelevanceScore\|PersonalizedDiscovery\|UserDiscoverySourceStatus\|AnalysisSession\|SiteAdapterStatus\|DiscoveryBlockReason\|WorkModePreference\|JobInteractionType" backend/ tests/unit/ tests/e2e/ tests/integration/ scripts/
```

Expected: 0 命中（`executor/` 下的 `SiteAdapter` 是本项目的 Protocol，不在此模式中，勿误删）。

- [ ] **Step 6: 删除 CLAUDE.md 的 JobDiscoveryTask States 段落**

删除整个段落（含代码块与 `>` 引注）：

```markdown
### JobDiscoveryTask States

```
queued -> running -> succeeded / partial_success / needs_manual_review / failed / cancelled
```

> The `JobDiscoveryTask` model and table persist, but the worker that processed them has been retired. This state machine documents the retained schema, not an active processing flow.
```

- [ ] **Step 7: 验证（单元套件 + lint + import 冒烟）**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q
# Expected: 全绿（用例数 = 改动前计数；只删代码不加用例，用例数应不变）

.\.venv\Scripts\python.exe -m ruff check backend tests scripts
# Expected: 无新增违规（models.py 删除后 import 排序可能变化，若 ruff 报 I001 用 --fix）

.\.venv\Scripts\python.exe -c "from backend.app.db.models import Base, MatchReport, User; print('import ok')"
# Expected: import ok
```

- [ ] **Step 8: Commit**

```bash
git add -A backend/ pyproject.toml CLAUDE.md tests/
git commit -m "refactor(db): remove 14 legacy models and dead code residuals"
```

---

### Task 4: 迁移回放测试断言（test_mysql_migration.py）

**Files:**
- Modify: `tests/integration/test_mysql_migration.py`
- Test: 真实 MySQL destructive fixture 回放

**Interfaces:**
- Consumes: Task 2 的迁移 0024（head 升级后 14 张表消失）。
- Produces: head 级断言——任何未来的迁移若让遗留表复活，测试即红。

- [ ] **Step 1: 加 `LEGACY_TABLES` 常量**

在 `ALEMBIC_TABLES = {"alembic_version"}`（第 35 行）之后插入：

```python
LEGACY_TABLES = {
    "analysis_sessions",
    "job_discovery_tasks",
    "job_discovery_evidence",
    "discovered_job_candidates",
    "job_discovery_strategies",
    "job_discovery_trajectories",
    "site_adapters",
    "observed_sites",
    "user_preferences",
    "user_job_interactions",
    "job_relevance_scores",
    "personalized_discovery_runs",
    "personalized_discovery_recommendations",
    "user_discovery_source_statuses",
}
```

> 注意：**不要**从 `BUSINESS_TABLES` 移除 `analysis_sessions`——第 201 行的全表等值断言发生在 0004 阶段（该表此时仍存在），删掉会误报。

- [ ] **Step 2: 在 head 升级后加断言**

在 `_run_alembic("upgrade", "head", env=env)`（第 351 行）之后、`assert FEEDBACK_TABLES <= ...`（第 352 行）之前插入：

```python
            head_inspector = sa.inspect(engine)
            assert LEGACY_TABLES.isdisjoint(set(head_inspector.get_table_names()))
            match_columns_at_head = {
                col["name"] for col in head_inspector.get_columns("match_reports")
            }
            assert "analysis_session_id" not in match_columns_at_head
```

- [ ] **Step 3: 运行回放测试（需要本机 MySQL destructive fixture）**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/integration/test_mysql_migration.py -v
```

Expected: 3 个用例通过（含 destructive fixture 的完整 upgrade/downgrade 回放：downgrade base → upgrade 0003/0004/0006 → head → downgrade 回退 → head）。若 fixture 在本机不可用（skip），记录为手动验证项并在 Task 5 补跑。

- [ ] **Step 4: Commit**

```bash
git add tests/integration/test_mysql_migration.py
git commit -m "test(integration): assert legacy tables absent at migration head"
```

---

### Task 5: 全量验证（验收清单）

**Files:** 无（纯验证）

- [ ] **Step 1: 单元套件 + 覆盖门**

```powershell
.\.venv\Scripts\python.exe -m pytest tests/unit/ -q
.\.venv\Scripts\python.exe -m pytest tests/unit/ --cov --cov-report=term-missing
# Expected: 全绿；coverage fail_under=100 不破裂（只删代码，branch 数应下降或持平）
```

- [ ] **Step 2: lint**

```powershell
.\.venv\Scripts\python.exe -m ruff check backend tests scripts
# Expected: 通过
```

- [ ] **Step 3: 表数与残留核对（开发库）**

```powershell
mysql -h127.0.0.1 -P3307 -uroot -e "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema='career_assistant'"
# Expected: 39

.\.venv\Scripts\alembic.exe current
# Expected: 20260812_0024
```

- [ ] **Step 4: 迁移回放（含 downgrade）已由 Task 2 Step 4 与 Task 4 Step 3 覆盖；此处只复跑一次 head**

```powershell
.\.venv\Scripts\alembic.exe upgrade head
# Expected: 成功（幂等：已是最新则无操作）
```

- [ ] **Step 5: 对照 spec 验收标准逐项打勾**

| spec 验收 | 验证点 | 状态 |
|---|---|---|
| 1. 备份存在且行数一致 | Task 1 Step 3-4 的输出与 drop 前 `COUNT(*)` 一致 |  |
| 2. 干净库 upgrade/downgrade 回放 | Task 4 Step 3（destructive fixture 全链回放） |  |
| 3. 开发库 upgrade head 成功 | Task 2 Step 4 / Task 5 Step 4 |  |
| 4. `pytest tests/unit -q` 全绿 | Task 3 Step 7 / Task 5 Step 1 |  |
| 5. ruff 通过 | Task 3 Step 7 / Task 5 Step 2 |  |
| 6. 表数 53 → 39 | Task 2 Step 4c / Task 5 Step 3 |  |
| 7. 覆盖门不破裂 | Task 5 Step 1 |  |

- [ ] **Step 6: 收尾提交（若验证中发现任何遗漏修正）**

```bash
git status
# 若干净则跳过；若有修正，合并进相应 Task 的提交（git commit --amend 或新提交均可，保持历史可读）
```

---

## 参考（实现时需要的既有事实）

- `models.py` 中 14 个类的现状行号已在 Task 3 Step 1 表格给出；所有 FK/约束已在 brainstorming 阶段核对（唯一跨簇边 `match_reports.analysis_session_id → analysis_sessions.id` 为 0008 匿名约束）。
- 迁移 DDL 来源（downgrade 复制用）：`0001:48-70`（analysis_sessions）、`0009:21-56` + `0010:19-35`（site_adapters）、`0010:49-76`（observed_sites）、`7e8f22313271:22-112/112-143/143-226`（tasks/evidence/candidates）、`ffc4f5917966:21-51/52-76`（strategies/trajectories）、`0012:30-72/75-118/120-165`（preferences/interactions/relevance）+ `0013:40-52`（preferences 补列）、`0013:56-75/82-155/155-210`（runs/recommendations/source_statuses）。
- `match_reports` 在 backend 中无任何构造点（无 `MatchReport(` 调用），`analysis_session_id` 无写入路径——摘列安全。
- `tests/unit` 对 14 个模型/枚举/domain 模块零引用（已核对）；`tests/e2e`、`scripts/` 零引用。
- `executor/adapters/base.py` 的 `SiteAdapter` 是 executor 自己的 Protocol，与 `db.models.SiteAdapter` 无关，**勿误删**。
