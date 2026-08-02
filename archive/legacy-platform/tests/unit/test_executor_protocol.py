from __future__ import annotations

from pathlib import Path

from backend.app.services.devices import ALLOWED_TASK_LEASE_SCOPES
from executor.protocol import ExecutorTaskPayload, PROTOCOL_VERSION


FIXTURE = Path("tests/fixtures/executor/protocol_v1/task.json")


def test_executor_v1_fixture_is_non_sensitive_and_parseable() -> None:
    raw = FIXTURE.read_text(encoding="utf-8")
    payload = ExecutorTaskPayload.model_validate_json(raw)
    assert payload.protocol_version == PROTOCOL_VERSION == "executor.v1"
    assert payload.target_url.host in {"127.0.0.1", "localhost"}
    assert all(field.sensitive is False for field in payload.fields)
    assert not {
        "password", "cookie", "captcha", "id_card", "resume_text", "task_lease"
    } & set(raw.lower().replace('"', "").split())


def test_task_lease_scope_allowlist_has_no_submit_capability() -> None:
    assert ALLOWED_TASK_LEASE_SCOPES == frozenset(
        {"task:progress", "task:result"}
    )
    assert "task:submit" not in ALLOWED_TASK_LEASE_SCOPES
