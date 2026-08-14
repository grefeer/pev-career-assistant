from __future__ import annotations

from tests.question.eval_policy import (
    audit_success_record,
    non_success_count,
    root_cause,
    should_stop,
    status_counts,
    top_level_records,
)


def _record(case_id: str, status: str, **extra: object) -> dict:
    record = {"id": case_id, "result": {"status": status}}
    record.update(extra)
    return record


def test_non_success_uses_one_top_level_case_and_combines_statuses() -> None:
    records = [
        _record("Q001", "waiting_user"),
        _record("Q002", "failed"),
        _record("Q003", "succeeded"),
        _record("Q004", "paused"),
        _record("C001", "waiting_user", type="chain", links=[]),
        _record("C001-L1", "waiting_user"),
    ]

    assert len(top_level_records(records)) == 4
    assert status_counts(records) == {
        "waiting_user": 1,
        "failed": 1,
        "succeeded": 1,
        "unknown": 1,
    }
    assert non_success_count(records) == 3


def test_stop_threshold_is_strictly_greater_than_thirty() -> None:
    at_limit = [_record(f"Q{i:03}", "failed") for i in range(30)]
    over_limit = [*at_limit, _record("Q030", "waiting_user")]

    assert not should_stop(at_limit)
    assert should_stop(over_limit)


def test_root_cause_prefers_actionable_error_over_budget_symptom() -> None:
    record = _record(
        "Q001",
        "failed",
        turns=[
            {
                "role": "executor",
                "tool_name": "match-observed-jobs",
                "error_code": "invalid_tool_input",
            }
        ],
        result={"status": "failed", "error_code": "replan_budget_exhausted"},
    )

    assert root_cause(record) == "contract_or_policy_error"


def test_success_audit_accepts_complete_official_negative_source_scan() -> None:
    record = _record(
        "R034",
        "succeeded",
        artifacts=[
            {
                "artifact_id": "artifact-1",
                "artifact_type": "job_search_results",
                "source_url": "https://api.juejin.cn/search_api/v1/search",
                "content_hash": "a" * 64,
                "provider": "juejin_official_search",
                "source_scope": "juejin.cn",
                "time_window_days": 3,
                "coverage_complete": True,
                "scanned_result_count": 7,
                "matched_result_count": 0,
                "terminal_reason": "search_empty",
                "result_count": 0,
            }
        ],
    )

    assert audit_success_record(record) == {
        "status": "passed",
        "verified_negative_source_scans": 1,
    }


def test_success_audit_rejects_incomplete_or_generic_empty_search() -> None:
    base = {
        "artifact_id": "artifact-1",
        "artifact_type": "job_search_results",
        "source_url": "https://api.juejin.cn/search_api/v1/search",
        "content_hash": "a" * 64,
        "provider": "juejin_official_search",
        "source_scope": "juejin.cn",
        "time_window_days": 3,
        "coverage_complete": False,
        "scanned_result_count": 7,
        "matched_result_count": 0,
        "terminal_reason": "search_empty",
        "result_count": 0,
    }
    assert audit_success_record(
        _record("R034", "succeeded", artifacts=[base])
    )["status"] != "passed"
    assert audit_success_record(
        _record(
            "R034",
            "succeeded",
            artifacts=[
                {
                    **base,
                    "provider": "public_web_search",
                    "coverage_complete": True,
                }
            ],
        )
    )["status"] != "passed"
