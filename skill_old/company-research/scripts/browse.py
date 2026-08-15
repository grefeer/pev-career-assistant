#!/usr/bin/env python
"""Company-research page fetcher.

Fetches a single careers/about page with headless Chromium and writes the
rendered body text to ``output/evidence/pages/page_001.txt`` plus a
``browse_metadata.json`` summary whose ``status`` field is one of
``ok`` / ``blocked`` / ``empty`` / ``error``.  This mirrors the job-discovery
browse output contract so the shared ``run_skill_script`` wrapper can capture
the metadata from stdout and the runtime can parse the page files the same way.

This is a deliberately small, single-page crawler: company research needs one
page, not the multi-mode pagination crawl the job-discovery skill performs.

Security gate: a login/captcha/anti-bot verification wall surfaces as
``status=blocked`` and is never bypassed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


#: Markers observed on WeChat / portal verification walls.  Their presence
#: means the page did not render real content; we stop rather than bypass.
_BLOCK_MARKERS: tuple[str, ...] = (
    "完成验证后即可继续访问",
    "环境异常",
)
_MIN_USABLE_TEXT = 50


def _detect_block(text: str) -> str | None:
    """Return a block reason code if ``text`` looks like an anti-bot wall."""
    for marker in _BLOCK_MARKERS:
        if marker in text:
            return "anti_bot"
    return None


def _build_metadata(
    status: str, url: str, title: str | None, **extra: Any
) -> dict[str, Any]:
    payload: dict[str, Any] = {"status": status, "url": url}
    if title:
        payload["title"] = title
    payload.update(extra)
    return payload


def _write_metadata(out_dir: Path, payload: dict[str, Any]) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / "browse_metadata.json").write_text(
        json.dumps(payload, ensure_ascii=False), encoding="utf-8"
    )


def _fetch_page(url: str, wait_ms: int) -> tuple[str, str]:
    """Render ``url`` and return ``(body_text, title)``.

    Imported lazily so the module imports cleanly in environments without
    playwright (the runtime only reaches this path when actually browsing).
    """
    from playwright.sync_api import sync_playwright

    with sync_playwright() as pooled:
        browser = pooled.chromium.launch(headless=True)
        page = browser.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
        if wait_ms:
            page.wait_for_timeout(wait_ms)
        text = page.inner_text("body")
        title = page.title()
        browser.close()
    return text, title


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("url")
    parser.add_argument("--out", default="output/evidence")
    parser.add_argument("--max-pages", type=int, default=1)
    parser.add_argument("--wait", type=int, default=800)
    args = parser.parse_args(argv)

    out_dir = Path(args.out)
    pages_dir = out_dir / "pages"

    try:
        text, title = _fetch_page(args.url, args.wait)
    except Exception as exc:  # navigation/timeout/launch failure
        metadata = _build_metadata("error", args.url, None, error=str(exc)[:500])
        _write_metadata(out_dir, metadata)
        print(json.dumps(metadata, ensure_ascii=False))
        return 1

    block = _detect_block(text)
    if block:
        metadata = _build_metadata(
            "blocked", args.url, title, block_reason=block
        )
        _write_metadata(out_dir, metadata)
        print(json.dumps(metadata, ensure_ascii=False))
        return 0

    if not text or len(text.strip()) < _MIN_USABLE_TEXT:
        metadata = _build_metadata("empty", args.url, title)
        _write_metadata(out_dir, metadata)
        print(json.dumps(metadata, ensure_ascii=False))
        return 0

    pages_dir.mkdir(parents=True, exist_ok=True)
    (pages_dir / "page_001.txt").write_text(text, encoding="utf-8")
    metadata = _build_metadata(
        "ok", args.url, title, pages_collected=1
    )
    _write_metadata(out_dir, metadata)
    print(json.dumps(metadata, ensure_ascii=False))
    return 0


if __name__ == "__main__":  # pragma: no cover - script entry, exercised by the smoke test
    sys.exit(main())
