"""Unit tests for the company-research runtime (deterministic orchestrator)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.company_research.runtime import CompanyResearchRuntime


def _settings(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(company_research_artifact_root=str(tmp_path))


def _patch_browse(
    monkeypatch,
    *,
    page_text: str | None,
    metadata: dict,
) -> None:
    """Replace the browse subprocess with one that writes a page + metadata."""

    def fake_run(args, **kwargs):
        cwd = Path(kwargs["cwd"])
        if page_text is not None:
            pages_dir = cwd / "output" / "evidence" / "pages"
            pages_dir.mkdir(parents=True, exist_ok=True)
            (pages_dir / "page_001.txt").write_text(page_text, encoding="utf-8")
        return SimpleNamespace(
            stdout=json.dumps(metadata, ensure_ascii=False), stderr="", returncode=0
        )

    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run", fake_run
    )


_PUBLIC_JOB_PAGE = (
    '=== PUBLIC JOB 1 ===\n'
    '{"title":"后端工程师","company_name":"Acme","location":"上海",'
    '"responsibilities":"负责服务端开发"}\n'
)


def test_run_succeeds_and_parses_openings(tmp_path: Path, monkeypatch) -> None:
    _patch_browse(
        monkeypatch, page_text=_PUBLIC_JOB_PAGE,
        metadata={"status": "ok", "pages_collected": 1, "title": "Acme Careers"},
    )
    runtime = CompanyResearchRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="report-1", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "succeeded"
    assert result.succeeded is True
    assert result.block_reason is None
    assert result.openings[0]["title"] == "后端工程师"
    assert result.profile["company_name"] == "Acme"
    assert result.profile["opening_count"] == 1
    assert result.profile["locations"] == ["上海"]
    assert result.profile["description"]  # page excerpt
    assert result.evidence_refs[0]["evidence_type"] == "page_text"


def test_run_blocked_surfaces_anti_bot(tmp_path: Path, monkeypatch) -> None:
    _patch_browse(
        monkeypatch, page_text=None,
        metadata={"status": "blocked", "block_reason": "anti_bot"},
    )
    runtime = CompanyResearchRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="report-2", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "needs_manual_review"
    assert result.succeeded is False
    assert result.block_reason == "anti_bot"
    assert result.openings == []


def test_run_blocked_maps_specific_reason(tmp_path: Path, monkeypatch) -> None:
    _patch_browse(
        monkeypatch, page_text=None,
        metadata={"status": "blocked", "block_reason": "login_required"},
    )
    runtime = CompanyResearchRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="report-3", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "needs_manual_review"
    assert result.block_reason == "login_required"


def test_run_error_status_is_failed(tmp_path: Path, monkeypatch) -> None:
    _patch_browse(
        monkeypatch, page_text=None,
        metadata={"status": "error", "error": "navigation timeout"},
    )
    runtime = CompanyResearchRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="report-4", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "failed"
    assert result.last_error == "navigation timeout"


def test_run_ok_without_page_file_is_no_evidence(tmp_path: Path, monkeypatch) -> None:
    _patch_browse(
        monkeypatch, page_text=None,
        metadata={"status": "ok", "pages_collected": 1},
    )
    runtime = CompanyResearchRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="report-5", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "needs_manual_review"
    assert result.block_reason == "no_evidence"


def test_run_succeeds_with_zero_openings(tmp_path: Path, monkeypatch) -> None:
    _patch_browse(
        monkeypatch, page_text="Acme makes widgets. We are hiring.",
        metadata={"status": "ok", "pages_collected": 1},
    )
    runtime = CompanyResearchRuntime(_settings(tmp_path))
    result = runtime.run(
        report_id="report-6", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "succeeded"
    assert result.openings == []
    assert result.profile["opening_count"] == 0
    assert result.profile["locations"] == []


def test_run_invoke_exception_is_failed(tmp_path: Path, monkeypatch) -> None:
    runtime = CompanyResearchRuntime(_settings(tmp_path))

    def boom(*, source_url: str, skill_dir: Path) -> None:
        raise RuntimeError("browse exploded")

    runtime._invoke = boom  # type: ignore[method-assign]
    result = runtime.run(
        report_id="report-7", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "failed"
    assert result.last_error == "browse exploded"


def test_run_artifact_publish_failure_is_needs_manual_review(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_browse(
        monkeypatch, page_text=_PUBLIC_JOB_PAGE,
        metadata={"status": "ok", "pages_collected": 1},
    )

    class FailingStore:
        def put(self, **kwargs):
            raise OSError("minio down")

    runtime = CompanyResearchRuntime(
        _settings(tmp_path), object_store=FailingStore()
    )
    result = runtime.run(
        report_id="report-8", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "needs_manual_review"
    assert result.block_reason == "artifact_error"
    # The parsed profile is preserved for the manual reviewer.
    assert result.profile is not None
    assert result.openings[0]["title"] == "后端工程师"


def test_run_publishes_evidence_when_object_store_present(
    tmp_path: Path, monkeypatch
) -> None:
    _patch_browse(
        monkeypatch, page_text=_PUBLIC_JOB_PAGE,
        metadata={"status": "ok", "pages_collected": 1},
    )

    class CollectingStore:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def put(self, *, key: str, plaintext: bytes, content_type: str):
            self.keys.append(key)
            return object()

    store = CollectingStore()
    runtime = CompanyResearchRuntime(
        _settings(tmp_path), object_store=store
    )
    result = runtime.run(
        report_id="report-9", company_name="Acme",
        source_url="https://careers.acme.example",
    )
    assert result.status == "succeeded"
    assert any(key.startswith("company-research/report-9/") for key in store.keys)
