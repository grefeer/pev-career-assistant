from __future__ import annotations

import pytest

from executor.checkpoints import (
    CheckpointStore,
    CheckpointCorruptError,
    ExecutorCheckpoint,
)


def make_checkpoint(**overrides: object) -> ExecutorCheckpoint:
    data = dict(
        protocol_version="executor.v1",
        task_id="11111111-1111-4111-8111-111111111111",
        task_state_version=1,
        step="fill_page",
        page_index=1,
        page_fingerprint="sha256:abc123",
        completed_field_keys=["full_name"],
        completed_effect_keys=[],
        pending_field_key=None,
        pending_effect_key=None,
        issue_counts={"missing": 1, "low": 0, "readback": 0},
    )
    data.update(overrides)
    return ExecutorCheckpoint(**data)


def test_checkpoint_contains_only_keys_counts_and_fingerprint(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint()
    store.save(checkpoint)
    raw = store.path_for(checkpoint.task_id).read_text(encoding="utf-8")
    assert "Alice Example" not in raw
    assert "device-token" not in raw
    assert "task_lease" not in raw
    assert store.load(checkpoint.task_id) == checkpoint


def test_pending_effect_survives_restart_and_forbids_retry(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint(pending_effect_key="page-1:save-next")
    store.save(checkpoint)
    reloaded = CheckpointStore(tmp_path).load(checkpoint.task_id)
    assert reloaded is not None
    assert reloaded.pending_effect_key == "page-1:save-next"


def test_pending_field_survives_restart_and_forbids_blind_refill(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint(pending_field_key="full_name")
    store.save(checkpoint)
    reloaded = CheckpointStore(tmp_path).load(checkpoint.task_id)
    assert reloaded is not None
    assert reloaded.pending_field_key == "full_name"


def test_corrupt_file_returns_corrupt_error(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    path = store.path_for("11111111-1111-4111-8111-111111111111")
    path.write_text("{invalid json", encoding="utf-8")
    with pytest.raises(CheckpointCorruptError):
        store.load("11111111-1111-4111-8111-111111111111")


def test_atomic_write_leaves_no_tmp_files(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint()
    store.save(checkpoint)
    assert list(tmp_path.glob("*.tmp")) == []


def test_delete_removes_file(tmp_path) -> None:
    store = CheckpointStore(tmp_path)
    checkpoint = make_checkpoint()
    store.save(checkpoint)
    store.delete(checkpoint.task_id)
    assert store.load(checkpoint.task_id) is None
