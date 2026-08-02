"""Validates LangGraph structured output before persisting as MatchReport."""
from typing import Any


class MatchValidationError(ValueError):
    """Raised when model output fails validation. Contains stable error_code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def validate_match_output(
    output: dict[str, Any],
    job_snapshot: Any,
    profile_snapshot: Any,
) -> dict[str, Any]:
    """Validate model output against job/profile snapshots. Returns output if valid, raises MatchValidationError otherwise."""

    # Collect all requirement IDs for cross-reference
    all_requirement_ids: set[str] = set()
    for item in output.get("strengths", []) + output.get("gaps", []) + output.get("unknowns", []):
        req_id = item.get("requirement_id")
        if not req_id:
            raise MatchValidationError("match_validation_missing_requirement_id", f"Item missing requirement_id: {item}")
        if req_id in all_requirement_ids:
            raise MatchValidationError("match_validation_duplicate_requirement_id", f"Duplicate requirement_id: {req_id}")
        all_requirement_ids.add(req_id)

    # Collect all valid evidence IDs from profile
    valid_evidence_ids: set[str] = set()
    for field_path, ev_ids in profile_snapshot.evidence_refs.items():
        valid_evidence_ids.update(ev_ids)

    # Validate each assessment category
    _validate_assessments(output.get("strengths", []), "satisfied", "strengths", all_requirement_ids, valid_evidence_ids)
    _validate_assessments(output.get("gaps", []), "gap", "gaps", all_requirement_ids, valid_evidence_ids)
    _validate_assessments(output.get("unknowns", []), "unknown", "unknowns", all_requirement_ids, valid_evidence_ids)

    # Validate risks reference requirement IDs
    for risk in output.get("risks", []):
        risk_req_ids = risk.get("requirement_ids", [])
        if not risk_req_ids:
            raise MatchValidationError("match_validation_risk_missing_ref", "Risk missing requirement ref: requirement_ids is empty")
        for rid in risk_req_ids:
            if rid not in all_requirement_ids:
                raise MatchValidationError("match_validation_risk_invalid_ref", f"Risk references unknown requirement_id: {rid}")

    # Validate recommendation references requirement IDs
    rec = output.get("recommendation", {})
    rec_req_ids = rec.get("requirement_ids", [])
    for rid in rec_req_ids:
        if rid not in all_requirement_ids:
            raise MatchValidationError("match_validation_recommendation_invalid_ref", f"Recommendation references unknown requirement_id: {rid}")

    return output


def _validate_assessments(
    items: list[dict],
    expected_verdict: str,
    category: str,
    all_req_ids: set[str],
    valid_evidence_ids: set[str],
) -> None:
    for item in items:
        # Verdict must match category
        actual = item.get("verdict")
        if actual != expected_verdict:
            raise MatchValidationError(
                "match_validation_verdict_category_mismatch",
                f"verdict mismatch in '{category}': has '{actual}' instead of '{expected_verdict}' for requirement_id={item.get('requirement_id')}"
            )

        # satisfied must have profile_field_path and evidence
        if expected_verdict == "satisfied":
            if not item.get("profile_field_path"):
                raise MatchValidationError(
                    "match_validation_satisfied_missing_profile_path",
                    f"Satisfied item missing profile_field_path: requirement_id={item.get('requirement_id')}"
                )
            ev_ids = item.get("evidence_ids", [])
            if not ev_ids:
                raise MatchValidationError(
                    "match_validation_satisfied_missing_evidence",
                    f"Satisfied item has no evidence_ids: requirement_id={item.get('requirement_id')}"
                )
            for eid in ev_ids:
                if eid not in valid_evidence_ids:
                    raise MatchValidationError(
                        "match_evidence_ref_invalid",
                        f"evidence ID not found in profile: {eid} (requirement_id={item.get('requirement_id')})"
                    )

        # unknown must NOT have fabricated evidence
        if expected_verdict == "unknown":
            ev_ids = item.get("evidence_ids", [])
            if ev_ids:
                raise MatchValidationError(
                    "match_validation_unknown_with_evidence",
                    f"unknown item must not carry fabricated evidence: requirement_id={item.get('requirement_id')}"
                )
