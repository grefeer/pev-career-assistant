"""Constrained Playwright implementation of the declarative crawl driver."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any
from urllib.parse import urljoin, urlparse

from backend.app.services.job_discovery.crawling.crawl_plan import CrawlPlan
from backend.app.services.job_discovery.crawling.driver import ListingPage
from backend.app.services.job_discovery.schemas import (
    DiscoveryTaskInput,
    PaginationType,
    RawJobDetail,
    RawJobListing,
)


class SelectorNotFoundError(RuntimeError):
    pass


class ApiPayloadChangedError(RuntimeError):
    pass


class BlockedCrawlError(RuntimeError):
    pass


class UnsafePlanExecutionError(RuntimeError):
    pass


class PlaywrightCrawlDriver:
    """Interpret only fields declared by ``CrawlPlan``; never evaluate JS."""

    def __init__(
        self,
        page: Any | None = None,
        *,
        page_factory: Callable[[], Any] | None = None,
        close_callback: Callable[[], None] | None = None,
    ) -> None:
        self._page = page
        self._page_factory = page_factory
        self._close_callback = close_callback

    def close(self) -> None:
        """Release the per-task browser resources supplied by the Worker."""
        if self._close_callback is not None:
            callback, self._close_callback = self._close_callback, None
            callback()

    def fetch_listing_page(
        self,
        *,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        self._assert_safe_url(task.source_url, task.source_url)
        if plan.pagination.type is PaginationType.API_CURSOR:
            return self._fetch_api_cursor_page(plan, task, cursor)

        page = self._get_page()
        self._navigate_listing(page, plan, task, cursor)
        self._raise_if_blocked(page)
        terminal_evidence = self._dom_terminal_evidence(page, plan)
        if plan.pagination.type is PaginationType.SINGLE_PAGE:
            terminal_evidence = terminal_evidence or "single_page_declared"
        listings = self._extract_dom_listings(
            page,
            plan,
            task,
            allow_empty=terminal_evidence is not None,
        )
        next_cursor = None if terminal_evidence else self._next_page_cursor(cursor)
        return ListingPage(
            page_key=str((cursor or {}).get("page", 1)),
            listings=listings,
            next_cursor=next_cursor,
            terminal_evidence=terminal_evidence,
        )

    def fetch_detail(
        self,
        *,
        plan: CrawlPlan,
        listing: RawJobListing,
        resource_key: str,
    ) -> RawJobDetail:
        if listing.detail_url is None:
            raise SelectorNotFoundError("listing has no declared detail URL")
        detail_url = urljoin(listing.source_url, listing.detail_url)
        self._assert_safe_url(detail_url, listing.source_url)
        page = self._get_page()
        page.goto(detail_url)
        self._raise_if_blocked(page)
        body_selector = plan.detail.body_selector
        if not body_selector:
            raise SelectorNotFoundError("crawl plan has no detail body selector")
        body = self._text_for(page, body_selector)
        return RawJobDetail(
            detail_url=detail_url,
            full_text=body,
            title=self._optional_text(page, plan.detail.title_selector),
            locations=_split_locations(self._optional_text(page, plan.detail.location_selector)),
            structured_fields={
                "responsibilities": self._optional_text(
                    page, plan.detail.responsibility_selector
                ),
                "requirements": self._optional_text(page, plan.detail.requirement_selector),
            },
            detail_resource_key=resource_key,
        )

    def _fetch_api_cursor_page(
        self,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        cursor: dict[str, Any] | None,
    ) -> ListingPage:
        endpoint = self._api_endpoint(plan, cursor)
        self._assert_safe_url(endpoint, task.source_url)
        page = self._get_page()
        payload = self._fetch_json(page, endpoint)
        items_path = plan.pagination.items_path
        has_more_path = plan.pagination.has_more_path
        next_cursor_path = plan.pagination.next_cursor_path
        if not items_path or not has_more_path or not next_cursor_path:
            raise ApiPayloadChangedError(
                "crawl plan has incomplete API cursor JSON paths"
            )
        items = _json_path(payload, items_path)
        if not isinstance(items, list):
            raise ApiPayloadChangedError("declared items_path is not a list")
        listings = [self._listing_from_api_item(item, task.source_url) for item in items]
        has_more = _json_path(payload, has_more_path)
        next_value = _json_path(payload, next_cursor_path)
        has_more = bool(has_more)
        next_cursor = {"cursor": next_value} if has_more and next_value is not None else None
        return ListingPage(
            page_key=str((cursor or {}).get("cursor", "initial")),
            listings=listings,
            next_cursor=next_cursor,
            terminal_evidence="api_cursor_exhausted" if not has_more else None,
            expected_listing_count=_json_path(payload, plan.pagination.total_count_path),
        )

    def _listing_from_api_item(
        self, item: Any, source_url: str
    ) -> RawJobListing:
        if not isinstance(item, dict):
            raise ApiPayloadChangedError("declared items_path contains a non-object")
        detail_url = item.get("detail_url") or item.get("url") or item.get("apply_url")
        if detail_url:
            detail_url = urljoin(source_url, str(detail_url))
        locations = item.get("locations") or item.get("location") or []
        if isinstance(locations, str):
            locations = _split_locations(locations)
        return RawJobListing(
            source_url=source_url,
            detail_url=detail_url,
            apply_url=item.get("apply_url"),
            company=item.get("company") or item.get("company_name"),
            title=str(item.get("title") or ""),
            locations=list(locations),
            job_code=item.get("job_code"),
        )

    def _navigate_listing(
        self, page: Any, plan: CrawlPlan, task: DiscoveryTaskInput, cursor: dict[str, Any] | None
    ) -> None:
        page.goto(task.source_url)
        if plan.pagination.type is PaginationType.PAGE_NUMBER and cursor:
            selector = plan.pagination.page_selector
            if not selector:
                raise SelectorNotFoundError("crawl plan has no page_selector")
            locator = page.locator(selector)
            page_number = int(cursor.get("page", 1))
            if not hasattr(locator, "nth"):
                raise SelectorNotFoundError("declared page selector cannot select a page")
            locator.nth(page_number - 1).click()

    def _extract_dom_listings(
        self,
        page: Any,
        plan: CrawlPlan,
        task: DiscoveryTaskInput,
        *,
        allow_empty: bool,
    ) -> list[RawJobListing]:
        locator = page.locator(plan.listing.item_selector)
        items = locator.all() if hasattr(locator, "all") else []
        if not items and not allow_empty:
            raise SelectorNotFoundError(f"declared selector not found: {plan.listing.item_selector}")
        listings: list[RawJobListing] = []
        for item in items:
            detail_url = self._optional_attribute(item, plan.listing.detail_link_selector, "href")
            listings.append(
                RawJobListing(
                    source_url=task.source_url,
                    detail_url=urljoin(task.source_url, detail_url) if detail_url else None,
                    company=None,
                    title=self._text_for(item, plan.listing.title_selector),
                    locations=_split_locations(self._optional_text(item, plan.listing.location_selector)),
                    job_code=self._optional_text(item, plan.listing.job_code_selector),
                )
            )
        return listings

    def _dom_terminal_evidence(self, page: Any, plan: CrawlPlan) -> str | None:
        selector = plan.pagination.terminal_selector
        if not selector:
            return None
        locator = page.locator(selector)
        if hasattr(locator, "count") and locator.count() > 0:
            return f"terminal_selector:{selector}"
        if hasattr(locator, "all") and locator.all():
            return f"terminal_selector:{selector}"
        return None

    def _next_page_cursor(self, cursor: dict[str, Any] | None) -> dict[str, Any]:
        return {"page": int((cursor or {}).get("page", 1)) + 1}

    def _api_endpoint(self, plan: CrawlPlan, cursor: dict[str, Any] | None) -> str:
        pattern = plan.pagination.endpoint_pattern
        if not pattern:
            raise ApiPayloadChangedError("crawl plan has no endpoint_pattern")
        return pattern.format(cursor=(cursor or {}).get("cursor", ""))

    def _fetch_json(self, page: Any, endpoint: str) -> Any:
        context = getattr(page, "context", None)
        request = getattr(context, "request", None)
        get = getattr(request, "get", None)
        if not callable(get):
            raise ApiPayloadChangedError("page context lacks request API")
        response = get(endpoint)
        if getattr(response, "ok", True) is False:
            raise ApiPayloadChangedError("declared API request failed")
        json_payload = getattr(response, "json", None)
        if not callable(json_payload):
            raise ApiPayloadChangedError("declared API response is not JSON")
        try:
            return json_payload()
        except Exception as exc:
            raise ApiPayloadChangedError("declared API response JSON changed") from exc

    def _get_page(self) -> Any:
        if self._page is None:
            if self._page_factory is None:
                raise UnsafePlanExecutionError("no Playwright page was provided")
            self._page = self._page_factory()
        return self._page

    @staticmethod
    def _assert_safe_url(url: str, origin_url: str) -> None:
        parsed = urlparse(url)
        origin = urlparse(origin_url)
        if parsed.scheme.lower() == "javascript" or parsed.scheme.lower() not in {"http", "https"}:
            raise UnsafePlanExecutionError("unsafe crawl URL")
        if (parsed.scheme, parsed.netloc) != (origin.scheme, origin.netloc):
            raise UnsafePlanExecutionError("cross-origin crawl URL")

    @staticmethod
    def _raise_if_blocked(page: Any) -> None:
        content = str(page.content()).lower() if hasattr(page, "content") else ""
        if any(marker in content for marker in ("captcha", "验证码", "login", "登录")):
            raise BlockedCrawlError("login or captcha marker detected")

    @staticmethod
    def _text_for(target: Any, selector: str) -> str:
        text = PlaywrightCrawlDriver._optional_text(target, selector)
        if not text:
            raise SelectorNotFoundError(f"declared selector not found: {selector}")
        return text

    @staticmethod
    def _optional_text(target: Any, selector: str | None) -> str | None:
        if not selector:
            return None
        locator = target.locator(selector)
        if hasattr(locator, "inner_text"):
            text = locator.inner_text()
            return str(text).strip() or None
        return None

    @staticmethod
    def _optional_attribute(target: Any, selector: str | None, name: str) -> str | None:
        if not selector:
            return None
        locator = target.locator(selector)
        if hasattr(locator, "get_attribute"):
            value = locator.get_attribute(name)
            return str(value) if value else None
        return None


def _json_path(payload: Any, path: str | None) -> Any:
    if not path:
        return None
    current = payload
    for segment in path.split("."):
        if not isinstance(current, dict) or segment not in current:
            raise ApiPayloadChangedError(f"declared JSON path missing: {path}")
        current = current[segment]
    return current


def _split_locations(value: str | None) -> list[str]:
    if not value:
        return []
    return [part.strip() for part in value.replace("、", ",").split(",") if part.strip()]
