"""Resume Tailoring skill - agent-driven resume diff generation.

The concrete :class:`DraftGenerator` implementation that turns a target job
snapshot + confirmed profile facts + user preferences + match analysis into a
list of resume diff operations via a DeepSeek (OpenAI-compatible) LLM.

The skill reuses the existing ``ResumeDraftService`` -> ``ResumeDraft``
pipeline; this package only supplies the previously-missing generator seam plus
the bounded LLM factory that wires it into the application lifespan.
"""

from backend.app.services.resume_tailoring.generator import (
    DraftGenerationError,
    LLMDraftGenerator,
)
from backend.app.services.resume_tailoring.llm_factory import (
    DraftGeneratorConfigError,
    build_draft_generator_llm,
)

__all__ = [
    "DraftGenerationError",
    "DraftGeneratorConfigError",
    "LLMDraftGenerator",
    "build_draft_generator_llm",
]
