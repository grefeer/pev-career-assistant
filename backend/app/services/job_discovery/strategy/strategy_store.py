"""Strategy Store -- CRUD and atomic state updates for JobDiscoveryStrategy records.

All state transitions use atomic SQL UPDATEs to avoid read-modify-write races
in multi-worker deployments.

Key design notes:
- ``avg_duration_s`` stores the **latest** observed duration, not a rolling
  average.  This is intentional -- the plan trades statistical precision for
  implementation simplicity and instantaneous responsiveness.
- ``increment_success`` uses ``recovery_threshold`` for the recovery CASE
  expression. ``degradation_threshold`` governs degradation to ``unavailable``
  while ``recovery_threshold`` governs recovery from ``degraded`` to ``active``.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

import yaml
from sqlalchemy import and_, case, func, select, update
from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryStrategy
from backend.app.services.job_discovery.strategy.error_classifier import classify_error


_ALLOWED_TEMPLATE_ROOTS = {"task", "prev"}
_MAX_NESTING = 2  # task.field or prev.result.field (2 levels below root)


def validate_plan_yaml(plan_yaml: str) -> list[str]:
    """Validate all {{...}} template references in a plan YAML.

    Returns list of error messages (empty = valid).
    """
    errors: list[str] = []
    try:
        plan = yaml.safe_load(plan_yaml)
    except yaml.YAMLError as exc:
        return [f"Invalid YAML: {exc}"]
    steps = plan.get("plan", plan) if isinstance(plan, dict) else plan
    if not isinstance(steps, list):
        return ["plan_yaml must contain a list of steps"]

    template_pattern = re.compile(r"\{\{(.+?)\}\}")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            errors.append(f"step {i}: must be a dict, got {type(step).__name__}")
            continue
        params = step.get("params", {})
        for key, value in (params.items() if isinstance(params, dict) else {}):
            if not isinstance(value, str):
                continue
            for match in template_pattern.finditer(value):
                var_path = match.group(1).strip()
                parts = var_path.split(".")
                if parts[0] not in _ALLOWED_TEMPLATE_ROOTS:
                    errors.append(
                        f"step {i} param '{key}': unknown root '{parts[0]}' "
                        f"in '{{{{{var_path}}}}}'. Allowed: {_ALLOWED_TEMPLATE_ROOTS}"
                    )
                elif len(parts) - 1 > _MAX_NESTING:
                    errors.append(
                        f"step {i} param '{key}': nesting too deep in "
                        f"'{{{{{var_path}}}}}'. Max: {_MAX_NESTING} level"
                    )
    return errors


def get_active_strategies(db: Session) -> list[JobDiscoveryStrategy]:
    """Return all enabled strategies in active or degraded state, ordered by priority desc."""
    return list(
        db.scalars(
            select(JobDiscoveryStrategy)
            .where(
                JobDiscoveryStrategy.enabled == True,
                JobDiscoveryStrategy.status.in_(["active", "degraded"]),
            )
            .order_by(JobDiscoveryStrategy.priority.desc(), JobDiscoveryStrategy.success_count.desc())
        ).all()
    )


def get_strategy_by_id(db: Session, strategy_id: str) -> JobDiscoveryStrategy | None:
    """Fetch a single strategy by primary key."""
    return db.get(JobDiscoveryStrategy, strategy_id)


def increment_error_count(
    db: Session,
    strategy_id: str,
    last_error: dict[str, str],
) -> None:
    """Atomically increment error_count and record last error info.

    If error_count reaches degradation_threshold, atomically flips status to
    ``unavailable``.  Otherwise the strategy is marked ``degraded`` after the
    first error.
    """
    values: dict[str, Any] = {
        "error_count": JobDiscoveryStrategy.error_count + 1,
        "total_runs": JobDiscoveryStrategy.total_runs + 1,
        "last_error_tool": last_error.get("tool", ""),
        "last_error_reason": last_error.get("reason", "unknown"),
        "last_error_message": last_error.get("message", ""),
        "last_error_at": func.now(),
        "consecutive_ok": 0,
        "status": case(
            (JobDiscoveryStrategy.error_count + 1 >= JobDiscoveryStrategy.degradation_threshold, "unavailable"),
            (JobDiscoveryStrategy.error_count + 1 >= 1, "degraded"),
            else_=JobDiscoveryStrategy.status,
        ),
    }
    db.execute(
        update(JobDiscoveryStrategy)
        .where(JobDiscoveryStrategy.id == strategy_id)
        .values(**values)
    )


def increment_success(
    db: Session,
    strategy_id: str,
    duration_s: float | None = None,
) -> None:
    """Atomically increment success counters and reset error streak.

    Recovers status from ``degraded`` to ``active`` when ``consecutive_ok``
    reaches ``recovery_threshold`` (distinct from ``degradation_threshold``
    which governs the degradation direction).

    Args:
        db: Active database session.
        strategy_id: Primary key of the strategy to update.
        duration_s: Latest observed execution duration in seconds.  Stored as
            ``avg_duration_s`` but this is **not** a rolling average -- see
            module docstring.
    """
    values: dict[str, Any] = {
        "success_runs": JobDiscoveryStrategy.success_runs + 1,
        "total_runs": JobDiscoveryStrategy.total_runs + 1,
        "consecutive_ok": JobDiscoveryStrategy.consecutive_ok + 1,
        "error_count": 0,
        "status": case(
            (and_(
                JobDiscoveryStrategy.status == "degraded",
                JobDiscoveryStrategy.consecutive_ok + 1 >= JobDiscoveryStrategy.recovery_threshold,
            ), "active"),
            else_=JobDiscoveryStrategy.status,
        ),
    }
    if duration_s is not None:
        values["avg_duration_s"] = duration_s
    db.execute(
        update(JobDiscoveryStrategy)
        .where(JobDiscoveryStrategy.id == strategy_id)
        .values(**values)
    )


def get_strategies_due_for_health_check(
    db: Session,
    interval_hours: int = 24,
) -> list[JobDiscoveryStrategy]:
    """Return active/degraded strategies not health-checked within interval_hours."""
    cutoff = datetime.now(timezone.utc) - timedelta(hours=interval_hours)
    strategies = db.scalars(
        select(JobDiscoveryStrategy)
        .where(
            JobDiscoveryStrategy.status.in_(["active", "degraded"]),
            JobDiscoveryStrategy.enabled == True,
            (JobDiscoveryStrategy.last_health_check_at < cutoff)
            | (JobDiscoveryStrategy.last_health_check_at.is_(None)),
        )
    ).all()
    return list(strategies)


def record_health_check(
    db: Session,
    strategy_id: str,
    ok: bool,
    detail: str,
) -> None:
    """Record a health check result. Failure increments error_count atomically."""
    if ok:
        db.execute(
            update(JobDiscoveryStrategy)
            .where(JobDiscoveryStrategy.id == strategy_id)
            .values(last_health_check_at=func.now())
        )
    else:
        increment_error_count(
            db, strategy_id,
            last_error={
                "tool": "health_check",
                "reason": classify_error(detail),
                "message": detail,
            },
        )
        db.execute(
            update(JobDiscoveryStrategy)
            .where(JobDiscoveryStrategy.id == strategy_id)
            .values(last_health_check_at=func.now())
        )
