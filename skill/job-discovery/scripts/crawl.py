#!/usr/bin/env python3
"""crawl.py — 登录态爬取招聘站点（输出与 browse.py 同构）。

用法：
  python scripts/crawl.py --site liepin --keyword "AI" --max-pages 3
  python scripts/crawl.py --site moka --url "https://xxx.mokahr.com/social-recruitment/..." --mode detail

模式（spec §4.7）：
  search — 按档案 search_url_tpl 拼搜索 URL → 渲染列表 → 分页（尊重 pacing）
  detail — 列表渲染后逐个点击卡片/详情链接读取抽屉 JD（moka/猎聘详情形态）

流程：查缓存 → 登录态检查 → stealth 引擎打开 → 逐页抓取（限速/退避/挑战人工协作）
  → 证据落盘（sha256_<16>.txt/.png + pages/page_NN.txt）→ stdout JSON。

status 语义：ok / blocked:<code> / needs_manual_review / error
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from anti_crawl import engine  # noqa: E402
from anti_crawl.challenges import (  # noqa: E402
    ChallengeDetected, blocked_status, detect_challenge, handle_challenge,
)
from anti_crawl.pacing import PacingConfig, PacingController, PacingViolation, load_proxy_config  # noqa: E402
from anti_crawl.profiles import LoginStatus, store_dir  # noqa: E402
from anti_crawl.site_registry import get_site, search_url  # noqa: E402

_NEXT_TEXTS = ("下一页", "下一頁", "下页", "Next", "Next Page", ">", "»")
_LOAD_MORE_TEXTS = ("查看更多职位", "查看更多岗位", "查看全部职位", "查看全部岗位", "加载更多",
                    "更多职位", "更多岗位", "View more", "Load more", "Show more")
_NEXT_SELECTORS = (".next", ".pagination-next", "[aria-label='Next']", "a[rel='next']", ".ant-pagination-next")
_DETAIL_SELECTORS = (".job-detail", ".position-detail", ".drawer-content", ".modal-body",
                     "[role='dialog']", ".job-info", ".jd-content", ".detail-content")
_SKIP_CLICK_TEXT = re.compile(r"^(首页|下一页|上一页|末页|登录|注册|搜索|筛选|清除|确定|取消|知道了|提交|保存)$")


class _NavTracker:
    """记录最近一次主文档响应的 HTTP 状态，供 detect_challenge 判断 403/429。"""

    def __init__(self) -> None:
        self.status: int | None = None

    def attach(self, page: Any) -> None:
        page.on("response", self._on_response)

    def _on_response(self, response: Any) -> None:
        try:
            if response.request.resource_type == "document":
                self.status = response.status
        except Exception:
            pass


def _emit(result: dict) -> None:
    sys.stdout.buffer.write(json.dumps(result, ensure_ascii=False).encode("utf-8") + b"\n")
    sys.stdout.buffer.flush()


def _save_evidence(text: str, out_dir: Path) -> tuple[str, Path, Path]:
    content_hash = hashlib.sha256(text.encode("utf-8")).hexdigest()
    short_hash = f"sha256_{content_hash[:16]}"
    text_path = out_dir / f"{short_hash}.txt"
    screenshot_path = out_dir / f"{short_hash}.png"
    out_dir.mkdir(parents=True, exist_ok=True)
    if not text_path.exists():
        text_path.write_text(text, encoding="utf-8")
    return short_hash, text_path, screenshot_path


def _save_screenshot(page: Any, path: Path) -> None:
    try:
        page.screenshot(path=str(path), full_page=True)
    except Exception:
        pass


def _body_text(page: Any) -> str:
    return page.evaluate("() => document.body.innerText || ''")


def _find_next_button(page: Any) -> Any | None:
    for text in _NEXT_TEXTS:
        try:
            btn = page.get_by_text(text, exact=True).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    for sel in _NEXT_SELECTORS:
        try:
            el = page.locator(sel).first
            if el.is_visible():
                return el
        except Exception:
            continue
    for text in _LOAD_MORE_TEXTS:
        try:
            btn = page.get_by_text(text, exact=False).first
            if btn.is_visible():
                return btn
        except Exception:
            continue
    return None


def _extract_detail_text(page: Any) -> str:
    for sel in _DETAIL_SELECTORS:
        try:
            panel = page.locator(sel).first
            if panel.is_visible():
                text = panel.inner_text()
                if len(text) > 50:
                    return text
        except Exception:
            continue
    return _body_text(page)


def _click_detail_cards(page: Any, entry: dict[str, Any], max_cards: int, wait_ms: int) -> tuple[str, int, int]:
    """列表页卡片点击：优先按详情 URL 标记直连，否则退化为可见卡片点击。"""
    marker = entry.get("detail_url_marker", "#/job/")
    detail_urls: list[str] = []
    try:
        hrefs = page.evaluate("() => Array.from(document.querySelectorAll('a[href]')).map(a => a.getAttribute('href')).filter(Boolean)")
        base = page.url.split("#", 1)[0]
        seen: set[str] = set()
        for href in hrefs if isinstance(hrefs, list) else []:
            value = str(href or "").strip()
            if marker and marker not in value:
                continue
            full = value if value.startswith(("http://", "https://")) else base + value
            if full not in seen:
                seen.add(full)
                detail_urls.append(full)
            if len(detail_urls) >= max_cards:
                break
    except Exception:
        pass

    if not detail_urls:
        # 无详情链接标记 → 点击可见卡片（标题长度 3-120，非导航/分页文案）
        try:
            candidates = page.evaluate(
                "() => Array.from(document.querySelectorAll('a,button,[role=\"button\"]')).map(el => {"
                "  const t = (el.innerText || el.textContent || '').trim();"
                "  const r = el.getBoundingClientRect();"
                "  return {text: t, href: el.getAttribute('href') || '', visible: r.width > 0 && r.height > 0};"
                "}).filter(c => c.visible && c.text.length >= 3 && c.text.length <= 120)"
            )
            for cand in candidates if isinstance(candidates, list) else []:
                if _SKIP_CLICK_TEXT.match(cand["text"]):
                    continue
                if len(detail_urls) >= max_cards:
                    break
                if cand["href"]:
                    base = page.url.split("#", 1)[0]
                    full = cand["href"] if cand["href"].startswith(("http://", "https://")) else base + cand["href"]
                    if full not in detail_urls:
                        detail_urls.append(full)
        except Exception:
            pass

    sections: list[str] = []
    clicked = 0
    for i, url in enumerate(detail_urls[:max_cards], start=1):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(wait_ms)
            text = _extract_detail_text(page)
            if len(text.strip()) >= 50:
                sections.append(f"=== DETAIL {i} ({url}) ===\n{text}")
                clicked += 1
        except Exception:
            continue
    return "\n".join(sections), clicked, min(len(detail_urls), max_cards)


def _estimate_count(text: str | None) -> int | None:
    for pattern in (r"[（(]\s*(\d{2,4})\s*[）)]", r"(\d{2,4})\s*个?职位",
                    r"(\d{2,4})\s*results?", r"(\d{1,4})\s*结果", r"共\s*(\d{2,4})"):
        match = re.search(pattern, text or "")
        if match:
            return int(match.group(1))
    return None


def _cache_path(out_dir: Path) -> Path:
    return out_dir / "cache.json"


def _load_cache(out_dir: Path) -> dict[str, str]:
    path = _cache_path(out_dir)
    if path.exists():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}
    return {}


def _crawl_site(args: argparse.Namespace, entry: dict[str, Any], target_url: str) -> dict[str, Any]:
    out_dir = Path(args.out)
    site_key = args.site

    # ---- 登录态检查（需要登录的站） ----
    if entry.get("needs_login"):
        probe = engine.probe_site(site_key, entry, headless=True)
        if probe["login_status"] == LoginStatus.NOT_LOGGED_IN.value:
            return {
                "status": "needs_manual_review", "url": target_url,
                "reason": "login_required",
                "hint": f"请先运行: python scripts/login.py --site {site_key}",
            }

    pacing = PacingController(
        site_key,
        PacingConfig(base_interval_s=tuple(entry.get("base_interval_s") or [2, 5])),
    )
    proxy = load_proxy_config(store_dir() / "proxy.json")
    context = engine.launch_context(site_key, headless=args.headless, proxy=proxy)
    page = context.new_page()
    tracker = _NavTracker()
    tracker.attach(page)

    def load_page(url: str, wait_ms: int) -> str | None:
        """带 pacing + 退避 + 挑战处理的页面加载。返回未解除的挑战类型或 None。"""
        for attempt in range(1, pacing.config.max_backoff_attempts + 1):
            pacing.wait_before_request()
            try:
                response = page.goto(url, wait_until="domcontentloaded", timeout=45000)
                tracker.status = response.status if response else None
            except Exception:
                tracker.status = None
            page.wait_for_timeout(wait_ms)
            challenge = detect_challenge(page, tracker.status, page.url)
            if challenge != "rate_limited":
                return challenge
            if attempt < pacing.config.max_backoff_attempts:
                pacing.wait_on_backoff(attempt)
        return "rate_limited"

    try:
        challenge = load_page(target_url, args.wait)
        if challenge in ("slider", "captcha"):
            if not handle_challenge(page, challenge):
                return {"status": blocked_status(challenge), "url": target_url,
                        "reason": f"{challenge} 人工处理超时"}
        elif challenge == "js_challenge":
            try:
                handle_challenge(page, "js_challenge")
            except ChallengeDetected:
                return {"status": blocked_status("js_challenge"), "url": target_url,
                        "reason": "JS 挑战 5s 内未自动通过"}
        elif challenge == "login_wall":
            return {"status": blocked_status("login_wall"), "url": target_url,
                    "reason": "登录墙", "hint": f"python scripts/login.py --site {site_key}"}
        elif challenge == "rate_limited":
            return {"status": blocked_status("rate_limited"), "url": target_url,
                    "reason": "403/429 退避 3 次仍未恢复"}

        all_texts: list[str] = []
        pages_collected = 0
        used_path = "search"
        while pages_collected < args.max_pages:
            all_texts.append(_body_text(page))
            pages_collected += 1
            pacing.record_request()
            if pages_collected >= args.max_pages:
                break
            next_btn = _find_next_button(page)
            if next_btn is None:
                break
            pacing.wait_before_request()
            try:
                old_url = page.url
                next_btn.click(timeout=5000)
                page.wait_for_timeout(args.wait)
                if page.url == old_url:
                    new_text = _body_text(page)
                    if new_text == all_texts[-1]:
                        break
                    all_texts.append(new_text)
                    pages_collected += 1
                    pacing.record_request()
                else:
                    challenge = detect_challenge(page, tracker.status, page.url)
                    if challenge in ("slider", "captcha"):
                        if not handle_challenge(page, challenge):
                            break
                    elif challenge == "rate_limited":
                        break
            except Exception:
                break

        list_len = sum(len(t.strip()) for t in all_texts)
        if list_len < 80:
            # 内容哨兵：空壳 SPA / 页面未渲染时避免假成功（评审 I1）
            return {
                "status": "needs_manual_review", "url": target_url,
                "reason": "empty_page",
                "hint": "页面未渲染出有效内容（可能 SPA 未加载或页面结构变化），请人工检查或更新档案",
            }

        list_text = "\n\n--- PAGE BREAK ---\n\n".join(all_texts)
        detail_sections = ""
        if args.mode == "detail" and entry.get("detail_click"):
            try:
                detail_sections, clicked, found = _click_detail_cards(page, entry, args.max_cards, args.wait)
            except Exception:
                detail_sections, clicked, found = "", 0, 0
            used_path = "search+detail" if detail_sections else "search"
        full_text = f"=== LIST PAGE ===\n{list_text}\n{detail_sections}".rstrip()

        pages_dir = out_dir / "pages"
        pages_dir.mkdir(parents=True, exist_ok=True)
        page_files: list[str] = []
        for idx, text in enumerate(all_texts, start=1):
            p = pages_dir / f"page_{idx:02d}.txt"
            p.write_text(text, encoding="utf-8")
            page_files.append(str(p))
        short_hash, text_path, screenshot_path = _save_evidence(full_text, out_dir)
        _save_screenshot(page, screenshot_path)

        return {
            "status": "ok", "url": target_url, "title": page.title(),
            "content_hash": short_hash, "text_path": str(text_path),
            "screenshot_path": str(screenshot_path), "text_length": len(full_text),
            "job_count_estimate": _estimate_count(full_text),
            "pagination": {"pages_collected": pages_collected, "max_allowed": args.max_pages},
            "site": site_key, "used_path": used_path, "page_files": page_files,
            "cached": False,
        }
    except PacingViolation:
        return {"status": "needs_manual_review", "url": target_url,
                "reason": "daily_cap_reached", "hint": "今日抓取页数已达上限，明日再试"}
    except ChallengeDetected as exc:
        return {"status": blocked_status(exc.challenge_type), "url": target_url,
                "reason": exc.evidence}
    finally:
        engine.close_context(context)


def main() -> None:
    parser = argparse.ArgumentParser(description="登录态爬取招聘站点（anti-crawl）")
    parser.add_argument("--site", required=True, help="站点档案 key")
    parser.add_argument("--keyword", default="", help="搜索关键词（--url 优先）")
    parser.add_argument("--city", default=None, help="城市（仅模板含 {city} 时生效）")
    parser.add_argument("--url", default=None, help="直接指定目标 URL（覆盖模板）")
    parser.add_argument("--mode", choices=["search", "detail"], default=None,
                        help="search=列表分页；detail=列表+卡片详情（默认按档案 detail_click）")
    parser.add_argument("--max-pages", type=int, default=3, help="最大翻页数（默认 3）")
    parser.add_argument("--max-cards", type=int, default=20, help="详情卡片上限（默认 20）")
    parser.add_argument("--wait", type=int, default=3000, help="页面加载后等待 ms（默认 3000）")
    parser.add_argument("--out", default="output/evidence", help="证据输出目录（默认 output/evidence）")
    parser.add_argument("--headless", action="store_true", help="无头模式（默认有头）")
    parser.add_argument("--no-cache", action="store_true", help="跳过缓存直接重抓")
    args = parser.parse_args()

    try:
        entry = get_site(args.site)
    except KeyError as exc:
        _emit({"status": "error", "site": args.site, "error": str(exc)})
        sys.exit(1)

    if args.mode is None:
        args.mode = "detail" if entry.get("detail_click") else "search"
    if args.url:
        target_url = args.url
    else:
        target_url = search_url(entry, args.keyword, args.city)
        if not target_url:
            _emit({"status": "error", "site": args.site,
                   "error": "该站无 search_url_tpl，请用 --url 指定目标 URL"})
            sys.exit(1)

    out_dir = Path(args.out)
    cache_key = f"ac::{args.site}::{args.mode}::{target_url}"  # mode 入键：search/detail 同 URL 不串缓存（评审 I2）
    if not args.no_cache:
        cached = _load_cache(out_dir).get(cache_key)
        if cached:
            text_path = out_dir / f"{cached}.txt"
            screenshot_path = out_dir / f"{cached}.png"
            if text_path.exists():
                text = text_path.read_text(encoding="utf-8")
                _emit({
                    "status": "ok", "url": target_url, "title": "",
                    "content_hash": cached, "text_path": str(text_path),
                    "screenshot_path": str(screenshot_path), "text_length": len(text),
                    "job_count_estimate": _estimate_count(text),
                    "pagination": {"pages_collected": 0, "max_allowed": args.max_pages},
                    "site": args.site, "used_path": "cache", "page_files": [],
                    "cached": True,
                })
                sys.exit(0)

    result = _crawl_site(args, entry, target_url)
    if result.get("status") == "ok":
        cache = _load_cache(out_dir)
        cache[cache_key] = result["content_hash"]
        out_dir.mkdir(parents=True, exist_ok=True)
        _cache_path(out_dir).write_text(json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    _emit(result)
    sys.exit(0 if result.get("status") == "ok" else 1)


if __name__ == "__main__":
    main()
