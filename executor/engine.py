from __future__ import annotations

import logging
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Literal

from executor.browser import BrowserSession, FillReport, PageObservation
from executor.checkpoints import (
    CheckpointStore,
    ExecutorCheckpoint,
    CheckpointCorruptError,
)
from executor.client import (
    ExecutorApiClient,
    ApiUnauthorized,
    ApiConflict,
    ApiTaskNotFound,
    UncertainWriteResult,
)
from executor import EXECUTOR_VERSION
from executor.protocol import (
    ExecutorTaskDetail,
    ExecutorTaskPayload,
    PROTOCOL_VERSION,
)


logger = logging.getLogger(__name__)


class InjectedCrash(RuntimeError):
    """Raised at a configured fault point for recovery testing."""


class FaultPoint(StrEnum):
    AFTER_FIELD_CHECKPOINT_SAVED = "after_field_checkpoint_saved"
    AFTER_PENDING_EFFECT_CHECKPOINT_SAVED = (
        "after_pending_effect_checkpoint_saved"
    )


@dataclass(frozen=True)
class RunOutcome:
    kind: Literal[
        "ready_for_review",
        "waiting_for_human",
        "stopped_unauthorized",
        "stopped_conflict",
        "failed_safe",
        "result_observed",
    ]
    reason_code: str


@dataclass
class EngineState:
    task_id: str
    state_version: int = 0
    lease: str = ""


class ExecutorEngine:
    """Orchestrates executor simulation: heartbeat, lease, fill, report."""

    def __init__(
        self,
        client: ExecutorApiClient,
        browser: BrowserSession,
        checkpoints: CheckpointStore,
        fault_point: FaultPoint | None = None,
    ) -> None:
        self.client = client
        self.browser = browser
        self.checkpoints = checkpoints
        self._state: EngineState | None = None
        self._fault_point = fault_point

    # ------------------------------------------------------------------
    # Fault injection (recovery testing)
    # ------------------------------------------------------------------

    def _check_fault(self, point: FaultPoint) -> None:
        if self._fault_point is not None and self._fault_point == point:
            raise InjectedCrash(point)

    # ------------------------------------------------------------------
    # Checkpoint helpers
    # ------------------------------------------------------------------

    def _save_checkpoint(
        self,
        *,
        step: str = "fill_page",
        page_fingerprint: str = "sha256:pending",
        page_index: int | None = None,
        completed_field_keys: list[str] | None = None,
        completed_effect_keys: list[str] | None = None,
        pending_field_key: str | None = None,
        pending_effect_key: str | None = None,
        issue_counts: dict[str, int] | None = None,
    ) -> ExecutorCheckpoint | None:
        """Atomically persist a checkpoint from the current engine state."""
        state = self._state
        if state is None:
            return None
        try:
            cp = ExecutorCheckpoint(
                protocol_version=PROTOCOL_VERSION,
                task_id=state.task_id,
                task_state_version=state.state_version,
                step=step,
                page_index=page_index,
                page_fingerprint=page_fingerprint,
                completed_field_keys=completed_field_keys or [],
                completed_effect_keys=completed_effect_keys or [],
                pending_field_key=pending_field_key,
                pending_effect_key=pending_effect_key,
                issue_counts=issue_counts
                or {"missing": 0, "low": 0, "readback": 0, "defaulted": 0},
            )
            self.checkpoints.save(cp)
            return cp
        except Exception:
            logger.warning("failed to save checkpoint", exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Main run loop
    # ------------------------------------------------------------------

    def run(
        self,
        task_id: str | None = None,
        payload: ExecutorTaskPayload | None = None,
    ) -> RunOutcome:
        """Run one complete simulation cycle."""
        detail: ExecutorTaskDetail | None = None

        # Heartbeat
        try:
            self.client.heartbeat(EXECUTOR_VERSION)
        except Exception:
            logger.warning("heartbeat failed, continuing")

        # Issue lease and fetch detail
        if task_id:
            try:
                lease = self.client.issue_lease(task_id)
            except ApiUnauthorized:
                return RunOutcome("stopped_unauthorized", "lease_denied")
            except Exception:
                return RunOutcome("failed_safe", "lease_unavailable")

            try:
                detail = self.client.get_task(task_id, lease)
            except ApiUnauthorized:
                return RunOutcome("stopped_unauthorized", "detail_denied")
            except ApiTaskNotFound:
                return RunOutcome("failed_safe", "task_not_found")

            self._state = EngineState(
                task_id=detail.task_id,
                state_version=detail.state_version,
                lease=lease,
            )

            payload = detail.payload
        elif payload:
            self._state = EngineState(
                task_id=payload.task_id,
                state_version=payload.state_version,
                lease="",
            )
        else:
            return RunOutcome("failed_safe", "no_task_or_payload")

        if payload is None:
            return RunOutcome("failed_safe", "no_payload")

        # Check if we're in observation mode
        if detail and hasattr(detail, "status"):
            from executor.protocol import TaskStatus

            if detail.status == TaskStatus.OBSERVING_USER_SUBMISSION:
                return self._run_observation(payload)

        # Dispatch -> Running transition
        state = self._state
        if state and task_id:
            try:
                result = self.client.report_progress(
                    task_id=task_id,
                    lease=state.lease,
                    expected_version=state.state_version,
                    target_status="running",
                    page_fingerprint="sha256:pending",
                    page_index=None,
                    field_counts={
                        "confirmed": 0,
                        "defaulted": 0,
                        "missing": 0,
                        "low": 0,
                    },
                    reason_code=None,
                )
                state.state_version = result.state_version
            except ApiUnauthorized:
                return RunOutcome("stopped_unauthorized", "progress_denied")
            except ApiConflict as error:
                return self._reconcile_conflict(task_id, error)
            except UncertainWriteResult:
                pass

        # Open page and observe
        self.browser.open(str(payload.target_url))
        observation = self.browser.observe()

        # Load checkpoint for recovery
        cp: ExecutorCheckpoint | None = None
        if state:
            try:
                cp = self.checkpoints.load(state.task_id)
            except CheckpointCorruptError:
                cp = None

        if cp is not None:
            # Check for pending intermediate effect — don't retry the click
            if cp.pending_effect_key is not None:
                self.checkpoints.delete(state.task_id)
                return RunOutcome(
                    "ready_for_review", "intermediate_result_uncertain"
                )

            # Check if page fingerprint changed since checkpoint
            if (
                cp.page_fingerprint != "sha256:pending"
                and cp.page_fingerprint != observation.fingerprint
            ):
                self.checkpoints.delete(state.task_id)
                return RunOutcome(
                    "ready_for_review", "page_topology_changed"
                )

            # Filter out fields already completed in a previous run
            completed = set(cp.completed_field_keys)
            fields_to_fill = [f for f in payload.fields if f.field_key not in completed]

            # Delete checkpoint so the current run writes fresh ones
            self.checkpoints.delete(state.task_id)
        else:
            fields_to_fill = payload.fields

        # Check if human gate
        if observation.human_required:
            reason = f"{observation.human_required}_required"
            if task_id and state:
                try:
                    result = self.client.report_progress(
                        task_id=task_id,
                        lease=state.lease,
                        expected_version=state.state_version,
                        target_status="waiting_for_human",
                        page_fingerprint=observation.fingerprint,
                        page_index=observation.page_index,
                        field_counts={
                            "confirmed": 0,
                            "defaulted": 0,
                            "missing": 0,
                            "low": 0,
                        },
                        reason_code=reason,
                    )
                    state.state_version = result.state_version
                except Exception:
                    pass
            return RunOutcome("waiting_for_human", reason)

        # Fill confirmed fields
        report = self.browser.fill_confirmed(fields_to_fill)

        # Save checkpoint after fill (completed field keys recorded)
        if state:
            self._save_checkpoint(
                page_fingerprint=observation.fingerprint,
                page_index=observation.page_index,
                completed_field_keys=report.confirmed_keys,
                issue_counts={
                    "missing": len(report.missing_keys),
                    "low": len(report.low_confidence_keys),
                    "readback": len(report.readback_mismatch_keys),
                    "defaulted": len(report.defaulted_keys),
                },
            )

        # Fault injection after fill checkpoint is persisted
        self._check_fault(FaultPoint.AFTER_FIELD_CHECKPOINT_SAVED)

        # Check for readback mismatch
        if report.readback_mismatch_keys:
            if task_id and state:
                try:
                    result = self.client.report_progress(
                        task_id=task_id,
                        lease=state.lease,
                        expected_version=state.state_version,
                        target_status="ready_for_review",
                        page_fingerprint=observation.fingerprint,
                        page_index=observation.page_index,
                        field_counts={
                            "confirmed": len(report.confirmed_keys),
                            "defaulted": len(report.defaulted_keys),
                            "missing": len(report.missing_keys),
                            "low": len(report.low_confidence_keys),
                        },
                        reason_code="readback_mismatch",
                    )
                    state.state_version = result.state_version
                except Exception:
                    pass
            return RunOutcome("ready_for_review", "readback_mismatch")

        # Evaluate safety decision
        decision = self.browser.action_decision(observation)
        if not decision.allowed:
            if task_id and state:
                try:
                    result = self.client.report_progress(
                        task_id=task_id,
                        lease=state.lease,
                        expected_version=state.state_version,
                        target_status="ready_for_review",
                        page_fingerprint=observation.fingerprint,
                        page_index=observation.page_index,
                        field_counts={
                            "confirmed": len(report.confirmed_keys),
                            "defaulted": len(report.defaulted_keys),
                            "missing": len(report.missing_keys),
                            "low": len(report.low_confidence_keys),
                        },
                        reason_code=decision.reason_code,
                    )
                    state.state_version = result.state_version
                except Exception:
                    pass
            return RunOutcome("ready_for_review", decision.reason_code)

        # Safe intermediate action: save pending-effect checkpoint
        if state:
            self._save_checkpoint(
                step="safe_intermediate",
                page_fingerprint=observation.fingerprint,
                page_index=observation.page_index,
                completed_field_keys=report.confirmed_keys,
                pending_effect_key=f"page-{observation.page_index or 0}:safe-next",
                issue_counts={
                    "missing": len(report.missing_keys),
                    "low": len(report.low_confidence_keys),
                    "readback": len(report.readback_mismatch_keys),
                    "defaulted": len(report.defaulted_keys),
                },
            )

        # Fault injection after pending-effect checkpoint is persisted
        self._check_fault(FaultPoint.AFTER_PENDING_EFFECT_CHECKPOINT_SAVED)

        # Report progress update
        if task_id and state:
            try:
                result = self.client.report_progress(
                    task_id=task_id,
                    lease=state.lease,
                    expected_version=state.state_version,
                    target_status="running",
                    page_fingerprint=observation.fingerprint,
                    page_index=observation.page_index,
                    field_counts={
                        "confirmed": len(report.confirmed_keys),
                        "defaulted": len(report.defaulted_keys),
                        "missing": len(report.missing_keys),
                        "low": len(report.low_confidence_keys),
                    },
                    reason_code=None,
                )
                state.state_version = result.state_version
            except ApiConflict as error:
                return self._reconcile_conflict(task_id, error)
            except Exception:
                pass

        # Click safe intermediate
        try:
            self.browser.click_safe_intermediate(observation)
        except Exception:
            return RunOutcome("failed_safe", "intermediate_click_failed")

        # After click, re-observe and report review
        self.browser.observe()

        # Navigation confirmed — clear pending effect
        if state:
            self._save_checkpoint(
                step="safe_intermediate",
                page_fingerprint="sha256:after_nav",
                page_index=observation.page_index,
                completed_field_keys=report.confirmed_keys,
                completed_effect_keys=[f"page-{observation.page_index or 0}:safe-next"],
                pending_effect_key=None,
                issue_counts={
                    "missing": len(report.missing_keys),
                    "low": len(report.low_confidence_keys),
                    "readback": len(report.readback_mismatch_keys),
                    "defaulted": len(report.defaulted_keys),
                },
            )

        if task_id and state:
            try:
                result = self.client.report_progress(
                    task_id=task_id,
                    lease=state.lease,
                    expected_version=state.state_version,
                    target_status="ready_for_review",
                    page_fingerprint="sha256:after_nav",
                    page_index=None,
                    field_counts={
                        "confirmed": len(report.confirmed_keys),
                        "defaulted": len(report.defaulted_keys),
                        "missing": len(report.missing_keys),
                        "low": len(report.low_confidence_keys),
                    },
                    reason_code="navigated",
                )
                state.state_version = result.state_version
            except Exception:
                pass

        return RunOutcome("ready_for_review", "navigated")

    def _run_observation(self, payload: ExecutorTaskPayload) -> RunOutcome:
        """Observe the result page after human submission."""
        self.browser.open(str(payload.target_url))
        result = self.browser.observe_submission_result()
        if result == "success":
            target = "submitted_success"
        elif result == "failed":
            target = "submitted_failed"
        else:
            target = "result_unknown"

        if self._state:
            try:
                self.client.report_result(
                    task_id=self._state.task_id,
                    lease=self._state.lease,
                    expected_version=self._state.state_version,
                    target_status=target,
                    page_fingerprint="sha256:result",
                    reason_code=result,
                )
            except Exception:
                pass

        return RunOutcome("result_observed", target)

    def _reconcile_conflict(
        self, task_id: str, error: ApiConflict
    ) -> RunOutcome:
        """Handle conflict by reading authoritative task state."""
        if error.error_code == "stale_task_version":
            return RunOutcome("stopped_conflict", "stale_task_version")
        if error.error_code == "invalid_executor_transition":
            return RunOutcome("stopped_conflict", "invalid_transition")
        return RunOutcome("stopped_conflict", error.error_code)
