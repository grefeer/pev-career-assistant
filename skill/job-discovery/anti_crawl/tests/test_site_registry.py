from __future__ import annotations

import pytest

from anti_crawl.site_registry import SITE_REGISTRY, get_site, list_sites, search_url, validate_entry

REQUIRED_KEYS = {
    "key", "domains", "needs_login", "login_url", "login_signal",
    "defense_level", "defense_types", "crawl_modes", "base_interval_s",
    "search_url_tpl", "detail_click", "notes",
}


def test_five_sites_present() -> None:
    assert set(list_sites()) == {"moka", "nowcoder", "baidu", "58", "liepin"}


def test_every_entry_valid() -> None:
    for key, entry in SITE_REGISTRY.items():
        assert entry["key"] == key, f"{key}: key 字段不一致"
        problems = validate_entry(entry)
        assert not problems, f"{key}: {problems}"


def test_required_keys_present() -> None:
    for key, entry in SITE_REGISTRY.items():
        missing = REQUIRED_KEYS - set(entry)
        assert not missing, f"{key} 缺字段: {missing}"


def test_login_signal_shape() -> None:
    for key, entry in SITE_REGISTRY.items():
        if entry["needs_login"]:
            assert entry["login_signal"], f"{key}: needs_login 必须配 login_signal"
            for field in ("url_contains", "selector", "text"):
                assert field in entry["login_signal"], f"{key}: login_signal 缺 {field}"


def test_get_site_unknown_raises() -> None:
    with pytest.raises(KeyError):
        get_site("not-a-site")


def test_search_url_liepin_keyword() -> None:
    entry = get_site("liepin")
    assert search_url(entry, "AI") == "https://www.liepin.com/zhaopin/?key=AI"


def test_search_url_city_only_when_template_has_placeholder() -> None:
    entry = get_site("moka")
    assert search_url(entry, "AI") == ""  # 无模板 → 返回空，调用方改用 --url
    assert "{" not in search_url(get_site("58"), "算法")  # 无 {city} 占位符时 city 参数安全忽略
