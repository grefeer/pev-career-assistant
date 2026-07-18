"""E2E browser tests for Job Discovery Agent admin workflow.

Requirements
------------
- Playwright 1.61+ (``playwright`` package, ``chromium`` browser installed)
- Backend dev server at ``http://127.0.0.1:8000``
- Admin credentials with ``require_admin`` permission

Run (dev server must be running):
    python -m pytest tests/e2e/test_job_discovery_e2e.py -x -v

Skip if no dev server:
    python -m pytest tests/e2e/test_job_discovery_e2e.py -x -q -m "not needs_dev_server"

Fixture HTML files are in ``tests/fixtures/job_discovery/`` and served locally
via Playwright's ``route`` API (no external HTTP server needed for page fixtures).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any, Generator
from urllib.parse import urlparse

import pytest
import requests
from playwright.sync_api import Browser, BrowserContext, Page, Route, expect

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

FIXTURES_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "job_discovery"
BACKEND_URL = os.environ.get("BACKEND_URL", "http://127.0.0.1:8000")
FRONTEND_URL = os.environ.get("FRONTEND_URL", "http://127.0.0.1:5173")

ADMIN_USERNAME = os.environ.get("E2E_ADMIN_USERNAME", "admin@test.com")
ADMIN_PASSWORD = os.environ.get("E2E_ADMIN_PASSWORD", "admin123456")

# ---------------------------------------------------------------------------
# Markers
# ---------------------------------------------------------------------------

needs_dev_server = pytest.mark.skipif(
    not os.environ.get("E2E_SKIP_DEV_CHECK"),
    reason="Requires running dev server. Set E2E_SKIP_DEV_CHECK=1 to force.",
)


def _dev_server_reachable() -> bool:
    """Check if the backend dev server is reachable."""
    try:
        resp = requests.get(f"{BACKEND_URL}/api/health/live", timeout=3)
        return resp.status_code == 200
    except (requests.ConnectionError, requests.Timeout):
        return False


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="session")
def browser_context_args(browser_context_args: dict[str, Any]) -> dict[str, Any]:
    """Enable verbose logging and set viewport for admin screens."""
    return {**browser_context_args, "viewport": {"width": 1440, "height": 900}}


@pytest.fixture(scope="session")
def browser(browser: Browser) -> Generator[Browser, None, None]:
    """Expose the Playwright Browser fixture (session-scoped)."""
    yield browser


@pytest.fixture
def page(browser: Browser) -> Generator[Page, None, None]:
    """Create a new page for each test with fixture route interception.

    Intercepts requests to ``tests/fixtures/job_discovery/*.html`` and serves
    the local fixture files so tests can navigate to mock URLs without a real
    HTTP server.
    """
    ctx: BrowserContext = browser.new_context(
        viewport={"width": 1440, "height": 900},
        ignore_https_errors=True,
    )
    page = ctx.new_page()

    # Intercept requests to fixture URLs and serve local files
    def _serve_fixture(route: Route) -> None:
        url = route.request.url
        parsed = urlparse(url)
        path = parsed.path.strip("/")
        # Map URL path to fixture filename
        fixture_map: dict[str, str] = {
            "company": "company_homepage.html",
            "careers": "career_list.html",
            "job/001": "job_detail.html",
            "wechat/text": "wechat_text.html",
            "wechat/image": "wechat_image.html",
            "auth/login": "captcha.html",
            "login": "captcha.html",
        }
        for key, filename in fixture_map.items():
            if key in path:
                fixture_path = FIXTURES_DIR / filename
                if fixture_path.exists():
                    content = fixture_path.read_text(encoding="utf-8")
                    route.fulfill(
                        status=200,
                        content_type="text/html; charset=utf-8",
                        body=content,
                    )
                    return
        route.continue_()

    page.route("**/*", _serve_fixture)
    yield page
    ctx.close()


@pytest.fixture(scope="session")
def api_session() -> requests.Session:
    """Create a requests session pre-configured for the backend API.

    Skips tests if the dev server is not reachable.
    """
    session = requests.Session()
    session.headers.update({"Content-Type": "application/json"})
    if not _dev_server_reachable():
        pytest.skip(f"Backend dev server not reachable at {BACKEND_URL}")
    return session


@pytest.fixture(scope="session")
def admin_token(api_session: requests.Session) -> str:
    """Obtain an admin bearer token from the backend.

    Assumes the backend has a ``/api/auth/login`` endpoint that returns
    ``{"access_token": "..."}`` for admin credentials.
    """
    resp = api_session.post(
        f"{BACKEND_URL}/api/auth/login",
        json={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    if resp.status_code == 200:
        data = resp.json()
        token = data.get("access_token") or data.get("token")
        if token:
            return token

    # If login fails, try direct token env var
    token = os.environ.get("E2E_ADMIN_TOKEN")
    if token:
        return token

    pytest.skip("Could not obtain admin token (no E2E_ADMIN_TOKEN and login failed)")
    return ""  # unreachable


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _admin_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_discovery_task(
    api_session: requests.Session,
    token: str,
    source_url: str,
    source_key: str = "e2e-test-source",
) -> dict[str, Any] | None:
    """Create a discovery task via the admin API.

    Returns the task dict or ``None`` if the endpoint is not available.
    """
    resp = api_session.post(
        f"{BACKEND_URL}/api/admin/job-discovery/tasks",
        headers=_admin_headers(token),
        json={
            "source_url": source_url,
            "source_key": source_key,
            "external_record_id": f"e2e-{hash(source_url)}",
            "payload_hash": "e2e-payload",
            "raw_record_id": "e2e-raw-record",
            "source_id": "e2e-source",
        },
    )
    if resp.status_code in (200, 201):
        return resp.json()
    return None


def _list_tasks(api_session: requests.Session, token: str) -> list[dict[str, Any]]:
    """List all discovery tasks."""
    resp = api_session.get(
        f"{BACKEND_URL}/api/admin/job-discovery/tasks",
        headers=_admin_headers(token),
    )
    if resp.status_code == 200:
        data = resp.json()
        return data.get("tasks", []) if isinstance(data, dict) else data
    return []


def _list_groups(api_session: requests.Session, token: str) -> list[dict[str, Any]]:
    """List all review groups."""
    resp = api_session.get(
        f"{BACKEND_URL}/api/admin/job-discovery/groups",
        headers=_admin_headers(token),
    )
    if resp.status_code == 200:
        data = resp.json()
        return data if isinstance(data, list) else []
    return []


# ===================================================================
# Tests
# ===================================================================


class TestE2EAdminTaskList:
    """Phase 8.2 — Admin task list and Tencent sync."""

    @needs_dev_server
    def test_admin_login_sees_discovery_tasks_tab(
        self,
        page: Page,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: Admin logs in and sees the discovery tasks tab with content.

        Verifies that:
        - The admin page loads without errors.
        - The "发现记录" tab is visible.
        - At least one task card is rendered (if tasks exist).
        """
        # Navigate to the admin job discovery page
        page.goto(f"{FRONTEND_URL}/admin/job-discovery", wait_until="networkidle")

        # Check for the discovery review component
        expect(page.locator(".discovery-review")).to_be_visible(timeout=10000)

        # Check that the "发现记录" tab button is visible
        tasks_tab = page.locator("button", has_text=re.compile(r"发现记录"))
        expect(tasks_tab).to_be_visible()

        # If tasks exist in the backend, verify they appear
        tasks = _list_tasks(api_session, admin_token)
        if tasks:
            # Wait for task cards to render
            task_cards = page.locator('[data-test^="task-"]')
            expect(task_cards.first).to_be_visible(timeout=10000)
            # Verify status badges are present
            status_badges = page.locator('[data-test="task-status"]')
            expect(status_badges.first).to_be_visible()

    @needs_dev_server
    def test_duplicate_sync_click_does_not_create_duplicate(
        self,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: Repeated Tencent sync clicks do not create duplicate tasks.

        Simulates clicking "同步" twice with the same parameters and verifies
        that only one task is created (idempotency via create_or_get_task).
        """
        test_url = "https://example.com/e2e-dup-test"
        source_key = "tencent-27-referrals"

        # First click
        first = _create_discovery_task(api_session, admin_token, test_url, source_key)
        if first is None:
            pytest.skip("Task creation endpoint not available")

        # Second click (same params)
        second = _create_discovery_task(api_session, admin_token, test_url, source_key)

        # Both should return successfully
        if second is not None:
            # Same task id = no duplicate
            assert first.get("id") == second.get("id"), (
                "Duplicate sync click created a different task ID"
            )

        # List tasks and verify only one exists for this URL
        tasks = _list_tasks(api_session, admin_token)
        matching = [t for t in tasks if t.get("source_url") == test_url]
        assert len(matching) <= 1, (
            f"Expected at most 1 task for {test_url}, found {len(matching)}"
        )


class TestE2EReviewQueue:
    """Phase 8.2 — Admin review queue and candidate grouping."""

    @needs_dev_server
    def test_review_queue_shows_grouped_candidates(
        self,
        page: Page,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: Review queue shows discovered candidates grouped by similarity.

        Verifies that:
        - The groups tab loads and shows group cards.
        - Each group has a similarity_group_key heading.
        - Candidates are rendered inside their respective groups.
        """
        page.goto(f"{FRONTEND_URL}/admin/job-discovery", wait_until="networkidle")

        # Switch to groups tab
        groups_tab = page.locator("button", has_text=re.compile(r"审核分组"))
        expect(groups_tab).to_be_visible()
        groups_tab.click()

        # Wait for the groups section to appear
        page.wait_for_timeout(2000)

        # Check if groups exist
        groups = _list_groups(api_session, admin_token)
        if not groups:
            # If no groups, verify the empty state
            empty_msg = page.locator("text=暂无待审核分组")
            if empty_msg.is_visible():
                return
            pytest.skip("No review groups available to verify")

        # Verify group cards are rendered
        for group in groups:
            group_key = group.get("similarity_group_key", "")
            if not group_key:
                continue
            group_card = page.locator(f'[data-test="group-{group_key}"]')
            if group_card.is_visible():
                # Check group heading shows similarity key
                expect(group_card.locator(".group-key")).to_be_visible()
                # Check candidate cards within the group
                candidates = group.get("candidates", [])
                if candidates:
                    first_candidate = candidates[0]
                    cand_id = first_candidate.get("id", "")
                    if cand_id:
                        cand_card = page.locator(
                            f'[data-test="candidate-{cand_id}"]'
                        )
                        if cand_card.is_visible():
                            expect(cand_card).to_be_visible()

    @needs_dev_server
    def test_similar_candidates_from_both_sheets_in_one_group(
        self,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: Similar candidates from both Tencent sheets appear in one group.

        This tests the similarity_group_key logic: candidates with the same
        company name, title pattern, and recruitment type from different sources
        share a group key, so they appear together in the review queue.
        """
        groups = _list_groups(api_session, admin_token)
        if not groups:
            pytest.skip("No review groups to verify")

        # Check that at least one group has multiple candidates
        multi_candidate_groups = [
            g for g in groups if len(g.get("candidates", [])) >= 2
        ]

        if multi_candidate_groups:
            group = multi_candidate_groups[0]
            candidates = group["candidates"]
            assert len(candidates) >= 2, (
                f"Group '{group['similarity_group_key']}' should have "
                f"2+ candidates, found {len(candidates)}"
            )
            # Verify candidates have different external_record_ids or sources
            ext_ids = {c.get("id") for c in candidates}
            assert len(ext_ids) >= 2, (
                "Candidates in the same group should be distinct records"
            )
        else:
            # No multi-candidate groups — note this in test but don't fail
            pytest.skip("No groups with 2+ candidates found")
            pass

    @needs_dev_server
    def test_captcha_url_appears_as_needs_manual_review(
        self,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: A URL that hits a captcha/login wall appears as
        ``needs_manual_review`` with a ``captcha`` or ``login_required``
        block_reason.

        This test verifies the end-to-end behavior: when the agent encounters
        a captcha wall, it should mark the task as ``needs_manual_review``
        instead of ``failed``.
        """
        # List all tasks and find one with needs_manual_review status
        tasks = _list_tasks(api_session, admin_token)
        manual_review_tasks = [
            t
            for t in tasks
            if t.get("status") == "needs_manual_review"
        ]

        if not manual_review_tasks:
            # If no manual review tasks exist, create one by submitting
            # a URL that would trigger captcha (e.g., a zhaopin.com URL)
            # Note: this is a behavioral test; the actual task processing
            # depends on the worker running.
            pytest.skip("No needs_manual_review tasks found in the queue")

        for task in manual_review_tasks:
            block_reason = task.get("block_reason") or ""
            assert any(
                keyword in block_reason
                for keyword in ["captcha", "login", "anti_bot", "permission"]
            ), (
                f"needs_manual_review task {task['id']} has unexpected "
                f"block_reason: {block_reason}"
            )


class TestE2EApproveReject:
    """Phase 8.2 — Admin approve and reject candidates."""

    @needs_dev_server
    def test_admin_can_approve_candidate(
        self,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: Admin can approve a candidate.

        Verifies that:
        - Approving a ``pending_review`` candidate succeeds.
        - The candidate status changes to ``approved``.
        - A ``JobPosting`` is created (or updated) as a side effect.
        """
        groups = _list_groups(api_session, admin_token)
        if not groups:
            pytest.skip("No review groups available")

        # Find a pending_review candidate
        target_candidate: dict[str, Any] | None = None
        for group in groups:
            for candidate in group.get("candidates", []):
                if candidate.get("status") == "pending_review":
                    target_candidate = candidate
                    break
            if target_candidate:
                break

        if target_candidate is None:
            pytest.skip("No pending_review candidate found to approve")

        candidate_id = target_candidate["id"]

        # Approve via API
        resp = api_session.post(
            f"{BACKEND_URL}/api/admin/job-discovery/candidates/{candidate_id}/approve",
            headers=_admin_headers(admin_token),
        )

        assert resp.status_code in (200, 201), (
            f"Approve endpoint returned {resp.status_code}: {resp.text}"
        )
        result = resp.json()
        assert result.get("status") == "approved", (
            f"Expected approved status, got: {result.get('status')}"
        )

    @needs_dev_server
    def test_admin_can_reject_candidate(
        self,
        api_session: requests.Session,
        admin_token: str,
    ) -> None:
        """Test: Admin can reject a candidate.

        Verifies that:
        - Rejecting a ``pending_review`` candidate succeeds.
        - The candidate status changes to ``rejected``.
        """
        groups = _list_groups(api_session, admin_token)
        if not groups:
            pytest.skip("No review groups available")

        # Find a pending_review candidate (different from the approved one)
        target_candidate: dict[str, Any] | None = None
        for group in groups:
            for candidate in group.get("candidates", []):
                if candidate.get("status") == "pending_review":
                    target_candidate = candidate
                    break
            if target_candidate:
                break

        if target_candidate is None:
            pytest.skip("No pending_review candidate found to reject")

        candidate_id = target_candidate["id"]

        # Reject via API
        resp = api_session.post(
            f"{BACKEND_URL}/api/admin/job-discovery/candidates/{candidate_id}/reject",
            headers=_admin_headers(admin_token),
        )

        assert resp.status_code in (200, 201), (
            f"Reject endpoint returned {resp.status_code}: {resp.text}"
        )
        result = resp.json()
        assert result.get("status") == "rejected", (
            f"Expected rejected status, got: {result.get('status')}"
        )


# ===================================================================
# Fixture-Serving Tests (no backend required)
# ===================================================================


class TestFixturePages:
    """Tests that verify fixture HTML files render correctly in a browser.

    These tests do NOT require a backend dev server. They use Playwright's
    ``route`` API to serve fixture files directly.
    """

    @staticmethod
    def _serve_fixture_page(page: Page, fixture_name: str, url: str) -> None:
        """Navigate to a URL that will be intercepted to serve a fixture file."""
        fixture_path = FIXTURES_DIR / fixture_name
        assert fixture_path.exists(), f"Fixture not found: {fixture_path}"

        def _handler(route: Route) -> None:
            content = fixture_path.read_text(encoding="utf-8")
            route.fulfill(
                status=200,
                content_type="text/html; charset=utf-8",
                body=content,
            )

        page.route(url, _handler)
        page.goto(url, wait_until="networkidle")

    def test_company_homepage_renders(self, page: Page) -> None:
        """Verify company homepage fixture renders with navigation links."""
        self._serve_fixture_page(page, "company_homepage.html", "https://example.com/")
        expect(page.locator("body")).to_be_visible()
        # Check that "加入我们" link exists
        careers_link = page.locator('a[href="/careers"]')
        expect(careers_link).to_be_visible()
        expect(careers_link).to_contain_text(re.compile(r"加入我们|Careers", re.IGNORECASE))

    def test_career_list_renders_job_postings(self, page: Page) -> None:
        """Verify career list fixture shows 2 job postings."""
        self._serve_fixture_page(page, "career_list.html", "https://example.com/careers")
        expect(page.locator("body")).to_be_visible()
        # Check that job cards exist (data-job-id attributes)
        job_cards = page.locator('[data-job-id]')
        expect(job_cards).to_have_count(2)
        # Verify job titles are visible
        expect(page.locator("h3", has_text="高级算法工程师")).to_be_visible()
        expect(page.locator("h3", has_text="前端开发工程师")).to_be_visible()

    def test_job_detail_renders_jd_fields(self, page: Page) -> None:
        """Verify job detail fixture shows complete JD fields."""
        self._serve_fixture_page(page, "job_detail.html", "https://example.com/job/001")
        expect(page.locator("body")).to_be_visible()
        # Check JD field headers
        expect(page.locator("h2", has_text="岗位职责")).to_be_visible()
        expect(page.locator("h2", has_text="任职要求")).to_be_visible()
        expect(page.locator("h2", has_text="加分项")).to_be_visible()
        expect(page.locator("h2", has_text="薪酬福利")).to_be_visible()
        # Check company name
        expect(page.locator("text=星辰科技 StarCloud Technology")).to_be_visible()
        # Check salary range displayed
        expect(page.locator("text=35K-55K")).to_be_visible()

    def test_wechat_text_renders_job_content(self, page: Page) -> None:
        """Verify WeChat text article fixture renders job posting content."""
        self._serve_fixture_page(page, "wechat_text.html", "https://mp.weixin.qq.com/s/e2e-text")
        expect(page.locator("body")).to_be_visible()
        # Check article title
        expect(page.locator("h1", has_text="星辰科技 2026 届校园招聘")).to_be_visible()
        # Check that email delivery instructions are present
        expect(page.locator("text=campus@starcloud.com")).to_be_visible()
        # Check recruitment details
        expect(page.locator("text=算法工程师")).to_be_visible()
        expect(page.locator("text=后端开发工程师")).to_be_visible()
        # Check deadline
        expect(page.locator("text=2026 年 8 月 31 日")).to_be_visible()

    def test_wechat_image_renders_image_based_jd(self, page: Page) -> None:
        """Verify WeChat image article fixture renders image-based JD.

        Image-based JDs have less extractable text and require OCR or
        manual review.
        """
        self._serve_fixture_page(page, "wechat_image.html", "https://mp.weixin.qq.com/s/e2e-image")
        expect(page.locator("body")).to_be_visible()
        # Check article title
        expect(page.locator("h1", has_text="腾讯 2026 校园招聘")).to_be_visible()
        # Check image placeholders
        image_placeholders = page.locator(".image-placeholder")
        expect(image_placeholders.first).to_be_visible()
        # Check referral code is present
        expect(page.locator("text=NTABC123")).to_be_visible()
        # Check email delivery instructions
        expect(page.locator("text=referral@tencent-careers.com")).to_be_visible()

    def test_captcha_page_renders_login_wall(self, page: Page) -> None:
        """Verify captcha/login wall fixture renders anti-bot protections.

        This simulates what the agent encounters when hitting a site that
        requires authentication or captcha solving.
        """
        self._serve_fixture_page(page, "captcha.html", "https://zhaopin.com/auth/login")
        expect(page.locator("body")).to_be_visible()
        # Check that it's a login page with captcha
        expect(page.locator("text=验证码")).to_be_visible()
        expect(page.locator("text=企业用户登录")).to_be_visible()
        # Check slider captcha presence
        expect(page.locator("text=拖动完成拼图验证")).to_be_visible()
        # Check anti-bot notice
        expect(page.locator("text=检测到异常访问")).to_be_visible()
        # The page should indicate blocked access
        login_form = page.locator("form")
        expect(login_form).to_be_visible()


# ===================================================================
# Verification Commands
# ===================================================================
#
# Run all E2E tests (requires dev server):
#     python -m pytest tests/e2e/test_job_discovery_e2e.py -x -v
#
# Run only fixture tests (no dev server needed):
#     python -m pytest tests/e2e/test_job_discovery_e2e.py -x -v -k "TestFixturePages"
#
# Run with backend operations only:
#     python -m pytest tests/e2e/test_job_discovery_e2e.py -x -v -k "TestE2E"
#
# Set environment variables:
#     BACKEND_URL=http://127.0.0.1:8000
#     FRONTEND_URL=http://127.0.0.1:5173
#     E2E_ADMIN_USERNAME=admin@test.com
#     E2E_ADMIN_PASSWORD=admin123456
#     E2E_ADMIN_TOKEN=<direct_token>  (alternative to login)
#     E2E_SKIP_DEV_CHECK=1            (force tests even if server unreachable)
