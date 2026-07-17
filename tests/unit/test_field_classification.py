# tests/unit/test_field_classification.py
import pytest
from backend.app.services.field_classification import (
    classify_field,
    is_non_sensitive,
    is_local_sensitive,
    filter_non_sensitive,
    extract_local_sensitive_requirements,
    FieldClassification,
    UNKNOWN_FIELD,
)


class TestClassifyField:
    def test_known_non_sensitive(self):
        assert classify_field("education") == FieldClassification.NON_SENSITIVE
        assert classify_field("skills") == FieldClassification.NON_SENSITIVE
        assert classify_field("work_experience") == FieldClassification.NON_SENSITIVE

    def test_known_local_sensitive(self):
        assert classify_field("id_number") == FieldClassification.LOCAL_SENSITIVE
        assert classify_field("family_members") == FieldClassification.LOCAL_SENSITIVE
        assert classify_field("emergency_contact") == FieldClassification.LOCAL_SENSITIVE

    def test_nested_field_path(self):
        assert classify_field("education.0.school") == FieldClassification.NON_SENSITIVE
        assert classify_field("family_members.0.name") == FieldClassification.LOCAL_SENSITIVE

    def test_unknown_field(self):
        assert classify_field("unknown_field") == FieldClassification.UNKNOWN
        assert classify_field("passwords") == FieldClassification.UNKNOWN


class TestIsNonSensitive:
    def test_allows_known_fields(self):
        assert is_non_sensitive("education") is True
        assert is_non_sensitive("skills") is True

    def test_rejects_local_sensitive(self):
        assert is_non_sensitive("id_number") is False
        assert is_non_sensitive("family_members") is False

    def test_rejects_unknown(self):
        assert is_non_sensitive("random_field") is False


class TestFilterNonSensitive:
    def test_strips_local_sensitive_and_unknown(self):
        facts = {
            "education": [{"school": "PKU"}],
            "skills": ["Python"],
            "id_number": "110101199001011234",
            "family_members": [{"name": "John", "relation": "father"}],
            "unknown_field": "value",
        }
        result = filter_non_sensitive(facts)
        assert "education" in result
        assert "skills" in result
        assert "id_number" not in result
        assert "family_members" not in result
        assert "unknown_field" not in result


class TestExtractLocalSensitiveRequirements:
    def test_extracts_semantic_keys_only(self):
        facts = {
            "education": [{"school": "PKU"}],
            "id_number": "110101199001011234",
            "family_members": [{"name": "John", "relation": "father"}],
            "emergency_contact": {"name": "Jane", "phone": "13800000000"},
        }
        result = extract_local_sensitive_requirements(facts)
        keys = {r["field_key"] for r in result}
        assert "id_number" in keys
        assert "family_members" in keys
        assert "emergency_contact" in keys
        assert "education" not in keys
        # Must NOT contain plaintext values
        for r in result:
            assert "110101" not in str(r)
            assert "John" not in str(r)
            assert "13800000000" not in str(r)
