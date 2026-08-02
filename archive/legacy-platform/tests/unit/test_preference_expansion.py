from __future__ import annotations

from backend.app.services.job_discovery.preference_expansion import (
    _ROLE_FAMILY_MARKERS,
    PreferenceProfile,
    expand_preference,
    expand_preferences,
)


def _cf(s: str) -> str:
    return s.casefold().replace(" ", "")


def test_expand_ai_application_dev_yields_dev_markers_and_ai_application_domain() -> None:
    p = expand_preference("AI应用开发")
    assert p.role_type == "dev"
    assert "开发" in p.role_markers
    assert "工程师" in p.role_markers
    # keep tokens carry the AI应用 domain, not bare AI
    assert _cf("AI应用") in [_cf(k) for k in p.keep_tokens]
    assert "ai" not in [_cf(k) for k in p.keep_tokens]


def test_expand_ai_product_manager_yields_product_markers_not_dev() -> None:
    p = expand_preference("AI产品经理")
    assert p.role_type == "product"
    assert "产品经理" in p.role_markers
    assert "产品" in p.role_markers
    # A product preference must NOT carry dev-only role markers as its family
    assert "开发" not in p.role_markers
    assert "工程师" not in p.role_markers
    assert _cf("AI产品") in [_cf(k) for k in p.keep_tokens]


def test_expand_agent_dev_yields_dev_markers_and_agent_domain() -> None:
    p = expand_preference("Agent开发")
    assert p.role_type == "dev"
    assert "开发" in p.role_markers
    assert _cf("Agent") in [_cf(k) for k in p.keep_tokens]
    assert _cf("Agent开发") in [_cf(k) for k in p.keep_tokens]


def test_expand_data_analyst_yields_data_family() -> None:
    p = expand_preference("数据分析")
    assert p.role_type == "data"
    assert "数据" in p.role_markers or "分析" in p.role_markers
    assert _cf("数据") in [_cf(k) for k in p.keep_tokens]


def test_expand_chip_design_keeps_chip_roles_for_chip_preference() -> None:
    # A chip-design preference must NOT hard-exclude chips (unlike the old
    # AI-dev filter).  The chip role should match on domain+role.
    p = expand_preference("芯片设计工程师")
    assert p.role_type == "design"
    assert _cf("芯片") in [_cf(k) for k in p.keep_tokens]


def test_bare_generic_term_has_no_role_markers() -> None:
    p = expand_preference("人工智能")
    assert p.role_type == "generic"
    assert p.role_markers == ()


def test_inversion_dev_vs_product_preferences() -> None:
    """The core no-cheating proof: dev pref and product pref keep different roles."""
    dev = expand_preference("AI应用开发")
    prod = expand_preference("AI产品经理")

    def stage_a_keep(profile: PreferenceProfile, title: str) -> bool:
        label = _cf(title)
        has_keep = any(_cf(k) in label for k in profile.keep_tokens)
        has_role = any(_cf(m) in label for m in profile.role_markers)
        return has_keep and has_role

    # Dev preference keeps an AI-app dev role, filters a product-manager role.
    assert stage_a_keep(dev, "AI应用开发工程师") is True
    assert stage_a_keep(dev, "AI产品经理") is False
    # Product preference keeps a product role, filters a dev role.
    assert stage_a_keep(prod, "AI产品经理") is True
    assert stage_a_keep(prod, "大模型算法工程师") is False
    # Product preference does not match a non-AI product role (no domain keep token).
    assert stage_a_keep(prod, "后端产品经理") is False


def test_every_keep_token_is_a_substring_of_the_preference() -> None:
    """No-cheating invariant: keep tokens are derived from the preference only.

    A hardcoded AI-dev keep-list (大模型/具身智能/世界模型/AIGC/...) would violate
    this, since those are not substrings of e.g. 'AI应用开发'.
    """
    cases = [
        "AI应用开发", "Agent开发", "AI产品经理", "数据分析",
        "芯片设计工程师", "人工智能", "前端开发", "测试工程师",
    ]
    forbidden = {"大模型", "具身智能", "世界模型", "多模态", "aigc", "diffusion", "生成式"}
    for pref in cases:
        p = expand_preference(pref)
        pref_cf = _cf(pref)
        for kt in p.keep_tokens:
            assert _cf(kt) in pref_cf, f"{pref!r}: keep token {kt!r} not a substring"
            assert _cf(kt) not in forbidden, f"{pref!r}: injected forbidden token {kt!r}"


def test_role_taxonomy_is_generic_not_ai_dev_specific() -> None:
    """The taxonomy classifies role FAMILIES, not AI-dev roles.

    Asserts on the actual marker VALUES (not source text, which legitimately
    mentions AI-dev examples in docstrings): no family's markers are an AI-dev
    keep-list (no 大模型/agent/具身智能 literals).
    """
    families = set(_ROLE_FAMILY_MARKERS.keys())
    assert families == {
        "product", "dev", "design", "algo", "data", "ops", "test", "security", "research",
    }
    forbidden = ("大模型", "具身智能", "世界模型", "多模态", "aigc", "diffusion", "生成式", "agent", "智能体")
    for family, markers in _ROLE_FAMILY_MARKERS.items():
        for marker in markers:
            marker_cf = marker.casefold()
            for token in forbidden:
                assert token not in marker_cf, (
                    f"family {family!r} hardcodes AI-dev token {token!r} in marker {marker!r}"
                )


def test_expand_preferences_dedupes() -> None:
    out = expand_preferences(["AI应用开发", "AI应用开发", "AI产品经理"])
    assert [p.preference for p in out] == ["AI应用开发", "AI产品经理"]


def test_search_terms_contain_preference_and_role_keyword() -> None:
    p = expand_preference("AI产品经理")
    assert _cf("AI产品经理") in [_cf(s) for s in p.search_terms]
    assert _cf("产品经理") in [_cf(s) for s in p.search_terms]
