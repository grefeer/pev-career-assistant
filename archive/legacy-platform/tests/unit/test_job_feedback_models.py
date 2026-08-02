from backend.app.db.models import JobFeedback, JobFeedbackEvent


def _unique_columns(model: object) -> set[tuple[str, ...]]:
    return {
        tuple(column.name for column in constraint.columns)
        for constraint in model.__table__.constraints
        if constraint.__class__.__name__ == "UniqueConstraint"
    }


def test_feedback_aggregate_columns_and_unique_key() -> None:
    assert set(JobFeedback.__table__.columns.keys()) >= {
        "id", "user_id", "job_id", "category", "status", "note",
        "version", "created_at", "updated_at",
    }
    assert ("user_id", "job_id", "category") in _unique_columns(JobFeedback)


def test_feedback_event_is_append_only_idempotency_record() -> None:
    assert set(JobFeedbackEvent.__table__.columns.keys()) >= {
        "id", "feedback_id", "actor_user_id", "action", "from_status",
        "to_status", "feedback_version", "redacted_snapshot",
        "idempotency_key", "created_at",
    }
    assert ("actor_user_id", "idempotency_key") in _unique_columns(JobFeedbackEvent)
