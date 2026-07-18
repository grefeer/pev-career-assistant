from types import SimpleNamespace

import pytest

from src.evidence_matching.agents import assess_match


class FakeModel:
    def __init__(self, content: str) -> None:
        self.content = content

    async def ainvoke(self, _messages):
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_assess_match_converts_structured_validation_errors_to_fail_state():
    result = await assess_match(
        {
            "job_requirements": [
                {
                    "requirement_id": "req-001",
                    "requirement": "Python",
                    "job_field_path": "description_text",
                }
            ],
            "profile_snapshot": {
                "facts": {"skills": ["Python"]},
                "evidence_refs": {"skills": ["ev-skill-001"]},
            },
        },
        FakeModel(
            """
            {
              "strengths": [],
              "gaps": [],
              "unknowns": [],
              "risks": [{"requirement_ids": ["req-001"], "detail": "missing field"}],
              "recommendation": {"text": "Review manually", "requirement_ids": ["req-001"]}
            }
            """
        ),
    )

    assert result == {
        "error": "match_model_validation_failed",
        "next_step": "fail",
    }
