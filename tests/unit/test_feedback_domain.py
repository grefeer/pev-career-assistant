from __future__ import annotations

from backend.app.domain.feedbacks import FEEDBACK_CATEGORIES, JobFeedbackCategory


class TestJobFeedbackCategory:
    def test_four_stable_categories(self) -> None:
        expected = {
            "closed",
            "application_channel_unavailable",
            "content_changed",
            "incorrect_information",
        }
        actual = {item.value for item in JobFeedbackCategory}
        assert actual == expected

    def test_feedback_categories_frozenset_matches_enum(self) -> None:
        assert FEEDBACK_CATEGORIES == {item.value for item in JobFeedbackCategory}

    def test_category_values_are_stable(self) -> None:
        assert JobFeedbackCategory.CLOSED.value == "closed"
        assert JobFeedbackCategory.APPLICATION_CHANNEL_UNAVAILABLE.value == "application_channel_unavailable"
        assert JobFeedbackCategory.CONTENT_CHANGED.value == "content_changed"
        assert JobFeedbackCategory.INCORRECT_INFORMATION.value == "incorrect_information"
