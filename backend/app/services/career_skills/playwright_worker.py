"""One-shot Playwright worker used by the public-page fallback.

The parent process owns the deadline. This worker owns the Playwright runtime
from start to finish and exits after one page, so a wedged browser can be
terminated as a process tree instead of being abandoned in a daemon thread.
"""

from __future__ import annotations

import argparse
import json
from typing import Any

from backend.app.services.career_skills import job_discovery as jd


def _render(
    url: str, *, collect_links: bool, storage_state: str | None = None
) -> dict[str, Any]:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        try:
            context_kwargs: dict[str, Any] = {}
            if storage_state:
                context_kwargs["storage_state"] = storage_state
            browser_context = browser.new_context(**context_kwargs)
            page = browser_context.new_page()

            def abort_non_public(route: Any, request: Any) -> None:
                try:
                    if jd._is_public_url(request.url):
                        route.continue_()
                    else:
                        route.abort()
                except Exception:
                    route.abort()

            page.route("**/*", abort_non_public)
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=20_000)
                if response is None:
                    return {"error": "public_fetch_failed"}
                page.wait_for_timeout(1_500)
                body = page.inner_text("body") or ""
                stable_samples = 0
                for _ in range(30):
                    previous_len = len(body.strip())
                    page.wait_for_timeout(500)
                    body = page.inner_text("body") or ""
                    if (
                        len(body.strip()) >= jd._MIN_USABLE_TEXT_CHARS
                        and len(body.strip()) == previous_len
                    ):
                        stable_samples += 1
                        if stable_samples >= 2:
                            break
                    else:
                        stable_samples = 0
                result: dict[str, Any] = {
                    "body": body,
                    "title": page.title() or None,
                    "effective_url": page.url,
                    "status_code": response.status,
                }
                if collect_links:
                    result["links"] = jd._collect_page_links(page, url)
                return result
            finally:
                page.close()
                browser_context.close()
        finally:
            browser.close()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    parser.add_argument("--collect-links", action="store_true")
    parser.add_argument("--storage-state")
    args = parser.parse_args()
    try:
        payload = _render(
            args.url,
            collect_links=args.collect_links,
            storage_state=args.storage_state,
        )
    except jd.PublicJobFetchError as exc:
        payload = {"error": exc.code}
    except Exception:
        payload = {"error": "public_fetch_failed"}
    print(json.dumps(payload, ensure_ascii=False))


if __name__ == "__main__":
    main()
