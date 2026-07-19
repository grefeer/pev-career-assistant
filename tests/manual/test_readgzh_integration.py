"""Standalone smoke test for ReadGZH integration — no pytest/DB dependencies.

Usage:
    python tests/manual/test_readgzh_integration.py

Environment variables:
    READGZH_API_KEY     – Optional API key to bypass anonymous rate limit
    TEST_WECHAT_URL_1   – Override first WeChat test URL
    TEST_WECHAT_URL_2   – Override second WeChat test URL
"""

from __future__ import annotations

import json
import os
import re
import sys
from urllib.parse import urlsplit

import requests


# ── Copy of the core functions from deepagents_runner.py ──


def _extract_page_title(html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.IGNORECASE | re.DOTALL)
    return m.group(1).strip() if m else ""


def _extract_page_text(html: str) -> str:
    html = re.sub(r"<script[^>]*>.*?</script>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(r"<style[^>]*>.*?</style>", "", html, flags=re.IGNORECASE | re.DOTALL)
    html = re.sub(
        r"</?(?:p|div|br|li|h[1-6]|tr|blockquote|section)[^>]*>",
        "\n", html, flags=re.IGNORECASE,
    )
    text = re.sub(r"<[^>]+>", "", html)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">").replace("&nbsp;", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def fetch_wechat_via_readgzh(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch a WeChat article via the ReadGZH proxy."""
    headers: dict[str, str] = {"User-Agent": "Mozilla/5.0"}
    api_key = os.environ.get("READGZH_API_KEY", "")
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    try:
        api_url = f"https://api.readgzh.site/rd?url={url}"
        print(f"   GET {api_url[:80]}...")
        resp = requests.get(api_url, timeout=30, headers=headers)
        print(f"   Status: {resp.status_code}")
        raw = resp.text

        if not raw or len(raw) < 200:
            return None, None, "ReadGZH returned empty or too-short response"

        if raw.strip().startswith("{"):
            try:
                error_data = json.loads(raw)
                if isinstance(error_data, dict) and not error_data.get("success", True):
                    code = error_data.get("code", "unknown")
                    message = error_data.get("message", "Unknown error")
                    return None, None, f"ReadGZH API error [{code}]: {message}"
            except json.JSONDecodeError:
                pass

        if "环境异常" in raw and "完成验证后即可继续访问" in raw:
            return None, None, "ReadGZH proxy could not bypass WeChat verification"

        title = _extract_page_title(raw)
        text = _extract_page_text(raw)

        if not text or len(text.strip()) < 50:
            return None, None, "ReadGZH returned insufficient content"

        return text, title, None
    except requests.RequestException as e:
        return None, None, f"ReadGZH fetch failed: {e}"


def fetch_page_direct(url: str) -> tuple[str | None, str | None, str | None]:
    """Fetch a URL directly (requests, no browser)."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        title = _extract_page_title(resp.text)
        text = _extract_page_text(resp.text)
        return text, title, None
    except requests.RequestException as e:
        return None, None, f"Direct fetch failed: {e}"


# ── Main test ──


def main() -> int:
    print("=" * 70)
    print("ReadGZH Integration Smoke Test")
    print("=" * 70)

    # ── Test URLs ──
    wechat_urls = [
        os.environ.get("TEST_WECHAT_URL_1", ""),
        os.environ.get("TEST_WECHAT_URL_2", ""),
    ]
    # Filter out empty ones
    wechat_urls = [u for u in wechat_urls if u]

    regular_urls = [
        os.environ.get("TEST_REGULAR_URL_1", "https://campus.alibaba.com/positionList.htm"),
        os.environ.get("TEST_REGULAR_URL_2", "https://zhaopin.baidu.com/"),
    ]

    all_urls = wechat_urls + regular_urls

    if not wechat_urls:
        print("\n[WARN] No TEST_WECHAT_URL_1/2 set.")
        print("Set these env vars to test WeChat article fetching.")
        print("Example:")
        print('  set TEST_WECHAT_URL_1=https://mp.weixin.qq.com/s/your-article-id')
        print("\nTesting regular URL fallback only...\n")

    results: list[dict] = []

    for i, url in enumerate(all_urls):
        is_wechat = "mp.weixin.qq.com" in url
        label = f"WECHAT #{i+1}" if is_wechat else f"REGULAR #{i+1 - len(wechat_urls)}"
        print(f"\n--- [{label}] {url[:100]}... ---")

        if is_wechat:
            print("   Trying ReadGZH proxy...")
            text, title, error = fetch_wechat_via_readgzh(url)

            if error:
                print(f"   ReadGZH: FAILED — {error}")
                if "rate_limited" in error:
                    print("   => Anonymous IP limit reached (10/day).")
                    print("   => Register at https://readgzh.site/dashboard for a free API key")
                    print("   => Then set READGZH_API_KEY env var and re-run")
                # Fallback: try direct
                print("   Falling back to direct fetch...")
                text, title, error = fetch_page_direct(url)
                if error:
                    print(f"   Direct fetch: FAILED — {error}")
                else:
                    has_block = "环境异常" in (text or "") and "完成验证后即可继续访问" in (text or "")
                    if has_block:
                        print("   Direct fetch: WeChat verification wall hit (expected without ReadGZH)")
                    else:
                        print(f"   Direct fetch: OK — title='{title}', {len(text or '')} chars")
            else:
                print(f"   SUCCESS! title='{title}', {len(text or '')} chars")
                print(f"   Preview: {(text or '')[:200]}...")
        else:
            print("   Direct fetch...")
            text, title, error = fetch_page_direct(url)
            if error:
                print(f"   FAILED — {error}")
            else:
                print(f"   OK — title='{title}', {len(text or '')} chars")

        results.append({
            "label": label,
            "url": url,
            "is_wechat": is_wechat,
            "success": error is None and text is not None and len(text or "") >= 50,
            "title": title,
            "text_length": len(text or ""),
            "error": error,
        })

    # ── Summary ──
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    wechat_results = [r for r in results if r["is_wechat"]]
    regular_results = [r for r in results if not r["is_wechat"]]

    if wechat_results:
        wechat_ok = sum(1 for r in wechat_results if r["success"])
        print(f"WeChat articles: {wechat_ok}/{len(wechat_results)} successful")
        for r in wechat_results:
            status = "OK" if r["success"] else "FAIL"
            print(f"  [{status}] {r['title'] or r['error'] or '(no title)'}")
    else:
        print("WeChat articles: NOT TESTED (set TEST_WECHAT_URL_1/2)")

    if regular_results:
        regular_ok = sum(1 for r in regular_results if r["success"])
        print(f"Regular URLs: {regular_ok}/{len(regular_results)} successful")
        for r in regular_results:
            status = "OK" if r["success"] else "FAIL"
            print(f"  [{status}] {r['title'] or r['error'] or '(no title)'}")

    # Return 0 if all tested URLs succeeded, 1 otherwise
    tested = wechat_results + regular_results
    if not tested:
        return 1
    return 0 if all(r["success"] for r in tested) else 1


if __name__ == "__main__":
    sys.exit(main())
