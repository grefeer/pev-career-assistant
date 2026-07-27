"""Unit tests for personalized-discovery domain contracts (Task 1)."""

from __future__ import annotations

import pytest

from backend.app.domain.personalized_discovery import (
    RecommendationPresentationState,
    SourceStatusReason,
    normalize_role_terms,
    source_status_copy,
    title_matches_role_recall,
    validate_application_url,
)
from backend.app.domain.personalized_discovery import (
    UrlValidationFailure,
    ValidatedApplicationUrl,
)


def test_role_terms_are_trimmed_deduplicated_and_nonblank() -> None:
    assert normalize_role_terms([" AI应用开发 ", "ai应用开发", "Agent开发"]) == [
        "AI应用开发",
        "Agent开发",
    ]
    with pytest.raises(ValueError, match="blank"):
        normalize_role_terms([" "])


def test_role_terms_caps_length_and_count() -> None:
    long = "x" * 200
    out = normalize_role_terms([long])
    assert out == ["x" * 128]
    capped = normalize_role_terms([f"role{i}" for i in range(150)])
    assert len(capped) == 100


@pytest.mark.parametrize(
    "url",
    [
        "javascript:alert(1)",
        "mailto:a@example.com",
        "data:text/html,<script>",
        "https://u:p@jobs.example.com/a",
        "https://127.0.0.1/a",
        "https://10.0.0.1/a",
        "https://localhost/a",
        "ftp://jobs.example.com/a",
        "",
        None,
    ],
)
def test_url_validator_rejects_unsafe_urls(url: str | None) -> None:
    assert isinstance(
        validate_application_url(url, {"jobs.example.com"}),
        UrlValidationFailure,
    )


def test_url_validator_accepts_exact_allowed_host() -> None:
    res = validate_application_url(
        "https://jobs.example.com/positions/42", {"jobs.example.com"}
    )
    assert isinstance(res, ValidatedApplicationUrl)
    assert res.host == "jobs.example.com"


def test_url_validator_rejects_host_not_in_allowlist() -> None:
    failure = validate_application_url(
        "https://evil.example.com/x", {"jobs.example.com"}
    )
    assert isinstance(failure, UrlValidationFailure)
    assert failure.reason == "host_not_allowed"


def test_url_validator_enforces_max_length() -> None:
    long_url = "https://jobs.example.com/" + "a" * 2100
    failure = validate_application_url(long_url, {"jobs.example.com"})
    assert isinstance(failure, UrlValidationFailure)
    assert failure.reason == "too_long"


def test_broad_recall_keeps_synonym_but_exclusion_wins() -> None:
    assert title_matches_role_recall(
        "LLM Agent Engineer", ["AI应用开发"], ["agent"], []
    )
    assert not title_matches_role_recall(
        "Agent Engineer", ["AI应用开发"], ["agent"], ["agent"]
    )


def test_recall_desired_role_substring_matches() -> None:
    assert title_matches_role_recall(
        "AI应用开发工程师-应届", ["AI应用开发"], [], []
    )
    assert not title_matches_role_recall("销售经理", ["AI应用开发"], [], [])


def test_source_status_copy_is_closed_and_safe() -> None:
    text, guidance = source_status_copy(SourceStatusReason.CAPTCHA)
    assert "验证码" in text
    assert guidance
    # every enum member has fixed copy
    for member in SourceStatusReason:
        t, g = source_status_copy(member)
        assert t and g


def test_presentation_states_are_closed() -> None:
    assert RecommendationPresentationState.NEW == "new"
    assert RecommendationPresentationState.DISMISSED == "dismissed"
