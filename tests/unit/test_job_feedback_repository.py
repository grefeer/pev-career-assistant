from backend.app.repositories import job_feedback as repository


def test_repository_exposes_plan_contract() -> None:
    assert callable(repository.lock_verified_job)
    assert callable(repository.lock_user_feedback)
    assert callable(repository.lock_feedback)
    assert callable(repository.lock_actor_event)
    assert callable(repository.list_user_feedback)
    assert callable(repository.list_admin_feedback)
    assert callable(repository.aggregate_admin_feedback)
