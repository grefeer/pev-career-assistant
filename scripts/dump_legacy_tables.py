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
