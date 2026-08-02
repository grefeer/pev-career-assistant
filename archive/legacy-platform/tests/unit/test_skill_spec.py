"""Phase 0 extension point: a second skill must plug into the shared
artifact store and script tool without touching the job-discovery default path.

These tests pin the contract that ``SkillSpec`` / ``SKILL_REGISTRY`` /
``get_skill_spec`` and the parameterized ``SkillArtifactStore`` /
``_script_tool`` expose, and that the job-discovery defaults are unchanged.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from backend.app.services.job_discovery.skill_artifacts import SkillArtifactStore
from backend.app.services.job_discovery.skill_runtime import SkillToolPolicy, _script_tool
from backend.app.services.job_discovery.skill_spec import (
    APPLICATION_TRACKING_SPEC,
    INTERVIEW_PREP_SPEC,
    JOB_DISCOVERY_SCRIPTS,
    JOB_DISCOVERY_SPEC,
    RESUME_TAILORING_SPEC,
    SKILL_REGISTRY,
    SkillSpec,
    get_skill_spec,
)


def test_job_discovery_spec_is_registered_with_the_canonical_scripts() -> None:
    spec = get_skill_spec("job-discovery")

    assert spec is JOB_DISCOVERY_SPEC
    assert spec.name == "job-discovery"
    assert spec.skill_type == "deterministic"
    assert spec.allowed_scripts == JOB_DISCOVERY_SCRIPTS
    assert spec.allowed_scripts == frozenset({
        "browse", "validate", "normalize", "deduplicate", "ocr_image", "state",
        "read_evidence", "write_candidates", "coverage_gate",
    })
    assert SKILL_REGISTRY["job-discovery"] is JOB_DISCOVERY_SPEC


def test_get_skill_spec_raises_for_an_unregistered_skill() -> None:
    with pytest.raises(KeyError):
        get_skill_spec("not-a-real-skill")


def test_job_discovery_source_path_points_at_the_real_skill_dir() -> None:
    source = JOB_DISCOVERY_SPEC.source_path

    assert source.name == "job-discovery"
    assert (source / "SKILL.md").is_file()


def test_skill_spec_source_path_derives_from_name() -> None:
    # A spec built for a not-yet-existing skill still resolves its source path
    # from the name; the directory need not exist until prepare() is called.
    spec = SkillSpec(name="company-research", allowed_scripts=frozenset(), skill_type="deterministic")

    assert spec.source_path.name == "company-research"
    assert spec.source_path.parent.name == "skill"


def test_artifact_store_defaults_to_the_job_discovery_segment(tmp_path: Path) -> None:
    store = SkillArtifactStore("task-a", tmp_path)

    assert store.skill_name == "job-discovery"
    skill_dir = store.prepare()
    assert skill_dir.name == "job-discovery"
    assert (skill_dir / "SKILL.md").is_file()


def test_artifact_store_derives_source_from_skill_name(tmp_path: Path) -> None:
    # When skill_source is omitted the store derives skill/<name> from the repo
    # root; for job-discovery this resolves to the real source directory.
    store = SkillArtifactStore("task-a", tmp_path, skill_name="job-discovery")

    assert store.skill_source.name == "job-discovery"
    assert (store.skill_source / "SKILL.md").is_file()


def test_artifact_store_namespaces_skill_dir_and_evidence_by_skill_name(
    tmp_path: Path,
) -> None:
    source = tmp_path / "fake-skill"
    (source / "scripts").mkdir(parents=True)
    (source / "SKILL.md").write_text("# fake skill", encoding="utf-8")

    store = SkillArtifactStore(
        "task-a", tmp_path, run_id="attempt-1",
        skill_name="company-research", skill_source=source,
    )

    assert store.skill_name == "company-research"
    skill_dir = store.prepare()
    assert skill_dir.name == "company-research"
    assert skill_dir.parent.name == "skill"

    evidence = skill_dir / "output" / "evidence"
    evidence.mkdir(parents=True)
    page = evidence / "page_001.txt"
    page.write_text("company evidence", encoding="utf-8")

    class FakeObjectStore:
        def __init__(self) -> None:
            self.keys: list[str] = []

        def put(self, *, key: str, plaintext: bytes, content_type: str):
            self.keys.append(key)
            return object()

    fake = FakeObjectStore()
    published = store.publish_evidence(fake)

    assert published[page].startswith("object://company-research/task-a/attempt-1/")
    assert all(key.startswith("company-research/task-a/") for key in fake.keys)


def test_artifact_store_rejects_path_traversal_skill_name(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        SkillArtifactStore("task-a", tmp_path, skill_name="../escape")


def test_script_tool_defaults_to_the_job_discovery_allowlist(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "browse.py").write_text("", encoding="utf-8")
    (scripts / "evil.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    tool = _script_tool(skill_dir, SkillToolPolicy())

    # A registered job-discovery script runs; an unregistered one is rejected by
    # the allowlist even though its file exists on disk.
    assert not tool.invoke(
        {"script": "browse", "cli_args": "https://example.com --out output/evidence"},
    ).startswith("ERROR:")
    assert tool.invoke({"script": "evil"}) == "ERROR: unsupported Skill script 'evil'"


def test_script_tool_accepts_a_custom_allowlist_for_a_parallel_skill(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "extract_company.py").write_text("", encoding="utf-8")
    (scripts / "browse.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    custom = frozenset({"extract_company", "package_report"})
    tool = _script_tool(
        skill_dir, SkillToolPolicy(max_browse_calls=2), allowed_scripts=custom,
    )

    # The parallel skill's own script is allowed; job-discovery's browse is now
    # outside this skill's allowlist and is rejected with the allowlist message.
    assert not tool.invoke(
        {"script": "extract_company", "cli_args": "--out output/company.json"},
    ).startswith("ERROR:")
    assert tool.invoke(
        {"script": "browse", "cli_args": "https://example.com --out output/evidence"},
    ) == "ERROR: unsupported Skill script 'browse'"


def test_resume_tailoring_spec_is_registered_with_generate_and_validate() -> None:
    spec = get_skill_spec("resume-tailoring")

    assert spec is RESUME_TAILORING_SPEC
    assert spec.name == "resume-tailoring"
    assert spec.skill_type == "deterministic"
    assert spec.allowed_scripts == frozenset({"generate", "validate"})
    assert SKILL_REGISTRY["resume-tailoring"] is RESUME_TAILORING_SPEC


def test_resume_tailoring_source_path_points_at_the_real_skill_dir() -> None:
    source = RESUME_TAILORING_SPEC.source_path

    assert source.name == "resume-tailoring"
    assert (source / "SKILL.md").is_file()
    assert (source / "scripts" / "generate.py").is_file()
    assert (source / "scripts" / "validate.py").is_file()


def test_script_tool_allows_resume_tailoring_scripts_via_its_allowlist(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "generate.py").write_text("", encoding="utf-8")
    (scripts / "validate.py").write_text("", encoding="utf-8")
    (scripts / "browse.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    tool = _script_tool(
        skill_dir, SkillToolPolicy(), allowed_scripts=RESUME_TAILORING_SPEC.allowed_scripts,
    )

    # resume-tailoring scripts are allowed through its own allowlist...
    assert not tool.invoke(
        {"script": "generate", "cli_args": "--input output/input.json --out output/evidence/draft_diffs.json"},
    ).startswith("ERROR:")
    assert not tool.invoke(
        {"script": "validate", "cli_args": "--out output/validation.json"},
    ).startswith("ERROR:")
    # ...but a job-discovery script is outside this skill's allowlist.
    assert tool.invoke(
        {"script": "browse", "cli_args": "https://example.com --out output/evidence"},
    ) == "ERROR: unsupported Skill script 'browse'"


def test_interview_prep_spec_is_registered_with_generate() -> None:
    spec = get_skill_spec("interview-prep")

    assert spec is INTERVIEW_PREP_SPEC
    assert spec.name == "interview-prep"
    assert spec.skill_type == "deterministic"
    assert spec.allowed_scripts == frozenset({"generate"})
    assert SKILL_REGISTRY["interview-prep"] is INTERVIEW_PREP_SPEC


def test_interview_prep_source_path_points_at_the_real_skill_dir() -> None:
    source = INTERVIEW_PREP_SPEC.source_path

    assert source.name == "interview-prep"
    assert (source / "SKILL.md").is_file()
    assert (source / "scripts" / "generate.py").is_file()


def test_script_tool_allows_interview_prep_scripts_via_its_allowlist(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "generate.py").write_text("", encoding="utf-8")
    (scripts / "browse.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    tool = _script_tool(
        skill_dir, SkillToolPolicy(), allowed_scripts=INTERVIEW_PREP_SPEC.allowed_scripts,
    )

    # interview-prep's generate script is allowed through its own allowlist...
    assert not tool.invoke(
        {"script": "generate", "cli_args": "--input output/input.json --out output/evidence/prep_kit.json"},
    ).startswith("ERROR:")
    # ...but a job-discovery script is outside this skill's allowlist.
    assert tool.invoke(
        {"script": "browse", "cli_args": "https://example.com --out output/evidence"},
    ) == "ERROR: unsupported Skill script 'browse'"


def test_application_tracking_spec_is_registered_with_track() -> None:
    spec = get_skill_spec("application-tracking")

    assert spec is APPLICATION_TRACKING_SPEC
    assert spec.name == "application-tracking"
    assert spec.skill_type == "service"
    assert spec.allowed_scripts == frozenset({"track"})
    assert SKILL_REGISTRY["application-tracking"] is APPLICATION_TRACKING_SPEC


def test_application_tracking_source_path_points_at_the_real_skill_dir() -> None:
    source = APPLICATION_TRACKING_SPEC.source_path

    assert source.name == "application-tracking"
    assert (source / "SKILL.md").is_file()
    assert (source / "scripts" / "track.py").is_file()


def test_script_tool_allows_application_tracking_scripts_via_its_allowlist(
    tmp_path: Path, monkeypatch,
) -> None:
    skill_dir = tmp_path / "skill"
    scripts = skill_dir / "scripts"
    scripts.mkdir(parents=True)
    (scripts / "track.py").write_text("", encoding="utf-8")
    (scripts / "browse.py").write_text("", encoding="utf-8")
    monkeypatch.setattr(
        "backend.app.services.job_discovery.skill_runtime.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(stdout="{}", stderr="", returncode=0),
    )
    tool = _script_tool(
        skill_dir,
        SkillToolPolicy(),
        allowed_scripts=APPLICATION_TRACKING_SPEC.allowed_scripts,
    )

    # application-tracking's track script is allowed through its own allowlist
    # (--out optional; here omitted, which _valid_output_args permits)...
    assert not tool.invoke(
        {"script": "track", "cli_args": "list-statuses"},
    ).startswith("ERROR:")
    # ...and --out under output/ is accepted.
    assert not tool.invoke(
        {"script": "track", "cli_args": "list-statuses --out output/evidence/statuses.json"},
    ).startswith("ERROR:")
    # ...but a job-discovery script is outside this skill's allowlist.
    assert tool.invoke(
        {"script": "browse", "cli_args": "https://example.com --out output/evidence"},
    ) == "ERROR: unsupported Skill script 'browse'"
