from __future__ import annotations

import json
import hashlib
from pathlib import Path
from types import SimpleNamespace

from backend.app.services.job_discovery.schemas import DiscoveryTaskInput
from backend.app.services.job_discovery.skill_runtime import (
    SkillDiscoveryRuntime,
    SkillToolPolicy,
    _script_tool,
    _public_json_candidates,
    _search_card_candidates,
    _bind_page_candidate_evidence,
    _detail_evidence_candidates,
    _browse_manifest_is_truncated,
    _declared_total_pages,
    _complete_browse_manifest,
    _deduplicate_exact_body_candidates,
    _expected_page_candidate_count,
    _browse_mode,
    _task_company_name,
    _valid_output_args,
)


def _task() -> DiscoveryTaskInput:
    return DiscoveryTaskInput(
        source_id="source", raw_record_id="raw", external_record_id="external",
        source_key="manual", source_url="https://example.com/jobs", url_hash="hash",
        record_fields=[],
    )


def test_runtime_maps_verified_artifacts_to_result(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence_dir = skill_dir / "output" / "evidence"
        evidence_dir.mkdir(parents=True)
        (evidence_dir / "pages").mkdir()
        (evidence_dir / "pages" / "page_001.txt").write_text("JD body", encoding="utf-8")
        (evidence_dir / "tool_trace.jsonl").write_text(
            json.dumps({"script": "browse", "duration_ms": 12, "status": "ok"}) + "\n",
            encoding="utf-8",
        )
        (evidence_dir / "browse_metadata.json").write_text(
            json.dumps({"terminal_evidence": "last_page_disabled"}), encoding="utf-8",
        )
        (evidence_dir / "coverage_gate_result.json").write_text(
            json.dumps({"coverage_verified": True}), encoding="utf-8",
        )
        candidates_dir = skill_dir / "output" / "candidates"
        candidates_dir.mkdir()
        (candidates_dir / "page_001.json").write_text("[]", encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{
            "title": "AI应用开发工程师", "company_name": "示例", "responsibilities": "负责开发",
            "requirements": "熟悉 Python", "locations": ["上海"],
            "evidence_refs": [{"evidence_type": "browsed_detail_page"}],
        }]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-1")

    assert outcome.result.status == "succeeded"
    assert outcome.result.candidates[0].responsibilities == "负责开发"
    assert [candidate.title for candidate in outcome.preferred_candidates] == ["AI应用开发工程师"]
    assert outcome.role_preferences == ("AI应用开发", "Agent开发")
    assert outcome.trace_steps[0]["tool"] == "browse"
    assert outcome.result.evidence[0].metadata["storage_uri"].startswith("file:")


def test_runtime_requires_positive_coverage_marker(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        output = skill_dir / "output"
        output.mkdir()
        (output / "candidates_merged.json").write_text("[]", encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-2")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "coverage_unverified"


def test_runtime_returns_preference_matched_jd_without_full_site_coverage(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        output = skill_dir / "output"
        output.mkdir()
        (output / "candidates_merged.json").write_text(json.dumps([{
            "title": "AI Agent开发工程师", "responsibilities": "负责开发",
            "apply_url": "https://example.com/apply",
        }, {
            "title": "销售培训生", "responsibilities": "负责销售",
            "apply_url": "https://example.com/sales",
        }]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="targeted-task")

    assert outcome.result.status == "succeeded"
    assert [candidate.title for candidate in outcome.result.candidates] == ["AI Agent开发工程师"]
    assert [candidate.title for candidate in outcome.discovered_candidates] == [
        "AI Agent开发工程师", "销售培训生",
    ]


def test_runtime_uses_safe_source_page_as_targeted_apply_fallback(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        (skill_dir / "output").mkdir()
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{
            "title": "Agent开发工程师", "responsibilities": "负责开发",
        }]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="targeted-fallback")

    assert outcome.result.status == "succeeded"
    assert outcome.result.candidates[0].apply_url == "https://example.com/jobs"
    assert outcome.discovered_candidates[0].apply_url is None


def test_runtime_does_not_retain_raw_candidates_above_the_task_limit(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=1)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        (skill_dir / "output").mkdir()
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([
            {"title": "AI Agent开发工程师", "responsibilities": "负责开发"},
            {"title": "销售培训生", "responsibilities": "负责销售"},
        ]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="over-limit")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "candidate_limit_exceeded"
    assert outcome.discovered_candidates == []


def test_tool_policy_rejects_excess_browse_and_external_output(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "browse.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    tool = _script_tool(skill_dir, SkillToolPolicy(max_browse_calls=2))

    assert not tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"}).startswith("ERROR:")
    assert not tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"}).startswith("ERROR:")
    assert tool.invoke({"script": "browse", "cli_args": "https://example.com --out output/evidence"}).startswith("ERROR:")
    assert tool.invoke({"script": "browse", "cli_args": "https://example.com --out C:/outside"}).startswith("ERROR:")


def test_coverage_gate_ignores_agent_supplied_coverage_claims(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "coverage_gate.py").write_text("", encoding="utf-8")
    captured: dict[str, object] = {}

    def run(args, **_kwargs):
        captured["args"] = args
        return SimpleNamespace(stdout="{}", stderr="", returncode=0)

    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run", run,
    )
    tool = _script_tool(skill_dir, SkillToolPolicy())

    assert not tool.invoke({
        "script": "coverage_gate",
        "cli_args": "fake.json --pages invented.txt --terminal-evidence end",
    }).startswith("ERROR:")
    assert captured["args"][2:] == [
        "output/candidates_merged.json", "--manifest",
        "output/evidence/browse_metadata.json",
    ]


def test_output_flag_validation_rejects_repeated_and_equals_form_bypasses() -> None:
    assert not _valid_output_args(
        "browse", ["https://example.com", "--out", "output/evidence", "--out", "scripts"],
    )
    assert not _valid_output_args("browse", ["https://example.com", "--out=scripts"])


def test_browse_mode_reads_only_the_explicit_mode_argument() -> None:
    assert _browse_mode(["https://example.com", "--mode", "search-interact"]) == "search-interact"
    assert _browse_mode(["https://example.com"]) is None


def test_public_json_evidence_is_converted_without_llm(tmp_path: Path) -> None:
    page = tmp_path / "page_01.txt"
    page.write_text(
        '=== PUBLIC JOB 1 ===\n'
        '{"title":"AI Agent研发工程师","department":"技术","location":"上海",'
        '"responsibilities":"负责智能体开发"}\n',
        encoding="utf-8",
    )

    candidates = _public_json_candidates(page, "PDD")

    assert candidates == [{
        "title": "AI Agent研发工程师", "company_name": "PDD", "department": "技术",
        "responsibilities": "负责智能体开发", "locations": ["上海"],
        "recruitment_types": ["校园招聘"],
        "evidence_refs": [{
            "evidence_type": "public_json_job",
            "content_hash": hashlib.sha256(page.read_bytes()).hexdigest(),
            "relative_path": "output/evidence/pages/page_01.txt",
        }],
    }]


def test_task_company_context_is_used_without_falling_back_to_source_key() -> None:
    task = DiscoveryTaskInput(
        source_id="source", raw_record_id="raw", external_record_id="external",
        source_key="user-submission", source_url="https://example.com/jobs", url_hash="hash",
        record_fields=[{"field_name": "公司名称", "value": "小红书"}],
    )

    assert _task_company_name(task) == "小红书"


def test_public_json_candidate_keeps_company_empty_without_explicit_context(tmp_path: Path) -> None:
    page = tmp_path / "page_01.txt"
    page.write_text(
        '=== PUBLIC JOB 1 ===\n'
        '{"title":"AI Agent研发工程师","responsibilities":"负责智能体开发"}\n',
        encoding="utf-8",
    )

    candidates = _public_json_candidates(page, None)

    assert candidates[0]["company_name"] is None


def test_search_card_evidence_is_converted_without_llm(tmp_path: Path) -> None:
    page = tmp_path / "page_001.txt"
    page.write_text("Agent开发实习生\n后端开发\n北京市，上海市\n负责 Agent 工具调用与工作流编排。\n", encoding="utf-8")

    candidates = _search_card_candidates(page, "示例")

    assert [(candidate["title"], candidate["locations"]) for candidate in candidates] == [
        ("Agent开发实习生", ["北京市，上海市"]),
    ]


def test_search_card_parser_never_uses_a_long_responsibility_as_a_title(tmp_path: Path) -> None:
    page = tmp_path / "page_001.txt"
    body = "参与 Agent 平台研发，负责工具调用、工作流编排与上下文管理。" * 4
    page.write_text(f"Agent开发实习生\n北京市\n{body}\n", encoding="utf-8")

    candidates = _search_card_candidates(page, "示例")

    assert [candidate["title"] for candidate in candidates] == ["Agent开发实习生"]


def test_search_card_parser_rejects_short_responsibility_sentence_as_title(tmp_path: Path) -> None:
    page = tmp_path / "page_001.txt"
    page.write_text("参与 Agent 平台研发，负责工具调用。\n北京市\n另一条职责说明。\n", encoding="utf-8")

    assert _search_card_candidates(page, "示例") == []


def test_search_card_parser_prefers_numbered_responsibilities_over_internship_blurb(tmp_path: Path) -> None:
    page = tmp_path / "page_001.txt"
    page.write_text(
        "AI Agent开发实习生\n上海实习研发\nByteIntern：统一实习说明。\n团队介绍：部门介绍。\n"
        "1、负责 Agent 工具调用与工作流编排。\n2、负责线上稳定性优化。\n",
        encoding="utf-8",
    )

    candidates = _search_card_candidates(page, "示例")

    assert candidates[0]["responsibilities"] == "1、负责 Agent 工具调用与工作流编排。 2、负责线上稳定性优化。"


def test_page_candidate_binding_overrides_missing_agent_evidence_refs(tmp_path: Path) -> None:
    page = tmp_path / "output" / "evidence" / "pages" / "page_001.txt"
    page.parent.mkdir(parents=True)
    page.write_text("JD source", encoding="utf-8")
    candidate_file = tmp_path / "output" / "candidates" / "page_001.json"
    candidate_file.parent.mkdir(parents=True)
    candidate_file.write_text('[{"title":"岗位","responsibilities":"开发"}]', encoding="utf-8")

    _bind_page_candidate_evidence(tmp_path)

    row = json.loads(candidate_file.read_text(encoding="utf-8"))[0]
    assert row["evidence_refs"][0]["content_hash"] == hashlib.sha256(page.read_bytes()).hexdigest()


def test_detail_evidence_is_converted_without_an_extractor_agent(tmp_path: Path) -> None:
    page = tmp_path / "page_001.txt"
    page.write_text(
        "=== DETAIL 1 (https://jobs.example/1) ===\n"
        "首页/职位详情\n职位描述\n负责 AI 应用开发。\n"
        "职位信息\n职位名称\n职位名称\nAI应用开发工程师\n"
        "=== DETAIL 2 (https://jobs.example/2) ===\n"
        "职位描述\n负责 Agent 平台研发。\n"
        "职位信息\n职位名称\n职位名称\nAgent开发工程师\n",
        encoding="utf-8",
    )

    candidates = _detail_evidence_candidates(page, "示例公司")

    assert [(candidate["title"], candidate["apply_url"]) for candidate in candidates] == [
        ("AI应用开发工程师", "https://jobs.example/1"),
        ("Agent开发工程师", "https://jobs.example/2"),
    ]
    assert all(candidate["responsibilities"] for candidate in candidates)


def test_tool_trace_marks_nonzero_script_exit_as_failed(tmp_path: Path, monkeypatch) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "write_candidates.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="", stderr="boom", returncode=1),
    )

    tool = _script_tool(skill_dir, SkillToolPolicy())
    tool.invoke({"script": "write_candidates", "cli_args": "--out output/candidates/page_001.json", "stdin": "[]"})

    trace = json.loads((skill_dir / "output" / "evidence" / "tool_trace.jsonl").read_text(encoding="utf-8"))
    assert trace["status"] == "failed"


def test_runtime_detects_declared_page_count_truncation(tmp_path: Path) -> None:
    evidence = tmp_path / "output" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "browse_metadata.json").write_text(
        '{"declared_total_pages":44,"pages_collected":20}', encoding="utf-8",
    )
    assert _browse_manifest_is_truncated(tmp_path) is True
    assert _declared_total_pages(tmp_path) == 44


def test_runtime_completes_browser_manifest_only_from_actual_evidence_files(tmp_path: Path) -> None:
    pages = tmp_path / "output" / "evidence" / "pages"
    pages.mkdir(parents=True)
    (pages / "page_001.txt").write_text("one", encoding="utf-8")
    (pages / "page_002.txt").write_text("two", encoding="utf-8")
    metadata = pages.parent / "browse_metadata.json"
    metadata.write_text('{"total_pages":2,"pages_collected":2}', encoding="utf-8")

    _complete_browse_manifest(tmp_path)

    value = json.loads(metadata.read_text(encoding="utf-8"))
    assert value["page_files"] == [
        "output/evidence/pages/page_001.txt", "output/evidence/pages/page_002.txt",
    ]
    assert value["terminal_evidence"] == "browser_reported_pages_exhausted"


def test_runtime_materializes_consolidated_browser_evidence_for_extraction(tmp_path: Path) -> None:
    evidence = tmp_path / "output" / "evidence"
    evidence.mkdir(parents=True)
    source = evidence / "search_result.txt"
    source.write_text("AI Agent开发工程师", encoding="utf-8")
    metadata = evidence / "browse_metadata.json"
    metadata.write_text(json.dumps({"text_path": "output/evidence/search_result.txt"}), encoding="utf-8")

    _complete_browse_manifest(tmp_path)

    page = evidence / "pages" / "page_001.txt"
    assert page.read_text(encoding="utf-8") == "AI Agent开发工程师"
    assert json.loads(metadata.read_text(encoding="utf-8"))["page_files"] == [
        "output/evidence/pages/page_001.txt",
    ]


def test_runtime_materializes_browser_text_when_stdout_metadata_loses_text_path(tmp_path: Path) -> None:
    evidence = tmp_path / "output" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "sha256_text.txt").write_text("AI应用开发", encoding="utf-8")
    metadata = evidence / "browse_metadata.json"
    metadata.write_text('{"search_ok": true}', encoding="utf-8")

    _complete_browse_manifest(tmp_path)

    assert (evidence / "pages" / "page_001.txt").read_text(encoding="utf-8") == "AI应用开发"


def test_runtime_reads_public_listing_count_from_evidence_not_agent_input(tmp_path: Path) -> None:
    pages = tmp_path / "output" / "evidence" / "pages"
    pages.mkdir(parents=True)
    (pages / "page_001.txt").write_text("开启新的工作（439）", encoding="utf-8")
    metadata = pages.parent / "browse_metadata.json"
    metadata.write_text('{"total_pages":1,"pages_collected":1}', encoding="utf-8")

    _complete_browse_manifest(tmp_path)

    assert json.loads(metadata.read_text(encoding="utf-8"))["listing_count"] == 439


def test_runtime_only_deduplicates_identical_body_candidates(tmp_path: Path) -> None:
    output = tmp_path / "output"
    output.mkdir()
    merged = output / "candidates_merged.json"
    merged.write_text(json.dumps([
        {"title": "培训生", "responsibilities": "负责订单管理。"},
        {"title": "培训生", "responsibilities": "负责订单管理"},
        {"title": "培训生", "responsibilities": "负责产销平衡"},
    ], ensure_ascii=False), encoding="utf-8")
    _deduplicate_exact_body_candidates(tmp_path)
    assert len(json.loads(merged.read_text(encoding="utf-8"))) == 2


def test_runtime_derives_expected_candidates_for_last_pagination_page(tmp_path: Path) -> None:
    evidence = tmp_path / "output" / "evidence"
    evidence.mkdir(parents=True)
    (evidence / "browse_metadata.json").write_text(
        '{"size_val":10,"listing_count":439,"total_pages":44}', encoding="utf-8",
    )
    assert _expected_page_candidate_count(tmp_path, Path("page_001.txt")) == 10
    assert _expected_page_candidate_count(tmp_path, Path("page_044.txt")) == 9


def test_runtime_honors_configured_candidate_limit(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=1)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence = skill_dir / "output" / "evidence"
        evidence.mkdir(parents=True)
        (evidence / "browse_metadata.json").write_text(json.dumps({"terminal_evidence": "end"}), encoding="utf-8")
        (evidence / "coverage_gate_result.json").write_text(json.dumps({"coverage_verified": True}), encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([
            {"title": "A", "responsibilities": "body"}, {"title": "B", "responsibilities": "body"},
        ]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-limit")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "candidate_limit_exceeded"


def test_llm_factory_is_available_without_importing_supervisor() -> None:
    from backend.app.services.job_discovery.llm_factory import build_job_discovery_llm

    assert callable(build_job_discovery_llm)


def test_runtime_rejects_coverage_when_an_evidence_page_has_no_extraction(tmp_path: Path) -> None:
    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings)

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence = skill_dir / "output" / "evidence" / "pages"
        evidence.mkdir(parents=True)
        (evidence / "page_001.txt").write_text("first", encoding="utf-8")
        (evidence / "page_002.txt").write_text("second", encoding="utf-8")
        meta = evidence.parent
        (meta / "browse_metadata.json").write_text(json.dumps({"terminal_evidence": "end"}), encoding="utf-8")
        (meta / "coverage_gate_result.json").write_text(json.dumps({"coverage_verified": True}), encoding="utf-8")
        candidates = skill_dir / "output" / "candidates"
        candidates.mkdir()
        (candidates / "page_001.json").write_text(json.dumps([{"title": "A", "responsibilities": "body"}]), encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{"title": "A", "responsibilities": "body"}]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-pages")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "page_extraction_incomplete"


def test_runtime_marks_manual_review_when_artifact_upload_fails(tmp_path: Path) -> None:
    class BrokenObjectStore:
        def put(self, **kwargs) -> None:
            raise OSError("MinIO unavailable")

    settings = SimpleNamespace(job_discovery_skill_artifact_root=str(tmp_path), job_discovery_max_candidates_per_task=10)
    runtime = SkillDiscoveryRuntime(settings, object_store=BrokenObjectStore())

    def fake_invoke(*, task: DiscoveryTaskInput, skill_dir: Path) -> None:
        evidence = skill_dir / "output" / "evidence" / "pages"
        evidence.mkdir(parents=True)
        (evidence / "page_001.txt").write_text("body", encoding="utf-8")
        meta = evidence.parent
        (meta / "browse_metadata.json").write_text(json.dumps({"terminal_evidence": "end"}), encoding="utf-8")
        (meta / "coverage_gate_result.json").write_text(json.dumps({"coverage_verified": True}), encoding="utf-8")
        candidates = skill_dir / "output" / "candidates"
        candidates.mkdir()
        (candidates / "page_001.json").write_text("[]", encoding="utf-8")
        (skill_dir / "output" / "candidates_merged.json").write_text(json.dumps([{"title": "A", "responsibilities": "body"}]), encoding="utf-8")

    runtime._invoke = fake_invoke  # type: ignore[method-assign]
    outcome = runtime.run(_task(), task_id="task-object-store")

    assert outcome.result.status == "needs_manual_review"
    assert outcome.result.block_reason == "artifact_upload_failed"
