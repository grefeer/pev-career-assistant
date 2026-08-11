"""Round 5 regression tests for candidate URL state across verifier retries."""

from backend.app.services.agent_runtime.executor_agent import (
    _candidate_search_is_authorized,
    _load_failed_candidate_urls,
    _snapshot_execution_state,
)


def test_single_fetch_failure_survives_execution_state_snapshot() -> None:
    state = _snapshot_execution_state(
        succeeded_calls=[],
        prior_succeeded_calls=[],
        stable_failed_calls=[],
        prior_stable_failed_calls=[],
        failed_candidate_urls={"https://jobs.example/dead"},
        consecutive_stalls=0,
        total_wasted_turns=1,
    )

    assert _load_failed_candidate_urls(state) == {"https://jobs.example/dead"}


def test_search_authorizes_only_after_every_candidate_is_proven_unusable() -> None:
    candidates = frozenset(
        {"https://jobs.example/dead", "https://jobs.example/blocked"}
    )

    assert not _candidate_search_is_authorized(
        candidates,
        {"https://jobs.example/dead"},
    )
    assert _candidate_search_is_authorized(
        candidates,
        {"https://jobs.example/dead", "https://jobs.example/blocked"},
    )

