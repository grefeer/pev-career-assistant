from __future__ import annotations

from backend.app.services.agent_runtime.error_policy import (
    FailureClass,
    build_terminal_contract,
)
from backend.app.services.agent_runtime.schemas import ToolObservation


def test_nested_batch_blocker_is_external_and_not_replanable() -> None:
    observation = ToolObservation(
        tool_name="fetch-public-job-pages",
        status="succeeded",
        output={
            "pages": [],
            "failures": [{"source_url": "https://jobs.example/1", "error_code": "anti_bot_challenge"}],
        },
    )

    contract = build_terminal_contract(observations=[observation], source_role="executor")

    assert contract.failure_class is FailureClass.EXTERNAL_BLOCKED
    assert contract.reason_code == "anti_bot_challenge"
    assert contract.replan_allowed is False
    assert contract.as_payload()["evidence"]["blocked"] is True


def test_duplicate_and_model_errors_have_stable_classes() -> None:
    duplicate = build_terminal_contract(error_code="duplicate_tool_call")
    invalid = build_terminal_contract(error_code="invalid_model_response")

    assert duplicate.failure_class is FailureClass.NO_PROGRESS
    assert invalid.failure_class is FailureClass.MODEL_OUTPUT_INVALID
