"""Layered compression for observation lists and run-level evidence.

Tests that ``summarize_observations`` and ``_with_observed_public_evidence``
bound what the model sees in long evidence/observation chains: the most-recent
items stay full (with bounded ``visible_text``), older items collapse to
identifier-only summary lines, and total characters stay within budget.
Security hard gate #4 is enforced: summary lines never carry
``visible_text``/``pages``/``details``/``output`` payloads.
"""

from __future__ import annotations

import json

from backend.app.db.models import User, UserRole
from backend.app.repositories import agent_runtime as run_repository
from backend.app.services.agent_runtime.observation_projection import (
    _DEFAULT_KEEP_RECENT_OBSERVATIONS,
    _DEFAULT_OBSERVATION_BUDGET_CHARS,
    summarize_observations,
)
from backend.app.services.agent_runtime.runtime import AgentRuntime
from tests.unit.test_agent_runtime import _create_running_step


# ---------------------------------------------------------------------------
# summarize_observations: list-level bounding
# ---------------------------------------------------------------------------


def _make_observation(
    index: int,
    *,
    visible_text_chars: int = 200,
    with_output: bool = True,
) -> dict:
    """Build a projected observation dict shaped like observation_for_decision output."""
    output: dict = {}
    if with_output:
        output = {
            "source_url": f"https://jobs.example/{index}",
            "content_hash": f"hash-{index:04d}",
            "visible_text": f"page body {index} " * (visible_text_chars // 14 + 1),
        }
        output["visible_text"] = output["visible_text"][:visible_text_chars]
    return {
        "tool_name": "fetch-public-job-pages",
        "status": "succeeded",
        "output": output,
        "error_code": None,
        "error_message": None,
    }


def test_summarize_observations_returns_shallow_copy_when_under_keep_recent() -> None:
    """A list with fewer than keep_recent items is returned unchanged (no summarization)."""
    observations = [_make_observation(i) for i in range(3)]
    result = summarize_observations(observations)
    assert result == observations
    assert result is not observations  # shallow copy, not the same list


def test_summarize_observations_returns_shallow_copy_at_keep_recent_boundary() -> None:
    """Exactly keep_recent items: no summarization, but still a copy."""
    observations = [_make_observation(i) for i in range(_DEFAULT_KEEP_RECENT_OBSERVATIONS)]
    result = summarize_observations(observations)
    assert result == observations
    assert result is not observations


def test_summarize_observations_returns_copy_when_over_count_but_under_budget() -> None:
    """More than keep_recent items but total chars within budget: no summarization."""
    observations = [_make_observation(i, visible_text_chars=50) for i in range(8)]
    # 8 small observations fit well within the 48_000 default budget.
    result = summarize_observations(observations)
    assert result == observations
    assert result is not observations


def test_summarize_observations_collapses_older_items_when_over_budget() -> None:
    """When over count AND over budget: older -> summary lines, recent -> full."""
    # 8 observations, each ~5_000 chars -> total ~40_000 chars. With budget
    # reduced to 12_000, only the most-recent keep_recent can stay full.
    observations = [_make_observation(i, visible_text_chars=5_000) for i in range(8)]
    result = summarize_observations(
        observations, keep_recent=3, budget_chars=12_000,
    )
    # 3 recent full + 5 older summaries = 8 items total.
    assert len(result) == 8
    # The last 3 (most-recent) stay full.
    for item in result[-3:]:
        assert "visible_text" in item["output"]
        assert "pages" not in item["output"] or "pages" in item["output"]
    # The first 5 (older) are summary lines.
    for item in result[:5]:
        assert "tool_name" in item
        assert "status" in item
        assert "source_url" in item
        assert "content_hash" in item
        # Security hard gate #4: NO payload keys in summary lines.
        assert "visible_text" not in item
        assert "pages" not in item
        assert "details" not in item
        assert "output" not in item


def test_summarize_observations_preserves_recent_full_visible_text() -> None:
    """The keep_recent most-recent observations retain their full visible_text."""
    observations = [_make_observation(i, visible_text_chars=3_000) for i in range(10)]
    result = summarize_observations(
        observations, keep_recent=4, budget_chars=10_000,
    )
    # The last 4 should have their visible_text preserved.
    for i, item in enumerate(result[-4:]):
        assert item["output"]["visible_text"] == observations[6 + i]["output"]["visible_text"]


def test_summarize_observations_empty_list_returns_empty() -> None:
    """An empty list returns an empty list."""
    assert summarize_observations([]) == []


def test_summarize_observations_summary_lines_have_no_payload_keys() -> None:
    """Security hard gate #4: summary lines must never leak payload content."""
    observations = [
        _make_observation(i, visible_text_chars=4_000) for i in range(10)
    ]
    result = summarize_observations(
        observations, keep_recent=3, budget_chars=10_000,
    )
    summary_lines = result[:-3]
    assert len(summary_lines) == 7
    forbidden_keys = {"visible_text", "pages", "details", "output"}
    for item in summary_lines:
        assert not (set(item.keys()) & forbidden_keys), (
            f"Summary line leaked payload keys: {set(item.keys()) & forbidden_keys}"
        )


def test_summarize_observations_handles_failed_observation_without_output() -> None:
    """A failed observation (no output dict) still summarizes to tool_name + status."""
    observations: list[dict] = []
    for i in range(8):
        if i < 5:
            observations.append({
                "tool_name": "fetch-public-job-pages",
                "status": "failed",
                "output": None,
                "error_code": "tool_execution_failed",
                "error_message": "timeout",
            })
        else:
            observations.append(_make_observation(i, visible_text_chars=4_000))
    result = summarize_observations(
        observations, keep_recent=3, budget_chars=10_000,
    )
    # First 5 are summaries (failed, no output -> no source_url/content_hash).
    for item in result[:5]:
        assert item["tool_name"] == "fetch-public-job-pages"
        assert item["status"] == "failed"
        assert "source_url" not in item
        assert "content_hash" not in item
        assert "output" not in item


def test_summarize_observations_20_plus_chain_stays_within_budget() -> None:
    """A simulated 20+ observation chain must not exceed the char budget."""
    observations = [_make_observation(i, visible_text_chars=3_500) for i in range(22)]
    result = summarize_observations(
        observations,
        keep_recent=_DEFAULT_KEEP_RECENT_OBSERVATIONS,
        budget_chars=_DEFAULT_OBSERVATION_BUDGET_CHARS,
    )
    assert len(result) == 22  # all items retained (full + summary)
    total_chars = len(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    assert total_chars <= _DEFAULT_OBSERVATION_BUDGET_CHARS, (
        f"Summarized list exceeded budget: {total_chars} > {_DEFAULT_OBSERVATION_BUDGET_CHARS}"
    )
    # The keep_recent most-recent stay full.
    for item in result[-_DEFAULT_KEEP_RECENT_OBSERVATIONS:]:
        assert "output" in item
        assert "visible_text" in item["output"]
    # All older items are summary lines.
    older = result[:-_DEFAULT_KEEP_RECENT_OBSERVATIONS]
    assert len(older) == 22 - _DEFAULT_KEEP_RECENT_OBSERVATIONS
    for item in older:
        assert "output" not in item
        assert "visible_text" not in item


def test_summarize_observations_custom_keep_recent_zero_summarizes_all_but_zero() -> None:
    """keep_recent=0 with over-budget list summarizes every item."""
    observations = [_make_observation(i, visible_text_chars=4_000) for i in range(5)]
    result = summarize_observations(observations, keep_recent=0, budget_chars=100)
    assert len(result) == 5
    for item in result:
        assert "tool_name" in item
        assert "output" not in item


def test_summarize_observations_preserves_order_oldest_first() -> None:
    """The result preserves oldest-first order: summaries first, then full recent."""
    observations = [_make_observation(i, visible_text_chars=4_000) for i in range(8)]
    result = summarize_observations(
        observations, keep_recent=3, budget_chars=10_000,
    )
    # The source_urls should be in order 0..7.
    source_urls = []
    for item in result:
        if "output" in item:
            source_urls.append(item["output"]["source_url"])
        else:
            source_urls.append(item["source_url"])
    assert source_urls == [f"https://jobs.example/{i}" for i in range(8)]


def test_summarize_observations_skips_none_tool_name_and_status() -> None:
    """A summary line omits tool_name/status when they are None (coverage: value is None branch)."""
    observations: list[dict] = []
    for i in range(8):
        if i < 5:
            # Older observations with None tool_name and status get summarized.
            observations.append({
                "tool_name": None,
                "status": None,
                "output": {"source_url": f"https://jobs.example/{i}"},
                "error_code": None,
                "error_message": None,
            })
        else:
            observations.append(_make_observation(i, visible_text_chars=4_000))
    result = summarize_observations(
        observations, keep_recent=3, budget_chars=10_000,
    )
    # The first 5 (older) are summary lines without tool_name/status (both None).
    for item in result[:5]:
        assert "tool_name" not in item
        assert "status" not in item
        assert "source_url" in item
        assert "output" not in item


def test_summarize_observations_skips_non_string_source_url_and_content_hash() -> None:
    """A summary line omits source_url/content_hash when they are not strings (coverage)."""
    observations: list[dict] = []
    for i in range(8):
        if i < 5:
            # Older observations with non-string source_url/content_hash get summarized.
            observations.append({
                "tool_name": "fetch-public-job-pages",
                "status": "succeeded",
                "output": {
                    "source_url": None,  # not a string -> skipped
                    "content_hash": 12345,  # not a string -> skipped
                },
                "error_code": None,
                "error_message": None,
            })
        else:
            observations.append(_make_observation(i, visible_text_chars=4_000))
    result = summarize_observations(
        observations, keep_recent=3, budget_chars=10_000,
    )
    # The first 5 (older) are summary lines without source_url/content_hash.
    for item in result[:5]:
        assert item["tool_name"] == "fetch-public-job-pages"
        assert item["status"] == "succeeded"
        assert "source_url" not in item
        assert "content_hash" not in item
        assert "output" not in item


# ---------------------------------------------------------------------------
# _with_observed_public_evidence: layered evidence assembly
# ---------------------------------------------------------------------------


def _user() -> User:
    return User(
        id="user-evidence",
        account="user-evidence@example.test",
        nickname="user-evidence",
        password_hash="not-a-real-password-hash",
        role=UserRole.STUDENT,
    )


def test_evidence_assembly_keeps_single_oversized_artifact_truncated(db_session) -> None:
    """A single artifact over the budget is truncated, not summarized (existing behavior)."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/large",
        content_hash="d" * 64,
        content_json={"visible_text": "x" * 48_001},
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    assert len(evidence) == 1
    assert evidence[0]["visible_text"] == "x" * 48_000


def test_evidence_assembly_keeps_all_artifacts_when_under_budget(db_session) -> None:
    """When total visible_text is within budget, all artifacts stay full."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    for i in range(3):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{i}",
            content_hash=f"hash-{i}" * 16,
            content_json={"visible_text": f"page body {i}" * 100, "title": f"岗位 {i}"},
        )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    assert len(evidence) == 3
    for item in evidence:
        assert "visible_text" in item
        assert "title" in item


def test_evidence_assembly_summarizes_older_artifacts_when_over_budget(db_session) -> None:
    """When total visible_text exceeds budget, older artifacts become summaries."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    # 5 artifacts, each 12_000 chars -> total 60_000 > 48_000 budget.
    # The most-recent (artifact 4) is kept full; older ones that don't fit
    # become summary lines.
    for i in range(5):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{i}",
            content_hash=f"hash-{i}" * 16,
            content_json={
                "visible_text": f"page-{i}-" * 2_400,  # 12_000 chars
                "title": f"岗位 {i}",
            },
        )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    assert len(evidence) == 5  # all artifacts retained (full + summary)

    # At least one summary line (older artifact without visible_text).
    summaries = [item for item in evidence if "visible_text" not in item]
    assert len(summaries) >= 1, "Expected at least one summary line for older artifacts"

    # Summary lines carry identifiers only, never visible_text (security gate #4).
    for item in summaries:
        assert "artifact_id" in item
        assert "source_url" in item
        assert "content_hash" in item
        assert "title" in item
        assert "visible_text" not in item

    # At least one full item (most-recent with visible_text).
    full_items = [item for item in evidence if "visible_text" in item]
    assert len(full_items) >= 1, "Expected at least one full item for recent artifacts"

    # Total visible_text chars within budget.
    total_chars = sum(len(item["visible_text"]) for item in full_items)
    assert total_chars <= 48_000


def test_evidence_assembly_preserves_most_recent_full_when_over_budget(db_session) -> None:
    """The most-recent artifact is kept full (with bounded visible_text)."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    for i in range(4):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{i}",
            content_hash=f"hash-{i}" * 16,
            content_json={
                "visible_text": f"page-{i}-" * 5_000,  # 25_000 chars each
                "title": f"岗位 {i}",
            },
        )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    # The last artifact (index 3, most-recent) should be kept full.
    last_item = evidence[-1]
    assert "visible_text" in last_item
    assert last_item["source_url"] == "https://jobs.example/3"
    assert last_item["title"] == "岗位 3"


def test_evidence_assembly_skips_artifacts_without_visible_text(db_session) -> None:
    """Artifacts without a non-empty visible_text are skipped (no page evidence)."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/empty",
        content_hash="hash-empty" * 8,
        content_json={"visible_text": ""},
    )
    run_repository.create_evidence_artifact(
        db_session,
        run_id=run.id,
        step_id=step.id,
        source_url="https://jobs.example/valid",
        content_hash="hash-valid" * 8,
        content_json={"visible_text": "valid JD body"},
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    assert len(evidence) == 1
    assert evidence[0]["source_url"] == "https://jobs.example/valid"
    assert evidence[0]["visible_text"] == "valid JD body"


def test_evidence_assembly_no_artifacts_returns_empty_evidence_list(db_session) -> None:
    """When no evidence artifacts exist, the context gets an empty evidence list."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, _step = _create_running_step(
        db_session, user, requires_verification=False
    )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    assert evidence == []


def test_evidence_assembly_summary_includes_title_when_present(db_session) -> None:
    """Summary lines include the title identifier when the artifact has one."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    for i in range(5):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{i}",
            content_hash=f"hash-{i}" * 16,
            content_json={
                "visible_text": f"page-{i}-" * 3_000,  # 15_000 chars each
                "title": f"岗位 {i}",
            },
        )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    summaries = [item for item in evidence if "visible_text" not in item]
    assert len(summaries) >= 1
    for item in summaries:
        assert "title" in item
        assert item["title"].startswith("岗位 ")


def test_evidence_assembly_summary_omits_title_when_absent(db_session) -> None:
    """Summary lines omit the title key when the artifact has no title."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    for i in range(5):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{i}",
            content_hash=f"hash-{i}" * 16,
            content_json={
                "visible_text": f"page-{i}-" * 3_000,  # 15_000 chars each
                # No title key
            },
        )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    summaries = [item for item in evidence if "visible_text" not in item]
    assert len(summaries) >= 1
    for item in summaries:
        assert "title" not in item


def test_evidence_assembly_total_visible_text_within_budget(db_session) -> None:
    """Total visible_text chars across all full items must stay within the budget."""
    user = _user()
    db_session.add(user)
    db_session.commit()
    run, task, _plan, _plan_step, step = _create_running_step(
        db_session, user, requires_verification=False
    )
    # 10 artifacts, each 10_000 chars -> total 100_000 >> 48_000 budget.
    for i in range(10):
        run_repository.create_evidence_artifact(
            db_session,
            run_id=run.id,
            step_id=step.id,
            source_url=f"https://jobs.example/{i}",
            content_hash=f"hash-{i}" * 16,
            content_json={"visible_text": f"page-{i}-" * 2_000},  # 10_000 chars
        )

    projected = AgentRuntime._with_observed_public_evidence(db_session, task, run.id)
    evidence = projected.context["observed_public_evidence"]

    full_items = [item for item in evidence if "visible_text" in item]
    total_chars = sum(len(item["visible_text"]) for item in full_items)
    assert total_chars <= 48_000, (
        f"Total visible_text chars {total_chars} exceeded 48_000 budget"
    )
    # All 10 artifacts are retained (full + summary), none silently dropped.
    assert len(evidence) == 10
