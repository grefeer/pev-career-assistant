# tests/unit/test_field_classification.py
from backend.app.services.field_classification import (
    classify_field,
    is_non_sensitive,
    filter_non_sensitive,
    build_local_sensitive_requirements,
    FieldClassification,
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


class TestBuildLocalSensitiveRequirements:
    def test_validates_and_passes_through_references(self):
        references = {
            "government_id": {
                "reference": "lsr:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "updated_at": "2026-01-01T00:00:00+00:00",
            },
            "family_member": {
                "reference": "lsr:v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
                "updated_at": "2026-01-02T00:00:00+00:00",
            },
            "emergency_contact": {
                "reference": "lsr:v1:cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
                "updated_at": "2026-01-03T00:00:00+00:00",
            },
        }
        result = build_local_sensitive_requirements(references)
        keys = {r["field_key"] for r in result}
        assert "government_id" in keys
        assert "family_member" in keys
        assert "emergency_contact" in keys
        # Must pass through the exact references without generating new ones
        for r in result:
            cat = r["field_key"]
            assert r["local_reference"] == references[cat]["reference"]
            assert r["category"] == cat
