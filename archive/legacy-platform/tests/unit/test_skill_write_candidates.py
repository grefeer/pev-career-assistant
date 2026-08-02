from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_writer():
    path = Path("skill/job-discovery/scripts/write_candidates.py")
    spec = importlib.util.spec_from_file_location("skill_write_candidates", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_utf8_safe_replaces_lone_surrogates_without_losing_candidate_fields() -> None:
    writer = _load_writer()

    safe = writer._utf8_safe({"title": "AI Agent\udc80工程师", "locations": ["北京"]})

    assert safe["title"] == "AI Agent?工程师"
    assert safe["locations"] == ["北京"]
    assert safe["title"].encode("utf-8")
