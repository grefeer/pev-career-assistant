"""Tests for snapshot_validators — validates snapshot content, dynamic answers, and local sensitive requirements."""
import pytest

from backend.app.services.snapshot_validators import (
    validate_snapshot_content,
    validate_dynamic_answers,
    validate_local_sensitive_requirements,
    SnapshotValidationError,
    ApplicationSnapshotContent,
)


class TestValidateDynamicAnswers:
    """validate_dynamic_answers(answers) — every answer must be classified non_sensitive."""

    def test_valid_non_sensitive_answers_pass(self):
        answers = [
            {"field_key": "education", "value": "PKU", "classification": "non_sensitive"},
            {"field_key": "skills", "value": "Python", "classification": "non_sensitive"},
        ]
        result = validate_dynamic_answers(answers)
        assert result == answers

    def test_sensitive_classification_fails(self):
        answers = [
            {"field_key": "education", "value": "PKU", "classification": "sensitive"},
        ]
        with pytest.raises(SnapshotValidationError, match="non_sensitive"):
            validate_dynamic_answers(answers)

    def test_unknown_classification_fails(self):
        answers = [
            {"field_key": "skills", "value": "Rust", "classification": "unknown"},
        ]
        with pytest.raises(SnapshotValidationError, match="non_sensitive"):
            validate_dynamic_answers(answers)

    def test_local_sensitive_field_rejected(self):
        answers = [
            {"field_key": "id_number", "value": "110101...", "classification": "non_sensitive"},
        ]
        with pytest.raises(SnapshotValidationError, match="local_sensitive"):
            validate_dynamic_answers(answers)

    def test_missing_classification_fails(self):
        answers = [
            {"field_key": "education", "value": "PKU"},
        ]
        with pytest.raises(SnapshotValidationError, match="classification"):
            validate_dynamic_answers(answers)

    def test_unknown_field_key_fails(self):
        answers = [
            {"field_key": "nonexistent_field", "value": "something", "classification": "non_sensitive"},
        ]
        with pytest.raises(SnapshotValidationError, match="unknown"):
            validate_dynamic_answers(answers)

    def test_empty_answers_list_passes(self):
        result = validate_dynamic_answers([])
        assert result == []


class TestValidateLocalSensitiveRequirements:
    """validate_local_sensitive_requirements(reqs) — each req must be a valid local-sensitive reference."""

    def test_valid_requirements_pass(self):
        reqs = [
            {
                "field_key": "id_number",
                "category": "government_id",
                "local_reference": "lsr:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
            },
            {
                "field_key": "family_members",
                "category": "family_member",
                "local_reference": "lsr:v1:bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
            },
        ]
        result = validate_local_sensitive_requirements(reqs)
        assert result == reqs

    def test_missing_field_key_fails(self):
        reqs = [
            {"category": "government_id", "local_reference": "lsr:v1:a"},
        ]
        with pytest.raises(SnapshotValidationError, match="field_key"):
            validate_local_sensitive_requirements(reqs)

    def test_missing_category_fails(self):
        reqs = [
            {"field_key": "id_number", "local_reference": "lsr:v1:a"},
        ]
        with pytest.raises(SnapshotValidationError, match="category"):
            validate_local_sensitive_requirements(reqs)

    def test_missing_local_reference_fails(self):
        reqs = [
            {"field_key": "id_number", "category": "government_id"},
        ]
        with pytest.raises(SnapshotValidationError, match="local_reference"):
            validate_local_sensitive_requirements(reqs)

    def test_plaintext_value_present_fails(self):
        reqs = [
            {
                "field_key": "id_number",
                "category": "government_id",
                "local_reference": "lsr:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "value": "110101199001011234",
            },
        ]
        with pytest.raises(SnapshotValidationError, match="plaintext"):
            validate_local_sensitive_requirements(reqs)

    def test_non_sensitive_field_key_fails(self):
        reqs = [
            {
                "field_key": "education",
                "category": "education",
                "local_reference": "lsr:v1:aaa",
            },
        ]
        with pytest.raises(SnapshotValidationError, match="local_sensitive"):
            validate_local_sensitive_requirements(reqs)

    def test_unknown_field_key_fails(self):
        reqs = [
            {
                "field_key": "random_unknown",
                "category": "unknown",
                "local_reference": "lsr:v1:aaa",
            },
        ]
        with pytest.raises(SnapshotValidationError, match="local_sensitive"):
            validate_local_sensitive_requirements(reqs)

    def test_empty_value_is_allowed(self):
        reqs = [
            {
                "field_key": "id_number",
                "category": "government_id",
                "local_reference": "lsr:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                "value": "",
            },
        ]
        result = validate_local_sensitive_requirements(reqs)
        assert result == reqs

    def test_empty_reqs_list_passes(self):
        result = validate_local_sensitive_requirements([])
        assert result == []


class TestValidateSnapshotContent:
    """validate_snapshot_content(content) — validates all pieces together via ApplicationSnapshotContent."""

    def test_valid_snapshot_passes(self):
        content = ApplicationSnapshotContent(
            job_snapshot={"job_id": "job-001"},
            profile_facts={"education": [{"school": "PKU"}]},
            dynamic_answers=[
                {"field_key": "education", "value": "PKU", "classification": "non_sensitive"},
            ],
            local_sensitive_requirements=[
                {
                    "field_key": "id_number",
                    "category": "government_id",
                    "local_reference": "lsr:v1:aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
                },
            ],
            attachment_ids=["att-001", "att-002"],
        )
        result = validate_snapshot_content(content)
        assert result == content

    def test_empty_job_snapshot_fails(self):
        content = ApplicationSnapshotContent(
            job_snapshot={},
            profile_facts={"education": [{"school": "PKU"}]},
            dynamic_answers=[],
            local_sensitive_requirements=[],
            attachment_ids=["att-001"],
        )
        with pytest.raises(SnapshotValidationError, match="job_snapshot"):
            validate_snapshot_content(content)

    def test_empty_profile_facts_fails(self):
        content = ApplicationSnapshotContent(
            job_snapshot={"job_id": "job-001"},
            profile_facts={},
            dynamic_answers=[],
            local_sensitive_requirements=[],
            attachment_ids=["att-001"],
        )
        with pytest.raises(SnapshotValidationError, match="profile_facts"):
            validate_snapshot_content(content)

    def test_missing_attachment_ids_fails(self):
        content = ApplicationSnapshotContent(
            job_snapshot={"job_id": "job-001"},
            profile_facts={"education": [{"school": "PKU"}]},
            dynamic_answers=[],
            local_sensitive_requirements=[],
            attachment_ids=[],
        )
        with pytest.raises(SnapshotValidationError, match="attachment"):
            validate_snapshot_content(content)

    def test_invalid_dynamic_answers_propagates(self):
        content = ApplicationSnapshotContent(
            job_snapshot={"job_id": "job-001"},
            profile_facts={"education": [{"school": "PKU"}]},
            dynamic_answers=[
                {"field_key": "education", "value": "PKU", "classification": "sensitive"},
            ],
            local_sensitive_requirements=[],
            attachment_ids=["att-001"],
        )
        with pytest.raises(SnapshotValidationError, match="non_sensitive"):
            validate_snapshot_content(content)

    def test_invalid_local_sensitive_reqs_propagates(self):
        content = ApplicationSnapshotContent(
            job_snapshot={"job_id": "job-001"},
            profile_facts={"education": [{"school": "PKU"}]},
            dynamic_answers=[],
            local_sensitive_requirements=[
                {"category": "government_id", "local_reference": "lsr:v1:aaa"},
            ],
            attachment_ids=["att-001"],
        )
        with pytest.raises(SnapshotValidationError, match="field_key"):
            validate_snapshot_content(content)
