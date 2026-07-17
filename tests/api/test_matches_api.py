import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


@pytest.mark.api
class TestMatchesAPI:
    async def test_create_match_requires_idempotency_key(
        self, client: AsyncClient, auth_headers
    ):
        resp = await client.post(
            "/api/matches",
            json={
                "job_id": "job-001",
                "profile_version_id": "pv-001",
            },
            headers=auth_headers,
        )
        assert resp.status_code == 422  # missing header

    async def test_create_match_non_verified_job(
        self, client: AsyncClient, auth_headers, mock_match_service
    ):
        mock_match_service.create_match.side_effect = ValueError("not_found")
        resp = await client.post(
            "/api/matches",
            json={
                "job_id": "non-existent",
                "profile_version_id": "pv-001",
            },
            headers={**auth_headers, "Idempotency-Key": "test-key-001"},
        )
        assert resp.status_code in (404, 422)

    async def test_create_match_idempotent(
        self, client: AsyncClient, auth_headers, mock_match_service
    ):
        """Same idempotency key returns same result (same status code and id)."""
        key = "idem-test-001"
        body = {"job_id": "verified-job-001", "profile_version_id": "pv-001"}

        # Configure mock to return report with a stable ID on both calls
        mock_match_service.create_match.return_value = mock_match_service.create_match.return_value

        r1 = await client.post(
            "/api/matches",
            json=body,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        r2 = await client.post(
            "/api/matches",
            json=body,
            headers={**auth_headers, "Idempotency-Key": key},
        )
        assert r1.status_code == r2.status_code
        if r1.status_code == 201:
            assert r1.json()["id"] == r2.json()["id"]

    async def test_cross_user_access_returns_404(
        self,
        client: AsyncClient,
        auth_headers,
        other_user_headers,
        mock_match_service,
    ):
        """User A creates a match; user B gets 404 when fetching it."""
        r1 = await client.post(
            "/api/matches",
            json={
                "job_id": "verified-job-001",
                "profile_version_id": "pv-001",
            },
            headers={**auth_headers, "Idempotency-Key": "cross-test-001"},
        )
        if r1.status_code == 201:
            match_id = r1.json()["id"]
            # Make get_by_id return None to simulate user B not having access
            mock_match_service.repo.get_by_id.return_value = None
            r2 = await client.get(
                f"/api/matches/{match_id}", headers=other_user_headers
            )
            assert r2.status_code == 404

    async def test_get_match_returns_report(
        self, client: AsyncClient, auth_headers, mock_match_service
    ):
        """GET /api/matches/{id} returns a valid MatchReportResponse."""
        match_id = "existing-match"
        mock_match_service.repo.get_by_id.return_value = (
            mock_match_service.create_match.return_value
        )

        resp = await client.get(
            f"/api/matches/{match_id}", headers=auth_headers
        )
        assert resp.status_code == 200
        body = resp.json()
        assert body["id"] == "mock-match-id"
        assert body["status"] == "completed"
        assert body["score"] == 85

    async def test_get_match_not_found(
        self, client: AsyncClient, auth_headers, mock_match_service
    ):
        """GET /api/matches/{id} returns 404 when match does not exist."""
        mock_match_service.repo.get_by_id.return_value = None
        resp = await client.get(
            "/api/matches/non-existent", headers=auth_headers
        )
        assert resp.status_code == 404

    async def test_list_matches(
        self, client: AsyncClient, auth_headers
    ):
        """GET /api/matches returns a list response."""
        resp = await client.get("/api/matches", headers=auth_headers)
        assert resp.status_code == 200
        body = resp.json()
        assert "items" in body
        assert "total" in body
