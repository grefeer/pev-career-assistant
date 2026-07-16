from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import TypeAlias


JsonScalar: TypeAlias = None | bool | int | float | str
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]


class ResumeAssetStatus(StrEnum):
    PENDING_UPLOAD = "pending_upload"
    READY = "ready"
    UPLOAD_FAILED = "upload_failed"


class ResumeImportStatus(StrEnum):
    PENDING = "pending"
    PARSING = "parsing"
    AWAITING_CONFIRMATION = "awaiting_confirmation"
    NEEDS_MANUAL_ENTRY = "needs_manual_entry"
    FAILED = "failed"


class EvidenceDecisionAction(StrEnum):
    CONFIRM = "confirm"
    CORRECT = "correct"
    IGNORE = "ignore"


class EvidenceDiffAction(StrEnum):
    ADD = "add"
    REPLACE = "replace"
    UNCHANGED = "unchanged"
    CONFLICT = "conflict"


LOCAL_SENSITIVE_CATEGORIES = frozenset(
    {"government_id", "family_member", "emergency_contact"}
)
LOCAL_REFERENCE_PATTERN = re.compile(r"^lsr:v1:[0-9a-f]{64}$")
STANDARD_FIELD_PATHS = frozenset(
    {
        "basics.name",
        "basics.email",
        "basics.phone",
        "education",
        "experience",
        "projects",
        "skills",
        "awards",
        "certificates",
        "languages",
        "portfolio_links",
    }
)


class LocalSensitiveReferenceError(ValueError):
    error_code = "invalid_local_sensitive_reference"


class UnsupportedResumeTypeError(ValueError):
    error_code = "unsupported_resume_type"


@dataclass(frozen=True)
class ParsedResumeDocument:
    text: str
    needs_manual_entry: bool
    error_code: str | None


@dataclass(frozen=True)
class EvidenceCandidate:
    field_path: str
    candidate_value: JsonValue
    evidence_excerpt: str
    confidence: int


def validate_local_sensitive_reference(category: str, reference: str) -> None:
    if category not in LOCAL_SENSITIVE_CATEGORIES:
        raise LocalSensitiveReferenceError("unsupported category")
    if LOCAL_REFERENCE_PATTERN.fullmatch(reference) is None:
        raise LocalSensitiveReferenceError("invalid irreversible reference")
