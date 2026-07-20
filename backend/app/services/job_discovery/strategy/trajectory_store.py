"""TrajectoryStore -- persist execution trajectories and schedule LLM annotations."""
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from backend.app.db.models import JobDiscoveryTrajectory
from backend.app.services.job_discovery.schemas import DiscoveryRunResult
from backend.app.services.job_discovery.strategy.error_classifier import classify_error
from backend.app.services.job_discovery.strategy.trajectory_buffer import TrajectoryBuffer


def save_trajectory(
    db: Session,
    trajectory: TrajectoryBuffer,
    result: DiscoveryRunResult,
    url: str,
    url_pattern: str | None,
) -> str:
    """Persist a TrajectoryBuffer as a JobDiscoveryTrajectory row.

    Args:
        db: Active database session.
        trajectory: The in-memory trajectory buffer with recorded steps.
        result: The final run result status and summary.
        url: The source URL that was navigated.
        url_pattern: The URL pattern that matched this trajectory (may be None).

    Returns:
        The new JobDiscoveryTrajectory primary key (UUID string).
    """
    buf_dict = trajectory.to_dict()
    fail_idx = trajectory.failed_step_index
    failed_step = buf_dict["steps"][fail_idx] if fail_idx is not None else None

    traj = JobDiscoveryTrajectory(
        task_id=trajectory.task_id,
        strategy_id=trajectory.strategy_id,
        executor_type=trajectory.executor_type,
        overall_status=_map_status(result, trajectory),
        url=url,
        url_pattern=url_pattern,
    )

    if failed_step is not None:
        traj.failed_at_step = fail_idx
        traj.failed_tool = failed_step["tool"]
        traj.failed_params = failed_step.get("params")
        traj.failed_error_type = failed_step.get("error_type")
        traj.failed_error_message = failed_step.get("error", "")
        traj.failed_error_reason = classify_error(failed_step.get("error", ""))

    # Separate completed steps (before failure) from fallback steps (after).
    # overall_status is set by _map_status above -- no duplicate assignment here.
    if fail_idx is not None:
        pre_steps = buf_dict["steps"][:fail_idx]
        post_steps = buf_dict["steps"][fail_idx + 1 :]
        traj.completed_steps = [s for s in pre_steps if s["status"] == "ok"]
        traj.fallback_trace = [s for s in post_steps if s.get("is_fallback")]
    else:
        traj.completed_steps = [s for s in buf_dict["steps"] if s["status"] == "ok"]

    db.add(traj)
    db.flush()
    return traj.id


def schedule_annotation(db: Session, trajectory_id: str) -> None:
    """Mark a trajectory for LLM annotation by updating its annotations JSON.

    The actual annotation is performed asynchronously by the worker.
    This function simply sets ``_annotation_pending = True`` in the
    annotations JSON column so that ``get_pending_annotations`` can find it.

    If no trajectory exists with the given ID, this is a no-op.
    """
    traj = db.get(JobDiscoveryTrajectory, trajectory_id)
    if traj is None:
        return
    existing = traj.annotations or {}
    existing["_annotation_pending"] = True
    traj.annotations = existing


def get_pending_annotations(db: Session) -> list[JobDiscoveryTrajectory]:
    """Return trajectories that have been scheduled for annotation.

    Filters on the ``_annotation_pending`` flag stored inside the
    annotations JSON column.  Annotations are scheduled via
    :func:`schedule_annotation`.
    """
    # SQLite-compatible: filter by IS NOT NULL first, then check the flag.
    results = (
        db.query(JobDiscoveryTrajectory)
        .filter(JobDiscoveryTrajectory.annotations.isnot(None))
        .all()
    )
    return [
        t
        for t in results
        if t.annotations and t.annotations.get("_annotation_pending") is True
    ]


def _map_status(result: DiscoveryRunResult, trajectory: TrajectoryBuffer) -> str:
    """Derive ``overall_status`` from ``DiscoveryRunResult`` and trajectory state.

    - If no steps failed: delegate to ``result.status``.
    - If a step failed **and** subsequent fallback steps exist: ``partial_fallback``.
    - If a step failed **and** no fallback steps: ``failed``.

    NOTE: Uses ``is not None`` (walrus-operator-free) because ``fail_idx``
    of 0 is falsy but still a valid failure index.
    """
    fail_idx = trajectory.failed_step_index
    if fail_idx is not None:
        buf_dict = trajectory.to_dict()
        has_fallback = any(
            s.get("is_fallback") for s in buf_dict["steps"][fail_idx + 1 :]
        )
        return "partial_fallback" if has_fallback else "failed"
    return result.status if result.status in ("succeeded", "partial_success") else result.status
