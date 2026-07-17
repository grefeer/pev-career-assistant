"""Validates resume diff operations before applying draft changes."""
from typing import Any

VALID_OPS = frozenset({"reorder", "rephrase", "summarize", "omit", "highlight"})


class DraftValidationError(ValueError):
    """Raised when a draft diff fails validation. Contains stable error_code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def validate_draft_diffs(
    diffs: list[dict[str, Any]],
    confirmed_facts: dict[str, Any],
    evidence_refs: dict[str, list[str]],
) -> list[dict[str, Any]]:
    """Validate a list of resume diff operations against confirmed facts and evidence references.

    Each diff must have:
      - ``op`` in {reorder, rephrase, summarize, omit, highlight}
      - ``section`` non-empty
      - ``fact_ref`` present in ``confirmed_facts``
      - every ``evidence_ids`` entry present in ``evidence_refs`` values

    Returns the original list if valid; raises ``DraftValidationError`` otherwise.
    """
    valid_evidence_ids: set[str] = set()
    for ev_ids in evidence_refs.values():
        valid_evidence_ids.update(ev_ids)

    for idx, diff in enumerate(diffs):
        op = diff.get("op")
        if not op:
            raise DraftValidationError(
                "draft_validation_missing_op",
                f"Diff at index {idx} is missing 'op' field",
            )
        if op not in VALID_OPS:
            raise DraftValidationError(
                "draft_validation_invalid_op",
                f"Diff at index {idx} has invalid op '{op}'; expected one of {sorted(VALID_OPS)}",
            )

        section = diff.get("section")
        if not section:
            raise DraftValidationError(
                "draft_validation_empty_section",
                f"Diff at index {idx} has empty or missing 'section' field",
            )

        fact_ref = diff.get("fact_ref")
        if fact_ref not in confirmed_facts:
            raise DraftValidationError(
                "draft_validation_invalid_fact_ref",
                f"Diff at index {idx} references unknown fact_ref '{fact_ref}'",
            )

        for eid in diff.get("evidence_ids", []):
            if eid not in valid_evidence_ids:
                raise DraftValidationError(
                    "draft_validation_invalid_evidence",
                    f"Diff at index {idx} references unknown evidence_id '{eid}'",
                )

    return diffs
