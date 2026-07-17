"""Versioned field classification table — single source of truth for field allowlists."""
from enum import Enum
from typing import Any

from backend.app.domain.profiles import validate_local_sensitive_reference


class FieldClassification(str, Enum):
    NON_SENSITIVE = "non_sensitive"
    LOCAL_SENSITIVE = "local_sensitive"
    UNKNOWN = "unknown"


UNKNOWN_FIELD: FieldClassification = FieldClassification.UNKNOWN

# VERSION 1.0 — update when adding/removing fields; bump version in commit message
CLASSIFICATION_VERSION = "1.0"

# Top-level field paths → classification
ALLOWED_FIELDS: dict[str, FieldClassification] = {
    # --- Non-sensitive fields ---
    "name": FieldClassification.NON_SENSITIVE,
    "gender": FieldClassification.NON_SENSITIVE,
    "birth_date": FieldClassification.NON_SENSITIVE,
    "email": FieldClassification.NON_SENSITIVE,
    "phone": FieldClassification.NON_SENSITIVE,
    "education": FieldClassification.NON_SENSITIVE,
    "skills": FieldClassification.NON_SENSITIVE,
    "languages": FieldClassification.NON_SENSITIVE,
    "work_experience": FieldClassification.NON_SENSITIVE,
    "internship_experience": FieldClassification.NON_SENSITIVE,
    "project_experience": FieldClassification.NON_SENSITIVE,
    "awards": FieldClassification.NON_SENSITIVE,
    "certifications": FieldClassification.NON_SENSITIVE,
    "self_introduction": FieldClassification.NON_SENSITIVE,
    "career_objective": FieldClassification.NON_SENSITIVE,
    "expected_city": FieldClassification.NON_SENSITIVE,
    "expected_salary": FieldClassification.NON_SENSITIVE,
    "available_date": FieldClassification.NON_SENSITIVE,

    # --- Local-sensitive fields (semantic keys only, no plaintext in cloud) ---
    "id_number": FieldClassification.LOCAL_SENSITIVE,
    "family_members": FieldClassification.LOCAL_SENSITIVE,
    "emergency_contact": FieldClassification.LOCAL_SENSITIVE,
    "home_address": FieldClassification.LOCAL_SENSITIVE,
    "passport_number": FieldClassification.LOCAL_SENSITIVE,
    "bank_account": FieldClassification.LOCAL_SENSITIVE,
    "political_status": FieldClassification.LOCAL_SENSITIVE,
    "marital_status": FieldClassification.LOCAL_SENSITIVE,
}


def _top_level_key(path: str) -> str:
    return path.split(".")[0]


def classify_field(path: str) -> FieldClassification:
    """Classify a field path as non_sensitive, local_sensitive, or unknown."""
    return ALLOWED_FIELDS.get(_top_level_key(path), FieldClassification.UNKNOWN)


def is_non_sensitive(path: str) -> bool:
    return classify_field(path) == FieldClassification.NON_SENSITIVE


def is_local_sensitive(path: str) -> bool:
    return classify_field(path) == FieldClassification.LOCAL_SENSITIVE


def filter_non_sensitive(facts: dict[str, Any]) -> dict[str, Any]:
    """Return only non-sensitive fields from a facts dict."""
    return {
        k: v for k, v in facts.items()
        if classify_field(k) == FieldClassification.NON_SENSITIVE
    }


def build_local_sensitive_requirements(references: dict[str, Any]) -> list[dict[str, str]]:
    """Validate and return existing local-sensitive references from vault metadata.

    Iterates over the vault metadata dict (ConfirmedProfileVersion.local_sensitive_references),
    validates each reference via validate_local_sensitive_reference(), and passes through
    valid entries. NEVER generates new references.
    """
    requirements: list[dict[str, str]] = []
    for category, metadata in references.items():
        reference = metadata.get("reference", "") if isinstance(metadata, dict) else ""
        validate_local_sensitive_reference(category, reference)
        requirements.append({
            "field_key": category,
            "category": category,
            "local_reference": reference,
        })
    return requirements
