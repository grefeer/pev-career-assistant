from __future__ import annotations

from pathlib import Path

from executor.protocol import (
    ExecutorTaskPayloadV2,
    ExecutorTaskDetailV2,
    TaskStatus,
)


FIXTURE = Path("tests/fixtures/executor/protocol_v2/task.json")


def test_executor_v2_fixture_is_parseable() -> None:
    """Verify the v2 fixture matches ExecutorTaskPayloadV2 schema."""
    raw = FIXTURE.read_text(encoding="utf-8")
    payload = ExecutorTaskPayloadV2.model_validate_json(raw)

    assert payload.protocol_version == "executor.v2"
    assert len(payload.task_id) == 36
    assert len(payload.snapshot_id) == 36
    assert payload.target_url.host in {"127.0.0.1", "localhost"}
    assert isinstance(payload.state_version, int) and payload.state_version >= 0

    # non-sensitive fields
    assert "name" in payload.non_sensitive_fields
    assert "email" in payload.non_sensitive_fields
    assert payload.non_sensitive_fields["name"] == "Alice Example"

    # local-sensitive requirements — only semantic refs, no plaintext
    for req in payload.local_sensitive_requirements:
        assert "field_key" in req
        assert "category" in req
        assert "local_reference" in req
        assert req["local_reference"].startswith("vault://")
        assert "value" not in req or not req.get("value")

    # attachment IDs
    assert len(payload.attachment_ids) == 2
    assert all(len(aid) == 36 for aid in payload.attachment_ids)


def test_executor_v2_never_contains_forbidden_fields() -> None:
    """v2 payload MUST NOT contain object keys, full snapshot, passwords, etc."""
    raw = FIXTURE.read_text(encoding="utf-8").lower()
    forbidden = {
        "password", "cookie", "captcha", "id_card",
        "object_key", "object-key", "profile_facts",
        "resume_text", "task_lease",
    }
    tokens = set(raw.replace('"', "").split())
    present = forbidden & tokens
    assert not present, f"v2 fixture contains forbidden tokens: {present}"


def test_executor_v2_detail_wraps_payload() -> None:
    """ExecutorTaskDetailV2 should wrap an ExecutorTaskPayloadV2."""
    raw = FIXTURE.read_text(encoding="utf-8")
    payload = ExecutorTaskPayloadV2.model_validate_json(raw)

    detail = ExecutorTaskDetailV2(
        protocol_version="executor.v1",
        task_id=payload.task_id,
        target_job_id="job-001",
        snapshot_id=payload.snapshot_id,
        status=TaskStatus.DISPATCHED,
        state_version=0,
        payload=payload,
    )

    assert detail.payload.protocol_version == "executor.v2"
    assert detail.payload.snapshot_id == payload.snapshot_id
    assert detail.target_job_id == "job-001"
    assert detail.status == TaskStatus.DISPATCHED


def test_v2_payload_field_counts() -> None:
    """Validate the field counts in the v2 fixture."""
    raw = FIXTURE.read_text(encoding="utf-8")
    payload = ExecutorTaskPayloadV2.model_validate_json(raw)

    # 5 non-sensitive fields, 2 local-sensitive requirements, 2 attachment IDs
    assert len(payload.non_sensitive_fields) == 5
    assert len(payload.local_sensitive_requirements) == 2
    assert len(payload.attachment_ids) == 2
