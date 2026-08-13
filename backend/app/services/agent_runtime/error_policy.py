"""Generic observation error classification for the Agent Runtime.

The runtime only needs to know whether an observation can be retried or needs
human attention. Domain adapters register their own stable codes through this
small policy object instead of making the runtime know domain-specific names.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Iterable


class FailureClass(StrEnum):
    """Stable terminal categories used by runtime events and evaluation."""

    EXTERNAL_BLOCKED = "external_blocked"
    UPSTREAM_TOOL_FAILURE = "upstream_tool_failure"
    CONTRACT_VIOLATION = "contract_or_policy_error"
    NO_PROGRESS = "no_progress_duplicate"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    MODEL_DECISION = "model_or_verifier_decision"
    BUDGET_EXHAUSTED = "budget_exhausted"


@dataclass(frozen=True)
class TerminalContract:
    """Machine-readable explanation for a waiting or terminal transition."""

    failure_class: FailureClass
    reason_code: str
    source_role: str
    phase: str
    resumable: bool = True
    retry_allowed: bool = False
    replan_allowed: bool = False
    user_action: str = "provide_missing_information"
    contract_met: bool = False
    blocked: bool = False
    artifact_count: int = 0
    evidence_diagnostics: dict[str, Any] | None = None

    def as_payload(self) -> dict[str, Any]:
        return {
            "contract_version": "terminal.v1",
            "failure_class": self.failure_class.value,
            "reason_code": self.reason_code,
            "source_role": self.source_role,
            "phase": self.phase,
            "resumable": self.resumable,
            "retry_allowed": self.retry_allowed,
            "replan_allowed": self.replan_allowed,
            "user_action": self.user_action,
            "evidence": {
                "contract_met": self.contract_met,
                "blocked": self.blocked,
                "artifact_count": self.artifact_count,
            },
            **(
                {"evidence_diagnostics": self.evidence_diagnostics}
                if self.evidence_diagnostics is not None
                else {}
            ),
        }


def _nested_error_codes(observations: Iterable[object]) -> list[str]:
    codes: list[str] = []
    for observation in observations:
        code = getattr(observation, "error_code", None)
        if isinstance(code, str) and code:
            codes.append(code)
        output = getattr(observation, "output", None)
        if not isinstance(output, dict):
            continue
        failures = output.get("failures")
        if isinstance(failures, list):
            for failure in failures:
                if isinstance(failure, dict) and isinstance(failure.get("error_code"), str):
                    codes.append(failure["error_code"])
        if output.get("status") in {"blocked", "needs_manual_review"}:
            codes.append(str(output.get("reason") or output.get("status")))
    return codes


def build_terminal_contract(
    *,
    error_code: str | None = None,
    observations: Iterable[object] = (),
    source_role: str = "runtime",
    phase: str = "tool",
    contract_met: bool = False,
    artifact_count: int = 0,
    evidence_diagnostics: dict[str, Any] | None = None,
) -> TerminalContract:
    """Classify structured error signals without inspecting model prose."""
    codes = _nested_error_codes(observations)
    if error_code:
        codes.insert(0, error_code)
    code = next((value for value in codes if value), "need_user")
    external_code = next(
        (value for value in codes if _is_external_terminal_code(value)), None
    )
    if external_code is not None:
        code = external_code
    if _is_external_terminal_code(code):
        return TerminalContract(FailureClass.EXTERNAL_BLOCKED, code, source_role, phase, user_action="provide_public_job_url_or_jd_text", blocked=True, artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)
    if code in {
        "invalid_tool_input", "tool_skill_forbidden", "unknown_tool", "tool_role_forbidden",
        "verification_failed", "success_contract_not_satisfied", "observed_evidence_not_found",
        "public_page_content_insufficient",
    }:
        return TerminalContract(FailureClass.CONTRACT_VIOLATION, code, source_role, phase, user_action="provide_missing_tool_input", artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)
    if code in {"duplicate_tool_call", "candidate_urls_already_supplied", "route_already_consumed", "repeated_plan_fingerprint", "no_progress_duplicate"}:
        return TerminalContract(FailureClass.NO_PROGRESS, code, source_role, phase, user_action="provide_alternate_public_source", artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)
    if code == "invalid_model_response":
        return TerminalContract(FailureClass.MODEL_OUTPUT_INVALID, code, source_role, phase, user_action="retry_model_or_provide_information", artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)
    if code in {"sheet_rate_limited", "sheet_call_failed", "sheet_bridge_unavailable", "source_unavailable", "public_search_failed", "public_fetch_failed", "search_empty"}:
        return TerminalContract(FailureClass.UPSTREAM_TOOL_FAILURE, code, source_role, phase, user_action="provide_alternate_public_source", artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)
    if code.endswith("budget_exhausted") or code == "wall_clock_budget_exhausted":
        return TerminalContract(FailureClass.BUDGET_EXHAUSTED, code, source_role, phase, user_action="retry_within_a_new_budget_window", artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)
    return TerminalContract(FailureClass.MODEL_DECISION, code, source_role, phase, artifact_count=artifact_count, evidence_diagnostics=evidence_diagnostics)


def _is_external_terminal_code(code: str) -> bool:
    """Recognize stable source-access blocks before model-level symptoms."""
    if code in {
        "anti_bot",
        "anti_bot_challenge",
        "captcha",
        "login_required",
        "access_denied",
        "domain_temporarily_blocked",
        "needs_manual_review",
        "ocr_disabled",
    }:
        return True
    if code in {
        "adapter:url_not_allowlisted",
        "adapter:empty_result",
        "adapter:malformed_payload",
        "adapter:adapter_error",
        "adapter:adapter_unknown",
        "adapter:adapter_invalid",
        "adapter:allowlist_missing",
    }:
        return True
    if code.startswith("adapter:http_error:"):
        status = code.removeprefix("adapter:http_error:")
        return status.isdigit() and 400 <= int(status) < 500 and status not in {"408", "429"}
    return False


class ErrorDisposition(StrEnum):
    """Runtime action category for a tool or artifact failure."""

    RETRYABLE = "retryable"
    BLOCKED = "blocked"
    PERMANENT = "permanent"
    INVALID_ACTION = "invalid_action"
    HUMAN_REQUIRED = "human_required"


@dataclass(frozen=True)
class ErrorPolicy:
    """A deterministic, immutable error-code classifier."""

    blocked_codes: frozenset[str] = frozenset()
    human_required_codes: frozenset[str] = frozenset()
    permanent_codes: frozenset[str] = frozenset()
    invalid_action_codes: frozenset[str] = frozenset()
    transient_codes: frozenset[str] = frozenset()
    blocked_prefixes: tuple[str, ...] = ()
    blocked_http_statuses: frozenset[str] = frozenset()

    def classify(self, error_code: str | None) -> ErrorDisposition:
        """Classify an error without exposing its message or payload."""
        if not error_code:
            return ErrorDisposition.RETRYABLE
        if error_code in self.human_required_codes:
            return ErrorDisposition.HUMAN_REQUIRED
        if error_code in self.invalid_action_codes:
            return ErrorDisposition.INVALID_ACTION
        if error_code in self.permanent_codes:
            return ErrorDisposition.PERMANENT
        if error_code.startswith("adapter:http_error:"):
            status = error_code.removeprefix("adapter:http_error:")
            return (
                ErrorDisposition.BLOCKED
                if status in self.blocked_http_statuses
                else ErrorDisposition.RETRYABLE
            )
        if error_code in self.transient_codes:
            return ErrorDisposition.RETRYABLE
        if error_code in self.blocked_codes:
            return ErrorDisposition.BLOCKED
        if any(error_code.startswith(prefix) for prefix in self.blocked_prefixes):
            return ErrorDisposition.BLOCKED
        return ErrorDisposition.RETRYABLE

    def is_blocked(self, error_code: str | None) -> bool:
        """Return whether retrying is not a safe automatic action."""
        return self.classify(error_code) in {
            ErrorDisposition.BLOCKED,
            ErrorDisposition.HUMAN_REQUIRED,
            ErrorDisposition.PERMANENT,
        }


def default_error_policy() -> ErrorPolicy:
    """Return the runtime's domain-neutral safety defaults.

    Security gates are generic runtime concepts. Adapter-specific prefixes and
    codes are supplied by the owning skill package.
    """
    return ErrorPolicy(
        human_required_codes=frozenset(
            {"login_required", "captcha", "anti_bot", "anti_bot_challenge"}
        ),
        blocked_codes=frozenset({"access_denied", "unsafe_public_url"}),
        invalid_action_codes=frozenset(
            {"unknown_tool", "tool_role_forbidden", "tool_skill_forbidden", "invalid_tool_input"}
        ),
        transient_codes=frozenset(
            {"public_fetch_failed", "public_search_failed", "public_host_unresolvable"}
        ),
    )


def merge_error_policies(*policies: ErrorPolicy) -> ErrorPolicy:
    """Compose policy fragments while keeping the runtime dependency generic."""
    return ErrorPolicy(
        blocked_codes=frozenset().union(*(policy.blocked_codes for policy in policies)),
        human_required_codes=frozenset().union(
            *(policy.human_required_codes for policy in policies)
        ),
        permanent_codes=frozenset().union(*(policy.permanent_codes for policy in policies)),
        invalid_action_codes=frozenset().union(
            *(policy.invalid_action_codes for policy in policies)
        ),
        transient_codes=frozenset().union(*(policy.transient_codes for policy in policies)),
        blocked_prefixes=tuple(
            dict.fromkeys(prefix for policy in policies for prefix in policy.blocked_prefixes)
        ),
        blocked_http_statuses=frozenset().union(
            *(policy.blocked_http_statuses for policy in policies)
        ),
    )
