"""Validates snapshot content, dynamic answers, and local-sensitive requirements before persisting."""
from dataclasses import dataclass
from typing import Any

from backend.app.services.field_classification import classify_field, FieldClassification
from backend.app.domain.profiles import validate_local_sensitive_reference, LocalSensitiveReferenceError


@dataclass
class ApplicationSnapshotContent:
    """Wrapper type for all data that makes up an application snapshot."""
    job_snapshot: Any
    profile_facts: dict[str, Any]
    dynamic_answers: list[dict[str, Any]]
    local_sensitive_requirements: list[dict[str, Any]]
    attachment_ids: list[str]


# ── Public API ────────────────────────────────────────────────────────────────


class SnapshotValidationError(ValueError):
    """Raised when snapshot content fails validation. Contains stable error_code."""

    def __init__(self, error_code: str, message: str):
        super().__init__(message)
        self.error_code = error_code


def validate_snapshot_content(
    content: ApplicationSnapshotContent,
) -> ApplicationSnapshotContent:
    """Validate all pieces of an application snapshot before persisting.

    Raises ``SnapshotValidationError`` on any failure.
    """
    if not content.job_snapshot:
        raise SnapshotValidationError(
            "snapshot_validation_empty_job_snapshot",
            "job_snapshot must be non-empty",
        )
    if not content.profile_facts:
        raise SnapshotValidationError(
            "snapshot_validation_empty_profile_facts",
            "profile_facts must be non-empty",
        )
    if not content.attachment_ids:
        raise SnapshotValidationError(
            "snapshot_validation_no_attachments",
            "attachment_ids must contain at least one attachment",
        )

    validate_dynamic_answers(content.dynamic_answers)
    validate_local_sensitive_requirements(content.local_sensitive_requirements)

    return content


def validate_dynamic_answers(answers: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Validate dynamic answers: every answer must be classified as non_sensitive.

    Each answer dict must have ``classification == "non_sensitive"`` and its
    ``field_key`` must classify as ``NON_SENSITIVE``.  Unknown or local-sensitive
    fields are rejected.

    Returns the original list if valid; raises ``SnapshotValidationError`` otherwise.
    """
    for answer in answers:
        classification = answer.get("classification")
        if not classification:
            raise SnapshotValidationError(
                "snapshot_validation_missing_classification",
                f"Dynamic answer missing 'classification' field: {answer}",
            )
        if classification != "non_sensitive":
            raise SnapshotValidationError(
                "snapshot_validation_non_sensitive_required",
                f"Dynamic answer must have classification 'non_sensitive', got '{classification}': field_key={answer.get('field_key')}",
            )

        field_key = answer.get("field_key", "")
        field_cls = classify_field(field_key)
        if field_cls != FieldClassification.NON_SENSITIVE:
            raise SnapshotValidationError(
                "snapshot_validation_field_not_allowed",
                f"Dynamic answer field '{field_key}' classifies as '{field_cls.value}', only 'non_sensitive' allowed",
            )

    return answers


def validate_local_sensitive_requirements(
    reqs: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Validate local-sensitive requirements.

    Each requirement must:
      - Have a ``field_key`` that classifies as ``LOCAL_SENSITIVE``
      - Have a non-empty ``category``
      - Have a non-empty ``local_reference``
      - NOT contain any plaintext ``value`` (empty string or missing is OK)

    Returns the original list if valid; raises ``SnapshotValidationError`` otherwise.
    """
    for req in reqs:
        field_key = req.get("field_key")
        if not field_key:
            raise SnapshotValidationError(
                "snapshot_validation_missing_field_key",
                f"Local-sensitive requirement missing 'field_key': {req}",
            )

        field_cls = classify_field(field_key)
        if field_cls != FieldClassification.LOCAL_SENSITIVE:
            raise SnapshotValidationError(
                "snapshot_validation_field_not_local_sensitive",
                f"Requirement field_key '{field_key}' classifies as '{field_cls.value}', expected 'local_sensitive'",
            )

        category = req.get("category")
        if not category:
            raise SnapshotValidationError(
                "snapshot_validation_missing_category",
                f"Local-sensitive requirement missing 'category': {req}",
            )

        local_ref = req.get("local_reference")
        if not local_ref:
            raise SnapshotValidationError(
                "snapshot_validation_missing_local_reference",
                f"Local-sensitive requirement missing 'local_reference': {req}",
            )

        # Validate the local reference format via domain rules
        try:
            validate_local_sensitive_reference(req.get("category", "unknown"), req.get("local_reference", ""))
        except LocalSensitiveReferenceError as e:
            raise SnapshotValidationError(
                "snapshot_validation_invalid_local_reference",
                f"Local-sensitive requirement has invalid local_reference: {e}",
            )

        # Reject plaintext values — only empty string or absent are acceptable
        value = req.get("value")
        if value:
            raise SnapshotValidationError(
                "snapshot_validation_plaintext_value",
                f"Local-sensitive requirement must not contain plaintext value: field_key={field_key}",
            )

    return reqs
