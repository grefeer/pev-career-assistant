"""API tests for application snapshot endpoints."""

from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from httpx import AsyncClient

from backend.app.api import dependencies as deps
from backend.app.db.models import ApplicationTaskStatus
from backend.app.services.applications import (
    InvalidTransitionError,
    StaleTaskVersionError,
    TaskNotFoundError,
)
from backend.app.services.snapshot_validators import SnapshotValidationError

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_SNAPSHOT_MODULE = "backend.app.api.routes.application_snapshots"
_SNAPSHOT_SERVICE = "backend.app.services.application_snapshot_service"


# ===================================================================
# POST /api/application-snapshots
# ===================================================================


@pytest.mark.api
class TestCreateSnapshot:
    """POST /api/application-snapshots"""

    async def test_requires_idempotency_key(
        self, client: AsyncClient, auth_headers
    ):
        """Missing Idempotency-Key header returns 422."""
        resp = await client.post(
            "/api/application-snapshots",
            json={
                "job_id": "job-001",
                "approved_resume_version_id": "arv-001",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_creates_snapshot_success(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Valid request returns 201 with a SnapshotResponse body."""

        def mock_create(
            db, user_id, job_id, approved_resume_version_id,
            dynamic_answers, local_sensitive_requirements, idempotency_key,
        ):
            from tests.api.conftest import _make_fake_snapshot
            return _make_fake_snapshot("new-snapshot-id")

        monkeypatch.setattr(f"{_SNAPSHOT_MODULE}.create_snapshot", mock_create)

        resp = await client.post(
            "/api/application-snapshots",
            json={
                "job_id": "job-001",
                "approved_resume_version_id": "arv-001",
            },
            headers={**auth_headers, "Idempotency-Key": "create-snap-key-001"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "new-snapshot-id"
        assert body["job_id"] == "job-001"
        assert body["gui_eligible"] is True
        assert body["company_name"] == "Test Corp"
        assert body["title"] == "Software Engineer"

    async def test_creates_snapshot_idempotent(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Same idempotency key returns same result on retry."""
        key = "idem-snap-001"

        def mock_create(
            db, user_id, job_id, approved_resume_version_id,
            dynamic_answers, local_sensitive_requirements, idempotency_key,
        ):
            from tests.api.conftest import _make_fake_snapshot
            return _make_fake_snapshot("stable-snap-id")

        monkeypatch.setattr(f"{_SNAPSHOT_MODULE}.create_snapshot", mock_create)

        body = {
            "job_id": "job-001",
            "approved_resume_version_id": "arv-001",
        }
        r1 = await client.post(
            "/api/application-snapshots",
            json=body,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        r2 = await client.post(
            "/api/application-snapshots",
            json=body,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        assert r1.status_code == r2.status_code
        if r1.status_code == 201:
            assert r1.json()["id"] == r2.json()["id"]

    async def test_create_snapshot_not_found(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Service raises ValueError('job_not_found') -> 404."""

        def mock_create(
            db, user_id, job_id, approved_resume_version_id,
            dynamic_answers, local_sensitive_requirements, idempotency_key,
        ):
            raise ValueError("job_not_found: job-999")

        monkeypatch.setattr(f"{_SNAPSHOT_MODULE}.create_snapshot", mock_create)

        resp = await client.post(
            "/api/application-snapshots",
            json={
                "job_id": "job-999",
                "approved_resume_version_id": "arv-001",
            },
            headers={**auth_headers, "Idempotency-Key": "nf-key-001"},
        )
        assert resp.status_code == 404

    async def test_create_snapshot_arv_not_found(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Service raises ValueError('approved_resume_version_not_found') -> 404."""

        def mock_create(
            db, user_id, job_id, approved_resume_version_id,
            dynamic_answers, local_sensitive_requirements, idempotency_key,
        ):
            raise ValueError("approved_resume_version_not_found")

        monkeypatch.setattr(f"{_SNAPSHOT_MODULE}.create_snapshot", mock_create)

        resp = await client.post(
            "/api/application-snapshots",
            json={
                "job_id": "job-001",
                "approved_resume_version_id": "non-existent-arv",
            },
            headers={**auth_headers, "Idempotency-Key": "arv-nf-key"},
        )
        assert resp.status_code == 404
        assert resp.json()["detail"]["code"] == "approved_resume_version_not_found"

    async def test_create_snapshot_validation_error(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """SnapshotValidationError -> 422."""

        def mock_create(
            db, user_id, job_id, approved_resume_version_id,
            dynamic_answers, local_sensitive_requirements, idempotency_key,
        ):
            raise SnapshotValidationError(
                "snapshot_validation_empty_profile_facts",
                "Profile facts must be non-empty",
            )

        monkeypatch.setattr(f"{_SNAPSHOT_MODULE}.create_snapshot", mock_create)

        resp = await client.post(
            "/api/application-snapshots",
            json={
                "job_id": "job-001",
                "approved_resume_version_id": "arv-001",
            },
            headers={**auth_headers, "Idempotency-Key": "val-key-001"},
        )
        assert resp.status_code == 422
        assert resp.json()["detail"]["code"] == "snapshot_validation_empty_profile_facts"


# ===================================================================
# GET /api/application-snapshots/{snapshot_id}
# ===================================================================


@pytest.mark.api
class TestGetSnapshot:
    """GET /api/application-snapshots/{snapshot_id}"""

    async def test_get_snapshot_not_found(
        self, client: AsyncClient, auth_headers
    ):
        """Returns 404 when snapshot does not exist."""
        resp = await client.get(
            "/api/application-snapshots/non-existent",
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_get_snapshot_returns_snapshot(
        self, client: AsyncClient, auth_headers, monkeypatch, app: FastAPI
    ):
        """Returns a valid SnapshotResponse using dependency_overrides."""
        from tests.api.conftest import _make_fake_snapshot

        fake = _make_fake_snapshot("existing-snap")

        # Create a mock db session that returns our fake snapshot
        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.first.return_value = fake

        def override_get_db():
            yield db_mock

        app.dependency_overrides[deps._get_db] = override_get_db
        try:
            resp = await client.get(
                "/api/application-snapshots/existing-snap",
                headers=auth_headers,
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "existing-snap"
        assert body["company_name"] == "Test Corp"
        assert body["title"] == "Software Engineer"
        assert body["gui_eligible"] is True
        assert body["job_status_at_snapshot"] == "verified"

    async def test_cross_user_access_returns_404(
        self,
        client: AsyncClient,
        auth_headers,
        other_user_headers,
        monkeypatch,
        app: FastAPI,
    ):
        """User A creates a snapshot; user B gets 404 when fetching it."""
        from tests.api.conftest import _make_fake_snapshot

        fake = _make_fake_snapshot("cross-user-snap")

        def _make_db(return_none: bool = False):
            db_mock = MagicMock()
            if return_none:
                db_mock.query.return_value.filter.return_value.first.return_value = None
            else:
                db_mock.query.return_value.filter.return_value.first.return_value = fake
            return db_mock

        # First call with auth_headers (simulates user A) - must return snapshot
        db_mock_a = MagicMock()
        db_mock_a.query.return_value.filter.return_value.first.return_value = fake

        def override_db_a():
            yield db_mock_a

        app.dependency_overrides[deps._get_db] = override_db_a
        try:
            resp_a = await client.get(
                "/api/application-snapshots/cross-user-snap",
                headers=auth_headers,
            )
        finally:
            app.dependency_overrides.clear()

        assert resp_a.status_code == 200

        # Second call with other_user_headers (simulates user B) - returns None
        db_mock_b = MagicMock()
        db_mock_b.query.return_value.filter.return_value.first.return_value = None

        def override_db_b():
            yield db_mock_b

        app.dependency_overrides[deps._get_db] = override_db_b
        try:
            resp_b = await client.get(
                "/api/application-snapshots/cross-user-snap",
                headers=other_user_headers,
            )
        finally:
            app.dependency_overrides.clear()

        assert resp_b.status_code == 404


# ===================================================================
# GET /api/application-snapshots
# ===================================================================


@pytest.mark.api
class TestListSnapshots:
    """GET /api/application-snapshots"""

    async def test_list_snapshots_success(
        self, client: AsyncClient, auth_headers, app: FastAPI
    ):
        """Returns a list of snapshots."""
        from tests.api.conftest import _make_fake_snapshot

        snap1 = _make_fake_snapshot("snap-1")
        snap2 = _make_fake_snapshot("snap-2")
        snap2.id = "snap-2"
        snap2.job_snapshot = {
            "company_name": "Another Corp",
            "title": "Data Scientist",
        }

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = [
            snap1,
            snap2,
        ]

        def override_get_db():
            yield db_mock

        app.dependency_overrides[deps._get_db] = override_get_db
        try:
            resp = await client.get(
                "/api/application-snapshots", headers=auth_headers
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 2
        assert body["items"][0]["id"] == "snap-1"
        assert body["items"][1]["id"] == "snap-2"
        assert body["items"][1]["company_name"] == "Another Corp"

    async def test_list_snapshots_empty(
        self, client: AsyncClient, auth_headers, app: FastAPI
    ):
        """Returns empty list when user has no snapshots."""

        db_mock = MagicMock()
        db_mock.query.return_value.filter.return_value.order_by.return_value.all.return_value = []

        def override_get_db():
            yield db_mock

        app.dependency_overrides[deps._get_db] = override_get_db
        try:
            resp = await client.get(
                "/api/application-snapshots", headers=auth_headers
            )
        finally:
            app.dependency_overrides.clear()

        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0


# ===================================================================
# POST /api/application-snapshots/{snapshot_id}/create-task
# ===================================================================


@pytest.mark.api
class TestCreateTask:
    """POST /api/application-snapshots/{snapshot_id}/create-task"""

    async def test_requires_idempotency_key(
        self, client: AsyncClient, auth_headers
    ):
        """Missing Idempotency-Key header returns 422."""
        resp = await client.post(
            "/api/application-snapshots/snap-001/create-task",
            json={},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_create_task_success(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Valid request returns 201 with task info."""

        def mock_create_task(
            db, user_id, snapshot_id, idempotency_key, device_id=None,
        ):
            from tests.api.conftest import _make_fake_task
            return _make_fake_task("new-task-id")

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.create_application_task",
            mock_create_task,
        )

        resp = await client.post(
            "/api/application-snapshots/snap-001/create-task",
            json={},
            headers={**auth_headers, "Idempotency-Key": "create-task-key-001"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["task_id"] == "new-task-id"
        assert body["snapshot_id"] == "snap-001"
        assert body["status"] == "created"

    async def test_create_task_with_device(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Can optionally specify device_id."""

        def mock_create_task(
            db, user_id, snapshot_id, idempotency_key, device_id=None,
        ):
            from tests.api.conftest import _make_fake_task
            task = _make_fake_task("task-with-device")
            task.device_id = device_id
            return task

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.create_application_task",
            mock_create_task,
        )

        resp = await client.post(
            "/api/application-snapshots/snap-001/create-task",
            json={"device_id": "device-001"},
            headers={**auth_headers, "Idempotency-Key": "device-task-key"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["task_id"] == "task-with-device"

    async def test_create_task_snapshot_not_found(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Service raises ValueError('snapshot_not_found') -> 404."""

        def mock_create_task(
            db, user_id, snapshot_id, idempotency_key, device_id=None,
        ):
            raise ValueError("snapshot_not_found")

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.create_application_task",
            mock_create_task,
        )

        resp = await client.post(
            "/api/application-snapshots/non-existent/create-task",
            json={},
            headers={**auth_headers, "Idempotency-Key": "nf-task-key"},
        )
        assert resp.status_code == 404

    async def test_create_task_not_eligible(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Service raises ValueError('task_not_eligible: ...') -> 422."""

        def mock_create_task(
            db, user_id, snapshot_id, idempotency_key, device_id=None,
        ):
            raise ValueError("task_not_eligible: snapshot_gui_not_eligible")

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.create_application_task",
            mock_create_task,
        )

        resp = await client.post(
            "/api/application-snapshots/snap-001/create-task",
            json={},
            headers={**auth_headers, "Idempotency-Key": "nelig-task-key"},
        )
        assert resp.status_code == 422


# ===================================================================
# GET /api/application-snapshots/{snapshot_id}/task-eligibility
# ===================================================================


@pytest.mark.api
class TestTaskEligibility:
    """GET /api/application-snapshots/{snapshot_id}/task-eligibility"""

    async def test_eligibility_allowed(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Returns can_create_task=True when all gates pass."""

        def mock_check(db, user_id, snapshot_id):
            return True, None

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.check_task_eligibility",
            mock_check,
        )

        resp = await client.get(
            "/api/application-snapshots/snap-001/task-eligibility",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_create_task"] is True
        assert body["reason_code"] is None

    async def test_eligibility_denied(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Returns can_create_task=False with reason when gates fail."""

        def mock_check(db, user_id, snapshot_id):
            return False, "snapshot_gui_not_eligible"

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.check_task_eligibility",
            mock_check,
        )

        resp = await client.get(
            "/api/application-snapshots/snap-001/task-eligibility",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["can_create_task"] is False
        assert body["reason_code"] == "snapshot_gui_not_eligible"


# ===================================================================
# POST /api/application-tasks/{task_id}/dispatch
# ===================================================================


@pytest.mark.api
class TestDispatchTask:
    """POST /api/application-tasks/{task_id}/dispatch"""

    async def test_dispatch_success(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Valid dispatch returns 200 with DispatchTaskResponse."""
        from tests.api.conftest import _make_fake_task

        def mock_dispatch(
            db, user_id, task_id, device_id, expected_version,
        ):
            task = _make_fake_task("dispatch-task-id")
            task.status = ApplicationTaskStatus.DISPATCHED
            task.state_version = 2
            task.device_id = device_id
            return task

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.assign_and_dispatch_task",
            mock_dispatch,
        )

        resp = await client.post(
            "/api/application-tasks/dispatch-task-id/dispatch",
            json={"device_id": "device-001", "expected_version": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["task_id"] == "dispatch-task-id"
        assert body["status"] == "dispatched"
        assert body["state_version"] == 2

    async def test_dispatch_task_not_found(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """TaskNotFoundError -> 404."""

        def mock_dispatch(
            db, user_id, task_id, device_id, expected_version,
        ):
            raise TaskNotFoundError(task_id)

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.assign_and_dispatch_task",
            mock_dispatch,
        )

        resp = await client.post(
            "/api/application-tasks/non-existent/dispatch",
            json={"device_id": "device-001", "expected_version": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_dispatch_stale_version(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """StaleTaskVersionError -> 409."""

        def mock_dispatch(
            db, user_id, task_id, device_id, expected_version,
        ):
            raise StaleTaskVersionError(task_id)

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.assign_and_dispatch_task",
            mock_dispatch,
        )

        resp = await client.post(
            "/api/application-tasks/task-001/dispatch",
            json={"device_id": "device-001", "expected_version": 999},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    async def test_dispatch_invalid_transition(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """InvalidTransitionError -> 409."""

        def mock_dispatch(
            db, user_id, task_id, device_id, expected_version,
        ):
            raise InvalidTransitionError("cannot transition from dispatched")

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.assign_and_dispatch_task",
            mock_dispatch,
        )

        resp = await client.post(
            "/api/application-tasks/task-001/dispatch",
            json={"device_id": "device-001", "expected_version": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    async def test_dispatch_device_error(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """ValueError('device_not_found') -> 422."""

        def mock_dispatch(
            db, user_id, task_id, device_id, expected_version,
        ):
            raise ValueError("device_not_found")

        monkeypatch.setattr(
            f"{_SNAPSHOT_MODULE}.assign_and_dispatch_task",
            mock_dispatch,
        )

        resp = await client.post(
            "/api/application-tasks/task-001/dispatch",
            json={"device_id": "non-existent-device", "expected_version": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 422
        body = resp.json()
        assert body["detail"]["code"] == "device_not_found"
