import pytest

from backend.app.domain.profiles import (
    LocalSensitiveReferenceError,
    validate_local_sensitive_reference,
)


@pytest.mark.parametrize(
    "category",
    ["government_id", "family_member", "emergency_contact"],
)
def test_local_sensitive_reference_accepts_only_metadata(category: str) -> None:
    validate_local_sensitive_reference(category, "lsr:v1:" + "a" * 64)


@pytest.mark.parametrize(
    ("category", "reference"),
    [
        ("phone", "lsr:v1:" + "a" * 64),
        ("government_id", "110101200001011234"),
        ("family_member", "lsr:v1:" + "A" * 64),
        ("emergency_contact", "lsr:v1:" + "a" * 63),
    ],
)
def test_local_sensitive_reference_rejects_unknown_or_plaintext(
    category: str, reference: str
) -> None:
    with pytest.raises(LocalSensitiveReferenceError):
        validate_local_sensitive_reference(category, reference)
