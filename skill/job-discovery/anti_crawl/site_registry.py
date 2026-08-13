"""站点档案注册表（spec §4.5 / §5）。

档案是起点不是真理：站改版时按"实际观察优先"更新本文件与
references/site-adapters.md，并跑 anti_crawl/tests/test_site_registry.py 校验。
"""
from __future__ import annotations

from typing import Any

SITE_REGISTRY: dict[str, dict[str, Any]] = {
    "moka": {
        "key": "moka",
        "domains": ["*.mokahr.com"],
        "needs_login": False,
        "login_url": "",
        "login_signal": None,
        "defense_level": "weak",
        "defense_types": ["spa_shell"],
        "crawl_modes": ["search", "detail"],
        "base_interval_s": [1.5, 3.0],
        "search_url_tpl": "",
        "detail_click": True,
        "detail_url_marker": "#/job/",
        "notes": "SPA 卡片抽屉；无统一域名，用 --url 直传；沿用 site-catalog 的 interact 思路",
    },
    "nowcoder": {
        "key": "nowcoder",
        "domains": ["nowcoder.com", "*.nowcoder.com"],
        "needs_login": True,
        "login_url": "https://www.nowcoder.com/login",
        "login_signal": {
            "url_contains": "user",
            "selector": "a[href*='/user/'], a[href*='/jobs/recommend?userId=']",
            "text": "我的主页",
        },
        "defense_level": "medium",
        "defense_types": ["login_wall", "rate_limit"],
        "crawl_modes": ["search"],
        "base_interval_s": [2, 5],
        "search_url_tpl": "https://www.nowcoder.com/jobs/recommend?query={keyword}",
        "detail_click": False,
        "notes": "校招聚合；部分内容登录墙；入口/信号首轮冒烟实测定",
    },
    "baidu": {
        "key": "baidu",
        "domains": ["baijob.baidu.com", "*.baijob.baidu.com"],
        "needs_login": False,
        "login_url": "",
        "login_signal": None,
        "defense_level": "medium",
        "defense_types": ["js_challenge", "rate_limit"],
        "crawl_modes": ["search"],
        "base_interval_s": [2, 5],
        "search_url_tpl": "https://zhaopin.baidu.com/s?wd={keyword}",
        "detail_click": False,
        "notes": "百度百聘；旧域名 baijob.baidu.com 已 DNS 失效(2026-08-13 NXDOMAIN)；新入口 zhaopin.baidu.com 实测定 200 且渲染职位列表",
    },
    "58": {
        "key": "58",
        "domains": ["58.com", "*.58.com"],
        "needs_login": True,
        "login_url": "https://passport.58.com/login",
        "login_signal": {
            "url_contains": "my.58",
            "selector": "a[href*='my.58.com'], [class*='user']",
            "text": "退出",
        },
        "defense_level": "medium-strong",
        "defense_types": ["login_wall", "slider", "rate_limit"],
        "crawl_modes": ["search"],
        "base_interval_s": [3, 6],
        "search_url_tpl": "https://bj.58.com/job/?key={keyword}",
        "detail_click": False,
        "notes": "招聘列表反爬较强：滑块+登录墙；旧模板 jobs.58.com/search/ 已 404(2026-08-13)；bj.58.com/job/ 实测 200 但命中验证码墙(请输入验证码)，需人工过验证；模板写死北京，他城市需改模板",
    },
    "liepin": {
        "key": "liepin",
        "domains": ["liepin.com", "*.liepin.com"],
        "needs_login": True,
        "login_url": "https://www.liepin.com/",
        "login_signal": {
            "url_contains": "mylife",
            "selector": "a[href*='mylife']",
            "text": "我的求职",
        },
        "defense_level": "strong",
        "defense_types": ["behavior_risk", "rate_limit", "login_wall"],
        "crawl_modes": ["search", "detail"],
        "base_interval_s": [2, 5],
        "search_url_tpl": "https://www.liepin.com/zhaopin/?key={keyword}",
        "detail_click": True,
        "detail_url_marker": "/job/",
        "notes": "行为风控为主；登录后保持真实节奏；城市参数实测定",
    },
}


def get_site(key: str) -> dict[str, Any]:
    if key not in SITE_REGISTRY:
        raise KeyError(f"未知站点档案 {key!r}，可用：{', '.join(list_sites())}")
    return SITE_REGISTRY[key]


def list_sites() -> list[str]:
    return list(SITE_REGISTRY)


def search_url(entry: dict[str, Any], keyword: str, city: str | None = None) -> str:
    """按档案模板拼搜索 URL；无模板返回 ""（调用方改用 --url）。"""
    tpl = entry.get("search_url_tpl", "")
    if not tpl:
        return ""
    parts: dict[str, str] = {"keyword": keyword}
    if "{city}" in tpl:
        parts["city"] = city or ""
    return tpl.format(**parts)


def validate_entry(entry: dict[str, Any]) -> list[str]:
    problems: list[str] = []
    if not entry.get("key"):
        problems.append("缺 key")
    if entry.get("needs_login") and not entry.get("login_signal"):
        problems.append("needs_login 但缺 login_signal")
    if entry.get("needs_login") and entry.get("login_signal"):
        signal = entry["login_signal"]
        if not any(signal.get(f) for f in ("url_contains", "selector", "text")):
            problems.append("login_signal 三个字段全空")
    interval = entry.get("base_interval_s") or []
    if len(interval) != 2 or interval[0] <= 0 or interval[1] < interval[0]:
        problems.append(f"base_interval_s 非法: {interval}")
    if entry.get("defense_level") not in ("weak", "medium", "medium-strong", "strong"):
        problems.append(f"defense_level 非法: {entry.get('defense_level')}")
    return problems
