"""Standalone 4-URL smoke test for Web Navigation Agent with ReadGZH.

Runs the agent against 2 WeChat URLs + 2 regular URLs and reports trajectory.
No pytest or DB imports needed.

Usage:
    python tests/manual/run_readgzh_agent_smoke.py
"""
from __future__ import annotations

import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any

import requests
from dotenv import dotenv_values

# ── Load .env ──
_ENV_PATH = Path("D:/Python/langgraph-multi-agent-career-assistant-main/.env")
_env_vals = dotenv_values(_ENV_PATH, interpolate=False)
_READGZH_API_KEY = _env_vals.get("READGZH_API_KEY", "") or os.environ.get("READGZH_API_KEY", "")

# ── Standalone copies of core functions (no DB deps) ──


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
    """Use ReadGZH API to fetch a WeChat article."""
    headers: dict[str, str] = {"User-Agent": "Mozilla/5.0"}
    if _READGZH_API_KEY:
        headers["Authorization"] = f"Bearer {_READGZH_API_KEY}"

    try:
        api_url = f"https://api.readgzh.site/rd?url={url}"
        resp = requests.get(api_url, timeout=45, headers=headers)
        raw = resp.text

        if not raw or len(raw) < 200:
            return None, None, f"ReadGZH: empty/short response ({len(raw)} chars)"

        # JSON error response
        if raw.strip().startswith("{"):
            try:
                data = json.loads(raw)
                if isinstance(data, dict) and not data.get("success", True):
                    code = data.get("code", "unknown")
                    msg = data.get("message", "Unknown error")
                    return None, None, f"ReadGZH [{code}]: {msg}"
            except json.JSONDecodeError:
                pass

        # WeChat verification wall
        if "环境异常" in raw and "完成验证后即可继续访问" in raw:
            return None, None, "ReadGZH: WeChat verification wall"

        title = _extract_page_title(raw)
        text = _extract_page_text(raw)

        if not text or len(text.strip()) < 50:
            return None, None, f"ReadGZH: insufficient text ({len(text.strip())} chars)"

        return text, title, None
    except requests.RequestException as e:
        return None, None, f"ReadGZH fetch error: {e}"


def fetch_page_direct(url: str) -> tuple[str | None, str | None, str | None]:
    """Direct HTTP fetch (no browser)."""
    try:
        resp = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        ct = resp.headers.get("Content-Type", "")
        if "text/html" not in ct and "text/plain" not in ct:
            return None, None, f"Non-HTML content: {ct}"
        return _extract_page_text(resp.text), _extract_page_title(resp.text), None
    except requests.RequestException as e:
        return None, None, f"HTTP error: {e}"


# ── Simulated agent tool: open_url ──
# This is what the Web Navigation Agent would call internally.
# Same logic as deepagents_runner.open_url()


def agent_open_url(url: str) -> dict[str, Any]:
    """Simulate the agent's open_url tool, which tries ReadGZH for WeChat URLs."""
    from urllib.parse import urlsplit

    is_wechat = "mp.weixin.qq.com" in urlsplit(url).netloc.lower()
    result: dict[str, Any] = {
        "url": url,
        "is_wechat": is_wechat,
        "used_readgzh": False,
        "readgzh_error": None,
        "fallback_used": False,
    }

    if is_wechat:
        text, title, error = fetch_wechat_via_readgzh(url)
        if error is None:
            result["title"] = title
            result["text"] = text
            result["text_length"] = len(text or "")
            result["used_readgzh"] = True
            result["success"] = True
            return result
        else:
            result["readgzh_error"] = error
            # Fallback: direct fetch
            text2, title2, error2 = fetch_page_direct(url)
            result["fallback_used"] = True
            if error2:
                result["success"] = False
                result["error"] = f"ReadGZH: {error}; Fallback: {error2}"
            else:
                result["title"] = title2
                result["text"] = text2
                result["text_length"] = len(text2 or "")
                result["success"] = True
                # Check if fallback hit verification wall
                if "环境异常" in (text2 or "") and "完成验证后即可继续访问" in (text2 or ""):
                    result["wechat_verification_wall"] = True
                    result["success"] = False
                    result["error"] = "WeChat verification wall (ReadGZH failed, fallback blocked)"
            return result
    else:
        # Regular URL: direct fetch
        text, title, error = fetch_page_direct(url)
        if error:
            result["success"] = False
            result["error"] = error
        else:
            result["title"] = title
            result["text"] = text
            result["text_length"] = len(text or "")
            result["success"] = True
        return result


# ── Main smoke test ──

def main() -> int:
    print("=" * 70)
    print("Web Navigation Agent — ReadGZH Integration Smoke Test")
    print(f"ReadGZH API Key: {'configured' if _READGZH_API_KEY else 'MISSING'}")
    print("=" * 70)

    # 4 test URLs: 2 WeChat + 2 regular
    test_urls: list[tuple[str, str]] = [
        # WeChat URLs (from Tencent recruitment — real articles)
        ("WECHAT #1", "https://mp.weixin.qq.com/s/KiQVFIf1NgsS3K7ro1ZZ6Q"),
        ("WECHAT #2", "https://mp.weixin.qq.com/s/4F4rnwPlnySrO8uQkOXBMA"),
        # Regular job listing URLs
        ("REGULAR #1", "https://campus.alibaba.com/positionList.htm"),
        ("REGULAR #2", "https://zhaopin.baidu.com/"),
    ]

    results: list[dict[str, Any]] = []

    for label, url in test_urls:
        print(f"\n{'─' * 60}")
        print(f"[{label}] {url[:100]}")
        print(f"{'─' * 60}")

        t0 = time.time()
        result = agent_open_url(url)
        elapsed = time.time() - t0

        result["label"] = label
        result["elapsed"] = f"{elapsed:.1f}s"
        results.append(result)

        # Print results
        if result["used_readgzh"]:
            print(f"  [ReadGZH] SUCCESS — {result['text_length']} chars")
            print(f"  Title: {result.get('title', '?')}")
            print(f"  Preview: {(result.get('text', '') or '')[:200]}...")
        elif result.get("readgzh_error"):
            print(f"  [ReadGZH] FAILED — {result['readgzh_error'][:120]}")
            if result.get("fallback_used"):
                if result.get("wechat_verification_wall"):
                    print(f"  [Fallback] BLOCKED by WeChat verification wall")
                elif result.get("success"):
                    print(f"  [Fallback] OK — {result['text_length']} chars")
                else:
                    print(f"  [Fallback] FAILED — {result.get('error', '')[:120]}")
        elif result.get("is_wechat"):
            print(f"  [Direct] WeChat URL but not using ReadGZH")
            print(f"  [{('OK' if result['success'] else 'FAIL')}] {result.get('error', '')[:120]}")
        else:
            status = "OK" if result.get("success") else "FAIL"
            print(f"  [Direct] {status}")
            if result.get("success"):
                print(f"  Title: {result.get('title', '?')}")
                print(f"  Text: {result['text_length']} chars")

        print(f"  Time: {elapsed:.1f}s")

    # ── Summary ──
    print("\n" + "=" * 70)
    print("RESULTS SUMMARY")
    print("=" * 70)

    wechat_results = [r for r in results if r["is_wechat"]]
    regular_results = [r for r in results if not r["is_wechat"]]

    print(f"\nWeChat Articles ({len(wechat_results)}):")
    for r in wechat_results:
        if r.get("used_readgzh"):
            print(f"  ✓ {r['label']}: ReadGZH WORKED — {r['text_length']} chars")
        elif r.get("wechat_verification_wall"):
            print(f"  ✗ {r['label']}: BLOCKED — ReadGZH failed + fallback hit verification wall")
        elif r.get("readgzh_error"):
            print(f"  ⚠ {r['label']}: ReadGZH error: {r['readgzh_error'][:100]}")
        else:
            print(f"  ? {r['label']}: Unknown status")

    print(f"\nRegular URLs ({len(regular_results)}):")
    for r in regular_results:
        status = "✓" if r.get("success") else "✗"
        title = r.get("title", "?") or "?"
        print(f"  {status} {r['label']}: {title} ({r.get('text_length', 0)} chars)")

    # Check overall success
    wechat_ok = sum(1 for r in wechat_results if r.get("used_readgzh"))
    print(f"\nWeChat ReadGZH success: {wechat_ok}/{len(wechat_results)}")
    if wechat_ok == 0:
        print("→ ReadGZH integration was NOT able to fetch any WeChat articles.")
        print("→ Check: API key validity, article availability, network access.")
        return 1
    elif wechat_ok == len(wechat_results):
        print("→ ALL WeChat articles successfully fetched via ReadGZH!")
        return 0
    else:
        print("→ PARTIAL success — some articles worked, some didn't.")
        return 1


if __name__ == "__main__":
    sys.exit(main())
