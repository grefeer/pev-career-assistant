import json
from langchain_core.messages import HumanMessage
from .schemas import RequirementAssessment, ReferencedRecommendation, MatchComputationOutput
from .prompts import EXTRACT_REQUIREMENTS_PROMPT, MATCH_ASSESSMENT_PROMPT


async def extract_requirements(state: dict, model) -> dict:
    job = state["job_snapshot"]
    prompt = EXTRACT_REQUIREMENTS_PROMPT.format(
        company_name=job.get("company_name", ""),
        title=job.get("title", ""),
        description_text=job.get("description_text", ""),
        locations=job.get("locations", []),
        industries=job.get("industries", []),
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    try:
        requirements = json.loads(response.content)
    except json.JSONDecodeError:
        return {"error": "match_model_validation_failed", "next_step": "fail"}
    return {"job_requirements": requirements, "next_step": "assess"}


async def assess_match(state: dict, model) -> dict:
    requirements_json = json.dumps(state["job_requirements"], ensure_ascii=False)
    profile_json = json.dumps(state["profile_snapshot"]["facts"], ensure_ascii=False)
    evidence_json = json.dumps(state["profile_snapshot"].get("evidence_refs", {}), ensure_ascii=False)

    prompt = MATCH_ASSESSMENT_PROMPT.format(
        requirements_json=requirements_json,
        profile_json=profile_json,
        evidence_json=evidence_json,
    )
    response = await model.ainvoke([HumanMessage(content=prompt)])
    try:
        raw = json.loads(response.content)
    except json.JSONDecodeError:
        return {"error": "match_model_validation_failed", "next_step": "fail"}

    # Parse into structured output
    strengths = [RequirementAssessment(**a) for a in raw.get("strengths", [])]
    gaps = [RequirementAssessment(**a) for a in raw.get("gaps", [])]
    unknowns = [RequirementAssessment(**a) for a in raw.get("unknowns", [])]
    risks = [RequirementAssessment(**a) for a in raw.get("risks", [])]
    recommendation = ReferencedRecommendation(**raw.get("recommendation", {"text": "", "requirement_ids": []}))

    result = MatchComputationOutput(
        strengths=strengths,
        gaps=gaps,
        unknowns=unknowns,
        risks=risks,
        recommendation=recommendation,
    )
    return {"result": result, "next_step": "finish"}
