"""Stealth Chromium 引擎：有头默认、持久化 profile、CDP init script 防检测。

设计要点（规格 §4.1）：
- 有头模式默认；--headless=new 由调用方显式传入 headless=True。
- 每站独立 user_data_dir，同 profile 内指纹参数固定（viewport/locale/timezone）。
- 不伪造 WebGL vendor/renderer、不伪造 canvas —— 保持真实。
- UA 不手动覆盖：Playwright 默认 UA 即 Chromium 真实 UA，HTTP header 与
  navigator.userAgent 天然一致（手动覆盖反而可能穿帮）。
- playwright-stealth 为可选叠加；自写 init script 是主防线，独立工作。
"""
from __future__ import annotations

import sys
import urllib.parse
from pathlib import Path
from typing import Any

from .profiles import LoginStatus, check_login_signal, profile_dir_for, profile_meta

# 注意：challenges.py 由 Task 3 创建，engine 内对其延迟导入（见 probe_site），
# 保证 Task 2 交付时本包可独立导入、anti_crawl_selftest.py 可直接运行。

# 注入到每个 document（document-start）。只修正与真实 Chrome 不一致的默认值，
# 不做欺骗性伪造。刻意保持窄小：每个键都对应一个真实浏览器可见的暴露点。
STEALTH_INIT_SCRIPT = r"""
(() => {
  const mask = () => {
    // 1. navigator.webdriver —— 自动化最直接暴露点
    try { Object.defineProperty(navigator, 'webdriver', { get: () => undefined }); } catch (e) {}
    // 2. languages —— Playwright 默认 ['en-US']，真实中文系统通常是 ['zh-CN','en']
    //    注意：getter 内不得再读 navigator.languages（已被自身遮蔽，会无限递归），
    //    须先捕获原始值。
    try {
      const langs = ['zh-CN', 'zh', 'en-US', 'en'];
      const realLangs = navigator.languages;
      Object.defineProperty(navigator, 'languages', {
        get: () => (Array.isArray(realLangs) && realLangs.length > 1)
          ? realLangs : langs,
      });
      if (navigator.language === 'en-US' && typeof navigator.language === 'string') {
        Object.defineProperty(navigator, 'language', { get: () => 'zh-CN' });
      }
    } catch (e) {}
    // 3. plugins/mimeTypes —— 真实 Chrome 至少带 PDF 查看器插件
    try {
      if (navigator.plugins.length === 0 || navigator.mimeTypes.length === 0) {
        Object.defineProperty(navigator, 'plugins', { get: () => {
          const p = { 0: { name: 'Chromium PDF Plugin', filename: 'internal-pdf-viewer' },
                      1: { name: 'Chromium PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai' },
                      2: { name: 'Native Client', filename: 'internal-nacl-plugin' },
                      length: 3, item: (i) => (i < 3 ? p[i] : null), namedItem: () => null };
          return p;
        }});
        Object.defineProperty(navigator, 'mimeTypes', { get: () => {
          const m = { length: 1, 0: { type: 'application/pdf' }, item: (i) => (i < 1 ? m[0] : null),
                      namedItem: () => null };
          return m;
        }});
      }
    } catch (e) {}
    // 4. 不隐藏 window.chrome 的真实存在性：headless 新内核也有 chrome 对象，
    //    但补全常见子键（真实 Chrome 拥有它们），避免 `!!window.chrome.runtime` 探测穿帮。
    try {
      if (window.chrome && typeof window.chrome === 'object') {
        for (const k of ['runtime', 'loadTimes', 'csi', 'app']) {
          if (!(k in window.chrome)) { try { window.chrome[k] = {}; } catch (e) {} }
        }
      }
    } catch (e) {}
  };
  mask();
})();
"""


def install_public_network_guard(context: Any) -> None:
    """只放行公网 HTTP(S) 目标（与 browse.py 的守卫同语义）。

    反爬层同样只抓公开的招聘内容，不面向内网/私网地址。
    """
    def is_safe_public_url(value: str) -> bool:
        import ipaddress
        import socket
        parsed = urllib.parse.urlparse(value)
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
            return False
        if parsed.username is not None or parsed.password is not None:
            return False
        try:
            return ipaddress.ip_address(parsed.hostname).is_global
        except ValueError:
            pass
        try:
            resolved = {entry[4][0] for entry in socket.getaddrinfo(parsed.hostname, None, type=socket.SOCK_STREAM) if entry[4]}
            return bool(resolved) and all(ipaddress.ip_address(a).is_global for a in resolved)
        except (socket.gaierror, ValueError):
            return False

    def guard(route: Any) -> None:
        if not is_safe_public_url(route.request.url):
            route.abort()
            return
        route.continue_()

    context.route("**/*", guard)


def apply_stealth(context: Any) -> None:
    """注入自写 init script（主防线）；playwright-stealth 存在则叠加（可选）。"""
    context.add_init_script(STEALTH_INIT_SCRIPT)
    try:
        from playwright_stealth import Stealth  # type: ignore
    except ImportError:
        print(
            "[anti-crawl] playwright-stealth 未安装（可选）：自写 init script 独立生效。"
            " 如需叠加可 pip install playwright-stealth。",
            file=sys.stderr,
        )
        return
    try:
        Stealth().apply_stealth_sync(context)  # 2.x API（1.x 的 stealth_sync 为 page 级且已弃用，不支持）
    except Exception as exc:  # 叠加失败不影响主防线
        print(f"[anti-crawl] playwright-stealth 叠加失败（忽略）：{exc}", file=sys.stderr)


def launch_context(
    site_key: str,
    *,
    headless: bool = False,
    profile_dir: Path | None = None,
    proxy: dict[str, Any] | None = None,
) -> Any:
    """启动 stealth 引擎，返回持久化 BrowserContext（user_data_dir 每站独立）。"""
    from playwright.sync_api import sync_playwright

    profile = profile_dir or profile_dir_for(site_key)
    meta = profile_meta(site_key)

    # 注意：user_data_dir 必须走 launch_persistent_context（new_context 不接受该参数）
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        str(profile),
        headless=headless,
        proxy=proxy if proxy else None,
        viewport=meta["viewport"],
        device_scale_factor=meta["device_scale_factor"],
        locale=meta["locale"] or None,
        timezone_id=meta["timezone"] or None,
        args=["--start-maximized"] if not headless else [],
    )
    context.set_default_timeout(30000)
    context._pw = pw  # 供 close_context 统一回收（Playwright 对象允许挂任意属性）
    apply_stealth(context)
    install_public_network_guard(context)
    return context


def close_context(context: Any) -> None:
    try:
        context.close()
    except Exception:
        pass
    browser = getattr(context, "browser", None)
    if browser is not None:
        try:
            browser.close()
        except Exception:
            pass
    pw = getattr(context, "_pw", None)
    if pw is not None:
        try:
            pw.stop()
        except Exception:
            pass


def probe_site(site_key: str, entry: dict[str, Any], *, headless: bool = True) -> dict[str, Any]:
    """轻量探测一站：登录态 + 反爬状态。供 check_login.py / crawl.py 复用。"""
    from .challenges import detect_challenge  # 延迟导入（challenges.py 在 Task 3 创建）

    context = launch_context(site_key, headless=headless)
    try:
        page = context.new_page()
        response = None
        target = entry.get("login_url") or ""
        if target:
            try:
                response = page.goto(target, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                response = None
            page.wait_for_timeout(2500)
        challenge = detect_challenge(page, response.status if response else None, page.url)
        login = LoginStatus.UNKNOWN
        signal = entry.get("login_signal")
        if signal:
            login = (
                LoginStatus.LOGGED_IN
                if check_login_signal(page, signal)
                else LoginStatus.NOT_LOGGED_IN
            )
        return {"login_status": login.value, "challenge": challenge, "url": page.url}
    finally:
        close_context(context)
