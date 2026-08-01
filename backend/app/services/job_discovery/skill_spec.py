"""Skill runtime extension point.

A ``SkillSpec`` bundles the runtime identity of one bundled skill: its
``name`` (the on-disk ``skill/<name>`` directory and the artifact-store path
segment), the allowlisted helper scripts a coordinator may invoke through the
restricted ``run_skill_script`` tool, and its runtime ``type``
(``deterministic`` orchestrator, ``agent``-driven, or plain ``service``).

The job-discovery skill is the default spec.  Adding a parallel skill means
adding an entry to :data:`SKILL_REGISTRY` plus its source directory under
``skill/``; the shared plumbing (``SkillArtifactStore``, the script tool, the
tool policy) is then parameterized by that spec without forking the default
path.  This module never touches the filesystem at construction time; the
source directory is only read by ``SkillArtifactStore.prepare``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


_REPO_ROOT = Path(__file__).resolve().parents[4]


#: The job-discovery coordinator may only invoke these bundled helper scripts
#: through the restricted ``run_skill_script`` tool.  Keeping the canonical set
#: here (rather than only in ``skill_runtime``) lets a second skill declare its
#: own set without editing the default path's allowlist.
JOB_DISCOVERY_SCRIPTS: frozenset[str] = frozenset({
    "browse", "validate", "normalize", "deduplicate", "ocr_image", "state",
    "read_evidence", "write_candidates", "coverage_gate",
})


@dataclass(frozen=True)
class SkillSpec:
    """Runtime identity of one bundled skill.

    ``name`` doubles as the on-disk skill directory (``skill/<name>``) and the
    artifact-store path segment, so a task's cloned skill working directory and
    its published evidence object keys are namespaced per skill and cannot
    collide across skills.
    """

    name: str
    allowed_scripts: frozenset[str]
    skill_type: str  # "deterministic" | "agent" | "service"

    @property
    def source_path(self) -> Path:
        """Immutable repository source for this skill (``skill/<name>``)."""
        return _REPO_ROOT / "skill" / self.name


JOB_DISCOVERY_SPEC = SkillSpec(
    name="job-discovery",
    allowed_scripts=JOB_DISCOVERY_SCRIPTS,
    skill_type="deterministic",
)


#: The company-research coordinator only needs its own page fetcher.  Unlike
#: job-discovery it does not paginate, preference-filter, or run a coverage
#: gate, so its allowlist is intentionally a single script.  Extraction is
#: deterministic (page text is parsed in-process by the runtime).
COMPANY_RESEARCH_SCRIPTS: frozenset[str] = frozenset({"browse"})

COMPANY_RESEARCH_SPEC = SkillSpec(
    name="company-research",
    allowed_scripts=COMPANY_RESEARCH_SCRIPTS,
    skill_type="deterministic",
)


#: The resume-tailoring skill produces LLM resume-diff operations for a target
#: job.  ``generate`` runs the bounded LLM call + tolerant JSON parse; ``validate``
#: grounds each diff against confirmed facts/evidence before application.  Both
#: are deterministic CLIs (the runtime calls them through ``run_skill_script``);
#: the skill is a parallel artifact and does not touch the backend resume store.
RESUME_TAILORING_SCRIPTS: frozenset[str] = frozenset({"generate", "validate"})

RESUME_TAILORING_SPEC = SkillSpec(
    name="resume-tailoring",
    allowed_scripts=RESUME_TAILORING_SCRIPTS,
    skill_type="deterministic",
)


#: The interview-prep skill produces a five-section interview-prep kit for a
#: target job via a single LLM call.  Only ``generate`` is needed - there is no
#: grounding step (the five content sections are study material, not fact
#: references).  Deterministic CLI; the runtime calls it through
#: ``run_skill_script``.  Like resume-tailoring it is a parallel artifact and
#: does not touch the backend interview-prep store.
INTERVIEW_PREP_SCRIPTS: frozenset[str] = frozenset({"generate"})

INTERVIEW_PREP_SPEC = SkillSpec(
    name="interview-prep",
    allowed_scripts=INTERVIEW_PREP_SCRIPTS,
    skill_type="deterministic",
)


#: Registry of skills available to the runtime.  Add a parallel skill by
#: appending a ``SkillSpec`` here and creating its ``skill/<name>`` source
#: directory; the shared artifact store and script tool then accept its
#: ``allowed_scripts`` without touching the job-discovery default path.
SKILL_REGISTRY: dict[str, SkillSpec] = {
    JOB_DISCOVERY_SPEC.name: JOB_DISCOVERY_SPEC,
    COMPANY_RESEARCH_SPEC.name: COMPANY_RESEARCH_SPEC,
    RESUME_TAILORING_SPEC.name: RESUME_TAILORING_SPEC,
    INTERVIEW_PREP_SPEC.name: INTERVIEW_PREP_SPEC,
}


def get_skill_spec(name: str) -> SkillSpec:
    """Return the registered spec for ``name``.

    Raises ``KeyError`` for an unregistered skill so a typo fails loudly at
    runtime construction rather than silently cloning the wrong source.
    """
    try:
        return SKILL_REGISTRY[name]
    except KeyError:
        raise KeyError(f"no registered SkillSpec for {name!r}") from None
