from __future__ import annotations

import pytest

from executor.safety import (
    PageTopology,
    classify_topology,
    decide_action,
)


@pytest.mark.parametrize(
    ("topology", "label", "action_kind", "allowed", "reason"),
    [
        (PageTopology.SINGLE_PAGE, "\u4fdd\u5b58", "save", False, "single_page_bottom_action"),
        (PageTopology.MULTI_STEP_FINAL, "\u63d0\u4ea4\u7533\u8bf7", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "\u4fdd\u5b58\u5e76\u4e0b\u4e00\u6b65", "next", True, "safe_intermediate_action"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "\u4fdd\u5b58\u5e76\u63d0\u4ea4", "combined", False, "combined_action_forbidden"),
        (PageTopology.UNKNOWN, "\u7ee7\u7eed", "unknown", False, "ambiguous_action_forbidden"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "\u5b8c\u6210\u7533\u8bf7", "next", False, "final_action_forbidden"),
        (PageTopology.UNKNOWN, "", "unknown", False, "ambiguous_action_forbidden"),
        (PageTopology.UNKNOWN, "\u26a1", "unknown", False, "ambiguous_action_forbidden"),
        (PageTopology.MULTI_STEP_FINAL, "submit", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_FINAL, "confirm application", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_FINAL, "finish", "final", False, "final_action_forbidden"),
        (PageTopology.MULTI_STEP_INTERMEDIATE, "save and submit", "combined", False, "combined_action_forbidden"),
    ],
)
def test_action_decision_table(
    topology, label, action_kind, allowed, reason
) -> None:
    decision = decide_action(
        topology=topology,
        label=label,
        action_kind=action_kind,
        is_bottom_action=True,
        has_verified_next_step=topology is PageTopology.MULTI_STEP_INTERMEDIATE,
    )
    assert (decision.allowed, decision.reason_code) == (allowed, reason)


def test_classify_topology_single() -> None:
    result = classify_topology(
        declared_topology="single",
        step_index=None,
        step_count=None,
        has_step_navigation=False,
    )
    assert result is PageTopology.SINGLE_PAGE


def test_classify_topology_multi_intermediate() -> None:
    result = classify_topology(
        declared_topology="multi",
        step_index=1,
        step_count=2,
        has_step_navigation=True,
    )
    assert result is PageTopology.MULTI_STEP_INTERMEDIATE


def test_classify_topology_multi_final() -> None:
    result = classify_topology(
        declared_topology="multi",
        step_index=2,
        step_count=2,
        has_step_navigation=True,
    )
    assert result is PageTopology.MULTI_STEP_FINAL


def test_classify_topology_no_navigation_returns_unknown() -> None:
    result = classify_topology(
        declared_topology="multi",
        step_index=1,
        step_count=2,
        has_step_navigation=False,
    )
    assert result is PageTopology.UNKNOWN


def test_classify_topology_invalid_step_returns_unknown() -> None:
    result = classify_topology(
        declared_topology="multi",
        step_index=3,
        step_count=2,
        has_step_navigation=True,
    )
    assert result is PageTopology.UNKNOWN
