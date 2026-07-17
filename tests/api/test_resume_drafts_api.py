"""API tests for resume draft endpoints."""

from unittest.mock import MagicMock

import pytest
from httpx import AsyncClient

from backend.app.repositories.drafts import StaleDraftVersionError

pytestmark = pytest.mark.asyncio


@pytest.mark.api
class TestCreateDraft:
    """POST /api/resume-drafts"""

    async def test_requires_idempotency_key(
        self, client: AsyncClient, auth_headers
    ):
        """Missing Idempotency-Key header returns 422."""
        resp = await client.post(
            "/api/resume-drafts",
            json={"match_report_id": "report-001"},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_creates_draft_success(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Valid request returns 201 with a ResumeDraftResponse body."""
        mock_draft_service.create_draft.return_value = MagicMock(
            id="new-draft-id",
            match_report_id="report-001",
            target_job_id="job-001",
            diffs=None,
            status="generating",
            error_code=None,
            state_version=0,
            created_at="2025-01-01 00:00:00+00:00",
            approved_at=None,
        )
        resp = await client.post(
            "/api/resume-drafts",
            json={"match_report_id": "report-001"},
            headers={**auth_headers, "Idempotency-Key": "create-key-001"},
        )
        assert resp.status_code == 201
        body = resp.json()
        assert body["id"] == "new-draft-id"
        assert body["match_report_id"] == "report-001"
        assert body["status"] == "generating"
        assert "diffs" not in body or body["diffs"] is None

    async def test_creates_draft_idempotent(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Same idempotency key returns same result on retry."""
        key = "idem-create-001"
        body = {"match_report_id": "report-001"}
        draft = MagicMock(
            id="stable-draft-id",
            match_report_id="report-001",
            target_job_id="job-001",
            diffs=None,
            status="draft",
            error_code=None,
            state_version=1,
            created_at="2025-01-01 00:00:00+00:00",
            approved_at=None,
        )
        mock_draft_service.create_draft.return_value = draft

        r1 = await client.post(
            "/api/resume-drafts",
            json=body,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        r2 = await client.post(
            "/api/resume-drafts",
            json=body,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        assert r1.status_code == r2.status_code
        if r1.status_code == 201:
            assert r1.json()["id"] == r2.json()["id"]

    async def test_create_draft_not_found(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('not_found') -> 404."""
        mock_draft_service.create_draft.side_effect = ValueError("not_found")
        resp = await client.post(
            "/api/resume-drafts",
            json={"match_report_id": "non-existent"},
            headers={**auth_headers, "Idempotency-Key": "nf-key-001"},
        )
        assert resp.status_code == 404

    async def test_create_draft_match_not_completed(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('draft_match_not_completed') -> 422."""
        mock_draft_service.create_draft.side_effect = ValueError(
            "draft_match_not_completed"
        )
        resp = await client.post(
            "/api/resume-drafts",
            json={"match_report_id": "pending-report"},
            headers={**auth_headers, "Idempotency-Key": "nc-key-001"},
        )
        assert resp.status_code == 422

    async def test_create_draft_idempotency_conflict(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('idempotency_key_conflict') -> 409."""
        mock_draft_service.create_draft.side_effect = ValueError(
            "idempotency_key_conflict"
        )
        resp = await client.post(
            "/api/resume-drafts",
            json={"match_report_id": "report-001"},
            headers={**auth_headers, "Idempotency-Key": "conflict-key-001"},
        )
        assert resp.status_code == 409


@pytest.mark.api
class TestGetDraft:
    """GET /api/resume-drafts/{draft_id}"""

    async def test_get_draft_not_found(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Returns 404 when draft does not exist."""
        mock_draft_service.repo.get_by_id.return_value = None
        resp = await client.get(
            "/api/resume-drafts/non-existent", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_get_draft_returns_draft(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Returns a valid ResumeDraftResponse."""
        mock_draft_service.repo.get_by_id.return_value = MagicMock(
            id="existing-draft",
            match_report_id="report-001",
            target_job_id="job-001",
            diffs=[
                {
                    "op": "rephrase",
                    "section": "skills",
                    "before": "Python",
                    "after": "Python (advanced)",
                    "fact_ref": "abc123",
                    "evidence_ids": ["ev1"],
                },
            ],
            status="draft",
            error_code=None,
            state_version=2,
            created_at="2025-01-01 00:00:00+00:00",
            approved_at=None,
        )
        resp = await client.get(
            "/api/resume-drafts/existing-draft", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "existing-draft"
        assert body["status"] == "draft"
        assert body["state_version"] == 2
        assert body["diffs"] is not None
        assert len(body["diffs"]) == 1
        assert body["diffs"][0]["op"] == "rephrase"


@pytest.mark.api
class TestListDrafts:
    """GET /api/resume-drafts"""

    async def test_list_drafts_success(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Returns a list of drafts."""
        draft = MagicMock(
            id="draft-1",
            match_report_id="report-001",
            target_job_id="job-001",
            diffs=None,
            status="draft",
            error_code=None,
            state_version=1,
            created_at="2025-01-01 00:00:00+00:00",
            approved_at=None,
        )
        draft2 = MagicMock(
            id="draft-2",
            match_report_id="report-002",
            target_job_id="job-002",
            diffs=None,
            status="approved",
            error_code=None,
            state_version=2,
            created_at="2025-01-02 00:00:00+00:00",
            approved_at="2025-01-03 00:00:00+00:00",
        )
        mock_draft_service.repo.list_by_user.return_value = [draft, draft2]

        resp = await client.get("/api/resume-drafts", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
        assert body["total"] == 2
        assert body["items"][0]["id"] == "draft-1"
        assert body["items"][1]["id"] == "draft-2"

    async def test_list_drafts_empty(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Returns empty list when user has no drafts."""
        mock_draft_service.repo.list_by_user.return_value = []
        resp = await client.get("/api/resume-drafts", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert body["items"] == []
        assert body["total"] == 0


@pytest.mark.api
class TestApproveDraft:
    """POST /api/resume-drafts/{draft_id}/approve"""

    async def test_requires_idempotency_key(
        self, client: AsyncClient, auth_headers
    ):
        """Missing Idempotency-Key header returns 422."""
        resp = await client.post(
            "/api/resume-drafts/draft-001/approve",
            json={"expected_version": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 422

    async def test_approve_success(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Valid approve request returns 200 with ApprovedResumeVersionResponse."""
        mock_draft_service.approve_draft.return_value = MagicMock(
            id="arv-001",
            draft_id="draft-001",
            approved_at="2025-01-01 00:00:00+00:00",
        )
        resp = await client.post(
            "/api/resume-drafts/draft-001/approve",
            json={"expected_version": 1},
            headers={**auth_headers, "Idempotency-Key": "approve-key-001"},
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "arv-001"
        assert body["draft_id"] == "draft-001"
        assert "attachments" in body

    async def test_approve_not_found(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('not_found') -> 404."""
        mock_draft_service.approve_draft.side_effect = ValueError("not_found")
        resp = await client.post(
            "/api/resume-drafts/non-existent/approve",
            json={"expected_version": 1},
            headers={**auth_headers, "Idempotency-Key": "nf-approve-key"},
        )
        assert resp.status_code == 404

    async def test_approve_stale_version(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises StaleDraftVersionError -> 409."""
        mock_draft_service.approve_draft.side_effect = StaleDraftVersionError(
            "draft-001"
        )
        resp = await client.post(
            "/api/resume-drafts/draft-001/approve",
            json={"expected_version": 0},
            headers={**auth_headers, "Idempotency-Key": "stale-approve-key"},
        )
        assert resp.status_code == 409

    async def test_approve_invalid_state(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('draft_cannot_approve_status_generating') -> 422."""
        mock_draft_service.approve_draft.side_effect = ValueError(
            "draft_cannot_approve_status_generating"
        )
        resp = await client.post(
            "/api/resume-drafts/draft-001/approve",
            json={"expected_version": 1},
            headers={**auth_headers, "Idempotency-Key": "invalid-state-key"},
        )
        assert resp.status_code == 422


@pytest.mark.api
class TestRejectDraft:
    """POST /api/resume-drafts/{draft_id}/reject"""

    async def test_reject_success(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Valid reject request returns 200 with ResumeDraftResponse."""
        mock_draft_service.reject_draft.return_value = MagicMock(
            id="draft-001",
            match_report_id="report-001",
            target_job_id="job-001",
            diffs=None,
            status="rejected",
            error_code=None,
            state_version=2,
            created_at="2025-01-01 00:00:00+00:00",
            approved_at=None,
        )
        resp = await client.post(
            "/api/resume-drafts/draft-001/reject",
            json={"expected_version": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "draft-001"
        assert body["status"] == "rejected"
        assert body["state_version"] == 2

    async def test_reject_not_found(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('not_found') -> 404."""
        mock_draft_service.reject_draft.side_effect = ValueError("not_found")
        resp = await client.post(
            "/api/resume-drafts/non-existent/reject",
            json={"expected_version": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 404

    async def test_reject_stale_version(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises StaleDraftVersionError -> 409."""
        mock_draft_service.reject_draft.side_effect = StaleDraftVersionError(
            "draft-001"
        )
        resp = await client.post(
            "/api/resume-drafts/draft-001/reject",
            json={"expected_version": 0},
            headers=auth_headers,
        )
        assert resp.status_code == 409

    async def test_reject_invalid_state(
        self, client: AsyncClient, auth_headers, mock_draft_service
    ):
        """Service raises ValueError('draft_cannot_reject_status_approved') -> 422."""
        mock_draft_service.reject_draft.side_effect = ValueError(
            "draft_cannot_reject_status_approved"
        )
        resp = await client.post(
            "/api/resume-drafts/draft-001/reject",
            json={"expected_version": 1},
            headers=auth_headers,
        )
        assert resp.status_code == 422


@pytest.mark.api
class TestDownloadAttachment:
    """GET /api/approved-resume-attachments/{attachment_id}/download"""

    async def test_download_not_found(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """FileNotFoundError from download_attachment -> 404."""
        import backend.app.api.routes.resume_drafts as routes_mod

        original = routes_mod.download_attachment

        def mock_download(db, attachment_id, user_id, object_store):
            raise FileNotFoundError("not found")

        monkeypatch.setattr(
            routes_mod, "download_attachment", mock_download
        )
        resp = await client.get(
            "/api/approved-resume-attachments/missing-attachment/download",
            headers=auth_headers,
        )
        assert resp.status_code == 404

        # Restore
        monkeypatch.setattr(routes_mod, "download_attachment", original)

    async def test_download_forbidden(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """PermissionError from download_attachment -> 403."""
        import backend.app.api.routes.resume_drafts as routes_mod

        original = routes_mod.download_attachment

        def mock_download(db, attachment_id, user_id, object_store):
            raise PermissionError("forbidden")

        monkeypatch.setattr(
            routes_mod, "download_attachment", mock_download
        )
        resp = await client.get(
            "/api/approved-resume-attachments/others-attachment/download",
            headers=auth_headers,
        )
        assert resp.status_code == 403

        monkeypatch.setattr(routes_mod, "download_attachment", original)

    async def test_download_success(
        self, client: AsyncClient, auth_headers, monkeypatch
    ):
        """Successful download returns file content with correct headers."""
        import backend.app.api.routes.resume_drafts as routes_mod

        original = routes_mod.download_attachment

        def mock_download(db, attachment_id, user_id, object_store):
            return (
                b"%PDF-1.4 mock pdf content",
                "application/pdf",
                "resume.pdf",
            )

        monkeypatch.setattr(
            routes_mod, "download_attachment", mock_download
        )
        resp = await client.get(
            "/api/approved-resume-attachments/valid-attachment/download",
            headers=auth_headers,
        )
        assert resp.status_code == 200
        assert resp.headers["content-type"] == "application/pdf"
        assert (
            'attachment; filename="resume.pdf"'
            in resp.headers["content-disposition"]
        )
        assert resp.content == b"%PDF-1.4 mock pdf content"

        monkeypatch.setattr(routes_mod, "download_attachment", original)
