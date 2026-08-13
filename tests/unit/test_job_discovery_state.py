from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_state_module():
    path = Path("skill/job-discovery/scripts/state.py").resolve()
    spec = importlib.util.spec_from_file_location("job_discovery_state", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_state_save_is_atomic_and_load_recovers_previous_snapshot(tmp_path: Path) -> None:
    module = _load_state_module()
    module.STATE_PATH = tmp_path / "state.json"
    module.STATE_BACKUP_PATH = tmp_path / "state.json.bak"
    first = {"source_sheets": {}, "processed": {"one": {"url": "https://a"}}}
    second = {"source_sheets": {}, "processed": {"two": {"url": "https://b"}}}

    module._save_state(first)
    module._save_state(second)
    module.STATE_PATH.write_text("{\"processed\":", encoding="utf-8")

    assert module._load_state() == first
    assert not list(tmp_path.glob("state.json.*.tmp"))


def test_state_load_rejects_invalid_shape_and_returns_empty_state(tmp_path: Path) -> None:
    module = _load_state_module()
    module.STATE_PATH = tmp_path / "state.json"
    module.STATE_BACKUP_PATH = tmp_path / "state.json.bak"
    module.STATE_PATH.write_text(
        '{"source_sheets": [], "processed": {}}', encoding="utf-8"
    )

    assert module._load_state() == {"source_sheets": {}, "processed": {}}
