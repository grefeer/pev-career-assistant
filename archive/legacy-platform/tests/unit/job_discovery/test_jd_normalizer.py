"""Unit tests for JD text normalization and core_hash (Phase 4)."""

from __future__ import annotations

from backend.app.services.job_discovery.normalization.jd_normalizer import (
    core_hash,
    normalize_company,
    normalize_text,
    normalize_title,
)


class TestNormalizeText:
    def test_strips_whitespace_and_lowercases(self) -> None:
        assert normalize_text("  Java 工程师 ") == "java工程师"

    def test_nfkc_folds_full_width(self) -> None:
        # Full-width digits/letters fold to ASCII half-width.
        assert normalize_text("Ｊａｖａ") == "java"

    def test_strips_structural_punctuation(self) -> None:
        assert normalize_text("岗位：名称，。！") == "岗位名称"

    def test_keeps_programming_language_markers(self) -> None:
        # C++, C#, R&D must NOT collide with bare C / R.
        assert normalize_text("C++ 开发工程师") == "c++开发工程师"
        assert normalize_text("C# 工程师") == "c#工程师"
        assert normalize_text("R&D 工程师") == "r&d工程师"

    def test_strips_zero_width_chars(self) -> None:
        # ZWSP / ZWJ / ZWNJ / BOM / ideographic space must vanish.
        assert normalize_text("​工程师‍") == "工程师"
        assert normalize_text("a　b") == "ab"

    def test_empty_or_none(self) -> None:
        assert normalize_text(None) == ""
        assert normalize_text("") == ""
        assert normalize_text("   ") == ""


class TestCoreHash:
    def test_same_jd_different_location_same_hash(self) -> None:
        # D3: location excluded from identity.
        h1 = core_hash("负责算法研发", "硕士及以上")
        h2 = core_hash("负责算法研发", "硕士及以上")  # identical body
        assert h1 == h2
        assert isinstance(h1, str) and len(h1) == 64

    def test_different_jd_different_hash(self) -> None:
        assert core_hash("负责A", "要求B") != core_hash("负责C", "要求D")

    def test_whitespace_and_punctuation_invariant(self) -> None:
        # Cosmetic whitespace / punctuation differences do not change identity.
        h1 = core_hash("负责 算法 研发，", "硕士！")
        h2 = core_hash("负责算法研发", "硕士")
        assert h1 == h2

    def test_empty_body_is_stable(self) -> None:
        assert core_hash(None, None) == core_hash("", "")


class TestCompanyTitle:
    def test_company_normalization(self) -> None:
        assert normalize_company("字节 跳动") == "字节跳动"
        assert normalize_company(None) == ""

    def test_title_normalization(self) -> None:
        assert normalize_title("【2027秋招】算法工程师") == "2027秋招算法工程师"


class TestTitleTrailingParenStrip:
    def test_strips_trailing_location_paren(self) -> None:
        # Same job captured with and without a location suffix must collide.
        assert normalize_title("产品管培生（上海）") == normalize_title("产品管培生")

    def test_strips_trailing_specialization_paren(self) -> None:
        assert normalize_title("内容审核专员（法律专项）") == normalize_title("内容审核专员")

    def test_strips_multi_word_paren_content(self) -> None:
        assert normalize_title("用户体验运营管培（多语种优势-上海）") == \
            normalize_title("用户体验运营管培")

    def test_strips_multiple_trailing_parens(self) -> None:
        assert normalize_title("算法工程师（北京）（校招）") == normalize_title("算法工程师")

    def test_keeps_leading_or_mid_parens(self) -> None:
        # Only TRAILING paren groups are stripped; a leading tag stays (its
        # bracket chars are later deleted, but the content remains).
        assert normalize_title("（上海）算法工程师") != normalize_title("算法工程师")

    def test_bare_title_unchanged(self) -> None:
        assert normalize_title("算法工程师") == "算法工程师"

    def test_ascii_parens_also_stripped(self) -> None:
        assert normalize_title("SDE (Backend)") == normalize_title("SDE")

    def test_strips_trailing_lenticular_program_tag(self) -> None:
        # Same job captured with and without a trailing 【...】 campaign / program
        # tag must collide. The XHR-payload extractor keeps ``【2027届云弧计划】``
        # while the page-text extractor strips it, so the two captures surface
        # as one job, not two.
        assert normalize_title("AI Infra研发工程师【2027届云弧计划】") == \
            normalize_title("AI Infra研发工程师")

    def test_strips_mixed_paren_and_lenticular(self) -> None:
        # A trailing （...） group followed by a trailing 【...】 group peels both.
        assert normalize_title("算法工程师（北京）【校招】") == \
            normalize_title("算法工程师")

    def test_keeps_leading_lenticular_tag(self) -> None:
        # Only TRAILING 【...】 groups are stripped; a leading campaign tag stays
        # (its bracket chars are later deleted, but the tag content remains), so
        # a leading-tag title does NOT collide with the bare title.
        assert normalize_title("【2027秋招】算法工程师") != \
            normalize_title("算法工程师")

