from __future__ import annotations

import logging
from dataclasses import dataclass
from enum import StrEnum
from typing import Literal
from urllib.parse import urlsplit

from executor.browser import BrowserSession
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

UNKNOWN_PAGE_FINGERPRINT = "sha256:" + ("0" * 64)


class InjectedCrash(RuntimeError):
    """Raised at a configured fault point for recovery testing."""


class FaultPoint(StrEnum):
    AFTER_FIELD_WRITE_BEFORE_VERIFIED = (
        "after_field_write_before_verified"
    )
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
        page_fingerprint: str = UNKNOWN_PAGE_FINGERPRINT,
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

        if urlsplit(str(payload.target_url)).hostname not in {
            "127.0.0.1",
            "localhost",
            "::1",
        }:
            return RunOutcome("failed_safe", "target_not_loopback")

        # Check if we're in observation mode
        if detail and hasattr(detail, "status"):
            from executor.protocol import TaskStatus

            if detail.status == TaskStatus.OBSERVING_USER_SUBMISSION:
                return self._run_observation(payload)

        # Dispatch -> Running transition
        state = self._state
        if state and task_id and detail and detail.status.value in {
            "dispatched",
            "waiting_for_human",
        }:
            try:
                result = self.client.report_progress(
                    task_id=task_id,
                    lease=state.lease,
                    expected_version=state.state_version,
                    target_status="running",
                    page_fingerprint=UNKNOWN_PAGE_FINGERPRINT,
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
                return RunOutcome("failed_safe", "progress_result_uncertain")
            except Exception:
                return RunOutcome("failed_safe", "progress_unavailable")

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
            if (
                cp.protocol_version != PROTOCOL_VERSION
                or cp.task_state_version != state.state_version
            ):
                return RunOutcome(
                    "ready_for_review", "checkpoint_version_mismatch"
                )

            # Check for pending intermediate effect — don't retry the click
            if cp.pending_effect_key is not None:
                return RunOutcome(
                    "ready_for_review", "intermediate_result_uncertain"
                )

            # Check if page fingerprint changed since checkpoint
            if (
                cp.page_fingerprint != observation.fingerprint
            ):
                return RunOutcome(
                    "ready_for_review", "page_topology_changed"
                )

            completed = set(cp.completed_field_keys)
            completed_effects = set(cp.completed_effect_keys)

            if cp.pending_field_key is not None:
                pending = next(
                    (
                        field
                        for field in payload.fields
                        if field.field_key == cp.pending_field_key
                    ),
                    None,
                )
                if (
                    pending is None
                    or pending.value is None
                    or self.browser.field_value(cp.pending_field_key)
                    != pending.value
                ):
                    return RunOutcome(
                        "ready_for_review", "field_write_uncertain"
                    )
                completed.add(cp.pending_field_key)
                if self._save_checkpoint(
                    page_fingerprint=observation.fingerprint,
                    page_index=observation.page_index,
                    completed_field_keys=sorted(completed),
                    completed_effect_keys=sorted(completed_effects),
                    pending_field_key=None,
                    issue_counts=cp.issue_counts,
                ) is None:
                    return RunOutcome(
                        "failed_safe", "checkpoint_unavailable"
                    )
        else:
            completed = set()
            completed_effects = set()

        fields_to_fill = [
            field
            for field in payload.fields
            if field.field_key not in completed
        ]

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
                except Exception as error:
                    return self._progress_failure(task_id, error)
            return RunOutcome("waiting_for_human", reason)

        def before_write(field_key: str) -> None:
            if self._save_checkpoint(
                page_fingerprint=observation.fingerprint,
                page_index=observation.page_index,
                completed_field_keys=sorted(completed),
                completed_effect_keys=sorted(completed_effects),
                pending_field_key=field_key,
            ) is None:
                raise RuntimeError("checkpoint unavailable before field write")

        def after_verified(field_key: str) -> None:
            self._check_fault(
                FaultPoint.AFTER_FIELD_WRITE_BEFORE_VERIFIED
            )
            completed.add(field_key)
            if self._save_checkpoint(
                page_fingerprint=observation.fingerprint,
                page_index=observation.page_index,
                completed_field_keys=sorted(completed),
                completed_effect_keys=sorted(completed_effects),
                pending_field_key=None,
            ) is None:
                raise RuntimeError(
                    "checkpoint unavailable after field verification"
                )

        self.browser.set_checkpoint_callbacks(
            before_write=before_write,
            after_verified=after_verified,
        )

        # Fill confirmed fields
        try:
            report = self.browser.fill_confirmed(fields_to_fill)
        except InjectedCrash:
            raise
        except RuntimeError:
            return RunOutcome("failed_safe", "checkpoint_unavailable")

        # Save checkpoint after fill (completed field keys recorded)
        if state:
            checkpoint = self._save_checkpoint(
                page_fingerprint=observation.fingerprint,
                page_index=observation.page_index,
                completed_field_keys=sorted(completed),
                completed_effect_keys=sorted(completed_effects),
                issue_counts={
                    "missing": len(report.missing_keys),
                    "low": len(report.low_confidence_keys),
                    "readback": len(report.readback_mismatch_keys),
                    "defaulted": len(report.defaulted_keys),
                },
            )
            if checkpoint is None:
                return RunOutcome("failed_safe", "checkpoint_unavailable")

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
                except Exception as error:
                    return self._progress_failure(task_id, error)
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
                except Exception as error:
                    return self._progress_failure(task_id, error)
            return RunOutcome("ready_for_review", decision.reason_code)

        # Safe intermediate action: save pending-effect checkpoint
        if state:
            checkpoint = self._save_checkpoint(
                step="safe_intermediate",
                page_fingerprint=observation.fingerprint,
                page_index=observation.page_index,
                completed_field_keys=sorted(completed),
                completed_effect_keys=sorted(completed_effects),
                pending_effect_key=f"page-{observation.page_index or 0}:safe-next",
                issue_counts={
                    "missing": len(report.missing_keys),
                    "low": len(report.low_confidence_keys),
                    "readback": len(report.readback_mismatch_keys),
                    "defaulted": len(report.defaulted_keys),
                },
            )
            if checkpoint is None:
                return RunOutcome("failed_safe", "checkpoint_unavailable")

        # Fault injection after pending-effect checkpoint is persisted
        self._check_fault(FaultPoint.AFTER_PENDING_EFFECT_CHECKPOINT_SAVED)

        # Revalidate lease and authoritative version immediately before the
        # browser side effect.  This is a read, so it is safe to retry in the
        # HTTP client and avoids an invalid RUNNING -> RUNNING transition.
        if task_id and state:
            try:
                current = self.client.get_task(task_id, state.lease)
                if current.state_version != state.state_version:
                    return RunOutcome("stopped_conflict", "stale_task_version")
            except ApiUnauthorized:
                return RunOutcome("stopped_unauthorized", "progress_denied")
            except ApiTaskNotFound:
                return RunOutcome("failed_safe", "task_not_found")
            except ApiConflict as error:
                return self._reconcile_conflict(task_id, error)
            except Exception:
                return RunOutcome("failed_safe", "lease_revalidation_failed")

        # Click safe intermediate
        try:
            self.browser.click_safe_intermediate(observation)
        except Exception:
            return RunOutcome("failed_safe", "intermediate_click_failed")

        # After click, re-observe and report review
        post_navigation = self.browser.observe()

        # Navigation confirmed — clear pending effect
        if state:
            completed_effects.add(
                f"page-{observation.page_index or 0}:safe-next"
            )
            checkpoint = self._save_checkpoint(
                step="safe_intermediate",
                page_fingerprint=post_navigation.fingerprint,
                page_index=post_navigation.page_index,
                completed_field_keys=sorted(completed),
                completed_effect_keys=sorted(completed_effects),
                pending_effect_key=None,
                issue_counts={
                    "missing": len(report.missing_keys),
                    "low": len(report.low_confidence_keys),
                    "readback": len(report.readback_mismatch_keys),
                    "defaulted": len(report.defaulted_keys),
                },
            )
            if checkpoint is None:
                return RunOutcome(
                    "failed_safe", "intermediate_result_uncertain"
                )

        if task_id and state:
            try:
                result = self.client.report_progress(
                    task_id=task_id,
                    lease=state.lease,
                    expected_version=state.state_version,
                    target_status="ready_for_review",
                    page_fingerprint=post_navigation.fingerprint,
                    page_index=post_navigation.page_index,
                    field_counts={
                        "confirmed": len(report.confirmed_keys),
                        "defaulted": len(report.defaulted_keys),
                        "missing": len(report.missing_keys),
                        "low": len(report.low_confidence_keys),
                    },
                    reason_code="navigated",
                )
                state.state_version = result.state_version
            except Exception as error:
                return self._progress_failure(task_id, error)

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
            except ApiUnauthorized:
                return RunOutcome("stopped_unauthorized", "result_denied")
            except (UncertainWriteResult, ApiConflict):
                return self._reconcile_result_write(target)
            except Exception:
                return RunOutcome("failed_safe", "result_report_unavailable")

        return RunOutcome("result_observed", target)

    def _progress_failure(
        self, task_id: str, error: Exception
    ) -> RunOutcome:
        if isinstance(error, ApiUnauthorized):
            return RunOutcome("stopped_unauthorized", "progress_denied")
        if isinstance(error, ApiConflict):
            return self._reconcile_conflict(task_id, error)
        if isinstance(error, UncertainWriteResult):
            return RunOutcome("failed_safe", "progress_result_uncertain")
        return RunOutcome("failed_safe", "progress_unavailable")

    def _reconcile_result_write(self, target: str) -> RunOutcome:
        state = self._state
        if state is None:
            return RunOutcome("failed_safe", "result_write_uncertain")
        try:
            current = self.client.get_task(state.task_id, state.lease)
        except ApiUnauthorized:
            return RunOutcome("stopped_unauthorized", "result_denied")
        except Exception:
            return RunOutcome("failed_safe", "result_write_uncertain")
        if current.status.value == target:
            state.state_version = current.state_version
            return RunOutcome("result_observed", target)
        return RunOutcome("failed_safe", "result_write_uncertain")

    def _reconcile_conflict(
        self, task_id: str, error: ApiConflict
    ) -> RunOutcome:
        """Handle conflict by reading authoritative task state."""
        if error.error_code == "stale_task_version":
            return RunOutcome("stopped_conflict", "stale_task_version")
        if error.error_code == "invalid_executor_transition":
            return RunOutcome("stopped_conflict", "invalid_transition")
        return RunOutcome("stopped_conflict", error.error_code)
