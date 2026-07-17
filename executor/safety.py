from __future__ import annotations

import sys
from dataclasses import dataclass

if sys.version_info >= (3, 11):
    from enum import StrEnum
else:
    from enum import Enum

    class StrEnum(str, Enum):
        """Minimal StrEnum polyfill for Python < 3.11."""
        pass


class PageTopology(StrEnum):
    SINGLE_PAGE = "single_page"
    MULTI_STEP_INTERMEDIATE = "multi_step_intermediate"
    MULTI_STEP_FINAL = "multi_step_final"
    UNKNOWN = "unknown"


class ActionRisk(StrEnum):
    SAFE_INTERMEDIATE = "safe_intermediate"
    FINAL = "final"
    COMBINED = "combined"
    AMBIGUOUS = "ambiguous"


@dataclass(frozen=True)
class SafetyDecision:
    allowed: bool
    risk: ActionRisk
    reason_code: str


FINAL_TOKENS = frozenset(
    {
        "\u63d0\u4ea4",  # 提交
        "\u6295\u9012",  # 投递
        "\u5b8c\u6210\u7533\u8bf7",  # 完成申请
        "submit",
        "confirmapplication",
        "finish",
    }
)
COMBINED_TOKENS = frozenset(
    {
        "\u4fdd\u5b58\u5e76\u63d0\u4ea4",  # 保存并提交
        "saveandsubmit",
    }
)


def classify_topology(
    *,
    declared_topology: str | None,
    step_index: int | None,
    step_count: int | None,
    has_step_navigation: bool,
) -> PageTopology:
    if declared_topology == "single" and step_index is None and step_count is None:
        return PageTopology.SINGLE_PAGE
    if (
        declared_topology == "multi"
        and has_step_navigation
        and step_index is not None
        and step_count is not None
        and 1 <= step_index <= step_count
    ):
        if step_index < step_count:
            return PageTopology.MULTI_STEP_INTERMEDIATE
        return PageTopology.MULTI_STEP_FINAL
    return PageTopology.UNKNOWN


def decide_action(
    *,
    topology: PageTopology,
    label: str,
    action_kind: str,
    is_bottom_action: bool,
    has_verified_next_step: bool,
) -> SafetyDecision:
    # Normalize label
    normalized = label.casefold().replace(" ", "")

    # Check combined tokens first (more specific, may contain final substrings)
    for token in COMBINED_TOKENS:
        if token in normalized:
            return SafetyDecision(
                allowed=False,
                risk=ActionRisk.COMBINED,
                reason_code="combined_action_forbidden",
            )

    # Check for final tokens before considering topology
    for token in FINAL_TOKENS:
        if token in normalized:
            return SafetyDecision(
                allowed=False,
                risk=ActionRisk.FINAL,
                reason_code="final_action_forbidden",
            )

    # Single page: bottom actions are forbidden
    if topology is PageTopology.SINGLE_PAGE:
        return SafetyDecision(
            allowed=False,
            risk=ActionRisk.AMBIGUOUS,
            reason_code="single_page_bottom_action",
        )

    # Multi-step final: all actions forbidden
    if topology is PageTopology.MULTI_STEP_FINAL:
        return SafetyDecision(
            allowed=False,
            risk=ActionRisk.FINAL,
            reason_code="final_action_forbidden",
        )

    # Multi-step intermediate: allow only when conditions are met
    if topology is PageTopology.MULTI_STEP_INTERMEDIATE:
        if (
            action_kind == "next"
            and has_verified_next_step
            and label
            and not any(token in normalized for token in FINAL_TOKENS)
            and not any(token in normalized for token in COMBINED_TOKENS)
        ):
            return SafetyDecision(
                allowed=True,
                risk=ActionRisk.SAFE_INTERMEDIATE,
                reason_code="safe_intermediate_action",
            )
        return SafetyDecision(
            allowed=False,
            risk=ActionRisk.AMBIGUOUS,
            reason_code="ambiguous_action_forbidden",
        )

    # Unknown topology: always deny
    return SafetyDecision(
        allowed=False,
        risk=ActionRisk.AMBIGUOUS,
        reason_code="ambiguous_action_forbidden",
    )
