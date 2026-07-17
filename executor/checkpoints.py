from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import re

from pydantic import BaseModel, ConfigDict, Field, ValidationError


SAFE_KEY = re.compile(r"^[a-z0-9][a-z0-9:_-]{0,119}$")
ALLOWED_ISSUE_KEYS = frozenset({"missing", "low", "readback", "defaulted"})


class CheckpointCorruptError(RuntimeError):
    pass


class CheckpointMismatchError(RuntimeError):
    pass


class ExecutorCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    protocol_version: str
    task_id: str = Field(min_length=36, max_length=36)
    task_state_version: int = Field(ge=0)
    step: str = Field(pattern=r"^[a-z_]{1,40}$")
    page_index: int | None = Field(default=None, ge=1, le=100)
    page_fingerprint: str = Field(pattern=r"^sha256:[0-9a-f]{6,64}$")
    completed_field_keys: list[str]
    completed_effect_keys: list[str]
    pending_field_key: str | None = None
    pending_effect_key: str | None | None = None
    issue_counts: dict[str, int]
    saved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class CheckpointStore:
    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, task_id: str) -> Path:
        if not SAFE_KEY.fullmatch(task_id):
            raise ValueError("invalid task checkpoint key")
        return self.root / f"{task_id}.json"

    def save(self, checkpoint: ExecutorCheckpoint) -> None:
        for key in checkpoint.completed_field_keys:
            if not SAFE_KEY.fullmatch(key):
                raise ValueError(f"invalid completed_field_key: {key}")
        for key in checkpoint.completed_effect_keys:
            if not SAFE_KEY.fullmatch(key):
                raise ValueError(f"invalid completed_effect_key: {key}")
        if checkpoint.pending_field_key is not None and not SAFE_KEY.fullmatch(
            checkpoint.pending_field_key
        ):
            raise ValueError(f"invalid pending_field_key: {checkpoint.pending_field_key}")
        if checkpoint.pending_effect_key is not None and not SAFE_KEY.fullmatch(
            checkpoint.pending_effect_key
        ):
            raise ValueError(
                f"invalid pending_effect_key: {checkpoint.pending_effect_key}"
            )
        for key in checkpoint.issue_counts:
            if key not in ALLOWED_ISSUE_KEYS:
                raise ValueError(f"unexpected issue key: {key}")

        target = self.path_for(checkpoint.task_id)
        temporary = target.with_suffix(".json.tmp")
        data = checkpoint.model_dump_json(indent=2)
        temporary.write_text(data, encoding="utf-8", newline="\n")
        temporary.replace(target)

    def load(self, task_id: str) -> ExecutorCheckpoint | None:
        target = self.path_for(task_id)
        if not target.exists():
            return None
        try:
            return ExecutorCheckpoint.model_validate_json(
                target.read_text(encoding="utf-8")
            )
        except (OSError, ValidationError, json.JSONDecodeError) as error:
            raise CheckpointCorruptError(task_id) from error

    def delete(self, task_id: str) -> None:
        self.path_for(task_id).unlink(missing_ok=True)
