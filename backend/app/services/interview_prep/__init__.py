"""Interview Prep skill - agent-driven interview-prep kit generation.

The concrete generator that turns a target job snapshot (+ confirmed profile
facts + preferences + match analysis) into a structured interview-prep kit via
a DeepSeek (OpenAI-compatible) LLM.  Delivered through the
``InterviewPrepService`` -> ``InterviewPrepKit`` pipeline.
"""

from backend.app.services.interview_prep.generator import (
    InterviewPrepGenerationError,
    LLMInterviewPrepGenerator,
)
from backend.app.services.interview_prep.llm_factory import (
    InterviewPrepConfigError,
    build_interview_prep_llm,
)

__all__ = [
    "InterviewPrepConfigError",
    "InterviewPrepGenerationError",
    "LLMInterviewPrepGenerator",
    "build_interview_prep_llm",
]
